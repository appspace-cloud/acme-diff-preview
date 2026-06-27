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
JFROG_WEBHOOK_SECRET = os.environ.get("JFROG_WEBHOOK_SECRET", "")
# Deduplication window: skip hard-refresh if same chart:version was processed
# within this many seconds. Handles JFrog retries and rapid successive pushes.
JFROG_DEDUP_WINDOW   = int(os.environ.get("JFROG_DEDUP_WINDOW", "15"))
# Human-readable name shown on the Bitbucket PR build status and comment header.
STATUS_NAME        = "ACME Diff Preview"
# Marker written into the footer of every comment we post. find_existing_comment
# also matches the legacy "argocd-diff-preview" marker so comments created by
# older pods are still updated in place (no duplicate comment) during rollout.
COMMENT_MARKER     = "acme-diff-preview"
_COMMENT_MARKERS   = ("acme-diff-preview", "argocd-diff-preview")
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
# Capacity knobs (env-overridable). Defaults sized for a single PR that diffs
# hundreds of apps (a chart version bump rolled out to many clusters at once).
# The diff is a pure local `helm template` render (no ArgoCD agent round-trips),
# so the client can fan out wide: the only shared limit is the Bitbucket API
# (BB_API_CONCURRENCY) used to fetch value files.
MAX_APPS_PER_RUN   = int(os.environ.get("MAX_APPS_PER_RUN", "800"))   # cover 600+ apps/PR with headroom
DIFF_WORKERS       = int(os.environ.get("DIFF_WORKERS", "16"))        # parallel per-app helm-template diffs
DIFF_TIMEOUT       = int(os.environ.get("DIFF_TIMEOUT", "120"))       # seconds per diff (OCI cache-miss pulls are slow)
WARM_WORKERS       = int(os.environ.get("WARM_WORKERS", "4"))         # parallel chart-cache warm-up pulls
WARM_THRESHOLD     = int(os.environ.get("WARM_THRESHOLD", "8"))       # only warm when a PR fans out to more apps than this
MAX_COMMENT_BYTES  = 245_000 # Bitbucket ~256KB limit; leave headroom
JFROG_MAX_BODY_BYTES = int(os.environ.get("JFROG_MAX_BODY_BYTES", "65536"))  # 64 KB — reject oversized bodies before HMAC

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

# Diff operation counters — exposed at GET /diff-preview/stats
_diff_stats:      dict          = {
    "prs_processed": 0,      # PRs where we ran at least one diff
    "apps_diff": 0,          # apps with real changes
    "apps_no_diff": 0,       # apps confirmed unchanged
    "apps_indeterminate": 0, # diffs that could not be computed
    "apps_oci_not_found": 0, # permanent OCI version missing
    "last_iteration_s": None,# seconds taken by most recent iteration
    "last_iteration_at": None,
}
_diff_stats_lock: threading.Lock = threading.Lock()

# In-memory SHA dedup: avoids reprocessing same PR SHA within this pod run
_seen: dict    = {}
_shutdown: bool = False   # set True by SIGTERM handler
_last_ok: float = time.monotonic()  # updated after each successful iteration
_ready: bool    = False   # set True after first successful argocd_login()
_wake           = threading.Event()  # set by POST /diff-preview/webhook
_seen_lock      = threading.Lock()   # guards _seen for concurrent PR processing

# Max parallel PR processing workers. Each worker fans out up to DIFF_WORKERS
# per-app helm-template diffs internally, so the effective worker pool is
# MAX_PR_WORKERS × DIFF_WORKERS. Env-overridable via PR_WORKERS.
MAX_PR_WORKERS  = int(os.environ.get("PR_WORKERS", "3"))

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
            # Healthy if the main loop completed successfully within 5 min.
            age = time.monotonic() - _last_ok
            ok  = age < 300
            self.send_response(200 if ok else 503)
            self.end_headers()
            self.wfile.write(
                b"ok" if ok else f"stale: last success {age:.0f}s ago".encode()
            )
        elif self.path == "/readyz":
            # Ready once argocd_login() has succeeded at startup.
            self.send_response(200 if _ready else 503)
            self.end_headers()
            self.wfile.write(b"ready" if _ready else b"not ready")
        else:
            self.send_response(404)
            self.end_headers()

# HTTP POST handler — receives Bitbucket webhook events
    def do_POST(self):
        if self.path == "/diff-preview/webhook":
            # Bitbucket PR webhook — wake the diff loop immediately.
            # Cap body size (same guard as the JFrog webhook) so a large
            # malformed request cannot exhaust pod memory.
            try:
                length = int(self.headers.get("Content-Length", 0))
            except (ValueError, TypeError):
                length = 0
            if length > JFROG_MAX_BODY_BYTES:
                self.send_response(413)
                self.end_headers()
                return
            if length:
                self.rfile.read(length)
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
            if length > JFROG_MAX_BODY_BYTES:
                log(f"JFrog webhook: body too large ({length} bytes > {JFROG_MAX_BODY_BYTES}), rejecting", "WARNING")
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
            threading.Thread(
                target=_jfrog_hard_refresh,
                args=(chart_name, chart_ver),
                daemon=True,
                name=f"jfrog-refresh-{chart_name}:{chart_ver}",
            ).start()

        else:
            self.send_response(404)
            self.end_headers()

def _verify_jfrog_hmac(body: bytes, header: str) -> bool:
    """Verify X-JFrog-Event-Auth HMAC-SHA256 against the shared webhook secret.

    JFrog signs the payload with HMAC-SHA256 using the secret configured in
    Administration -> Webhooks. The signature is the hex digest of the HMAC,
    sent in the X-JFrog-Event-Auth header.
    """
    import hmac, hashlib
    if not JFROG_WEBHOOK_SECRET or not header:
        return False
    expected = hmac.new(JFROG_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header, expected)


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
        capture_output=True, text=True, timeout=60)

    if r.returncode != 0:
        log(f"JFrog webhook: app list failed: {r.stderr[:200]}", "ERROR")
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
    REFRESH_WORKERS = int(os.environ.get("JFROG_REFRESH_WORKERS", "8"))

    def _do_refresh(app_name: str):
        try:
            r = subprocess.run(
                [ARGOCD_BIN, "app", "get", app_name, "--hard-refresh"] + _auth_flags(),
                capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                log(f"  hard-refresh OK: {app_name}")
                return True
            log(f"  hard-refresh FAILED: {app_name}: {r.stderr[:100]}", "WARNING")
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
    # Auth and transport flags are now stored in ~/.argocd/config (written by
    # argocd_login). Pass only --config so the CLI picks up the token without
    # any credential on the command line (not visible in `ps aux`).
    return ["--config", _ARGOCD_CFG_FILE]

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
            if e.code in (429, 503) and attempt < 2:
                wait = 2 ** attempt
                print(f"    [http] {e.code} - retry {attempt+1}/2 in {wait}s",
                      file=sys.stderr)
                time.sleep(wait)
                last_exc = e
                continue
            raise
        except (OSError, urllib.error.URLError) as e:
            if attempt < 2:
                wait = 2 ** attempt
                print(f"    [http] network error - retry {attempt+1}/2 in {wait}s",
                      file=sys.stderr)
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
        # Invalidate if either the number of path keys OR the total app-reference
        # count has changed (a new app under an existing path changes app count
        # without changing key count, so key count alone is insufficient).
        current_app_count = sum(len(v) for v in _path_map_cache.values())
        if (len(_path_map_cache) == _path_map_count
                and current_app_count == _path_map_app_count):
            return _path_map_cache
    r = subprocess.run(
        [ARGOCD_BIN, "app", "list", "-o", "json"] + _auth_flags(),
        capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        raise RuntimeError(f"argocd app list failed: {r.stderr[:200]}")
    try:
        apps = json.loads(r.stdout)
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


def get_affected_apps(changed_files, path_map):
    """Return sorted app names whose manifest-generate-paths overlap with changed files."""
    apps = set()
    for f in changed_files:
        if f in path_map:
            apps.update(path_map[f])
        else:
            for p, app_list in path_map.items():
                if f.startswith(p + "/") or p.startswith(f + "/"):
                    apps.update(app_list)
    return sorted(apps)


# ── ArgoCD login (used only for app discovery, never for the diff itself) ──
# We avoid passing ARGOCD_PASS as a CLI arg (which is visible in `ps aux`).
# Instead, call the ArgoCD REST API to obtain a JWT token, then write it to
# the ArgoCD config file (~/.argocd/config). Subsequent `argocd app list`
# calls use the config file auth token automatically — no password on argv.
_ARGOCD_CFG_DIR  = os.path.expanduser("~/.argocd")
_ARGOCD_CFG_FILE = os.path.join(_ARGOCD_CFG_DIR, "config")

def _argocd_fetch_token() -> str:
    """Call ArgoCD REST API to get a session JWT. Returns the raw token string."""
    url  = f"https://{ARGOCD_SERVER}/api/v1/session"
    data = json.dumps({"username": ARGOCD_USER, "password": ARGOCD_PASS}).encode()
    req  = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST")
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode    = ssl.CERT_NONE
    with urllib.request.urlopen(req, context=ssl_ctx, timeout=30) as resp:
        return json.loads(resp.read())["token"]


def _write_argocd_config(token: str) -> None:
    """Write a minimal ArgoCD CLI config file with the auth token."""
    import stat
    os.makedirs(_ARGOCD_CFG_DIR, mode=0o700, exist_ok=True)
    cfg = (
        f"contexts:\n"
        f"- context:\n"
        f"    server: {ARGOCD_SERVER}\n"
        f"    user: {ARGOCD_SERVER}\n"
        f"  name: {ARGOCD_SERVER}\n"
        f"current-context: {ARGOCD_SERVER}\n"
        f"servers:\n"
        f"- grpcWeb: true\n"
        f"  insecure: true\n"
        f"  server: {ARGOCD_SERVER}\n"
        f"users:\n"
        f"- auth-token: {token}\n"
        f"  name: {ARGOCD_SERVER}\n"
    )
    tmp = _ARGOCD_CFG_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(cfg)
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600 — owner only
    os.replace(tmp, _ARGOCD_CFG_FILE)


def argocd_login():
    global _ready, _path_map_ts, _path_map_count, _path_map_app_count
    token = _argocd_fetch_token()
    _write_argocd_config(token)
    _path_map_ts        = 0.0  # Invalidate path map cache on re-login.
    _path_map_count     = 0
    _path_map_app_count = 0
    _ready = True
    log(f"ArgoCD auth: session token stored for {ARGOCD_USER} (no password on CLI)")

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

# Structured result of a single argocd_diff() call.
#   text     : reconstructed diff text (only for OUT_DIFF)
#   has_diff : True only for OUT_DIFF (kept for readability at call sites)
#   error    : human-readable detail for INDETERMINATE / ERROR, else None
#   outcome  : one of the OUT_* constants
#   reason   : short machine code for logs/metrics (e.g. "oci_login_401")
DiffResult = namedtuple("DiffResult", ["text", "has_diff", "error", "outcome", "reason"])

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

# Reasons worth retrying in-process with backoff (transient).
RETRYABLE_REASONS = {REASON_OCI_PULL, REASON_METADATA, REASON_TIMEOUT}
# Reasons that permanently block the PR (the deployer would fail the same way).
PERMANENT_REASONS = {REASON_OCI_NOT_FOUND}

# Operator-friendly one-liners shown in the PR comment for each reason.
# The full stderr is in the pod logs at LOG_LEVEL=DEBUG.
_REASON_HINTS = {
    REASON_OCI_NOT_FOUND: "Chart version not found in OCI registry — check that the version exists",
    REASON_OCI_PULL:      "could not pull the OCI chart (registry login or network)",
    REASON_METADATA:      "app not yet in the discovery cache (added since last refresh)",
    REASON_RENDER:        "helm template failed to render the chart with these values",
    REASON_TIMEOUT:       f"a diff step exceeded {DIFF_TIMEOUT}s",
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


_appspace_key_re = re.compile(r"^\s*appspace:\s*(#.*)?$")
_version_key_re  = re.compile(r"^\s*version:\s*([^\s#]+)")


def _extract_chart_version(content: str):
    """Return the chart targetRevision from a config file's `appspace.version`.

    The ApplicationSet sets spec.sources[1].targetRevision = appspace.version, so
    the only value we want is the `version:` that is a DIRECT child of the
    top-level `appspace:` mapping. A plain regex for the first `version:` is
    unsafe: config files carry other, deeper `version:` keys (e.g.
    appspace.elastic.version: 8.15.1) that must never be mistaken for the chart
    revision. We track indentation so only the direct child matches, and return
    None when there is no appspace.version (the PR did not bump the chart here).
    """
    in_appspace     = False
    appspace_indent = -1
    child_indent    = None
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
                return vm.group(1).strip("'\"")
    return None


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

# Registries that have been successfully authenticated this pod lifetime.
_helm_logged_in: set = set()
_helm_login_lock     = threading.Lock()
# Timestamp of the last successful login per registry. Re-login after this many
# seconds so a secret rotation (new OCI_PASS) is picked up without a pod restart.
HELM_LOGIN_TTL       = int(os.environ.get("HELM_LOGIN_TTL", str(6 * 3600)))  # 6h default
_helm_login_ts: dict = {}   # registry -> monotonic timestamp of last successful login
# Local chart path cache: "{registry}/{chart}:{version}" -> "/tmp/.../chart_dir"
_helm_chart_cache: dict = {}
_helm_cache_lock        = threading.Lock()
# Per-chart-version pull locks: prevent multiple threads pulling the same chart at once.
# Without this, concurrent diffs trigger parallel helm pulls to the same directory,
# causing "failed to untar: a file or directory already exists" errors.
_helm_pull_locks: dict  = {}
_helm_pull_locks_lock   = threading.Lock()


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
        log(f"Helm OCI login failed for {registry}: {r.stderr[:200]}", "WARNING")
        return False


def _ensure_chart(registry: str, chart: str, version: str) -> str:
    """Pull an OCI chart to the local cache and return the extracted chart directory.

    Raises OciChartNotFound if the version does not exist in the registry.
    Returns None on other pull failures (network, auth).
    """
    key = f"{registry}/{chart}:{version}"
    with _helm_cache_lock:
        if key in _helm_chart_cache:
            return _helm_chart_cache[key]

    chart_dir = os.path.join(HELM_CACHE_DIR, registry, chart, version)
    if os.path.isdir(chart_dir) and os.listdir(chart_dir):
        # Always resolve the chart subdirectory (helm --untar creates a subdir).
        # A previous code path stored chart_dir directly; _find_chart_subdir is
        # idempotent and harmless when called on an already-resolved path.
        path = _find_chart_subdir(chart_dir)
        with _helm_cache_lock:
            _helm_chart_cache[key] = path
        return path

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
            with _helm_cache_lock:
                _helm_chart_cache[key] = _find_chart_subdir(chart_dir)
            return _helm_chart_cache[key]

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
        return path


def _find_chart_subdir(chart_dir: str) -> str:
    """Return the chart directory inside chart_dir (helm --untar creates a subdir)."""
    try:
        subdirs = [d for d in os.listdir(chart_dir)
                   if os.path.isdir(os.path.join(chart_dir, d))]
        return os.path.join(chart_dir, subdirs[0]) if subdirs else chart_dir
    except OSError:
        return chart_dir


# Cap on pulled chart versions kept on the pod's ephemeral disk. Each mass
# version-bump pulls a couple of versions per chart; over a long pod lifetime
# these accumulate and can fill node ephemeral storage (not bounded by the
# memory limit). Keep the most-recently-used and evict the rest.
HELM_CACHE_MAX_CHARTS = int(os.environ.get("HELM_CACHE_MAX_CHARTS", "60"))


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
VF_CACHE_MAX = int(os.environ.get("VF_CACHE_MAX", "5000"))


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
BB_API_CONCURRENCY = int(os.environ.get("BB_API_CONCURRENCY", "30"))
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
        # Normalize path traversal (e.g. "gcp/dev/ap1/custom/cluster/../../config.yaml"
        # -> "gcp/dev/ap1/config.yaml"). Some apps use relative paths in valueFiles.
        clean = posixpath.normpath(vf.replace("$config/", "").lstrip("/"))
        cache_key = (sha, clean)
        with _vf_cache_lock:
            if cache_key in _vf_cache:
                return vf, _vf_cache[cache_key]
        content, status = _bb_fetch_status(clean, sha)
        # Cache ONLY definitive results: a fetched file (BB_OK) or a confirmed
        # 404 (BB_NOT_FOUND, a stable fact for this immutable sha). NEVER cache a
        # transient BB_ERROR as None — that would poison every app sharing this
        # (sha, path) key and surface as a false "missing value" on the render.
        # Leaving it uncached lets the next app/iteration re-fetch it.
        if status in (BB_OK, BB_NOT_FOUND):
            with _vf_cache_lock:
                _vf_cache[cache_key] = content
        return vf, content

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


def _pr_chart_revision(app, changed_files, pr_sha):
    """Return the new OCI chart targetRevision for an app if the PR changes it.

    Strategy: look at the changed config files that affect this app, fetch each
    one from Bitbucket at pr_sha, and search for an `appspace.version` YAML key.
    That value is the new helm chart targetRevision (the ApplicationSet sets
    spec.sources[1].targetRevision = appspace.version).

    Returns the new revision string if it differs from the current one cached in
    _app_chart_revision_map, otherwise returns None.
    """
    current_rev = _app_chart_revision_map.get(app)
    if not current_rev:
        return None
    # Find config files that (a) changed in this PR and (b) feed this app.
    path_map = _path_map_cache
    candidate_files = []
    for f in changed_files:
        apps_for_file = path_map.get(f, [])
        if app in apps_for_file:
            candidate_files.append(f)
        else:
            for p, app_list in path_map.items():
                if (f.startswith(p + "/") or p.startswith(f + "/")) and app in app_list:
                    candidate_files.append(f)
                    break
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



def _parse_manifest_resources(yaml_text):
    """Split a multi-document YAML string into a dict keyed by (group/Kind, ns/name).

    Each value is the normalized document text (stripped, consistent trailing newline).
    Documents without kind/metadata are skipped.
    """
    resources = {}
    for doc in re.split(r'\n---\s*\n|^---\s*\n', yaml_text, flags=re.MULTILINE):
        doc = doc.strip()
        if not doc:
            continue
        kind = ns = name = api = ""
        in_meta = False
        for line in doc.splitlines():
            if line.startswith("apiVersion:"):
                api = line.split(":", 1)[1].strip()
            elif line.startswith("kind:"):
                kind = line.split(":", 1)[1].strip()
            elif line.startswith("metadata:"):
                in_meta = True
            elif in_meta:
                if line.startswith("  namespace:"):
                    ns = line.split(":", 1)[1].strip()
                elif line.startswith("  name:"):
                    name = line.split(":", 1)[1].strip()
                elif line and not line.startswith(" "):
                    in_meta = False
        if not (kind and name):
            continue
        # Use ArgoCD-style key: /Kind ns/name (group prefix for non-core)
        grp = api.split("/")[0] if "/" in api else ""
        type_key = f"{grp}/{kind}" if grp and grp not in ("v1", "") else kind
        key = (type_key, ns or "", name)
        resources[key] = doc + "\n"
    return resources


def _diff_manifests(main_yaml, pr_yaml):
    """Diff two multi-doc YAML strings resource by resource.

    Returns a diff string in the ArgoCD `===== /Kind ns/name =====` format so the
    rest of the pipeline (parse_diff_sections, format_comment) works unchanged.
    Returns empty string if there are no differences.
    """
    import difflib  # stdlib, lazy import acceptable here (one per diff call)
    main_res = _parse_manifest_resources(main_yaml)
    pr_res   = _parse_manifest_resources(pr_yaml)

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
        # Header format ArgoCD uses: ===== /Kind ns/name ======
        hdr = f"/{type_key} {ns}/{name}" if ns else f"/{type_key} {name}"
        parts.append(f"===== {hdr} ======\n" + "".join(delta))
    return "\n".join(parts)


def _run_one_diff(app, pr_sha, main_sha, chart_revision=None):
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
        # Fetch PR and main value files in parallel — each set takes ~300ms
        # (7 parallel Bitbucket API calls) vs ~2.1s sequential.
        with ThreadPoolExecutor(max_workers=2) as ex:
            pr_vf_fut   = ex.submit(_fetch_value_files, value_files, pr_sha)
            main_vf_fut = ex.submit(_fetch_value_files, value_files, main_sha)
            pr_vals   = pr_vf_fut.result(timeout=DIFF_TIMEOUT)
            main_vals = main_vf_fut.result(timeout=DIFF_TIMEOUT)

        # Render both chart versions in parallel
        with ThreadPoolExecutor(max_workers=2) as ex:
            pr_fut   = ex.submit(_helm_template, pr_chart,   release, namespace, pr_vals)
            main_fut = ex.submit(_helm_template, main_chart, release, namespace, main_vals)
            pr_yaml,   pr_err   = pr_fut.result(timeout=DIFF_TIMEOUT)
            main_yaml, main_err = main_fut.result(timeout=DIFF_TIMEOUT)
    except (subprocess.TimeoutExpired, concurrent.futures.TimeoutError):
        return None, REASON_TIMEOUT, f"render exceeded {DIFF_TIMEOUT}s"
    except Exception as e:
        return None, REASON_RENDER, str(e)[:200]

    if pr_err:
        return None, REASON_RENDER, pr_err
    if main_err:
        return None, REASON_RENDER, main_err

    return _diff_manifests(main_yaml, pr_yaml), None, None


def _indeterminate(reason, detail):
    """Build an INDETERMINATE DiffResult (diff could not be computed)."""
    return DiffResult("", False, detail[:400], OUT_INDETERMINATE, reason)


# Retry budget for a single diff. During a mass version bump the hub is briefly
# saturated, so a transient 5xx/timeout on the first try is normal and clears
# within a few seconds once the chart cache warms. More attempts with growing
# backoff make the diff transparent to reviewers instead of "diff unavailable".
DIFF_RETRIES       = int(os.environ.get("DIFF_RETRIES", "5"))   # total attempts per diff
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


def argocd_diff(app, pr_sha, main_sha, chart_revision=None):
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
        diff_text, reason, detail = _run_one_diff(
            app, pr_sha, main_sha, chart_revision=chart_revision)

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
            return DiffResult("", False, None, OUT_NO_DIFF, "clean")

        # Filter noise sections (checksums, version annotations that always drift)
        filtered_sections = _filter_diff_sections(parse_diff_sections(diff_text))
        if not filtered_sections:
            return DiffResult("", False, None, OUT_NO_DIFF, "noise_only")

        clean_diff = "\n".join(
            f"===== {hdr} =====\n{body}"
            for hdr, body in filtered_sections
        )
        return DiffResult(clean_diff, True, None, OUT_DIFF, "changes")
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
def post_build_status(pr_sha, state, description):
    """Post build status. Swallows errors - never crashes the script."""
    try:
        bb("POST", f"commit/{pr_sha}/statuses/build", body={
            "state": state, "key": BUILD_KEY,
            "name": STATUS_NAME,
            "url": f"https://{ARGOCD_SERVER}",
            "description": description[:255],
        })
    except Exception as e:
        print(f"    [build status] failed to set {state}: {e}", file=sys.stderr)

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
    files, path, pages = [], f"pullrequests/{pr_id}/diffstat?pagelen=100", 0
    while path and pages < _BB_MAX_PAGES:
        data = bb("GET", path)
        for item in data.get("values", []):
            p = (item.get("new") or item.get("old") or {}).get("path", "")
            if p:
                files.append(p)
        nxt  = data.get("next", "")
        path = nxt.replace(f"{_BB_API_BASE}/", "") if nxt else ""
        pages += 1
    return files

def find_existing_comment(pr_id):
    """Search all comment pages for our marker.

    Returns (comment_id, sha_8, raw_text).
    sha_8 is 8-char hex or '' if not found in comment.
    Paginates through all pages so >100-comment PRs are handled correctly.
    """
    nxt, pages = f"pullrequests/{pr_id}/comments?pagelen=100", 0
    while nxt and pages < _BB_MAX_PAGES:
        try:
            data = bb("GET", nxt)
        except Exception:
            return None, "", ""
        for c in data.get("values", []):
            raw = c.get("content", {}).get("raw", "")
            # Match the current marker AND the legacy one so comments written by
            # older pods are updated in place instead of duplicated during rollout.
            if any(mk in raw for mk in _COMMENT_MARKERS):
                m = re.search(r'Commit `([0-9a-f]{8})`', raw)
                return c["id"], (m.group(1) if m else ""), raw
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
        print(f"    [comment] truncated: {len(encoded)//1024}KB -> "
              f"{MAX_COMMENT_BYTES//1024}KB", file=sys.stderr)
    payload = {"content": {"raw": body}}
    try:
        if existing_id:
            bb("PUT",  f"pullrequests/{pr_id}/comments/{existing_id}", body=payload)
        else:
            bb("POST", f"pullrequests/{pr_id}/comments", body=payload)
    except Exception as e:
        # If PUT returns 404 the comment was deleted — fall back to POST with the
        # original body so the diff is still visible and no error text appears.
        # Using an error message as fallback caused a re-run loop because the
        # error text triggered the "had errors" re-run check in process_pr.
        print(f"    [comment] upsert failed ({e}); retrying as new POST", file=sys.stderr)
        try:
            bb("POST", f"pullrequests/{pr_id}/comments", body=payload)
            print(f"    [comment] fallback POST succeeded", file=sys.stderr)
        except Exception as e2:
            print(f"    [comment] fallback POST also failed: {e2}", file=sys.stderr)

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
        # Derive correct state from the machine-readable token first (1.9.1+),
        # then fall back to parsing the human-readable comment text.
        _token_m = re.search(r'\[' + re.escape(COMMENT_MARKER) + r'\s+\[(clean|permanent|transient)\]',
                              comment_raw)
        if _token_m and _token_m.group(1) in ("permanent",):
            state, desc = "FAILED", "Diff failed - check PR comment"
        elif _token_m and _token_m.group(1) in ("clean",):
            if "resource(s) will change" in comment_raw:
                m = re.search(r"(\d+) resource\(s\) will change", comment_raw)
                n = m.group(1) if m else "?"
                state, desc = "SUCCESSFUL", f"{n} resource(s) will change - review comment"
            else:
                state, desc = "SUCCESSFUL", "No manifest changes"
        elif _token_m and _token_m.group(1) == "transient":
            state, desc = "SUCCESSFUL", "Diff unavailable - review comment"
        elif "Error running diff" in comment_raw or "\u274c" in comment_raw:
            state, desc = "FAILED", "Diff failed - check PR comment"
        elif "not found in OCI registry" in comment_raw:
            state, desc = "FAILED", "Chart version not found in OCI registry"
        elif "resource(s) will change" in comment_raw:
            m = re.search(r"(\d+) resource\(s\) will change", comment_raw)
            n = m.group(1) if m else "?"
            state, desc = "SUCCESSFUL", f"{n} resource(s) will change - review comment"
        elif "Diff incomplete" in comment_raw:
            state, desc = "SUCCESSFUL", "Diff unavailable - review comment"
        else:
            state, desc = "SUCCESSFUL", "No manifest changes"
        post_build_status(pr_sha, state, desc)
        print(f"    Fixed stuck INPROGRESS for PR #{pr_id} -> {state}")
    except Exception as e:
        print(f"    [fix_stuck_inprogress] PR #{pr_id}: {e}", file=sys.stderr)

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
VERTEX_MODEL             = "gemini-2.5-flash"

# Thresholds for switching between inline and collapsed diff display.
# Bitbucket does NOT render HTML <details>/<summary> tags, so there is no
# real "collapse" available. For large PRs we show a compact summary table +
# truncated inline diffs instead of trying to use <details>.
LARGE_PR_APP_THRESHOLD   = 5       # changed apps above this -> large mode
LARGE_PR_DIFF_BYTES      = 40_000  # total diff bytes above this -> large mode
# In large mode, show the diff for the top N most-changed apps inline.
# Others get a table row only (no diff block) to stay within the 245KB limit.
LARGE_PR_INLINE_APPS     = int(os.environ.get("LARGE_PR_INLINE_APPS", "6"))

# Limits for what we send to the model.
AI_MAX_SECTIONS_PER_APP  = 10
AI_MAX_BODY_CHARS        = 1500

def _gcp_access_token() -> str:
    """Return a valid GCE access token, reusing the cached one when possible.

    Tokens are valid for ~3600s. We refresh when fewer than 60s remain
    so there is no risk of using an expired token mid-request.
    """
    global _gcp_token, _gcp_token_exp
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
        changed = {
            app: parse_diff_sections(r.text)
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

        total_resources = sum(len(s) for s in changed.values())

        sections_parts = []
        for app, sections in changed.items():
            sections_parts.append(f"### App: {app}")
            for header, body in sections[:AI_MAX_SECTIONS_PER_APP]:
                trimmed = body[:AI_MAX_BODY_CHARS]
                if len(body) > AI_MAX_BODY_CHARS:
                    trimmed += "\n... (truncated)"
                sections_parts.append(f"Resource: {header}\n{trimmed}")

        error_note = ""
        if errors:
            error_note = (
                "\n\nApps whose diff could NOT be computed (treat as unknown, "
                f"not unchanged): {', '.join(errors.keys())}"
            )

        prompt = (
            "You are a Senior SRE reviewing a Kubernetes GitOps diff from a Helm-based platform.\n"
            f"Changeset: {len(changed)} app(s), {total_resources} resource section(s).\n\n"
            "ANALYSIS REQUIREMENTS:\n"
            "- Only analyse what is explicitly shown in the diff below.\n"
            "- Helm shows changes as '-' (old) and '+' (new) lines — this is normal for updates.\n"
            "- VERSION COMPARISON: only report a downgrade when the full version string actually "
            "decreases (e.g. 1.93.1 → 1.93.0 is a downgrade; 1.93.1-rc1 → 1.93.1-rc2 is NOT).\n"
            "- Skip annotation-only changes (argocd.argoproj.io/tracking-id, "
            "helm.sh/chart, kubectl.kubernetes.io/last-applied-configuration, checksum/).\n"
            "- For new Deployments/StatefulSets: say 'new service'.\n"
            "- For removed ones: say 'removed'.\n\n"
            "Respond in EXACTLY this format (no extra sections, no prose outside these headers):\n\n"
            f"**{len(changed)} app(s) updated · {total_resources} resource(s) changed**\n\n"
            "1. 🌍 **AFFECTED ENVIRONMENTS:** `cl-env1-a`, `cl-env2-a` (N total)\n\n"
            "2. 📊 **SUMMARY:**\n"
            "   One sentence overview of the change type (e.g. 'Version bump from X to Y across N envs').\n"
            "   Key service changes (max 8 entries, group similar ones):\n"
            "   - `service-name`: `old-ver` → `new-ver`\n"
            "   - `service-name`: new service added\n"
            "   - `service-name`: removed\n\n"
            "3. ⚠️ **CRITICAL CHANGES:**\n"
            "   - Version downgrades (full version string decreasing only)\n"
            "   - Replicas dropping to 0\n"
            "   - Services removed\n"
            "   - Liveness/readiness probes removed\n"
            "   If none: `No critical changes detected`\n\n"
            "Rules: max 250 words total. Do NOT repeat the environments list in the summary. "
            "Group similar changes with '+N more'. Be terse — operators scan, they do not read.\n\n"
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
                    # Disable thinking tokens in gemini-2.5-flash.
                    # Without this, the model uses ~1100 thinking tokens
                    # leaving almost nothing for actual output (finish=MAX_TOKENS).
                    "thinkingConfig": {"thinkingBudget": 0},
                },
            },
        )
        candidate = resp["candidates"][0]
        finish    = candidate.get("finishReason", "UNKNOWN")
        ai_text   = candidate["content"]["parts"][0]["text"].strip()
        elapsed   = round((_time.monotonic() - _t0) * 1000)
        usage     = resp.get("usageMetadata", {})
        in_tok    = usage.get("promptTokenCount", "?")
        out_tok   = usage.get("candidatesTokenCount", "?")
        print(f"      [AI] Response OK | finish={finish} | "
              f"tokens in={in_tok} out={out_tok} | "
              f"output={len(ai_text)} chars | elapsed={elapsed}ms")
        if finish == "MAX_TOKENS":
            print(
                "      [AI] WARNING: response truncated (MAX_TOKENS) — "
                "increase maxOutputTokens or shorten prompt",
                file=sys.stderr,
            )
        return _normalize_ai_markdown(ai_text)
    except Exception as e:
        err_str = str(e)
        if "404" in err_str and "does not have access" in err_str:
            print(
                "    [AI summary] Vertex AI Model Garden not enabled. "
                "Accept Gemini terms: https://console.cloud.google.com/"
                "vertex-ai/model-garden?project=appspace-devops",
                file=sys.stderr,
            )
        else:
            print(f"    [AI summary] Vertex AI call failed: {e}", file=sys.stderr)
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
        return DiffResult(text, True, None, OUT_DIFF, "changes")
    if error:
        return DiffResult("", False, error, OUT_INDETERMINATE, "legacy")
    return DiffResult("", False, None, OUT_NO_DIFF, "clean")


def _format_app_diff_block(app, sections, diff_text, show_diff=True):
    """Return a list of markdown lines for one app's diff block.

    show_diff=False outputs just the header line (for large-mode table overflow).
    Bitbucket does NOT render HTML <details>/<summary>, so we never use them.
    """
    n = len(sections) if sections else 1
    out = [f"\u26a0\ufe0f **`{app}`** \u2014 {n} resource(s) changed", ""]
    if not show_diff:
        return out
    if sections:
        for hdr, body in sections[:MAX_RESOURCES_FULL]:
            truncated = body[:MAX_DIFF_CHARS]
            if len(body) > MAX_DIFF_CHARS:
                truncated += "\n... (truncated)"
            out += [f"**`{hdr}`**", "", "```diff", truncated.rstrip(), "```", ""]
        if len(sections) > MAX_RESOURCES_FULL:
            out += [f"*\u2026 and {len(sections) - MAX_RESOURCES_FULL} more resource(s)*", ""]
    else:
        raw = diff_text[:MAX_DIFF_CHARS * 2]
        if len(diff_text) > MAX_DIFF_CHARS * 2:
            raw += "\n... (truncated)"
        out += ["```diff", raw.rstrip(), "```", ""]
    return out


def format_comment(pr_sha, app_results, skipped_apps=None):
    """Format the full PR comment. Never uses <details>/<summary> — Bitbucket
    does not render them. Large changesets get a compact summary table at the
    top (all apps, one row each) and inline diffs for the top-N most-changed
    apps only to stay well inside the 245KB comment limit."""
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

    # ── AI Analysis block ────────────────────────────────────────────
    if ai_summary:
        lines += [
            "---",
            "### \U0001f916 AI Analysis",
            "> *Powered by Gemini 2.5 Flash \u2014 always verify before merging*",
            "",
            ai_summary,
            "",
        ]

    lines += ["---", ""]

    # ── Large-PR summary table ────────────────────────────────────────
    # For large changesets, show a compact overview table first so reviewers
    # can scan all affected apps at a glance before reading the inline diffs.
    if is_large:
        lines += [
            "#### Changeset overview",
            "",
            "| App | Status | Resources |",
            "|-----|--------|-----------|",
        ]
        for app, r in results.items():
            if r.outcome == OUT_DIFF:
                n = len(parse_diff_sections(r.text)) if r.text else 1
                lines.append(f"| `{app}` | \u26a0\ufe0f changed | {n} |")
            elif r.outcome == OUT_INDETERMINATE:
                lines.append(f"| `{app}` | \u2754 diff unavailable | \u2014 |")
            elif r.outcome == OUT_ERROR:
                lines.append(f"| `{app}` | \u274c error | \u2014 |")
            else:
                lines.append(f"| `{app}` | \u2705 no changes | 0 |")
        lines += [""]

    # ── Per-app diff sections ─────────────────────────────────────────
    # For large PRs: show inline diffs for top-N most-changed apps only.
    # Sort by resource count descending so the most impactful diffs appear first.
    if is_large and changed_apps:
        inline_set = {
            app for app, _ in sorted(
                changed_apps,
                key=lambda x: len(parse_diff_sections(x[1].text) or []),
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
        inline_set = None   # show all inline

    for app, r in results.items():
        diff_text = r.text
        if r.outcome == OUT_ERROR:
            any_error = True
            lines += [f"\u274c **`{app}`** \u2014 error: {(r.error or '')[:200]}", ""]

        elif r.outcome == OUT_INDETERMINATE:
            any_unknown = True
            unknown_apps.append(app)
            hint = _REASON_HINTS.get(r.reason, "diff could not be computed")
            lines += [
                f"\u2754 **`{app}`** \u2014 diff unavailable ({hint})",
                "",
            ]

        elif r.outcome == OUT_DIFF:
            any_change = True
            sections   = parse_diff_sections(diff_text)
            n          = len(sections) if sections else 1
            total_changed += n
            show_diff = (inline_set is None) or (app in inline_set)
            lines += _format_app_diff_block(app, sections, diff_text, show_diff=show_diff)

        else:
            lines += [f"\u2705 **`{app}`** \u2014 no manifest changes", ""]

    # ── Skipped apps note ────────────────────────────────────────────
    if skipped_apps:
        lines += [
            f"*{len(skipped_apps)} app(s) skipped (cap {MAX_APPS_PER_RUN}): "
            f"{', '.join(skipped_apps[:5])}{'...' if len(skipped_apps) > 5 else ''}*", ""]

    # ── Footer ───────────────────────────────────────────────────────
    unknown_note = ""
    if any_unknown:
        unknown_note = (
            f" \u2014 \u2754 {len(unknown_apps)} app(s) could not be evaluated "
            f"(diff unavailable, NOT confirmed unchanged)"
        )
    if any_error:
        status = "\u274c Error running diff"
    elif any_change:
        status = f"\u26a0\ufe0f {total_changed} resource(s) will change{unknown_note}"
    elif any_unknown:
        status = (f"\u2754 Diff incomplete \u2014 {len(unknown_apps)} app(s) could not "
                  f"be evaluated (NOT confirmed unchanged)")
    else:
        status = "\u2705 No manifest changes"

    # Machine-readable token embedded in the footer. Used by process_pr to decide
    # whether to re-run without parsing the human-readable status string.
    # Tokens: clean | permanent | transient
    # - clean     : all apps diffed successfully (no retry, mark seen)
    # - permanent : oci_not_found or hard error (no retry, mark seen)
    # - transient : diff unavailable on transient blip (retry next loop)
    if any_error:
        _status_token = "permanent"
    elif any_unknown:
        # Distinguish oci_not_found (permanent) from soft indeterminate (transient)
        resolved = [_result(v) for v in app_results.values()]
        indet    = [r for r in resolved if r.outcome == OUT_INDETERMINATE]
        all_permanent = bool(indet) and all(r.reason in PERMANENT_REASONS for r in indet)
        _status_token = "permanent" if all_permanent else "transient"
    else:
        _status_token = "clean"

    lines += [
        "---",
        f"**Status:** {status}",
        f"*{_ts()} \u2014 {COMMENT_MARKER} [{_status_token}]*",
    ]
    return "\n".join(lines)

# ── Per-PR processing (isolated) ──────────────────────────────────────
def process_pr(pr, path_map, base_sha=""):
    """Process one PR. All exceptions are caught so other PRs are not affected."""
    pr_id  = pr["id"]
    pr_sha = pr["source"]["commit"]["hash"]
    dest   = pr["destination"]["branch"]["name"]
    print(f"  PR #{pr_id}: {pr['title'][:50]!r} -> {dest} ({pr_sha[:8]})")

    if dest != "main":
        return

    # In-memory dedup: skip same SHA already processed in this pod run
    with _seen_lock:
        if _seen.get(pr_id) == pr_sha:
            print(f"    Skipping: SHA {pr_sha[:8]} already processed in this run")
            return

    # Cross-pod dedup: existing comment already covers this exact SHA
    existing_id, comment_sha, comment_raw = find_existing_comment(pr_id)
    if comment_sha == pr_sha[:8]:
        # Use the machine-readable [token] embedded in the comment footer (1.9.1+)
        # to decide if a re-run is needed. For legacy comments that lack the token
        # fall back to string matching on human-readable text.
        _token_match = re.search(r'\[' + re.escape(COMMENT_MARKER) + r'\s+\[(clean|permanent|transient)\]',
                                 comment_raw)
        if _token_match:
            _token = _token_match.group(1)
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
        if rerun:
            print(f"    Re-running: previous comment for SHA {pr_sha[:8]} was not clean, retrying diff")
            # existing_id is kept — the comment will be updated in place, not duplicated.
        else:
            with _seen_lock:
                _seen[pr_id] = pr_sha
            print(f"    Skipping: comment up to date for SHA {pr_sha[:8]}")
            # Fix potential stuck INPROGRESS from a previously killed pod
            fix_stuck_inprogress(pr_sha, pr_id, comment_raw)
            return

    try:
        changed  = get_pr_changed_files(pr_id)
        affected = get_affected_apps(changed, path_map)
        print(f"    Changed files: {len(changed)} | Affected apps: {len(affected)}")

        if not affected:
            # No infra apps matched - post SUCCESSFUL so merge gates don't block.
            # Always write a comment so the reviewer sees a clear explanation,
            # especially for new-environment PRs where no Application exists yet.
            print(f"    No ArgoCD apps affected - posting SUCCESSFUL")
            post_build_status(pr_sha, "SUCCESSFUL",
                "No ArgoCD apps affected by this PR")
            no_apps_body = (
                f"## \U0001f52d {STATUS_NAME}\n\n"
                f"Commit `{pr_sha[:8]}` vs `main` | `{BB_REPO}`\n\n"
                f"\u2705 **No ArgoCD apps are currently affected by the files "
                f"changed in this commit.**\n\n"
                f"If this PR adds configuration for a **new environment** that "
                f"has not been deployed before, this is expected. ArgoCD does not "
                f"have an Application for it yet - the ApplicationSet will create "
                f"one automatically once this PR is merged to `main`. "
                f"Subsequent PRs for this environment will show a normal diff.\n\n"
                f"---\n**Status:** \u2705 No manifest changes\n"
                f"*{_ts()} \u2014 {COMMENT_MARKER} [clean]*"
            )
            upsert_comment(pr_id, no_apps_body, existing_id)
            with _seen_lock:
                _seen[pr_id] = pr_sha
            return

        print(f"    Apps: {affected}")
        post_build_status(pr_sha, "INPROGRESS", "Running ArgoCD diff...")

        skipped_apps = []
        if len(affected) > MAX_APPS_PER_RUN:
            skipped_apps = affected[MAX_APPS_PER_RUN:]
            affected    = affected[:MAX_APPS_PER_RUN]
            print(f"    Capped to {MAX_APPS_PER_RUN} apps "
                  f"({len(skipped_apps)} skipped)")

        app_results   = {}
        any_hard_error = False   # OUT_ERROR — unexpected failure
        any_unknown    = False   # OUT_INDETERMINATE — diff not computable
        outcome_counts = Counter()
        reason_counts  = Counter()

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
        with ThreadPoolExecutor(max_workers=max(1, min(DIFF_WORKERS, len(affected)))) as ex:
            rev_futs = {ex.submit(_pr_chart_revision, app, changed, pr_sha): app
                        for app in affected}
            for fut in as_completed(rev_futs):
                app = rev_futs[fut]
                try:
                    new_rev = fut.result()
                except Exception:
                    new_rev = None
                if new_rev:
                    pr_chart_revisions[app] = new_rev
        if pr_chart_revisions:
            unique_bumps = sorted(set(pr_chart_revisions.values()))
            log(f"PR #{pr_id}: chart version bumps detected for "
                f"{len(pr_chart_revisions)} app(s) -> {unique_bumps}",
                pr=pr_id)

        def run_diff(app):
            t0 = time.monotonic()
            chart_rev = pr_chart_revisions.get(app)
            result = argocd_diff(app, pr_sha, main_sha=base_sha,
                                 chart_revision=chart_rev)
            elapsed = round(time.monotonic() - t0, 1)
            return app, result, elapsed

        def process_batch(apps, workers):
            """Diff a list of apps with a bounded pool, accumulating results."""
            nonlocal any_hard_error, any_unknown
            if not apps:
                return
            with ThreadPoolExecutor(max_workers=max(1, min(workers, len(apps)))) as ex:
                futures = {ex.submit(run_diff, app): app for app in apps}
                for fut in as_completed(futures):
                    app, result, elapsed = fut.result()
                    app_results[app] = result
                    outcome_counts[result.outcome] += 1
                    if result.outcome == OUT_ERROR:
                        any_hard_error = True
                        reason_counts[result.reason] += 1
                    elif result.outcome == OUT_INDETERMINATE:
                        any_unknown = True
                        reason_counts[result.reason] += 1
                    n_sections = (len(parse_diff_sections(result.text))
                                  if result.outcome == OUT_DIFF else 0)
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
            pulls_needed = {
                (reg, chart, ver) for reg, chart, ver in unique_chart_pulls
                if not os.path.isdir(os.path.join(HELM_CACHE_DIR, reg, chart, ver))
                and f"{reg}/{chart}:{ver}" not in _helm_chart_cache
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

        # Per-PR breakdown — at a glance, how many apps failed and why.
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(outcome_counts.items()))
        reasons   = ", ".join(f"{k}={v}" for k, v in sorted(reason_counts.items()))
        log(f"PR #{pr_id} diff summary: {breakdown}"
            + (f" | reasons: {reasons}" if reasons else ""),
            pr=pr_id, **{f"n_{k}": v for k, v in outcome_counts.items()})

        body = format_comment(pr_sha, app_results, skipped_apps)
        comment_kb = round(len(body.encode()) / 1024, 1)
        upsert_comment(pr_id, body, existing_id)
        action = "updated" if existing_id else "posted"
        print(f"    Comment {action} on PR #{pr_id} ({comment_kb}KB)")

        # Count changed resources and classify indeterminate reasons FIRST,
        # then update stats and build status (oci_not_found_count must be defined
        # before the stats block references it — bug fix for UnboundLocalError).
        sections_total = sum(
            max(len(parse_diff_sections(r.text)), 1)
            for r in app_results.values() if r.outcome == OUT_DIFF)
        n_unknown = outcome_counts[OUT_INDETERMINATE]
        # oci_not_found is a hard permanent error (wrong version in config) that
        # MUST block the PR — the deployer will fail the same way.
        oci_not_found_count = sum(
            1 for r in app_results.values()
            if r.outcome == OUT_INDETERMINATE and r.reason == REASON_OCI_NOT_FOUND
        )
        has_blocking_indet = oci_not_found_count > 0

        # Update global diff counters for /diff-preview/stats
        with _diff_stats_lock:
            _diff_stats["prs_processed"] += 1
            _diff_stats["apps_diff"] += outcome_counts.get(OUT_DIFF, 0)
            _diff_stats["apps_no_diff"] += outcome_counts.get(OUT_NO_DIFF, 0)
            _diff_stats["apps_indeterminate"] += outcome_counts.get(OUT_INDETERMINATE, 0)
            _diff_stats["apps_oci_not_found"] += oci_not_found_count

        if any_hard_error or has_blocking_indet:
            if oci_not_found_count:
                post_build_status(pr_sha, "FAILED",
                    f"{oci_not_found_count} app(s): chart version not found in OCI registry")
            else:
                post_build_status(pr_sha, "FAILED", "Diff failed - check PR comment")
        elif sections_total > 0:
            extra = f" ({n_unknown} unavailable)" if any_unknown else ""
            post_build_status(pr_sha, "SUCCESSFUL",
                f"{sections_total} resource(s) will change - review comment{extra}")
        elif any_unknown:
            # Soft indeterminate (transient timeout, etc.) - not a hard block but
            # operator should review. Use SUCCESSFUL so it does not block merge
            # gates on a transient failure; the next iteration will retry.
            post_build_status(pr_sha, "SUCCESSFUL",
                f"Diff unavailable for {n_unknown} app(s) - review comment")
        else:
            post_build_status(pr_sha, "SUCCESSFUL", "No manifest changes")

        # Mark as seen logic:
        # - Clean run (no error, no indeterminate): mark seen -> skip next iteration
        # - Soft indeterminate (transient timeout): leave unseen -> retry next iteration
        # - oci_not_found / hard error: mark seen -> DO NOT retry (permanent failure;
        #   the version does not exist and retrying is wasteful + misleading)
        is_permanent_failure = any_hard_error or has_blocking_indet
        is_transient_failure = any_unknown and not has_blocking_indet
        if not is_transient_failure:
            # Mark seen for both clean runs AND permanent failures so we don't
            # spam the PR with repeated "not found" comments every 60s.
            with _seen_lock:
                _seen[pr_id] = pr_sha
        return outcome_counts

    except Exception as e:
        print(f"    [ERROR] PR #{pr_id}: {e}", file=sys.stderr)
        try:
            post_build_status(pr_sha, "FAILED", f"Diff error: {str(e)[:200]}")
        except Exception:
            pass
        err_body = (
            f"## \U0001f52d {STATUS_NAME}\n\n"
            f"Commit `{pr_sha[:8]}` vs `main` | `{BB_REPO}`\n\n"
            f"\u274c **Error processing diff:** {str(e)[:400]}\n\n"
            f"---\n**Status:** \u274c Error running diff\n"
            f"*{_ts()} \u2014 {COMMENT_MARKER} [permanent]*"
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

    # Trim the on-disk chart cache before any diffs so it never races a pull.
    _prune_helm_cache()

    try:
        path_map = discover_path_app_map()
    except Exception as e:
        log(f"Cannot discover ArgoCD apps: {e}", "ERROR")
        # Best-effort: mark all main-targeting open PRs as FAILED
        try:
            for pr in get_open_prs():
                if pr.get("destination", {}).get("branch", {}).get("name") == "main":
                    post_build_status(
                        pr["source"]["commit"]["hash"],
                        "FAILED", f"ArgoCD unavailable: {str(e)[:180]}")
        except Exception:
            pass
        # Re-login in case the ArgoCD session expired
        try:
            argocd_login()
        except Exception:
            pass
        return
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
        prs = get_open_prs()
    except Exception as e:
        log(f"Bitbucket API error: {e}", "ERROR")
        return
    log(f"Open PRs: {len(prs)}")

    # Evict _seen entries for PRs no longer open. Without this, a PR that
    # is declined and immediately reopened with the same SHA would be silently
    # skipped because the old SHA is still in _seen.
    open_ids = {pr["id"] for pr in prs}
    with _seen_lock:
        for stale_id in [k for k in _seen if k not in open_ids]:
            del _seen[stale_id]

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
    global _last_ok
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
            _wake.wait(timeout=60)
            _wake.clear()

    log("Shutdown complete", "WARNING")

if __name__ == "__main__":
    main()