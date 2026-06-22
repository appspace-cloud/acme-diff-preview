#!/usr/bin/env python3
"""ArgoCD Diff Preview - dynamic discovery, robust error handling, SHA dedup.

All apps are multi-source: source-1 = acme-config-dev, source-2 = Helm OCI.
--source-positions 1 is correct for all apps in this setup.

Error handling:
- argocd app list failure: FAILED on all open main-targeting PRs, clean exit
- Bitbucket API 429/503/network: retry with exponential backoff (3 attempts)
- diff timeout (120s): caught per-app, reported as error in comment
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
import json, os, posixpath, re, signal, ssl, sys, subprocess, time, threading, urllib.error, urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

BB_WORKSPACE       = "appspace-cloud"
BB_REPO            = "acme-config-dev"
BB_USER            = os.environ["BB_USER"]
BB_TOKEN           = os.environ["BB_TOKEN"]
ARGOCD_SERVER      = "argocd.appspace.com"
ARGOCD_BIN         = os.environ.get("ARGOCD_BIN", "/usr/local/bin/argocd")
ARGOCD_ADMIN_PASS  = os.environ["ARGOCD_ADMIN_PASS"]
COMMENT_MARKER     = "argocd-diff-preview"
BUILD_KEY          = "argocd-diff-preview"
MAX_RESOURCES_FULL = 5       # resources shown with full diff block
MAX_DIFF_CHARS     = 2000    # chars per resource diff block
MAX_APPS_PER_RUN   = 30      # safety cap
DIFF_WORKERS       = 4       # parallel argocd app diff calls
MAX_COMMENT_BYTES  = 245_000 # Bitbucket ~256KB limit; leave headroom

# In-memory SHA dedup: avoids reprocessing same PR SHA within this pod run
_seen: dict    = {}
_shutdown: bool = False   # set True by SIGTERM handler
_last_ok: float = time.monotonic()  # updated after each successful iteration
_ready: bool    = False   # set True after first successful argocd_login()
_wake           = threading.Event()  # set by POST /diff-preview/webhook
_seen_lock      = threading.Lock()   # guards _seen for concurrent PR processing

# Max parallel PR processing workers. Each worker runs 4 argocd app diff
# subprocesses internally, so the effective subprocess pool is
# MAX_PR_WORKERS × DIFF_WORKERS.
MAX_PR_WORKERS  = 3

# Path map TTL cache: argocd app list is ~350ms and downloads ~50KB.
# The map only changes when apps are added/removed (rare).
# Cache for 5 min so idle iterations cost ~1ms instead of ~350ms.
_path_map_cache: dict  = {}
_path_map_ts:    float = 0.0
_path_map_count: int   = 0    # extra invalidation: rebuild if app count changes
PATH_MAP_TTL            = 300   # seconds

# GCE access token cache: token valid ~3600s, no reason to refetch each PR.
_gcp_token:     str   = ""
_gcp_token_exp: float = 0.0

def log(msg: str, severity: str = "INFO", **labels) -> None:
    """Emit a structured JSON log line in GCP Cloud Logging format."""
    entry: dict = {
        "severity":  severity,
        "message":   msg,
        "time":      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "component": "argocd-diff-preview",
    }
    if labels:
        entry["labels"] = {k: str(v) for k, v in labels.items()}
    print(json.dumps(entry), flush=True)

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
        if self.path == "/healthz":
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
            length = int(self.headers.get("Content-Length", 0))
            if length:
                self.rfile.read(length)
            event_key = self.headers.get("X-Event-Key", "")
            if event_key.startswith("pullrequest:"):
                log(f"Webhook received: {event_key} — waking loop")
                _wake.set()
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

def _start_health_server(port: int = 8080) -> HTTPServer:
    """Start the health server in a daemon thread and handle webhook POSTs."""
    server = HTTPServer(("", port), _HealthHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True, name="health-server")
    t.start()
    log(f"Health server listening on :{port} (/healthz /readyz /diff-preview/webhook)")
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
    global _path_map_cache, _path_map_ts, _path_map_count
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
    for app in apps:
        name = app["metadata"]["name"]
        ns   = app["metadata"].get("namespace", "")
        # Use namespace/name so argocd CLI resolves the app in the correct namespace.
        # Apps in managed-mode agent namespaces (e.g. gcp-qa-pv-ap1-a/pv-qa88-a-ms)
        # are no longer in the default 'argocd' namespace after the COPS-2474 migration.
        full_name = f"{ns}/{name}" if ns and ns != "argocd" else name
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
    _path_map_cache = path_map
    _path_map_ts    = time.monotonic()
    _path_map_count = len(path_map)
    return path_map

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

# ── ArgoCD diff ───────────────────────────────────────────────────────
def argocd_login():
    global _ready, _path_map_ts
    r = subprocess.run(
        [ARGOCD_BIN, "login", ARGOCD_SERVER,
         "--username", "admin", "--password", ARGOCD_ADMIN_PASS,
         "--grpc-web", "--insecure"],
        capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"argocd login failed: {r.stderr[:200]}")
    _path_map_ts    = 0.0  # Invalidate path map cache on re-login.
    _path_map_count = 0
    _ready = True
    print("  ArgoCD auth: logged in as admin")

# Resource patterns filtered from ALL diff output and AI analysis.
# micro-versions-info is an auto-generated ConfigMap that always changes
# alongside actual image updates — it lists all deployed image versions.
# Showing it adds noise: the real change is visible in the Deployment diff.
# Checksum annotations that cascade from it are also suppressed.
DIFF_IGNORE_RESOURCE_PATTERNS = [
    "micro-versions-info",
]

def _is_checksum_only_section(body: str) -> bool:
    """True when every changed line in the diff body is a checksum annotation.

    These sections appear in Deployments as a cascading side-effect whenever
    a referenced ConfigMap changes. They carry no operator-useful information.
    """
    changed = [
        l for l in body.splitlines()
        if l.startswith("< ") or l.startswith("> ")
    ]
    return bool(changed) and all("checksum/" in l for l in changed)

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

def argocd_diff(app, pr_sha):
    """Returns (diff_text, has_diff, error_msg). Never raises.

    Exit codes: 0=no diff, 1=diff found (or auth error with empty stdout),
    2+=error. Timeout raises TimeoutExpired, caught here.
    """
    try:
        r = subprocess.run(
            [ARGOCD_BIN, "app", "diff", app,
             "--revisions", pr_sha, "--source-positions", "1"] + _auth_flags(),
            capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return "", False, "diff timed out after 120s"
    if r.returncode == 0:
        return "", False, None
    if r.returncode == 1:
        if not r.stdout and r.stderr:
            # Exit 1 with only stderr = auth/rendering error, not a diff
            return "", False, r.stderr[:400]
        # Apply section filter: remove micro-versions-info and checksum-only
        # noise before the diff reaches both the comment and the AI analysis.
        raw = r.stdout
        filtered_sections = _filter_diff_sections(parse_diff_sections(raw))
        if not filtered_sections:
            # All sections were noise — treat as no meaningful diff
            return "", False, None
        # Reconstruct diff text from remaining sections
        diff_text = "\n".join(
            f"===== {hdr} =====\n{body}"
            for hdr, body in filtered_sections
        )
        return diff_text, True, None
    err_out = (r.stderr or r.stdout or "unknown error")
    # Apps on skip-reconcile clusters (argocd-agent managed mode) route through
    # the resource proxy, which does not expose full K8s API. These errors are
    # expected and should be treated as no-diff, not fatal errors.
    MANAGED_MODE_ERRORS = [
        "error getting cached app managed resources",
        "error getting server version",
        "the server is not currently accepting requests",
        "rpc error: code = PermissionDenied",
        "permission denied",
        "context canceled",
        "code = Canceled",
        "502",
        "code = Unknown desc = POST",
    ]
    if any(e in err_out for e in MANAGED_MODE_ERRORS):
        return "", False, None
    return "", False, err_out[:400]

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
            "name": "ArgoCD Diff Preview",
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
            if COMMENT_MARKER in raw:
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
        else:
            state, desc = "SUCCESSFUL", "No manifest changes"
        post_build_status(pr_sha, state, desc)
        print(f"    Fixed stuck INPROGRESS for PR #{pr_id} -> {state}")
    except Exception as e:
        print(f"    [fix_stuck_inprogress] PR #{pr_id}: {e}", file=sys.stderr)

# ── Vertex AI (Gemini) summary ─────────────────────────────────────────
# COPS-2496: AI-powered diff summary using Vertex AI Gemini.
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
        changed = {
            app: parse_diff_sections(diff)
            for app, (diff, hd, _) in app_results.items()
            if hd
        }
        errors = {
            app: err
            for app, (_, _, err) in app_results.items()
            if err
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
                f"\n\nFailed apps (diff error): {', '.join(errors.keys())}"
            )

        prompt = (
            "You are a Cloud Platform Engineer reviewing a Kubernetes GitOps diff.\n"
            f"Changeset: {len(changed)} app(s), {total_resources} resource(s).\n\n"
            "Respond in EXACTLY this format — no preamble, no extra commentary:\n\n"
            f"**{len(changed)} app(s) updated \u00b7 {total_resources} resource(s) changed**\n\n"
            "Then a flat bullet list, one bullet per changed resource (skip unchanged):\n"
            "- `<env/service>`: <old-image-tag> \u2192 <new-image-tag>  (or: config annotation updated)\n"
            "  Add \u26a0\ufe0f CRITICAL after the bullet if: image version is lower semver, "
            "date suffix regresses, replica drops to 0, or liveness/readiness probe removed.\n\n"
            "Then a final line (mandatory):\n"
            "\u26a0\ufe0f **Critical:** <short description> OR \u2705 **Critical:** None detected\n\n"
            "Rules: image as `old` \u2192 `new`. Skip checksum-only Deployments (say nothing). "
            "ConfigMap listing multiple images: list each image change. Max 250 words.\n\n"
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
                    "maxOutputTokens": 600,
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
def format_comment(pr_sha, app_results, skipped_apps=None):
    skipped_apps  = skipped_apps or []
    any_change    = False
    any_error     = False
    total_changed = 0

    # Calculate changeset size to pick display mode.
    changed_apps      = [(app, diff) for app, (diff, hd, _) in app_results.items() if hd]
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
        "## \U0001f52d ArgoCD Diff Preview", "",
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
    for app, (diff_text, has_diff, error) in app_results.items():
        if error:
            any_error = True
            lines += [f"\u274c **`{app}`** \u2014 error: {error[:200]}", ""]

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
    if any_error:
        status = "\u274c Error running diff"
    elif any_change:
        status = f"\u26a0\ufe0f {total_changed} resource(s) will change"
    else:
        status = "\u2705 No manifest changes"

    lines += [
        "---",
        f"**Status:** {status}",
        f"*{_ts()} \u2014 {COMMENT_MARKER}*",
    ]
    return "\n".join(lines)

# ── Per-PR processing (isolated) ──────────────────────────────────────
def process_pr(pr, path_map):
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
        # If the existing comment has errors (invalid session, timeout, etc.)
        # re-run the diff now that ArgoCD is available again.
        # A previous pod may have failed mid-flight and left a stale error comment.
        if ("\u274c" in comment_raw and ("Error running diff" in comment_raw or "invalid session" in comment_raw or "error:" in comment_raw)) or "no-diff ERR:" in comment_raw:
            print(f"    Re-running: previous comment for SHA {pr_sha[:8]} had errors, retrying diff")
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
                f"## \U0001f52d ArgoCD Diff Preview\n\n"
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

        app_results = {}
        any_error   = False
        def run_diff(app):
            t0 = time.monotonic()
            result = argocd_diff(app, pr_sha)
            elapsed = round(time.monotonic() - t0, 1)
            return app, result, elapsed
        with ThreadPoolExecutor(max_workers=DIFF_WORKERS) as ex:
            futures = {ex.submit(run_diff, app): app for app in affected}
            for fut in as_completed(futures):
                app, result, elapsed = fut.result()
                app_results[app] = result
                diff_text, has_diff, err = result
                if err:
                    any_error = True
                n_sections = len(parse_diff_sections(diff_text)) if has_diff else 0
                status = (
                    f"diff ({n_sections} resource(s))" if has_diff
                    else ("no-diff" if not err else "error")
                )
                print(f"    {app}: {status} [{elapsed}s]"
                      f"{' | ERR: '+err[:80] if err else ''}")

        body = format_comment(pr_sha, app_results, skipped_apps)
        comment_kb = round(len(body.encode()) / 1024, 1)
        upsert_comment(pr_id, body, existing_id)
        action = "updated" if existing_id else "posted"
        print(f"    Comment {action} on PR #{pr_id} ({comment_kb}KB)")

        # Count changed resources for build status description
        sections_total = sum(
            max(len(parse_diff_sections(dt)), (1 if hd else 0))
            for dt, hd, _ in app_results.values() if hd)
        if any_error:
            post_build_status(pr_sha, "FAILED", "Diff failed - check PR comment")
        elif sections_total > 0:
            post_build_status(pr_sha, "SUCCESSFUL",
                f"{sections_total} resource(s) will change - review comment")
        else:
            post_build_status(pr_sha, "SUCCESSFUL", "No manifest changes")

        with _seen_lock:
            _seen[pr_id] = pr_sha

    except Exception as e:
        print(f"    [ERROR] PR #{pr_id}: {e}", file=sys.stderr)
        try:
            post_build_status(pr_sha, "FAILED", f"Diff error: {str(e)[:200]}")
        except Exception:
            pass
        err_body = (
            f"## \U0001f52d ArgoCD Diff Preview\n\n"
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
    log("ArgoCD diff preview iteration starting")

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
    open_ids = {str(pr["id"]) for pr in prs}
    for stale_id in list(_seen.keys()):
        if stale_id not in open_ids:
            del _seen[stale_id]

    pending = [
        pr for pr in prs
        if pr["source"]["commit"]["hash"] != base_sha
    ]
    if pending:
        with ThreadPoolExecutor(max_workers=MAX_PR_WORKERS) as executor:
            futs = {executor.submit(process_pr, pr, path_map): pr for pr in pending}
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as exc:
                    pr = futs[fut]
                    log(f"Unhandled error processing PR #{pr['id']}: {exc}", "ERROR")

    log("Iteration done")

# ── Main entry point (long-running Deployment mode) ───────────────────
def main():
    """Start health server, login to ArgoCD, then run poll loop until SIGTERM."""
    global _last_ok
    log("argocd-diff-preview starting (Deployment mode)")
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