#!/usr/bin/env python3
"""ACME Diff Preview - dynamic discovery, robust error handling, SHA dedup.

All apps are multi-source: source-1 = acme-config-dev, source-2 = Helm OCI.

Diff strategy: manifest-based (PR render vs main render), NOT live-state comparison.
Uses `argocd app manifests` twice (PR sha + main sha) and diffs the YAML locally.
This avoids fetching live resources from spoke agents, making each diff 10-30x
faster (2-10s vs 20-360s per app). No per-agent serialization needed; all apps
can be diffed in parallel.

When the PR bumps appspace.version (the OCI chart targetRevision), both sources
are overridden for the PR render: --source-positions 1 (git) and 2 (OCI chart).
The chart version is detected by reading the PR config file via Bitbucket API.

Diff outcome model (see classify_diff_error):
- diff          : argocd produced a real manifest diff (exit 1 + stdout)
- no_diff       : argocd confirmed the manifests match (clean exit 0)
- indeterminate : the diff could NOT be computed (OCI login 401, repo-server
                  502 on GetManifests, spoke Redis timeout, managed-mode live
                  state unavailable, ArgoCD busy, ...). This is NOT "no changes"
                  and must never be shown as a green check.
- error         : an unexpected / unknown failure.

Why indeterminate matters: ArgoCD renders each multi-source app from the Helm
OCI registry. On a manifest cache miss the repo-server runs `helm registry
login`; if that fails (bad OCI credential) or the agent/proxy/Redis path is
slow, the diff fails. Previously those failures were swallowed as "no manifest
changes", hiding real changes from reviewers. They are now surfaced explicitly.

Error handling:
- argocd app list failure: FAILED on all open main-targeting PRs, clean exit
- Bitbucket API 429/503/network: retry with exponential backoff (3 attempts)
- diff timeout (60s): caught per-app, reported as indeterminate in comment
- diff with no === sections: fallback to raw diff block in comment
- large comment (>245KB): truncated with note, still posted
- upsert_comment failure: fallback minimal note attempted
- any per-PR exception: FAILED status + error comment, other PRs continue
- 0 apps affected: SUCCESSFUL posted so merge
 gates don't block non-infra PRs

SHA dedup:
- In-memory: skips same PR SHA within this pod's loop iterations
- Cross-pod: compares comment SHA; skips and fixes stuck INPROGRESS if needed
"""
import json, os, posixpath, random, re, signal, ssl, sys, subprocess, time, threading, urllib.error, urllib.request
from collections import Counter, namedtuple
from http.server import HTTPServer, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

BB_WORKSPACE       = "appspace-cloud"
BB_REPO            = "acme-config-dev"
BB_USER            = os.environ["BB_USER"]
BB_TOKEN           = os.environ["BB_TOKEN"]
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
# The hub is protected by reposerver.parallelism.limit (per-pod render queue)
# and a 100-processor principal, so the client can fan out wider safely.
MAX_APPS_PER_RUN   = int(os.environ.get("MAX_APPS_PER_RUN", "800"))   # cover 600+ apps/PR with headroom
DIFF_WORKERS       = int(os.environ.get("DIFF_WORKERS", "16"))        # parallel argocd app diff calls
DIFF_TIMEOUT       = int(os.environ.get("DIFF_TIMEOUT", "120"))       # seconds per argocd app diff (OCI cache-miss renders are slow)
WARM_WORKERS       = int(os.environ.get("WARM_WORKERS", "4"))         # parallel chart-cache warm-up diffs
WARM_THRESHOLD     = int(os.environ.get("WARM_THRESHOLD", "8"))       # only warm when a PR fans out to more apps than this
# Max concurrent diffs targeting a SINGLE agent / spoke cluster. All apps on one
# spoke share one argocd-agent + the principal resource-proxy connection to it.
# A single `argocd app diff` of a large app fans out into hundreds of live-resource
# requests to that one agent, so several diffs at once overrun the agent's response
# window and the principal drops the late responses ("resource response not tracked"),
# which surfaces as redis_timeout. Default 1 = serialize per spoke; throughput still
# scales by parallelizing ACROSS spokes (DIFF_WORKERS). Measured on a 24-app bump:
# 1 -> 26/27 clean, 0 principal panics; 3 -> 11 failures + send-on-closed-channel panics.
AGENT_MAX_CONCURRENCY = int(os.environ.get("AGENT_MAX_CONCURRENCY", "1"))
# Global per-agent semaphores, shared across ALL concurrently processed PRs.
# Must be module-level (not per-PR): two PRs that both touch the same spoke would
# otherwise each get their own cap and double the load on that one agent.
_agent_sems: dict          = {}
_agent_sems_lock: threading.Lock = threading.Lock()


def _agent_semaphore(app):
    """Return the shared semaphore for an app's target agent (created on demand)."""
    agent = _app_agent_map.get(app, "_")
    with _agent_sems_lock:
        sem = _agent_sems.get(agent)
        if sem is None:
            sem = threading.Semaphore(AGENT_MAX_CONCURRENCY)
            _agent_sems[agent] = sem
        return sem
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

# In-memory SHA dedup: avoids reprocessing same PR SHA within this pod run
_seen: dict    = {}
_shutdown: bool = False   # set True by SIGTERM handler
_last_ok: float = time.monotonic()  # updated after each successful iteration
_ready: bool    = False   # set True after first successful argocd_login()
_wake           = threading.Event()  # set by POST /diff-preview/webhook
_seen_lock      = threading.Lock()   # guards _seen for concurrent PR processing

# Max parallel PR processing workers. Each worker fans out up to DIFF_WORKERS
# argocd app diff subprocesses internally, so the effective subprocess pool is
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
# app full_name -> agent / destination cluster (spec.destination.name, e.g.
# "gcp-dev-cl-ap1-a"). All apps on one spoke share a single argocd-agent and its
# redis/resource proxy connection, so diffing too many of them at once saturates
# that agent (redis_timeout / "resource response not tracked"). Used to cap
# concurrency PER agent while keeping high total concurrency across agents.
_app_agent_map: dict   = {}

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
            # Bitbucket PR webhook — wake the diff loop immediately
            length = int(self.headers.get("Content-Length", 0))
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
    import hmac as _hmac, hashlib as _hashlib
    if not JFROG_WEBHOOK_SECRET or not header:
        return False
    expected = _hmac.new(JFROG_WEBHOOK_SECRET.encode(), body, _hashlib.sha256).hexdigest()
    return _hmac.compare_digest(header, expected)


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

    from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _asc
    ok = failed = 0
    with _TPE(max_workers=REFRESH_WORKERS) as pool:
        futures = {pool.submit(_do_refresh, app): app for app in matching}
        for fut in _asc(futures):
            if fut.result():
                ok += 1
            else:
                failed += 1

    with _jfrog_stats_lock:
        _jfrog_stats["refreshes_ok"]     += ok
        _jfrog_stats["refreshes_failed"] += failed

    log(f"JFrog webhook: done — {ok} refreshed, {failed} failed")


def _start_health_server(port: int = 8080) -> HTTPServer:
    """Start the health server in a daemon thread and handle webhook POSTs."""
    server = HTTPServer(("", port), _HealthHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True, name="health-server")
    t.start()
    log(f"Health server listening on :{port}")
    return server

def _auth_flags():
    return ["--grpc-web", "--insecure"]

def _ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

# ── HTTP with retry ───────────────────────────────────────────────────
# Default SSL context verifies certificates against the system CA bundle.
# ArgoCD uses subprocess with --insecure (for its self-signed cert) so
# this context only applies to external HTTPS calls: Bitbucket and Vertex AI.
_ssl = ssl.create_default_context()

def http(method, url, body=None, headers=None, auth=None):
    """HTTP call with exponential backoff on 429/503/network errors."""
    import base64
    hdrs = dict(headers or {})
    if auth:
        hdrs["Authorization"] = "Basic " + base64.b64encode(
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
    global _path_map_cache, _path_map_ts, _path_map_count, _app_chart_map, \
           _app_chart_revision_map, _app_chart_registry_map, \
           _app_value_files_map, _app_namespace_map, _app_agent_map
    if _path_map_cache and (time.monotonic() - _path_map_ts) < PATH_MAP_TTL:
        # Also check app count hasn't changed (new env added mid-TTL)
        if len(_path_map_cache) == _path_map_count:
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
    agent_map = {}
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
        if dest.get("name"):
            agent_map[full_name] = dest["name"]
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
    _app_agent_map           = agent_map
    _path_map_ts    = time.monotonic()
    _path_map_count = len(path_map)
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


def _extract_app_chart(app):
    """Backward-compat wrapper: return chart name only."""
    chart, _, _, _ = _extract_app_chart_info(app)
    return chart

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


def _select_warm_apps(affected):
    """Split affected apps into (warm_reps, rest) for chart-cache warm-up.

    Returns one representative app per distinct OCI chart that has more than one
    affected app (the followers reuse the warmed chart pull). Charts with a
    single affected app, and apps with an unknown chart, are left in `rest` since
    warming them separately only adds latency. Below WARM_THRESHOLD apps the
    whole PR fans out directly (warm_reps is empty).
    """
    if len(affected) <= WARM_THRESHOLD:
        return [], list(affected)
    by_chart = {}
    unknown  = []
    for app in affected:
        chart = _app_chart_map.get(app)
        if chart:
            by_chart.setdefault(chart, []).append(app)
        else:
            unknown.append(app)
    warm, rest = [], list(unknown)
    for chart, group in by_chart.items():
        if len(group) > 1:
            warm.append(group[0])
            rest.extend(group[1:])
        else:
            rest.extend(group)
    return warm, rest


def _interleave_by_agent(apps):
    """Round-robin apps across their target agent so the worker pool spreads load.

    Submitting all apps of one agent back-to-back would make the first workers
    pile onto a single spoke (and block on its per-agent semaphore) while other
    agents sit idle. Interleaving keeps every agent busy at its own safe rate.
    """
    buckets = {}
    for app in apps:
        buckets.setdefault(_app_agent_map.get(app, "_"), []).append(app)
    order, queues = [], list(buckets.values())
    i = 0
    while queues:
        q = queues[i % len(queues)]
        order.append(q.pop(0))
        if not q:
            queues.remove(q)
        else:
            i += 1
    return order

# ── ArgoCD diff ───────────────────────────────────────────────────────
def argocd_login():
    global _ready, _path_map_ts
    r = subprocess.run(
        [ARGOCD_BIN, "login", ARGOCD_SERVER,
         "--username", ARGOCD_USER, "--password", ARGOCD_PASS,
         "--grpc-web", "--insecure"],
        capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"argocd login failed: {r.stderr[:200]}")
    _path_map_ts    = 0.0  # Invalidate path map cache on re-login.
    _path_map_count = 0
    _ready = True
    log(f"ArgoCD auth: logged in as {ARGOCD_USER}")

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
    )
    changed = [
        l.lstrip("+-< >").strip()
        for l in body.splitlines()
        if l.startswith("< ") or l.startswith("> ") or l.startswith("-") or l.startswith("+")
    ]
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

# Errors that are worth retrying quickly within the same diff call — they often
# clear within 2-5s (a concurrent render populates the cache, the proxy/Redis
# hiccup passes, or the busy repo-server frees up).
# - i/o timeout / connection refused: spoke Redis via argocd-agent-redis-proxy
# - context deadline exceeded: short deadline expired, usually recovers fast
# - error logging into OCI registry / failed running helm: repo-server cache
#   miss + helm registry login; a retry may hit a now-cached manifest
# - GetManifests ... 502 / 503 / 504: repo-server briefly overloaded behind ingress
_RETRYABLE_DIFF_ERRORS = (
    "i/o timeout",
    "connection refused",
    "context deadline exceeded",
    "error logging into OCI registry",
    "failed running helm",
    "GetManifests failed with status code 502",
    "GetManifests failed with status code 503",
    "GetManifests failed with status code 504",
    # Burst-transient failures during a mass version bump: the repo-server is
    # busy doing parallel OCI cache-miss renders and briefly returns 5xx, or the
    # request to the spoke agent through the redis/resource proxy times out under
    # load. These clear once the chart cache warms and the load subsides, so
    # retry them in-process with backoff instead of waiting a whole loop.
    "GetManifests failed with status code 5",
    "code = Unknown desc = POST",
    "status code 502",
    "status code 503",
    "status code 504",
    "error getting cached app managed resources",
    "the server is not currently accepting requests",
    "code = Canceled",
    "context canceled",
)

# Auth-failure patterns that should trigger an ArgoCD re-login.
_AUTH_ERROR_PATTERNS = (
    "invalid session",
    "token has expired",
    "token is expired",
    "unauthenticated",
    "unauthorized",
)

# ── Diff error classification ─────────────────────────────────────────
# Each tuple is (substring patterns, reason code). Checked in order; the first
# matching group wins. All of these are INDETERMINATE: the diff could not be
# computed, so the result is unknown (NOT "no changes"). The previous code
# silently mapped several of these to no-diff, which hid real changes from
# reviewers — that masking is removed here.
_DIFF_ERROR_RULES = (
    # OCI chart version does not exist in the registry (someone bumped appspace.version
    # to a version that was never published). Surface a clear, actionable message.
    (("not found in", "chart not found", "unexpected status code: 404",
      "does not exist in oci registry"), "oci_not_found"),
    # OCI Helm chart could not be pulled/rendered (repo-server cache miss +
    # `helm registry login` failure, e.g. 401 Bad Credentials on the registry).
    (("error logging into OCI registry", "failed running helm",
      "helm registry login"), "oci_login"),
    # Spoke Redis unreachable through argocd-agent-redis-proxy while reading the
    # app's cached managed (live) resources.
    (("error getting cached app managed resources",), "managed_resources"),
    # Manifest generation failed at the repo-server / ingress (often the visible
    # symptom of an OCI render that exceeded the request deadline).
    (("GetManifests failed with status code 5", "code = Unknown desc = POST",
      "status code 502", "status code 503", "status code 504"), "manifests_5xx"),
    # ArgoCD server / app-controller temporarily unavailable or restarting.
    (("the server is not currently accepting requests",
      "error getting server version"), "server_unavailable"),
    # Request cancelled or deadline hit (transient).
    (("context canceled", "code = Canceled",
      "context deadline exceeded"), "canceled"),
    # diff-preview account lacks RBAC for this app (config issue, not no-diff).
    (("rpc error: code = PermissionDenied", "permission denied"), "permission"),
)

# Subset of managed_resources that is specifically a Redis/proxy timeout rather
# than a plain "resources not cached" state. Kept separate for clearer metrics.
_REDIS_TIMEOUT_HINTS = ("i/o timeout", "connection refused")

# Operator-friendly one-liners shown in the PR comment for each indeterminate
# reason. Keep these short — the full ArgoCD stderr is in the pod logs (DEBUG).
_REASON_HINTS = {
    "oci_not_found":      "Chart version not found in OCI registry — check that the version exists",
    "oci_login":          "Helm OCI registry login failed on the repo-server",
    "redis_timeout":      "spoke Redis timed out via argocd-agent proxy",
    "managed_no_cache":   "live state not cached for this agent-managed app",
    "manifests_5xx":      "repo-server returned 5xx while generating manifests",
    "server_unavailable": "ArgoCD server temporarily unavailable",
    "canceled":           "request cancelled / deadline exceeded",
    "permission":         "diff-preview account lacks RBAC for this app",
    "auth":               "ArgoCD session expired (re-login triggered)",
    "timeout":            "diff command timed out after 60s",
    "retry_exhausted":    "still failing after retry",
    "legacy":             "diff could not be computed",
}


def classify_diff_error(stderr_text: str) -> tuple:
    """Map an `argocd app diff` failure (exit >= 2, or exit 1 with empty stdout)
    to (outcome, reason, detail).

    Pure function — no subprocess, no globals — so it is unit-testable without
    ArgoCD. Every recognised failure is INDETERMINATE (diff not computable);
    only genuinely unknown output is OUT_ERROR.
    """
    text = stderr_text or "unknown error"
    lower = text.lower()

    # Auth first: an expired/invalid session also needs a background re-login.
    if any(p in lower for p in _AUTH_ERROR_PATTERNS):
        return OUT_INDETERMINATE, "auth", text

    for patterns, reason in _DIFF_ERROR_RULES:
        if any(p in text for p in patterns):
            if reason == "managed_resources":
                # Distinguish a Redis/proxy timeout from a plain uncached state.
                if any(h in text for h in _REDIS_TIMEOUT_HINTS):
                    return OUT_INDETERMINATE, "redis_timeout", text
                return OUT_INDETERMINATE, "managed_no_cache", text
            return OUT_INDETERMINATE, reason, text

    return OUT_ERROR, "unknown", text

def _async_relogin():
    """Re-login to ArgoCD in a background thread when auth errors are detected.

    Called when argocd app diff returns exit 1 with an auth failure message.
    The JWT token may have expired (default ArgoCD expiry: 24h). Re-logging
    in the background means the NEXT diff call will pick up a fresh session
    without blocking the current iteration.
    """
    try:
        argocd_login()
        log(f"[auth] background re-login succeeded as {ARGOCD_USER}")
    except Exception as e:
        print(f"  [auth] background re-login failed: {e}", flush=True)


def _bb_fetch_file_at_sha(filepath, sha):
    """Fetch raw file content from acme-config-dev at a specific commit SHA.

    Returns the decoded string content, or None on any error (missing file,
    network error, auth failure). Used to read the new chart version from a
    PR config file before running argocd app diff.

    Uses a direct urllib call instead of bb()/http() because those helpers
    always call json.loads() on the response, which fails for YAML/text files.
    """
    import base64
    url = (f"https://api.bitbucket.org/2.0/repositories/"
           f"{BB_WORKSPACE}/{BB_REPO}/src/{sha}/{filepath}")
    creds = base64.b64encode(f"{BB_USER}:{BB_TOKEN}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {creds}"})
    try:
        with urllib.request.urlopen(req, context=_ssl, timeout=20) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception:
        return None


_yaml_version_re = re.compile(r"^\s{0,8}version:\s*([^\s#]+)", re.MULTILINE)


# ── Helm-template local diff ─────────────────────────────────────────────────
# Credentials and config read from environment (added to pod via ExternalSecret).
HELM_BIN        = os.environ.get("HELM_BIN", "/usr/local/bin/helm")
OCI_USER        = os.environ.get("OCI_USER", "acme-repo")
OCI_PASS        = os.environ.get("OCI_PASS", "")
HELM_CACHE_DIR  = os.environ.get("HELM_CACHE_DIR", "/tmp/acme-helm-cache")

# Registries that have been successfully authenticated this pod lifetime.
_helm_logged_in: set = set()
_helm_login_lock     = threading.Lock()
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
    """Login to an OCI registry once per pod lifetime. Thread-safe."""
    with _helm_login_lock:
        if registry in _helm_logged_in:
            return True
        if not OCI_PASS:
            return False
        r = subprocess.run(
            [HELM_BIN, "registry", "login", registry,
             "--username", OCI_USER, "--password-stdin"],
            input=OCI_PASS, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            _helm_logged_in.add(registry)
            log(f"Helm OCI login OK: {registry}")
            return True
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
        with _helm_cache_lock:
            _helm_chart_cache[key] = chart_dir
        return chart_dir

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

        # Pull into a temp dir and atomically rename to avoid partial state
        import tempfile
        os.makedirs(HELM_CACHE_DIR, exist_ok=True)
        tmp_dir = tempfile.mkdtemp(dir=HELM_CACHE_DIR, prefix=f"{chart}-{version}-")
        try:
            r = subprocess.run(
                [HELM_BIN, "pull", f"oci://{registry}/{chart}",
                 "--version", version, "--untar", "-d", tmp_dir],
                capture_output=True, text=True, timeout=120)

            if r.returncode != 0:
                err = (r.stderr or r.stdout or "").lower()
                if any(p in err for p in ("not found", "404", "does not exist",
                                           "no such file", "unexpected status code: 404")):
                    raise OciChartNotFound(
                        f"Chart {chart}:{version} not found in {registry}. "
                        f"Check that the version exists in the OCI registry.")
                log(f"helm pull failed for {chart}:{version}: {r.stderr[:200]}", "WARNING")
                return None

            # Move from tmp to final location atomically
            os.makedirs(os.path.dirname(chart_dir), exist_ok=True)
            if not os.path.exists(chart_dir):
                os.rename(tmp_dir, chart_dir)
            else:
                # Another thread beat us to it; remove our tmp copy
                import shutil
                shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            import shutil
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


def _fetch_value_files(value_files: list, sha: str) -> dict:
    """Fetch all helm value files from Bitbucket at a specific commit sha.

    value_files is a list of paths like '$config/gcp/dev/.../config.yaml'.
    The '$config/' prefix is the git source alias; we strip it to get the
    actual path in acme-config-dev.

    Returns {original_path: file_content} for files that were fetched successfully.
    Files that return 404 (e.g. new clusters not yet in main) are silently skipped.
    """
    result = {}
    for vf in value_files:
        clean = vf.replace("$config/", "").lstrip("/")
        content = _bb_fetch_file_at_sha(clean, sha)
        if content:
            result[vf] = content
    return result


def _helm_template(chart_path: str, release: str, namespace: str,
                   value_files_content: dict) -> tuple:
    """Run `helm template` locally with the given value files.

    Returns (manifests_yaml: str, error: str|None).
    value_files_content: {path_label: yaml_content} dict (order matters for overrides).
    """
    import tempfile
    with tempfile.TemporaryDirectory(prefix="acme-diff-helm-") as tmpdir:
        value_args = []
        for idx, (label, content) in enumerate(value_files_content.items()):
            fname = os.path.join(tmpdir, f"values_{idx:03d}.yaml")
            with open(fname, "w") as f:
                f.write(content)
            value_args += ["-f", fname]

        cmd = ([HELM_BIN, "template", release, chart_path,
                "--namespace", namespace or release,
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
        content = _bb_fetch_file_at_sha(filepath, pr_sha)
        if not content:
            continue
        m = _yaml_version_re.search(content)
        if m:
            new_rev = m.group(1).strip("'\"")
            if new_rev and new_rev != current_rev:
                debug(f"chart version override: {current_rev} -> {new_rev}",
                      app=app, file=filepath)
                return new_rev
    return None


def _manifests_cmd(app, sha, chart_revision=None):
    """Build `argocd app manifests` command for a given git sha (and optional chart version)."""
    cmd = [ARGOCD_BIN, "app", "manifests", app,
           "--revisions", sha, "--source-positions", "1"]
    if chart_revision:
        cmd += ["--revisions", chart_revision, "--source-positions", "2"]
    return cmd + _auth_flags()


def _fetch_manifests(app, sha, chart_revision=None):
    """Return (yaml_text, error_str). Raises SubprocessTimeoutExpired on timeout."""
    cmd = _manifests_cmd(app, sha, chart_revision)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=DIFF_TIMEOUT)
    if r.returncode != 0:
        return None, (r.stderr or r.stdout or "manifests command failed")
    return r.stdout, None


def _parse_manifest_resources(yaml_text):
    """Split a multi-document YAML string into a dict keyed by (group/Kind, ns/name).

    Each value is the normalized document text (stripped, consistent trailing newline).
    Documents without kind/metadata are skipped.
    """
    import re as _re
    resources = {}
    for doc in _re.split(r'\n---\s*\n|^---\s*\n', yaml_text, flags=_re.MULTILINE):
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
    import difflib
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
    """Diff PR vs main manifests locally using helm template (fast, no agent access).

    Falls back to argocd app manifests when helm is not configured (no OCI_PASS).

    Returns (diff_text_or_None, timed_out_bool, error_str_or_None).
    OciChartNotFound is surfaced as a clear error string so it shows in the PR comment.
    """
    chart_name  = _app_chart_map.get(app)
    main_rev    = _app_chart_revision_map.get(app)
    registry    = _app_chart_registry_map.get(app, "")
    value_files = _app_value_files_map.get(app, [])
    namespace   = _app_namespace_map.get(app, "")
    release     = app.split("/")[-1]   # strip "namespace/" prefix if present

    pr_rev = chart_revision or main_rev

    # ── Helm-template path (fast - no agent, purely local rendering) ──────────
    if (HELM_BIN and OCI_PASS and chart_name and main_rev
            and value_files and registry):
        try:
            # Download both chart versions (cached after first pull)
            pr_chart   = _ensure_chart(registry, chart_name, pr_rev)
            main_chart = _ensure_chart(registry, chart_name, main_rev)
        except OciChartNotFound as e:
            return None, False, str(e)
        except subprocess.TimeoutExpired:
            return None, True, None

        if pr_chart and main_chart:
            try:
                pr_vals   = _fetch_value_files(value_files, pr_sha)
                main_vals = _fetch_value_files(value_files, main_sha)

                pr_yaml,   pr_err   = _helm_template(pr_chart,   release, namespace, pr_vals)
                main_yaml, main_err = _helm_template(main_chart, release, namespace, main_vals)
            except subprocess.TimeoutExpired:
                return None, True, None

            if pr_err:
                return None, False, pr_err
            if main_err:
                return None, False, main_err

            return _diff_manifests(main_yaml, pr_yaml), False, None
        # If chart pull failed for non-OCI-not-found reason, fall through to argocd

    # ── Fallback: argocd app manifests (slower, requires ArgoCD API) ──────────
    try:
        pr_yaml,   pr_err   = _fetch_manifests(app, pr_sha,   chart_revision)
        main_yaml, main_err = _fetch_manifests(app, main_sha, None)
    except subprocess.TimeoutExpired:
        return None, True, None

    if pr_err:
        return None, False, pr_err
    if main_err:
        return None, False, main_err

    return _diff_manifests(main_yaml, pr_yaml), False, None


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

    Uses `argocd app manifests` (repo-server rendering only) to get the rendered
    YAML for both the PR revision and the current main revision, then diffs them
    locally in Python. This avoids fetching live resources from spoke agents so:
    - Each diff takes 2-15s instead of 20-360s
    - No per-agent serialization (cap) is needed
    - The whole fleet can be diffed in parallel

    Retry policy: OCI cache-miss renders on the first pass are retried with
    backoff. Persistent failures (OCI not found, auth) surface as INDETERMINATE.
    """
    last_detail = ""
    last_attempt = DIFF_RETRIES - 1
    for attempt in range(DIFF_RETRIES):
        diff_text, timed_out, err = _run_one_diff(
            app, pr_sha, main_sha, chart_revision=chart_revision)

        if timed_out:
            debug(f"manifests timed out after {DIFF_TIMEOUT}s", app=app, attempt=attempt + 1)
            if attempt < last_attempt:
                time.sleep(_diff_backoff(attempt))
                continue
            return _indeterminate("timeout", f"manifests timed out after {DIFF_TIMEOUT}s")

        if err:
            last_detail = err
            outcome, reason, detail = classify_diff_error(err)
            debug(f"manifests error: {reason}", app=app, attempt=attempt + 1, stderr=detail[:800])
            if reason == "auth":
                threading.Thread(target=_async_relogin, daemon=True).start()
            # OCI not-found is permanent: the version does not exist. Never retry.
            if reason == "oci_not_found":
                return _indeterminate(reason, detail)
            if attempt < last_attempt and any(p in err for p in _RETRYABLE_DIFF_ERRORS):
                delay = _diff_backoff(attempt)
                print(f"    [{app}] transient error (attempt {attempt + 1}/{DIFF_RETRIES}), "
                      f"retrying in {delay:.0f}s: {err[:80]}", flush=True)
                time.sleep(delay)
                continue
            if outcome == OUT_INDETERMINATE:
                return _indeterminate(reason, detail)
            return DiffResult("", False, detail[:400], OUT_ERROR, reason)

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
    return _indeterminate("retry_exhausted", last_detail or "unknown error")


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

def get_open_prs():
    url = (f"https://api.bitbucket.org/2.0/repositories/{BB_WORKSPACE}/{BB_REPO}"
           "/pullrequests?state=OPEN&pagelen=50")
    prs, nxt = [], url
    while nxt:
        data = http("GET", nxt, auth=(BB_USER, BB_TOKEN))
        prs += data.get("values", [])
        nxt  = data.get("next")
    return prs

def get_pr_changed_files(pr_id):
    files, path = [], f"pullrequests/{pr_id}/diffstat?pagelen=100"
    while path:
        data = bb("GET", path)
        for item in data.get("values", []):
            p = (item.get("new") or item.get("old") or {}).get("path", "")
            if p:
                files.append(p)
        nxt  = data.get("next", "")
        path = nxt.replace(
            f"https://api.bitbucket.org/2.0/repositories/{BB_WORKSPACE}/{BB_REPO}/", "")
    return files

def find_existing_comment(pr_id):
    """Search all comment pages for our marker.

    Returns (comment_id, sha_8, raw_text).
    sha_8 is 8-char hex or '' if not found in comment.
    Paginates through all pages so >100-comment PRs are handled correctly.
    """
    nxt = f"pullrequests/{pr_id}/comments?pagelen=100"
    while nxt:
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
        nxt = next_url.replace(
            f"https://api.bitbucket.org/2.0/repositories/{BB_WORKSPACE}/{BB_REPO}/", ""
        ) if next_url else ""
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
        # Derive correct status from comment content
        if "Error running diff" in comment_raw or "\u274c" in comment_raw:
            state, desc = "FAILED", "Diff failed - check PR comment"
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
LARGE_PR_APP_THRESHOLD   = 5       # changed apps above this -> large mode
LARGE_PR_DIFF_BYTES      = 40_000  # total diff bytes above this -> large mode

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
            "- Helm shows changes as '-' (old) and '+' (new) — this is normal for updates.\n"
            "- VERSION COMPARISON: only report a downgrade when the full version string actually "
            "decreases (e.g. 1.93.1 → 1.93.0 is a downgrade; 1.93.1-rc1 → 1.93.1-rc2 is NOT).\n"
            "- Skip checksum-only changes and annotation noise (argocd.argoproj.io/tracking-id, "
            "helm.sh/chart, kubectl.kubernetes.io/last-applied-configuration).\n"
            "- For new Deployments/StatefulSets being added to the cluster, say 'new service'.\n"
            "- For removed ones say 'service removed'.\n\n"
            "Respond in EXACTLY this format (use the emoji headers):\n\n"
            f"**{len(changed)} app(s) updated · {total_resources} resource(s) changed**\n\n"
            "1. 🌍 **AFFECTED ENVIRONMENTS:**\n"
            "   • List each affected cluster/environment (e.g. cl-dev11-a, pv-dev-05-a)\n"
            "   • Total count\n\n"
            "2. 📊 **SUMMARY:**\n"
            "   • One sentence overview of the change type\n"
            "   • Flat bullet list of key service changes: "
            "`env/service`: `old-version` → `new-version`\n"
            "   • Omit checksum-only or annotation-only changes\n\n"
            "3. ⚠️ **CRITICAL CHANGES:**\n"
            "   • Version downgrades — only report when full version string decreases\n"
            "   • Replicas dropping to 0 (potential service disruption)\n"
            "   • Services being removed (resource deletion)\n"
            "   • Liveness/readiness probe removed\n"
            "   • If none: say 'No critical changes detected'\n\n"
            "Max 300 words. Be concise. Prioritise critical changes for human review.\n\n"
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
              f"maxTokens={1200}")
        import time as _time; _t0 = _time.monotonic()
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


def format_comment(pr_sha, app_results, skipped_apps=None):
    skipped_apps  = skipped_apps or []
    results       = {app: _result(v) for app, v in app_results.items()}
    any_change    = False
    any_error     = False
    any_unknown   = False   # diff could not be computed (indeterminate)
    total_changed = 0
    unknown_apps  = []

    # Calculate changeset size to pick display mode.
    changed_apps      = [(app, r.text) for app, r in results.items() if r.outcome == OUT_DIFF]
    total_diff_bytes  = sum(len(d) for _, d in changed_apps)
    is_large          = (
        len(changed_apps) > LARGE_PR_APP_THRESHOLD
        or total_diff_bytes > LARGE_PR_DIFF_BYTES
    )

    # AI summary (non-blocking — None means skip the block).
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
        if is_large:
            lines += [
                "> \U0001f50d Full diffs collapsed below \u2014 expand per app to review.",
                "",
            ]

    lines += ["---", ""]

    # ── Per-app sections ─────────────────────────────────────────────
    for app, r in results.items():
        diff_text, has_diff, error = r.text, (r.outcome == OUT_DIFF), r.error
        if r.outcome == OUT_ERROR:
            any_error = True
            lines += [f"\u274c **`{app}`** \u2014 error: {(error or '')[:200]}", ""]

        elif r.outcome == OUT_INDETERMINATE:
            # The diff could NOT be computed — do not imply "no changes".
            any_unknown = True
            unknown_apps.append(app)
            hint = _REASON_HINTS.get(r.reason, "diff could not be computed")
            lines += [
                f"\u2754 **`{app}`** \u2014 diff unavailable ({hint})",
                "",
            ]

        elif has_diff:
            any_change = True
            sections   = parse_diff_sections(diff_text)
            n          = len(sections) if sections else 1
            total_changed += n

            if is_large:
                # Collapsed: operators can expand if they need the raw diff.
                lines += [
                    "<details>",
                    f"<summary>\u26a0\ufe0f <strong><code>{app}</code></strong>"
                    f" \u2014 {n} resource(s) changed</summary>",
                    "",
                ]
                if sections:
                    for hdr, body in sections[:MAX_RESOURCES_FULL]:
                        truncated = body[:MAX_DIFF_CHARS]
                        if len(body) > MAX_DIFF_CHARS:
                            truncated += "\n... (truncated)"
                        lines += [
                            f"**`{hdr}`**", "",
                            "```diff", truncated.rstrip(), "```", "",
                        ]
                    if len(sections) > MAX_RESOURCES_FULL:
                        lines += [
                            f"*\u2026 and {len(sections) - MAX_RESOURCES_FULL} more resource(s)*", ""]
                else:
                    raw_block = diff_text[:MAX_DIFF_CHARS * 2]
                    if len(diff_text) > MAX_DIFF_CHARS * 2:
                        raw_block += "\n... (truncated)"
                    lines += ["```diff", raw_block.rstrip(), "```", ""]
                lines += ["</details>", ""]

            else:
                # Inline: full diff visible for small changesets.
                lines += [f"\u26a0\ufe0f **`{app}`** \u2014 {n} resource(s) changed", ""]
                if sections:
                    for hdr, body in sections[:MAX_RESOURCES_FULL]:
                        truncated = body[:MAX_DIFF_CHARS]
                        if len(body) > MAX_DIFF_CHARS:
                            truncated += "\n... (truncated)"
                        lines += [
                            f"**`{hdr}`**", "",
                            "```diff", truncated.rstrip(), "```", "",
                        ]
                    if len(sections) > MAX_RESOURCES_FULL:
                        lines += [
                            f"*\u2026 and {len(sections) - MAX_RESOURCES_FULL} more resource(s)*", ""]
                else:
                    raw_block = diff_text[:MAX_DIFF_CHARS * 2]
                    if len(diff_text) > MAX_DIFF_CHARS * 2:
                        raw_block += "\n... (truncated)"
                    lines += ["```diff", raw_block.rstrip(), "```", ""]

        else:
            lines += [f"\u2705 **`{app}`** \u2014 no manifest changes", ""]

    # ── Skipped apps note ────────────────────────────────────────────
    if skipped_apps:
        lines += [
            f"*{len(skipped_apps)} app(s) skipped (cap {MAX_APPS_PER_RUN}): "
            f"{', '.join(skipped_apps[:5])}{'...' if len(skipped_apps) > 5 else ''}*", ""]

    # ── Footer ───────────────────────────────────────────────────────
    # Priority: hard error > real changes > indeterminate > clean. The
    # indeterminate ("Diff incomplete") wording is also what the cross-iteration
    # retry in process_pr looks for, so these PRs are re-evaluated next loop.
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

    lines += [
        "---",
        f"**Status:** {status}",
        f"*{_ts()} \u2014 {COMMENT_MARKER}*",
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
        # If the existing comment was not a clean result (hard error, or a diff
        # that could not be computed) re-run now — the OCI/Redis/agent path may
        # have recovered since. "Diff incomplete" is the indeterminate footer
        # marker; the ❌ checks catch hard errors and legacy comments.
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
            existing_id = existing_id  # keep existing_id so we update (not create) the comment
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
                f"*{_ts()} \u2014 {COMMENT_MARKER}*"
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

        # For each affected app, detect whether the PR changes the OCI chart
        # targetRevision (appspace.version bump). If so, we pass a second
        # --revisions / --source-positions override so argocd renders the new
        # chart, making the diff show the actual image changes that will happen.
        pr_chart_revisions = {}
        for app in affected:
            new_rev = _pr_chart_revision(app, changed, pr_sha)
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
            # No per-agent cap needed: manifests-based diff uses repo-server only,
            # no live resource fetching from agents. Run all apps in parallel freely.
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

        # Helm chart pre-warm: when using helm-template diff, pull all needed chart
        # versions (both PR version and current main version) before diffing starts.
        # This avoids the race condition where multiple parallel diffs try to pull
        # the same chart into the same directory at the same time.
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
            if unique_chart_pulls:
                print(f"    Helm pre-warm: pulling {len(unique_chart_pulls)} "
                      f"chart version(s) before diff fan-out...", flush=True)
                with ThreadPoolExecutor(max_workers=max(1, min(WARM_WORKERS, len(unique_chart_pulls)))) as ex:
                    futures = [ex.submit(_ensure_chart, reg, chart, ver)
                               for reg, chart, ver in unique_chart_pulls]
                    for fut in as_completed(futures):
                        try:
                            fut.result()
                        except OciChartNotFound as e:
                            log(str(e), "WARNING")
                        except Exception:
                            pass

        # Cache-warm diff phase: one representative per chart to warm repo-server cache.
        warm_apps, rest_apps = _select_warm_apps(affected)
        if warm_apps:
            print(f"    Cache-warm: {len(warm_apps)} chart representative(s) "
                  f"before fanning out {len(rest_apps)} more", flush=True)
            process_batch(warm_apps, WARM_WORKERS)
        process_batch(rest_apps, DIFF_WORKERS)

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

        # Count changed resources for build status description
        sections_total = sum(
            max(len(parse_diff_sections(r.text)), 1)
            for r in app_results.values() if r.outcome == OUT_DIFF)
        n_unknown = outcome_counts[OUT_INDETERMINATE]
        if any_hard_error:
            post_build_status(pr_sha, "FAILED", "Diff failed - check PR comment")
        elif sections_total > 0:
            extra = f" ({n_unknown} unavailable)" if any_unknown else ""
            post_build_status(pr_sha, "SUCCESSFUL",
                f"{sections_total} resource(s) will change - review comment{extra}")
        elif any_unknown:
            # Non-blocking, but clearly NOT a clean pass: the diff is unknown.
            post_build_status(pr_sha, "SUCCESSFUL",
                f"Diff unavailable for {n_unknown} app(s) - review comment")
        else:
            post_build_status(pr_sha, "SUCCESSFUL", "No manifest changes")

        # Only mark as seen when the run was fully clean (no hard error, no
        # indeterminate). If any app failed or could not be evaluated we leave
        # _seen empty so the next iteration retries it — important so that once
        # the OCI credential / Redis path recovers the diff is recomputed and the
        # comment stops saying "unavailable". any_error keeps the historical name
        # the tests look for; it now also covers indeterminate results.
        any_error = any_hard_error or any_unknown
        if not any_error:
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
            f"*{_ts()} \u2014 {COMMENT_MARKER}*"
        )
        try:
            upsert_comment(pr_id, err_body, existing_id)
        except Exception:
            pass

# ── Main iteration (one poll cycle) ───────────────────────────────────
def main_iteration():
    """Run one complete poll cycle: discover apps, get open PRs, process each."""
    log("ACME diff preview iteration starting")

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
    for stale_id in list(_seen.keys()):
        if stale_id not in open_ids:
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
    if totals:
        rollup = ", ".join(f"{k}={v}" for k, v in sorted(totals.items()))
        unhealthy = totals.get(OUT_INDETERMINATE, 0) + totals.get(OUT_ERROR, 0)
        log(f"Iteration done — diff outcomes: {rollup}"
            + (f" | {unhealthy} app diff(s) could not be computed" if unhealthy else ""),
            severity=("WARNING" if unhealthy else "INFO"),
            **{f"n_{k}": v for k, v in totals.items()})
    else:
        log("Iteration done")

# ── Main entry point (long-running Deployment mode) ───────────────────
def main():
    """Start health server, login to ArgoCD, then run poll loop until SIGTERM."""
    global _last_ok
    log("acme-diff-preview starting (Deployment mode, manifest-based diff)",
        argocd_server=ARGOCD_SERVER, argocd_user=ARGOCD_USER,
        bb_repo=BB_REPO, diff_workers=DIFF_WORKERS, pr_workers=MAX_PR_WORKERS,
        max_apps_per_run=MAX_APPS_PER_RUN, diff_timeout=DIFF_TIMEOUT,
        diff_retries=DIFF_RETRIES, warm_workers=WARM_WORKERS,
        log_level=LOG_LEVEL, vertex_model=VERTEX_MODEL)
    _start_health_server()

    # Initial login — raises on failure so the container restarts immediately.
    argocd_login()
    log("ArgoCD login OK")

    while not _shutdown:
        try:
            main_iteration()
            _last_ok = time.monotonic()
        except Exception as e:
            log(f"Unhandled error in main loop: {e}", "ERROR")
        if not _shutdown:
            # Webhook wakes the loop instantly (<1s). The 60s timeout is
            # just a safety net in case webhook delivery is ever unavailable.
            _wake.wait(timeout=60)
            _wake.clear()

    log("Shutdown complete", "WARNING")

if __name__ == "__main__":
    main()