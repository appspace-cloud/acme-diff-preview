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
import json, os, posixpath, random, re, shutil, signal, ssl, sys, subprocess, time, threading, urllib.error, urllib.request
from collections import Counter, namedtuple
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


BB_WORKSPACE       = "appspace-cloud"
BB_REPO            = "acme-config-dev"
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
DISPLAY_BODY_MAX_CHARS = 6000  # v2.5.8: hard cap per resource body in the PR
                               # comment, WITH an explicit marker (protects
                               # the footer/status token from the blunt
                               # MAX_COMMENT_BYTES global cut)
# Capacity knobs (env-overridable). Defaults sized for a single PR that diffs
# hundreds of apps (a chart version bump rolled out to many clusters at once).
# The diff is a pure local `helm template` render (no ArgoCD agent round-trips),
# so the client can fan out wide: the only shared limit is the Bitbucket API
# (BB_API_CONCURRENCY) used to fetch value files.
MAX_APPS_PER_RUN   = _env_int("MAX_APPS_PER_RUN", 800)   # cover 600+ apps/PR with headroom
DIFF_WORKERS       = _env_int("DIFF_WORKERS", 16)        # parallel per-app helm-template diffs
DIFF_TIMEOUT       = _env_int("DIFF_TIMEOUT", 120)       # seconds per diff (OCI cache-miss pulls are slow)
WARM_WORKERS       = _env_int("WARM_WORKERS", 4)         # parallel chart-cache warm-up pulls
WARM_THRESHOLD     = _env_int("WARM_THRESHOLD", 8)       # only warm when a PR fans out to more apps than this
MAX_COMMENT_BYTES  = 245_000 # Bitbucket ~256KB limit; leave headroom
JFROG_MAX_BODY_BYTES = _env_int("JFROG_MAX_BODY_BYTES", 65536)  # 64 KB — reject oversized bodies before HMAC

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
                self.send_response(413)
                self.end_headers()
                return
            body = self.rfile.read(length)

            # HMAC-SHA256 verification (Bitbucket X-Hub-Signature header).
            # Permissive when BB_WEBHOOK_SECRET is not set (backward compat).
            if not _verify_bb_hmac(body, self.headers.get("X-Hub-Signature", "")):
                log("Bitbucket webhook: HMAC verification failed — rejecting request", "WARNING")
                self.send_response(401)
                self.end_headers()
                return

            event_key = self.headers.get("X-Event-Key", "")
            if event_key.startswith("pullrequest:"):
                log(f"Webhook received: {event_key} — waking loop")
                _wake.set()
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


def _start_health_server(port: int = 8080) -> ThreadingHTTPServer:
    """Start the health server in a daemon thread and handle webhook POSTs.

    Uses ThreadingHTTPServer so health probes (GET /healthz) are never blocked
    by a concurrent JFrog or Bitbucket webhook request.
    """
    server = ThreadingHTTPServer(("", port), _HealthHandler)
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

def http(method, url, body=None, headers=None, auth=None):
    """HTTP call with exponential backoff on 429/503/network errors."""
    hdrs = dict(headers or {})
    if auth:
        hdrs["Authorization"] = "Basic " + _base64.b64encode(
            f"{auth[0]}:{auth[1]}".encode()).decode()
    data = json.dumps(body).encode() if body else None
    if data:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    last_exc = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, context=_ssl, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                wait = 2 ** attempt
                if e.code == 429:
                    # Honor the server-mandated pause: Bitbucket rate-limit
                    # windows run ~60s while our old total backoff was ~3s,
                    # so a storm failed straight through (bughunt F4).
                    # Capped so a broken header cannot stall the loop.
                    try:
                        wait = max(wait, min(int((e.headers or {}).get("Retry-After", "")), 60))
                    except (TypeError, ValueError):
                        pass
                log(f"[http] {e.code} on {method} — retry {attempt+1}/2 in {wait}s", "WARNING")
                time.sleep(wait)
                last_exc = e
                continue
            raise
        except (OSError, urllib.error.URLError) as e:
            if attempt < 2:
                wait = 2 ** attempt
                log(f"[http] network error on {method} — retry {attempt+1}/2 in {wait}s", "WARNING")
                time.sleep(wait)
                last_exc = e
                continue
            raise
    raise last_exc

def bb(method, path, **kw):
    url = f"https://api.bitbucket.org/2.0/repositories/{BB_WORKSPACE}/{BB_REPO}/{path}"
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
           _app_value_files_map, _app_namespace_map
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
    _path_map_cache          = path_map
    _app_chart_map           = chart_map
    _app_chart_revision_map  = chart_rev_map
    _app_chart_registry_map  = chart_reg_map
    _app_value_files_map     = value_files_map
    _app_namespace_map       = namespace_map
    _path_map_ts        = time.monotonic()
    _path_map_count     = len(path_map)
    _path_map_app_count = sum(len(v) for v in path_map.values())
    return path_map


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
DIFF_IGNORE_RESOURCE_PATTERNS = [
    "micro-versions-info",
]

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
                         "version_change"],
                        defaults=[None])
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
# `helm template` failed specifically because a value file is not parseable
# YAML (as opposed to a valid-but-incomplete chart render). Distinct hint so
# the author knows to fix their YAML syntax rather than chart values (v2.4.9).
REASON_INVALID_YAML  = "invalid_yaml"

# Reasons worth retrying in-process with backoff (transient).
# REASON_RENDER is retried once — a brief subprocess glitch (node IO, tmp
# exhaustion) should not produce a permanent "diff unavailable" result.
RETRYABLE_REASONS = {REASON_OCI_PULL, REASON_METADATA, REASON_TIMEOUT, REASON_RENDER}
# Reasons that permanently block the PR (the deployer would fail the same way).
PERMANENT_REASONS = {REASON_OCI_NOT_FOUND, REASON_INVALID_VERSION, REASON_INVALID_YAML}

# Operator-friendly one-liners shown in the PR comment for each reason.
# The full stderr is in the pod logs at LOG_LEVEL=DEBUG.
_REASON_HINTS = {
    REASON_OCI_NOT_FOUND: "Chart version not found in OCI registry — check that the version exists",
    REASON_OCI_PULL:      "could not pull the OCI chart (registry login or network)",
    REASON_METADATA:      "app not yet in the discovery cache (added since last refresh)",
    REASON_RENDER:        "helm template failed to render the chart with these values",
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


def _bb_fetch_status(filepath, sha):
    """Fetch a raw file from acme-config-dev at a commit SHA.

    Returns (content_or_None, status) where status is one of BB_OK / BB_NOT_FOUND
    / BB_ERROR. The distinction matters for caching: a genuine 404 is a stable
    fact and may be cached, but a transient error must NOT be cached as "missing"
    or it would poison every app that shares the same (sha, path) key.

    Uses a direct urllib call instead of bb()/http() because those helpers always
    json.loads() the response, which fails for YAML/text files.
    """
    url = (f"https://api.bitbucket.org/2.0/repositories/"
           f"{BB_WORKSPACE}/{BB_REPO}/src/{sha}/{filepath}")
    req = urllib.request.Request(url, headers={"Authorization": _BB_AUTH_HEADER})
    for attempt in range(3):
        with _bb_api_sem:   # global rate limiter: caps concurrent BB API calls
            try:
                with urllib.request.urlopen(req, context=_ssl, timeout=20) as r:
                    return r.read().decode("utf-8", errors="replace"), BB_OK
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None, BB_NOT_FOUND   # genuinely absent at this sha
                if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                    wait = (attempt + 1) * 2  # 2s, 4s
                    debug(f"Bitbucket API {e.code} for {filepath}, retry {attempt+1}/2 in {wait}s")
                    time.sleep(wait)
                    continue
                return None, BB_ERROR   # other / exhausted HTTP error — transient
            except Exception:
                if attempt < 2:
                    time.sleep((attempt + 1) * 2)
                    continue
                return None, BB_ERROR   # network/timeout after retries — transient
    return None, BB_ERROR


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
_SAFE_CHART_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")


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
KUBE_VERSION    = os.environ.get("KUBE_VERSION", "1.30.0")

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
_main_render_sha: str    = ""   # the main_sha the cache is valid for
# Cap to prevent memory pressure during long-lived pods with many apps.
# Each entry holds parsed YAML dicts per resource — can be several hundred KB
# for a large micro-services chart. 200 entries ≈ a few hundred MB worst case.
MAIN_RENDER_CACHE_MAX = _env_int("MAIN_RENDER_CACHE_MAX", 200)


class OciChartNotFound(Exception):
    """Raised when an OCI chart version does not exist in the registry."""


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
        last_err = ""
        try:
            for pull_attempt in range(3):
                r = subprocess.run(
                    [HELM_BIN, "pull", f"oci://{registry}/{chart}",
                     "--version", version, "--untar", "-d", tmp_dir],
                    capture_output=True, text=True, timeout=120)

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
                    log(f"helm pull failed for {chart}:{version}: {last_err}", "WARNING")
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
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

        path = _find_chart_subdir(chart_dir)
        with _helm_cache_lock:
            _helm_chart_cache[key] = path
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
    if missing:
        debug(f"value files not found at sha {sha[:8]}: {missing}")
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
            return None, (r.stderr or r.stdout or "helm template failed")[:400]
        return r.stdout, None


def _detect_new_env_candidates(changed_files: list, path_map: dict, renames: dict = None) -> list:
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


def _evaluate_new_envs(new_env_candidates: list, pr_sha: str) -> tuple:
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
    for env_info in new_env_candidates:
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
        if sec["kind_counts"] is not None:
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
                shown = sec["workloads"][:40]
                apps = ", ".join(f"`{w}`" for w in shown)
                more = (f" *(+{len(sec['workloads'])-40} more)*"
                        if len(sec["workloads"]) > 40 else "")
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
    return lines, structural_envs, total_new


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
    raw_config, status = _bb_fetch_status(config_file, pr_sha)
    if status != BB_OK or not raw_config:
        return None, f"could not fetch {config_file} from Bitbucket", 0, None
    version = _extract_chart_version(raw_config)
    if not version:
        return None, "no appspace.version found in config file", 0, None

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

    # 4. Gather value files from the new env dir (files found in changed_files)
    value_files_prefixed = sorted(set(
        f"$config/{f}" for f in env_info["all_yaml_files"]
        if f.endswith((".yaml", ".yml"))
    ))
    if not value_files_prefixed:
        value_files_prefixed = [f"$config/{config_file}"]

    # 5. Fetch value file contents
    vals = _fetch_value_files(value_files_prefixed, pr_sha)
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


def _pr_chart_revision(app, candidate_files, pr_sha):
    """Return the new OCI chart targetRevision for an app if the PR changes it.

    Strategy: candidate_files is this app's own subset of the PR's changed
    files, already matched against path_map by the caller (see
    _match_files_to_apps, v2.4.8). Fetch each one from Bitbucket at pr_sha
    and search for an `appspace.version` YAML key. That value is the new
    helm chart targetRevision (the ApplicationSet sets
    spec.sources[1].targetRevision = appspace.version).

    PERF FIX (v2.4.8): this function used to re-derive candidate_files by
    scanning the full changed_files list against path_map on every call --
    once per affected app. With ~600 apps that scan ran 600 times per PR.
    The caller now does that scan ONCE for the whole PR and hands each app
    just its own file list, so this function is pure O(candidate_files).

    Returns the new revision string if it differs from the current one cached in
    _app_chart_revision_map, otherwise returns None.
    """
    current_rev = _app_chart_revision_map.get(app)
    if not current_rev:
        return None
    for filepath in candidate_files:
        # Route through _vf_cache so parallel calls for the same (pr_sha, path)
        # from different apps share one Bitbucket API call instead of all fetching
        # in parallel. The cache key is (sha, clean_path) same as _fetch_value_files.
        clean = posixpath.normpath(filepath.lstrip("/"))
        cache_key = (pr_sha, clean)
        with _vf_cache_lock:
            cached = _vf_cache.get(cache_key, ...)   # use ... as sentinel for "absent"
        if cached is ...:
            # Not yet in cache — fetch and store (only definitive results)
            raw, status = _bb_fetch_status(clean, pr_sha)
            if status in (BB_OK, BB_NOT_FOUND):
                with _vf_cache_lock:
                    _vf_cache[cache_key] = raw
            content = raw
        else:
            content = cached
        if not content:
            continue
        new_rev = _extract_chart_version(content)
        if new_rev and new_rev != current_rev:
            debug(f"chart version override: {current_rev} -> {new_rev}",
                  app=app, file=filepath)
            return new_rev
    return None


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
    """
    current_rev = _app_chart_revision_map.get(app)
    if not current_rev:
        return None, False
    invalid = False
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
        if new_rev and new_rev != current_rev:
            debug(f"chart version override: {current_rev} -> {new_rev}",
                  app=app, file=filepath)
            return new_rev, invalid
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
        if not doc:
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
        if not delta:
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
    if ("error converting yaml" in e or "did not find expected" in e
            or "could not find expected" in e or "mapping values are not allowed" in e
            or "yaml: line" in e or "found character that cannot start" in e
            or "yaml:" in e and "unmarshal" in e):
        return REASON_INVALID_YAML
    return REASON_RENDER


_IDENTITY_BASENAMES = ("customer.yaml", "config.yaml")


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
        old_content, _old_status = _bb_fetch_status(old_clean, main_sha)
        new_content, _new_status = _bb_fetch_status(new_clean, pr_sha)
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


def _evaluate_env_decommissions(candidates: list, pr_sha: str, main_sha: str) -> tuple:
    """Build the decommission warning block for confirmed deletions.

    Confirms each candidate's identity file is genuinely gone at pr_sha
    before saying anything (defense in depth — never warn on a guess).
    Best-effort resource listing: a render failure does not suppress the
    warning, since the deletion itself is already confirmed fact.

    Returns (markdown_lines, env_names_reported).
    """
    lines, envs_reported = [], []
    for c in candidates:
        _content, status = _bb_fetch_status(c["identity_file"], pr_sha)
        if status != BB_NOT_FOUND:
            continue  # not actually deleted — do not warn on a false positive
        versions = sorted({
            _app_chart_revision_map[a] for a in c["apps"]
            if _app_chart_revision_map.get(a)
        })
        total = 0
        kind_counts: dict = {}
        workloads: set = set()
        any_rendered = False
        for app in c["apps"]:
            try:
                resources = _render_main_side_resources(app, main_sha)
            except Exception as e:
                debug(f"decommission resource listing failed for {app}: {e}")
                continue
            any_rendered = True
            n, kc, wl = _summarize_resources_dict(resources)
            total += n
            for k, v in kc.items():
                kind_counts[k] = kind_counts.get(k, 0) + v
            workloads.update(wl)

        lines += [
            f"# \U0001f5d1\ufe0f\u26a0\ufe0f ENVIRONMENT DECOMMISSION \u26a0\ufe0f\U0001f5d1\ufe0f",
            "",
            f"**`{c['env_name']}` is being deleted by this PR "
            f"(was running chart version `{', '.join(versions) or 'unknown'}`). "
            f"This is a destructive, hard-to-reverse change — verify this is intentional.**",
            "",
        ]
        if any_rendered and total:
            kind_breakdown = ", ".join(
                f"{n} {k}" for k, n in sorted(kind_counts.items(), key=lambda kv: (-kv[1], kv[0])))
            lines.append(f"- **Resources that will be removed:** {total} total \u2014 {kind_breakdown}")
            if workloads:
                shown = sorted(workloads)[:DECOM_WORKLOADS_MAX_SHOWN]
                apps_str = ", ".join(f"`{w}`" for w in shown)
                more = (f" *(+{len(workloads) - DECOM_WORKLOADS_MAX_SHOWN} more, truncated)*"
                        if len(workloads) > DECOM_WORKLOADS_MAX_SHOWN else "")
                lines.append(f"- **Applications removed:** {apps_str}{more}")
        else:
            lines.append("- *(resource preview unavailable \u2014 the deletion itself is confirmed)*")
        lines.append("")
        envs_reported.append(c["env_name"])
    return lines, envs_reported


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
    try:
        with ThreadPoolExecutor(max_workers=2) as ex:
            pr_fut   = ex.submit(_ensure_chart, registry, chart_name, pr_rev)
            main_fut = ex.submit(_ensure_chart, registry, chart_name, main_rev)
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

    if not pr_chart:
        return None, REASON_OCI_PULL, f"helm pull failed for {chart_name}:{pr_rev}"
    if not main_chart:
        return None, REASON_OCI_PULL, f"helm pull failed for {chart_name}:{main_rev}"

    try:
        # Optimization: value files not changed in this PR are byte-identical at
        # pr_sha and main_sha. Fetch them only once (at main_sha) and reuse for
        # the PR side. Only files that appear in the PR's changed_paths need a
        # fresh fetch at pr_sha. This halves Bitbucket API calls for the common
        # case of a version-only bump where a single config.yaml changes.
        pool = _get_subtask_pool()
        main_vf_fut = pool.submit(_fetch_value_files, value_files, main_sha)

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
        main_fut = pool.submit(_helm_template, main_chart, release, namespace, main_vals) \
                   if needs_main_render else None

        pr_yaml, pr_err = pr_fut.result(timeout=DIFF_TIMEOUT)
        if pr_err:
            if main_fut:
                main_fut.cancel()
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
        return None, REASON_TIMEOUT, f"render exceeded {DIFF_TIMEOUT}s"
    except Exception as e:
        return None, REASON_RENDER, str(e)[:200]

    pr_resources = _parse_manifest_resources(pr_yaml)
    # v2.5.8: report the effective chart-version change (if any) so the
    # comment can shout on downgrades. pr_rev is final here — including a
    # tier-default version discovered after a folder move.
    version_change = (main_rev, pr_rev) if pr_rev != main_rev else None
    return _diff_resources(main_resources, pr_resources), None, None, version_change


def _indeterminate(reason, detail):
    """Build an INDETERMINATE DiffResult (diff could not be computed)."""
    return DiffResult("", [], 0, False, detail[:400], OUT_INDETERMINATE, reason)


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
        # Truncate to display budget NOW so we never hold the full YAML in memory.
        # format_comment and generate_ai_summary use result.sections directly.
        display_secs = filtered_sections[:MAX_RESOURCES_FULL]
        truncated_parts = []
        for hdr, body in display_secs:
            body_t = body[:MAX_DIFF_CHARS] + "\n... (truncated)" if len(body) > MAX_DIFF_CHARS else body
            truncated_parts.append(f"===== {hdr} =====\n{body_t}")
        clean_diff = "\n".join(truncated_parts)
        return DiffResult(clean_diff, filtered_sections[:AI_MAX_SECTIONS_PER_APP],
                          n_res, True, None, OUT_DIFF, "changes", version_change)
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
def post_build_status(pr_sha, state, description, pr_id=None):
    """Post build status. Swallows errors - never crashes the script.

    The status URL used to always point at the ArgoCD server (bughunt: this
    build status is the acme-diff-preview service itself running as an
    ArgoCD Application - the link told a reviewer nothing about the actual
    diff and required separate ArgoCD access to even load). The full diff
    and every detail is already in the PR comment, so the link now points
    back at the PR itself when the caller has pr_id; only the handful of
    call sites that fire before the PR id is known fall back to no
    meaningful destination (ARGOCD_SERVER), which practically never happens
    in the normal flow (pr_id is available everywhere process_pr runs).
    """
    url = (f"https://bitbucket.org/{BB_WORKSPACE}/{BB_REPO}/pull-requests/{pr_id}"
           if pr_id else f"https://{ARGOCD_SERVER}")
    try:
        bb("POST", f"commit/{pr_sha}/statuses/build", body={
            "state": state, "key": BUILD_KEY,
            "name": STATUS_NAME,
            "url": url,
            "description": description[:255],
        })
    except Exception as e:
        log(f"[build status] failed to set {state}: {e}", "WARNING")

_BB_API_BASE = f"https://api.bitbucket.org/2.0/repositories/{BB_WORKSPACE}/{BB_REPO}"
_BB_MAX_PAGES = 100   # safety guard: prevents infinite loops on malformed next-links


def get_open_prs():
    url = f"{_BB_API_BASE}/pullrequests?state=OPEN&pagelen=50"
    prs, nxt, pages = [], url, 0
    while nxt and pages < _BB_MAX_PAGES:
        data = http("GET", nxt, auth=(BB_USER, BB_TOKEN))
        prs += data.get("values", [])
        nxt  = data.get("next")
        pages += 1
    if pages >= _BB_MAX_PAGES:
        log(f"get_open_prs: hit page limit ({_BB_MAX_PAGES}), results may be incomplete",
            "WARNING")
    return prs


def get_pr_changed_files(pr_id):
    files, renames, path, pages = [], {}, f"pullrequests/{pr_id}/diffstat?pagelen=100", 0
    while path and pages < _BB_MAX_PAGES:
        data = bb("GET", path)
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
        path = nxt.replace(f"{_BB_API_BASE}/", "") if nxt else ""
        pages += 1
    if pages >= _BB_MAX_PAGES and path:
        # Page limit hit and more pages exist — affected app list is INCOMPLETE.
        # Missing changed files → apps appear unaffected → potential false no_diff.
        log(f"PR #{pr_id}: diffstat page limit ({_BB_MAX_PAGES}) hit with more pages "
            f"remaining — {len(files)} files captured; PR has >10k changed files. "
            f"App detection is incomplete for this PR.",
            "WARNING", pr=pr_id)
    return files, renames

def find_existing_comment(pr_id):
    """Find our comment on a PR, cheaply when possible.

    Returns (comment_id, sha_8, raw_text).
    sha_8 is 8-char hex or '' if not found in comment.

    Fast path: if we already know this PR's comment id (bughunt N5), fetch
    it directly (1 API call) instead of paginating every comment on the PR.
    Falls back to a full scan on first sight or if the cached id 404s
    (comment was deleted) or no longer carries our marker.
    """
    with _comment_id_cache_lock:
        cached_id = _comment_id_cache.get(pr_id)
    if cached_id:
        try:
            c = bb("GET", f"pullrequests/{pr_id}/comments/{cached_id}")
            raw = c.get("content", {}).get("raw", "")
            if any(mk in raw for mk in _COMMENT_MARKERS):
                return cached_id, _extract_comment_sha(raw), raw
            # Marker gone (comment edited by a human) — fall through to a
            # full scan rather than trust a comment that is no longer ours.
        except urllib.error.HTTPError as e:
            if e.code == 404:
                with _comment_id_cache_lock:
                    _comment_id_cache.pop(pr_id, None)
            else:
                raise  # transient — same contract as the full-scan path below
        except Exception:
            raise

    nxt, pages = f"pullrequests/{pr_id}/comments?pagelen=100", 0
    while nxt and pages < _BB_MAX_PAGES:
        try:
            data = bb("GET", nxt)
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
                    _comment_id_cache[pr_id] = c["id"]
                return c["id"], _extract_comment_sha(raw), raw
        next_url = data.get("next", "")
        nxt = next_url.replace(f"{_BB_API_BASE}/", "") if next_url else ""
        pages += 1
    return None, "", ""

def upsert_comment(pr_id, body, existing_id=None):
    """Post or update PR comment. Truncates if over limit; posts fallback on error."""
    encoded = body.encode("utf-8")
    if len(encoded) > MAX_COMMENT_BYTES:
        cutoff = MAX_COMMENT_BYTES - 300
        body   = body.encode("utf-8")[:cutoff].decode("utf-8", errors="ignore")
        body  += (f"\n\n*... comment truncated ({len(encoded)//1024}KB exceeds limit)"
                   f" - see ArgoCD UI for full diff - {COMMENT_MARKER}*")
        log(f"[comment] truncated: {len(encoded)//1024}KB -> {MAX_COMMENT_BYTES//1024}KB", "WARNING")
    payload = {"content": {"raw": body}}
    try:
        if existing_id:
            bb("PUT",  f"pullrequests/{pr_id}/comments/{existing_id}", body=payload)
        else:
            bb("POST", f"pullrequests/{pr_id}/comments", body=payload)
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
            _comment_id_cache.pop(pr_id, None)
        try:
            bb("POST", f"pullrequests/{pr_id}/comments", body=payload)
            log("[comment] fallback POST succeeded", "INFO")
        except Exception as e2:
            log(f"[comment] fallback POST also failed: {e2}", "ERROR")

def fix_stuck_inprogress(pr_sha, pr_id, comment_raw):
    """If build status is stuck INPROGRESS but comment is current, fix the status.

    This handles the case where a previous CronJob pod was killed after posting
    the comment but before posting the final SUCCESSFUL/FAILED status.
    """
    try:
        st = http("GET",
            f"https://api.bitbucket.org/2.0/repositories/{BB_WORKSPACE}/{BB_REPO}"
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

# Thresholds for switching between inline and collapsed diff display.
# Bitbucket does NOT render HTML <details>/<summary> tags, so there is no
# real "collapse" available. For large PRs we show a compact summary table +
# truncated inline diffs instead of trying to use <details>.
LARGE_PR_APP_THRESHOLD   = 5       # changed apps above this -> large mode
LARGE_PR_DIFF_BYTES      = 40_000  # total diff bytes above this -> large mode
# In large mode, show the diff for the top N most-changed apps inline.
# Others get a table row only (no diff block) to stay within the 245KB limit.
LARGE_PR_INLINE_APPS     = _env_int("LARGE_PR_INLINE_APPS", 6)

# Limits for what we send to the model.
AI_MAX_SECTIONS_PER_APP  = 10
AI_MAX_BODY_CHARS        = 1500

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
            if val in ("|", "|-", "|+", ">", ">-", ">+"):
                # Block scalar opener: the value itself is on the following
                # indented lines, not on this line. Enter block mode so those
                # continuation lines get masked too instead of leaking.
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
                if val.strip() in ("|", "|-", "|+", ">", ">-", ">+", ""):
                    # Block scalar opener: keep the `value: |` line, mask body.
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


def _redact_sensitive(text: str) -> str:
    """Redact secret-like values from diff text before sending to Vertex AI.

    Matches lines where the key name looks sensitive (password, token, key,
    secret, etc.) and replaces the value with [REDACTED]. Operates on the
    rendered diff lines ('+'/'-' prefixed) so structural diff context is kept.

    Only the VALUE portion (after ':', '=', or quoted assignment) is redacted;
    key names and diff markers are preserved for context.
    """
    redacted_lines = []
    for line in text.splitlines():
        # Match key: value or key=value patterns (YAML / env-style).
        m = re.match(r'^([+\- ]*)([\w.\-/]+\s*[:=]\s*)(.+)$', line)
        if m and _SENSITIVE_KEYS.search(m.group(2)):
            redacted_lines.append(f"{m.group(1)}{m.group(2)}[REDACTED]")
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


def generate_ai_summary(app_results: dict) -> str | None:
    """Call Vertex AI Gemini to produce an operator-friendly diff summary.

    Input: already-parsed app_results {app: (diff_text, has_diff, error)}.
    Output: structured markdown string for operators, or None on any failure.

    Format returned (for consistent rendering in format_comment):
      LINE 1:  bold metrics line  e.g.  **2 app(s) updated · 6 resource(s) changed**
      BODY:    per-app bullet sections
      LAST:    critical flag line
    """
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

        sections_parts = []
        for app, sections in changed.items():
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
            "   - Version downgrades (full version string decreasing only)\n"
            "   - Replicas dropping to 0\n"
            "   - Services removed\n"
            "   - Liveness/readiness probes removed\n"
            "   If none: `No critical changes detected`\n\n"
            "Rules: max 200 words total. Be terse \u2014 operators scan, they do not read.\n\n"
            "DIFF DATA:\n"
            + "\n".join(sections_parts)
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
        return _normalize_ai_markdown(head + ai_text)
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


def _format_app_diff_block(app, sections, diff_text, show_diff=True, n_res=None):
    """Return a list of markdown lines for one app's diff block.

    sections is DiffResult.sections — already truncated to display budget.
    n_res is the REAL total resource count (DiffResult.n_res); the header must
    report this, not len(sections), which is capped at AI_MAX_SECTIONS_PER_APP.
    Before v2.4.9 the header used len(sections), so an app that changed e.g.
    103 resources showed "10 resource(s) changed" and only 10 diffs, with no
    hint that 93 more changed silently (FIX B). show_diff=False outputs just
    the header line (large-mode table overflow).
    Bitbucket does NOT render HTML <details>/<summary>, so we never use them.
    """
    shown = len(sections) if sections else 0
    total = n_res if n_res is not None else shown
    n = total if total else 1
    out = [f"\u26a0\ufe0f **`{app}`** \u2014 {n} resource(s) changed", ""]
    if not show_diff:
        return out
    if sections:
        if total > shown:
            out += [
                f"> \U0001f50d Showing first {shown} of {total} changed "
                f"resources. See ArgoCD for the full set.",
                "",
            ]
        for hdr, body in sections:
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
            body_disp = _redact_for_display(hdr, body).rstrip()
            if len(body_disp) > DISPLAY_BODY_MAX_CHARS:
                body_disp = (body_disp[:DISPLAY_BODY_MAX_CHARS].rstrip()
                             + "\n... (diff truncated for display \u2014 see "
                               "ArgoCD for the full resource diff)")
            out += [f"**`{hdr}`**", "", "```diff", body_disp, "```", ""]
    elif diff_text:
        out += ["```diff", _redact_sensitive(diff_text).rstrip(), "```", ""]
    return out


def format_comment(pr_sha, app_results, skipped_apps=None, base_sha="",
                    new_env_lines=None, new_env_structural=False, new_env_desc="",
                    decommission_lines=None):
    """Format the full PR comment. Never uses <details>/<summary> — Bitbucket
    does not render them. Large changesets get a compact summary table at the
    top (all apps, one row each) and inline diffs for the top-N most-changed
    apps only to stay well inside the 245KB comment limit.

    new_env_lines/new_env_structural/new_env_desc (v2.5.4, Finding 4): a PR
    can touch existing apps AND add brand-new environments in the same
    commit. new_env_lines is the markdown block from _evaluate_new_envs to
    splice into this comment; new_env_structural forces the footer to treat
    a broken new environment as blocking even if every existing app's own
    diff is perfectly clean — a reviewer must never see a plain green check
    while an unvalidated new environment rode along in the same PR.
    """
    skipped_apps  = skipped_apps or []
    results       = {app: _result(v) for app, v in app_results.items()}
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

    mode_label = "large" if is_large else "small"
    print(f"    [comment] mode={mode_label} | changed_apps={len(changed_apps)} | "
          f"diff_bytes={total_diff_bytes}")
    ai_summary = generate_ai_summary(app_results)
    if ai_summary:
        print(f"    [comment] AI summary included ({len(ai_summary)} chars)")
    else:
        print("    [comment] AI summary absent (call failed or no changes)")

    # ── Header ──────────────────────────────────────────────────────
    large_label = f" | \U0001f4e6 Large changeset ({len(changed_apps)} apps)" if is_large else ""
    lines = [
        f"## \U0001f52d {STATUS_NAME}", "",
        f"**Commit** `{pr_sha[:8]}` \u2192 `main` | `{BB_REPO}`{large_label}", "",
    ]

    # ── Environment decommission warning (v2.5.10) ───────────────────
    # Most critical/destructive possible finding — shown before even the
    # downgrade warning.
    if decommission_lines:
        lines += decommission_lines

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
    if ai_summary:
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
            "| App | Status | Resources |",
            "|-----|--------|-----------|",
        ]
        no_change_count = 0
        for app, r in results.items():
            if r.outcome == OUT_DIFF:
                lines.append(f"| `{app}` | \u26a0\ufe0f changed | {r.n_res} |")
            elif r.outcome == OUT_DECOMMISSIONED:
                lines.append(f"| `{app}` | \U0001f5d1\ufe0f decommissioned | \u2014 |")
            elif r.outcome == OUT_INDETERMINATE:
                lines.append(f"| `{app}` | \u2754 diff unavailable | \u2014 |")
            elif r.outcome == OUT_ERROR:
                lines.append(f"| `{app}` | \u274c error | \u2014 |")
            else:
                no_change_count += 1
        if no_change_count:
            lines.append(f"| *(+{no_change_count} more)* | \u2705 no changes | \u2014 |")
        lines += [""]

    # ── Per-app diff sections ─────────────────────────────────────────
    # Use r.n_res (total resource count, pre-computed) for sorting — no
    # re-parsing of diff text needed.
    if is_large and changed_apps:
        inline_set = {
            app for app, r in sorted(
                changed_apps,
                key=lambda x: x[1].n_res,
                reverse=True,
            )[:LARGE_PR_INLINE_APPS]
        }
        if len(changed_apps) > LARGE_PR_INLINE_APPS:
            lines += [
                f"> \U0001f50d Showing inline diffs for the {LARGE_PR_INLINE_APPS} "
                f"most-changed apps. All {len(changed_apps)} changed apps listed in the "
                f"table above.",
                "",
            ]
    else:
        inline_set = None

    for app, r in results.items():
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
            if r.reason == REASON_OCI_NOT_FOUND and r.error:
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
            any_change = True
            total_changed += r.n_res
            show_diff = (inline_set is None) or (app in inline_set)
            # sections are already truncated in DiffResult — no re-parsing.
            # Pass r.n_res so the header shows the REAL count (FIX B).
            lines += _format_app_diff_block(app, r.sections, r.text,
                                            show_diff=show_diff, n_res=r.n_res)

        else:
            lines += [f"\u2705 **`{app}`** \u2014 no manifest changes", ""]

    # ── Skipped apps note ────────────────────────────────────────────
    if skipped_apps:
        lines += [
            f"*{len(skipped_apps)} app(s) skipped (cap {MAX_APPS_PER_RUN}): "
            f"{', '.join(skipped_apps[:5])}{'...' if len(skipped_apps) > 5 else ''}*", ""]

    # ── New environment(s) section (v2.5.4, Finding 4) ────────────────
    # Bundled new environments (added in the same commit as an existing-app
    # change) get their own section here, using the exact same rendering
    # and classification path as a new-env-only PR (_evaluate_new_envs).
    if new_env_lines:
        lines += ["---"] + new_env_lines

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

    lines += [
        "---",
        f"**Status:** {status}",
        f"*{_ts()} \u2014 {COMMENT_MARKER} [{_status_token}]" + (f" [base:{base_sha[:8]}]" if base_sha else "") + "*",
    ]
    return "\n".join(lines)

# ── Per-PR processing (isolated) ──────────────────────────────────────
def process_pr(pr, path_map, base_sha=""):
    """Process one PR. All exceptions are caught so other PRs are not affected."""
    pr_id  = pr["id"]
    pr_sha = pr["source"]["commit"]["hash"]
    dest   = pr["destination"]["branch"]["name"]
    _title = pr['title']
    _title_disp = _title if len(_title) <= 80 else _title[:80] + "..."
    print(f"  PR #{pr_id}: {_title_disp!r} -> {dest} ({pr_sha[:8]})")

    if dest != "main":
        return

    # A chart republish (JFrog webhook) can force this PR to recompute once,
    # bypassing both dedups below. Consume-once: if the recompute then fails,
    # the error-comment retry path takes over on the next iteration.
    with _seen_lock:
        forced = pr_id in _force_recompute
        if forced:
            _force_recompute.discard(pr_id)
            print(f"    Forced recompute: a chart this PR renders with was republished")

    # In-memory dedup: skip same SHA already processed in this pod run
    with _seen_lock:
        if not forced and _seen.get(pr_id) == (pr_sha, base_sha):
            print(f"    Skipping: SHA {pr_sha[:8]} (base {base_sha[:8] if base_sha else '?'}) already processed in this run")
            return

    # Cross-pod dedup: existing comment already covers this exact SHA
    existing_id, comment_sha, comment_raw = find_existing_comment(pr_id)
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
                _seen[pr_id] = (pr_sha, base_sha)
            print(f"    Skipping: comment up to date for SHA {pr_sha[:8]}")
            # Fix potential stuck INPROGRESS from a previously killed pod
            fix_stuck_inprogress(pr_sha, pr_id, comment_raw)
            return

    try:
        changed, renames = get_pr_changed_files(pr_id)
        # Single O(files x paths) match for the whole PR (v2.4.8 perf fix) —
        # _app_to_files is reused below for the version-bump detection pass
        # instead of every app independently rescanning changed x path_map.
        affected, _app_to_files = _match_files_to_apps(changed, path_map)
        print(f"    Changed files: {len(changed)} | Affected apps: {len(affected)}")

        # v2.5.4 (Finding 4): always check for new-env candidates, not just
        # when `affected` is empty. _detect_new_env_candidates already
        # excludes any file matching an existing app's path_map entry, so
        # this is safe to run unconditionally. Before this fix, a PR that
        # ALSO touched an existing app never ran this check at all, silently
        # dropping a bundled new environment from evaluation entirely —
        # confirmed live with both a broken (#6646) and a fully valid
        # (#6652) new environment.
        new_env_candidates = _detect_new_env_candidates(changed, path_map, renames)
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
        if decommission_candidates:
            decommission_lines, decommissioned_envs = _evaluate_env_decommissions(
                decommission_candidates, pr_sha, base_sha)
            if decommissioned_envs:
                log(f"PR #{pr_id}: environment decommission detected: "
                    f"{decommissioned_envs}", "WARNING", pr=pr_id)

        if not affected:
            # No existing ArgoCD app matched the changed files.
            if new_env_candidates:
                post_build_status(pr_sha, "INPROGRESS", "Rendering new environment(s)...", pr_id=pr_id)
                new_env_lines, structural_envs, total_new = _evaluate_new_envs(new_env_candidates, pr_sha)

                lines = [
                    f"## \U0001f52d {STATUS_NAME}", "",
                    f"**Commit** `{pr_sha[:8]}` \u2192 `main` | `{BB_REPO}`", "",
                ] + new_env_lines

                if structural_envs:
                    desc = (f"{len(structural_envs)} new environment(s) have a "
                            f"structural config problem: {', '.join(structural_envs)}")
                    post_build_status(pr_sha, "FAILED", desc, pr_id=pr_id)
                    status_line = (
                        f"**Status:** \u274c New environment(s) with a structural "
                        f"problem that must be fixed before merge: "
                        f"{', '.join(f'`{e}`' for e in structural_envs)}")
                    clean_tag = "[blocked]"
                else:
                    desc = f"{len(new_env_candidates)} new environment(s), ~{total_new} resource(s) to create"
                    post_build_status(pr_sha, "SUCCESSFUL", desc, pr_id=pr_id)
                    status_line = (
                        f"**Status:** \u2705 New environment(s) - all resources "
                        f"will be created on merge")
                    clean_tag = "[clean]"
                lines += [
                    "---",
                    status_line,
                    f"*{_ts()} \u2014 {COMMENT_MARKER} {clean_tag}" + (f" [base:{base_sha[:8]}]" if base_sha else "") + "*",
                ]
                upsert_comment(pr_id, "\n".join(lines), existing_id)
                with _seen_lock:
                    _seen[pr_id] = (pr_sha, base_sha)
                return

            # No apps affected and no new env pattern found.
            print(f"    No ArgoCD apps affected - posting SUCCESSFUL")
            post_build_status(pr_sha, "SUCCESSFUL",
                "No ArgoCD apps affected by this PR", pr_id=pr_id)
            no_apps_body = (
                f"## \U0001f52d {STATUS_NAME}\n\n"
                f"Commit `{pr_sha[:8]}` vs `main` | `{BB_REPO}`\n\n"
                f"\u2705 **No ArgoCD apps are currently affected by the files "
                f"changed in this commit.**\n\n"
                f"This is expected for documentation, tooling, or script changes that "
                f"do not affect any ArgoCD-managed environment configuration.\n\n"
                f"---\n**Status:** \u2705 No ArgoCD apps affected\n"
                f"*{_ts()} \u2014 {COMMENT_MARKER} [clean]" + (f" [base:{base_sha[:8]}]" if base_sha else "") + "*"
            )
            upsert_comment(pr_id, no_apps_body, existing_id)
            with _seen_lock:
                _seen[pr_id] = (pr_sha, base_sha)
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
            _pr_chart_targets[pr_id] = _targets

        _changed_paths_set = set(changed)   # for value-file skip optimization

        def run_diff(app):
            t0 = time.monotonic()
            # FIX A (v2.4.9): if the PR set this app's appspace.version to an
            # unsafe/invalid value, do not diff against the current revision
            # (that would render identically and show a misleading green "no
            # changes"). Report it as a permanent, blocking failure instead.
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

        def process_batch(apps, workers):
            """Diff a list of apps with a bounded pool, accumulating results.

            Checks _shutdown between futures so SIGTERM drains gracefully instead
            of waiting for the entire batch to complete before yielding.
            """
            nonlocal any_hard_error, any_unknown
            if not apps:
                return
            with ThreadPoolExecutor(max_workers=max(1, min(workers, len(apps)))) as ex:
                futures = {ex.submit(run_diff, app): app for app in apps}
                for fut in as_completed(futures):
                    if _shutdown:
                        log(f"SIGTERM received mid-batch — draining remaining futures",
                            "WARNING")
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
        log(f"PR #{pr_id} diff summary: {breakdown}"
            + (f" | reasons: {reasons}" if reasons else ""),
            pr=pr_id, **{f"n_{k}": v for k, v in outcome_counts.items()})

        # v2.5.4 (Finding 4): render any new-env candidates bundled with this
        # PR's existing-app changes, using the same path a new-env-only PR
        # uses. structural_envs forces the comment footer and, below, the
        # Bitbucket build status to block — a broken or unvalidated new
        # environment must never hide behind an unrelated app's clean diff.
        new_env_lines, structural_envs, total_new = ([], [], 0)
        if new_env_candidates:
            new_env_lines, structural_envs, total_new = _evaluate_new_envs(new_env_candidates, pr_sha)
        new_env_desc = (
            f"{len(structural_envs)} new environment(s) have a structural "
            f"config problem: {', '.join(structural_envs)}"
        ) if structural_envs else ""

        body = format_comment(pr_sha, app_results, skipped_apps, base_sha=base_sha,
                               new_env_lines=new_env_lines or None,
                               new_env_structural=bool(structural_envs),
                               new_env_desc=new_env_desc,
                               decommission_lines=decommission_lines or None)
        comment_kb = round(len(body.encode()) / 1024, 1)
        upsert_comment(pr_id, body, existing_id)
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
        if any_hard_error or has_blocking_indet or structural_envs:
            if structural_envs:
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
                f"{n_skipped} app(s) not evaluated (cap={MAX_APPS_PER_RUN}){extra} — review comment",
                pr_id=pr_id)
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
        is_permanent_failure = any_hard_error or has_blocking_indet or bool(structural_envs)
        is_transient_failure = any_unknown and not is_permanent_failure
        if not is_transient_failure:
            # Mark seen for both clean runs AND permanent failures so we don't
            # spam the PR with repeated "not found" comments every 60s.
            with _seen_lock:
                _seen[pr_id] = (pr_sha, base_sha)
        return outcome_counts

    except Exception as e:
        log(f"[ERROR] PR #{pr_id}: {e}", "ERROR")
        try:
            post_build_status(pr_sha, "FAILED", f"Diff error: {str(e)[:200]}", pr_id=pr_id)
        except Exception:
            pass
        err_body = (
            f"## \U0001f52d {STATUS_NAME}\n\n"
            f"Commit `{pr_sha[:8]}` vs `main` | `{BB_REPO}`\n\n"
            f"\u274c **Error processing diff:** {str(e)[:400]}\n\n"
            f"---\n**Status:** \u274c Error running diff\n"
            f"*{_ts()} \u2014 {COMMENT_MARKER} [permanent]" + (f" [base:{base_sha[:8]}]" if base_sha else "") + "*"
        )
        try:
            upsert_comment(pr_id, err_body, existing_id)
        except Exception:
            pass

# ── Main iteration (one poll cycle) ───────────────────────────────────
def main_iteration():
    """Run one complete poll cycle: discover apps, get open PRs, process each."""
    _iter_start = time.monotonic()
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

    try:
        main_info = http("GET",
            f"https://api.bitbucket.org/2.0/repositories/{BB_WORKSPACE}/{BB_REPO}"
            "/refs/branches/main", auth=(BB_USER, BB_TOKEN))
        base_sha = main_info["target"]["hash"]
        log(f"Base SHA (main): {base_sha[:8]}")
        # Invalidate the main-side render cache whenever main moves.
        global _main_render_sha
        if base_sha != _main_render_sha:
            with _main_render_lock:
                _main_render_cache.clear()
            _main_render_sha = base_sha
        prs = get_open_prs()
    except Exception as e:
        global _last_poll_ok, _consecutive_poll_fails
        _last_poll_ok = False
        _consecutive_poll_fails += 1
        log(f"Bitbucket API error (poll_fails={_consecutive_poll_fails}): {e}", "ERROR")
        return
    # Mark poll as healthy after successful fetch (outside try so it only runs on success)
    _last_poll_ok = True
    _consecutive_poll_fails = 0
    _touch_progress()  # C2 checkpoint: Bitbucket poll succeeded
    log(f"Open PRs: {len(prs)}")

    # Evict _seen entries for PRs no longer open. Without this, a PR that
    # is declined and immediately reopened with the same SHA would be silently
    # skipped because the old SHA is still in _seen.
    open_ids = {pr["id"] for pr in prs}
    with _seen_lock:
        for stale_id in [k for k in _seen if k not in open_ids]:
            del _seen[stale_id]
        for stale_id in [k for k in _pr_chart_targets if k not in open_ids]:
            del _pr_chart_targets[stale_id]
        _force_recompute.intersection_update(open_ids)
    with _comment_id_cache_lock:
        for stale_id in [k for k in _comment_id_cache if k not in open_ids]:
            del _comment_id_cache[stale_id]

    pending = [
        pr for pr in prs
        if pr["source"]["commit"]["hash"] != base_sha
    ]
    totals = Counter()
    if pending:
        with ThreadPoolExecutor(max_workers=MAX_PR_WORKERS) as executor:
            futs = {executor.submit(process_pr, pr, path_map, base_sha): pr for pr in pending}
            for fut in as_completed(futs):
                try:
                    counts = fut.result()
                    if counts:
                        totals.update(counts)
                except Exception as exc:
                    pr = futs[fut]
                    log(f"Unhandled error processing PR #{pr['id']}: {exc}", "ERROR")
                _touch_progress()  # C2 checkpoint: one PR finished processing

    # Iteration-level rollup across all PRs: a single line that shows whether
    # this cycle was healthy or how many app diffs could not be computed.
    elapsed_s = round(time.monotonic() - _iter_start, 1)
    with _diff_stats_lock:
        _diff_stats["last_iteration_s"] = elapsed_s
        _diff_stats["last_iteration_at"] = datetime.now(timezone.utc).isoformat()
    if totals:
        rollup = ", ".join(f"{k}={v}" for k, v in sorted(totals.items()))
        unhealthy = totals.get(OUT_INDETERMINATE, 0) + totals.get(OUT_ERROR, 0)
        log(f"Iteration done [{elapsed_s}s] — diff outcomes: {rollup}"
            + (f" | {unhealthy} app diff(s) could not be computed" if unhealthy else ""),
            severity=("WARNING" if unhealthy else "INFO"),
            **{f"n_{k}": v for k, v in totals.items()})
    else:
        log(f"Iteration done [{elapsed_s}s]")

# ── Main entry point (long-running Deployment mode) ───────────────────
def main():
    """Start health server, login to ArgoCD, then run poll loop until SIGTERM."""
    global _last_ok, _loop_idle
    log("acme-diff-preview starting (Deployment mode, helm-template diff)",
        argocd_server=ARGOCD_SERVER, argocd_user=ARGOCD_USER,
        bb_repo=BB_REPO, diff_workers=DIFF_WORKERS, pr_workers=MAX_PR_WORKERS,
        max_apps_per_run=MAX_APPS_PER_RUN, diff_timeout=DIFF_TIMEOUT,
        diff_retries=DIFF_RETRIES, warm_workers=WARM_WORKERS,
        kube_version=KUBE_VERSION, log_level=LOG_LEVEL, vertex_model=VERTEX_MODEL)

    # Self-check: the entire diff engine depends on an OCI pull, which needs
    # OCI_PASS. Without it _helm_login fails and EVERY diff returns "diff
    # unavailable". Fail loudly at startup instead of silently degrading.
    if not OCI_PASS:
        log("OCI_PASS is empty — helm OCI pulls will fail and every diff will be "
            "unavailable. Set secrets.ociPassKey/ociUserKey in the chart values.",
            "ERROR")
    else:
        log(f"OCI credentials present (user={OCI_USER})")

    _start_health_server()
    _start_heartbeat()    # keep /healthz alive during long PR processing
    _get_subtask_pool()   # warm the shared thread pool before the first iteration
    log(f"Sub-task pool ready ({_SUBTASK_POOL_WORKERS} workers)")

    # Initial login — raises on failure so the container restarts immediately.
    argocd_login()
    log("ArgoCD login OK")

    while not _shutdown:
        try:
            main_iteration()
            _last_ok = time.monotonic()  # only bumped on a clean iteration
        except Exception as e:
            log(f"Unhandled error in main loop: {e}", "ERROR")
            # Do NOT bump _last_ok here — /healthz must reflect real staleness.
        if not _shutdown:
            # Webhook wakes the loop instantly (<1s). The 60s timeout is
            # just a safety net in case webhook delivery is ever unavailable.
            # _loop_idle marks this as a known-safe, bounded wait — NOT a
            # hang — so the heartbeat can keep vouching for liveness while
            # the loop is legitimately doing nothing (v2.5.2 C2). Guarded by
            # the same lock _beat() reads it under, for a clean, unambiguous
            # happens-before relationship (belt-and-braces on top of the
            # GIL's own atomicity for a single bool assignment).
            with _progress_lock:
                _loop_idle = True
            _wake.wait(timeout=60)
            with _progress_lock:
                _loop_idle = False
            _wake.clear()

    log("Shutdown complete", "WARNING")

if __name__ == "__main__":
    main()