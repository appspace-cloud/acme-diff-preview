"""Unit tests for diff_preview.py — syntax, key functions, and bug-regression checks."""
import ast
import importlib
import os
import sys

SRC = os.path.join(os.path.dirname(__file__), "..", "src", "diff_preview.py")


def _source():
    with open(SRC) as f:
        return f.read()


def _tree():
    return ast.parse(_source())


def _import_module():
    """Import diff_preview with the env vars it reads at module load.

    The module reads BB_USER / BB_TOKEN / ARGOCD_PASS via os.environ[...] at
    import time, so they must be present before importing for the functional
    tests (classification, naming constants).
    """
    os.environ.setdefault("BB_USER", "test-user")
    os.environ.setdefault("BB_TOKEN", "test-token")
    os.environ.setdefault("ARGOCD_PASS", "test-pass")
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    mod = importlib.import_module("diff_preview")
    return importlib.reload(mod)


# ── Basic checks ────────────────────────────────────────────────────────────

def test_syntax():
    """diff_preview.py must parse without syntax errors."""
    assert _tree() is not None


def test_key_functions_defined():
    """Core functions must be present in the source."""
    func_names = {n.name for n in ast.walk(_tree()) if isinstance(n, ast.FunctionDef)}
    for required in [
        "process_pr", "main_iteration", "argocd_diff",
        "get_open_prs", "upsert_comment", "generate_ai_summary",
        "discover_path_app_map", "find_existing_comment",
    ]:
        assert required in func_names, f"Missing function: {required}"


def test_wake_event_defined():
    """_wake threading.Event must be present for webhook wakeup support."""
    src = _source()
    assert "_wake" in src
    assert "threading.Event" in src


def test_seen_lock_defined():
    """_seen_lock must be present (thread-safety for concurrent PR processing)."""
    assert "_seen_lock" in _source()


def test_no_gcloud_calls():
    """diff_preview.py must not call gcloud (credentials come from ESO)."""
    assert "gcloud" not in _source()


def test_dedicated_argocd_account():
    """Must use dedicated diff-preview local account, not the global admin user."""
    src = _source()
    assert "ARGOCD_PASS" in src
    assert "ARGOCD_USER" in src
    assert '--username", "admin"' not in src
    assert '--username", ARGOCD_USER' in src


# ── Bug-regression tests ─────────────────────────────────────────────────────

def test_seen_eviction_uses_integer_ids():
    """BUG: open_ids must NOT use str() — _seen keys are integers.

    open_ids = {str(pr["id"]) for pr in prs}  -- WRONG: int not in set-of-str
    open_ids = {pr["id"] for pr in prs}        -- CORRECT

    If str() is present, every _seen entry is deleted every iteration because
    Python's `int not in {str, str, ...}` is always True, causing every PR to
    be re-logged and re-checked on every 60s loop.
    """
    src = _source()
    # Ensure the bad pattern is gone
    assert 'open_ids = {str(pr["id"]) for pr in prs}' not in src, (
        "REGRESSION: _seen eviction still uses str(pr['id']) — "
        "integer IDs will never match the string set, clearing _seen every iteration."
    )
    # Ensure the correct pattern is present
    assert 'open_ids = {pr["id"] for pr in prs}' in src, (
        "_seen eviction must build open_ids from integer IDs to match _seen keys."
    )


def test_seen_not_cached_on_error():
    """BUG: _seen must NOT be set when any_error=True.

    If a PR has a transient error (Redis timeout, auth), setting _seen would
    prevent retries. The fix: only cache when the run was fully clean.
    """
    src = _source()
    assert "if not any_error:" in src, (
        "Fix missing: _seen must only be set when there are no errors, "
        "so transient failures trigger a retry on the next iteration."
    )


# ── Diff classification (functional) ─────────────────────────────────────────
# These import the module and call classify_diff_error / argocd_diff helpers
# directly, so they verify real behaviour rather than source-string presence.

def test_classify_oci_login_is_indeterminate():
    """OCI 'helm registry login' failure must be indeterminate, NOT no-diff.

    This is the core bug: a repo-server cache miss + bad OCI credential made the
    diff fail, but it was reported as a green 'no manifest changes'.
    """
    mod = _import_module()
    stderr = ('{"level":"fatal","msg":"rpc error: code = Unknown desc = error '
              'logging into OCI registry: failed to login to registry: failed '
              'running helm: `helm registry login helm-oci-dev.repo.appspace.com '
              '--username ****** --password ******` failed exit status 1: '
              'response status code 401: : Bad Credentials"}')
    outcome, reason, _ = mod.classify_diff_error(stderr)
    assert outcome == mod.OUT_INDETERMINATE
    assert reason == "oci_login"


def test_classify_502_getmanifests_is_indeterminate():
    """A 502 on GetManifests must be indeterminate, NOT silently no-diff."""
    mod = _import_module()
    stderr = ('{"level":"fatal","msg":"rpc error: code = Unknown desc = POST '
              'https://argocd.appspace.com/application.ApplicationService/'
              'GetManifests failed with status code 502"}')
    outcome, reason, _ = mod.classify_diff_error(stderr)
    assert outcome == mod.OUT_INDETERMINATE
    assert reason == "manifests_5xx"


def test_classify_redis_timeout_vs_managed_no_cache():
    """Cached-managed-resources error splits into redis_timeout vs managed_no_cache.

    Both are indeterminate (never no-diff): a failed live-state read means the
    diff is unknown, not 'no changes'.
    """
    mod = _import_module()
    base = "rpc error: error getting cached app managed resources"
    o1, r1, _ = mod.classify_diff_error(base + ": i/o timeout")
    assert (o1, r1) == (mod.OUT_INDETERMINATE, "redis_timeout")
    o2, r2, _ = mod.classify_diff_error(base)
    assert (o2, r2) == (mod.OUT_INDETERMINATE, "managed_no_cache")


def test_classify_auth_is_indeterminate():
    """Expired/invalid session is indeterminate and recognised for re-login."""
    mod = _import_module()
    outcome, reason, _ = mod.classify_diff_error("rpc error: invalid session token")
    assert outcome == mod.OUT_INDETERMINATE
    assert reason == "auth"


def test_classify_managed_mode_patterns_indeterminate():
    """Server-busy / permission / canceled patterns map to indeterminate.

    These were previously swallowed as silent no-diff. They must now surface as
    'diff could not be computed' so a failed evaluation is never shown as green.
    """
    mod = _import_module()
    cases = {
        "the server is not currently accepting requests": "server_unavailable",
        "error getting server version": "server_unavailable",
        "rpc error: code = PermissionDenied desc=x": "permission",
        "context canceled": "canceled",
    }
    for stderr, expected_reason in cases.items():
        outcome, reason, _ = mod.classify_diff_error(stderr)
        assert outcome == mod.OUT_INDETERMINATE, stderr
        assert reason == expected_reason, (stderr, reason)


def test_classify_unknown_is_error():
    """Genuinely unrecognised output is a hard error, not indeterminate."""
    mod = _import_module()
    outcome, reason, _ = mod.classify_diff_error("totally unexpected boom")
    assert outcome == mod.OUT_ERROR
    assert reason == "unknown"


def test_no_failure_is_classified_as_no_diff():
    """Regression guard: no known failure pattern may resolve to OUT_NO_DIFF.

    OUT_NO_DIFF must only come from a clean exit 0. classify_diff_error never
    returns it — it only returns INDETERMINATE or ERROR.
    """
    mod = _import_module()
    failure_samples = [
        "error logging into OCI registry",
        "GetManifests failed with status code 502",
        "error getting cached app managed resources: i/o timeout",
        "error getting cached app managed resources",
        "the server is not currently accepting requests",
        "permission denied",
        "invalid session",
        "random unknown failure",
    ]
    for s in failure_samples:
        outcome, _, _ = mod.classify_diff_error(s)
        assert outcome != mod.OUT_NO_DIFF, f"{s!r} must never be reported as no-diff"


# ── Retry and resilience tests ───────────────────────────────────────────────

def test_retryable_diff_errors_defined():
    """_RETRYABLE_DIFF_ERRORS must cover the known transient spoke-Redis patterns."""
    src = _source()
    assert "_RETRYABLE_DIFF_ERRORS" in src, "Missing _RETRYABLE_DIFF_ERRORS constant"
    for pattern in ("i/o timeout", "connection refused", "context deadline exceeded"):
        assert pattern in src, f"Retryable pattern missing: {pattern!r}"


def test_auth_error_patterns_defined():
    """_AUTH_ERROR_PATTERNS must cover known JWT expiry messages."""
    src = _source()
    assert "_AUTH_ERROR_PATTERNS" in src, "Missing _AUTH_ERROR_PATTERNS constant"
    for pattern in ("invalid session", "token has expired", "unauthenticated"):
        assert pattern in src, f"Auth pattern missing: {pattern!r}"


def test_async_relogin_function_exists():
    """_async_relogin must be present for background JWT refresh on auth errors."""
    src = _source()
    assert "def _async_relogin" in src, "Missing _async_relogin helper"
    assert "argocd_login()" in src, "_async_relogin must call argocd_login()"


def test_diff_timeout_configurable():
    """Per-attempt diff timeout is env-configurable with a 120s default.

    Mass version bumps force OCI cache-miss renders that can legitimately take
    over a minute under burst, so the default was raised to 120s and exposed as
    DIFF_TIMEOUT for tuning.
    """
    src = _source()
    assert 'DIFF_TIMEOUT       = int(os.environ.get("DIFF_TIMEOUT", "120"))' in src, (
        "DIFF_TIMEOUT must be env-configurable with default 120"
    )
    assert "timeout=DIFF_TIMEOUT" in src, (
        "_run_one_diff must use DIFF_TIMEOUT, not a hardcoded timeout"
    )


def test_capacity_knobs_configurable():
    """Worker/cap knobs must be env-overridable for mass-app PRs (600+ apps)."""
    src = _source()
    assert 'DIFF_WORKERS       = int(os.environ.get("DIFF_WORKERS", "16"))' in src, (
        "DIFF_WORKERS must be env-configurable (default 16)"
    )
    assert 'MAX_APPS_PER_RUN   = int(os.environ.get("MAX_APPS_PER_RUN", "800"))' in src, (
        "MAX_APPS_PER_RUN must be env-configurable (default 800) to cover 600+ apps"
    )
    assert 'MAX_PR_WORKERS  = int(os.environ.get("PR_WORKERS", "3"))' in src, (
        "MAX_PR_WORKERS must be env-configurable via PR_WORKERS"
    )


def test_retry_loop_with_backoff():
    """argocd_diff must retry transient errors with exponential backoff + jitter."""
    src = _source()
    assert "for attempt in range(DIFF_RETRIES):" in src, (
        "argocd_diff must loop over DIFF_RETRIES attempts"
    )
    assert 'DIFF_RETRIES       = int(os.environ.get("DIFF_RETRIES", "5"))' in src, (
        "DIFF_RETRIES must be env-configurable (default 5)"
    )
    assert "def _diff_backoff(" in src, "Missing exponential backoff helper"
    assert "random.uniform" in src, "Backoff must add jitter (random.uniform)"


def test_manifests_5xx_is_retryable():
    """Burst 5xx / agent gRPC blips must be retried in-process, not surfaced raw.

    Before this change 'code = Unknown desc = POST' (the gRPC symptom of a
    mass-bump repo-server overload) was not in _RETRYABLE_DIFF_ERRORS, so it was
    reported as indeterminate on the first try instead of being retried.
    """
    src = _source()
    start = src.index("_RETRYABLE_DIFF_ERRORS = (")
    end   = src.index(")", start)
    block = src[start:end]
    for needle in ("code = Unknown desc = POST",
                   "GetManifests failed with status code 5",
                   "error getting cached app managed resources"):
        assert needle in block, f"5xx/burst pattern missing from retryable: {needle!r}"


def test_cache_warm_selection():
    """_select_warm_apps groups by chart and warms one rep per multi-app chart."""
    mod = _import_module()

    # Below threshold: no warm-up, everything fans out directly.
    small = [f"app{i}" for i in range(3)]
    warm, rest = mod._select_warm_apps(small)
    assert warm == [] and rest == small

    # Above threshold with a shared chart: one representative is warmed.
    mod._app_chart_map = {f"ms{i}": "appspace-micro-services" for i in range(10)}
    mod._app_chart_map["solo"] = "other-chart"
    apps = [f"ms{i}" for i in range(10)] + ["solo"]
    warm, rest = mod._select_warm_apps(apps)
    assert len(warm) == 1, "exactly one rep for the shared multi-app chart"
    assert warm[0].startswith("ms"), "warm rep must come from the shared chart group"
    assert "solo" in rest, "single-app charts stay in the fan-out batch"
    assert set(warm) | set(rest) == set(apps), "no app may be dropped"


def test_pure_helm_diff():
    """Diff engine must use pure helm template — no argocd app diff or manifests calls."""
    src = _source()
    assert "_helm_template" in src, "missing _helm_template function"
    assert "_ensure_chart" in src, "missing _ensure_chart function"
    assert "_fetch_value_files" in src, "missing _fetch_value_files function"
    assert "_diff_manifests" in src, "missing _diff_manifests function"
    # No live cluster access: argocd app diff and argocd app manifests must NOT be
    # called during the diff path
    assert '"app", "diff"' not in src, (
        "argocd app diff must not be used - causes agent load and is slow"
    )
    assert '"app", "manifests"' not in src, (
        "argocd app manifests must not be called during diff (still uses agents in our setup)"
    )
    # argocd is still used for app discovery (app list) and auth (login) only
    assert '"app", "list"' in src, "argocd app list needed for app discovery at startup"


def test_chart_revision_detection():
    """_pr_chart_revision must detect version bumps; helm template must use the new version."""
    src = _source()
    assert "_pr_chart_revision" in src, "missing _pr_chart_revision function"
    assert "_app_chart_revision_map" in src, "missing chart revision cache"
    assert "_bb_fetch_file_at_sha" in src, "missing Bitbucket file fetch helper"
    # With pure helm path, the new chart version is passed to _ensure_chart / _helm_template.
    # The pr_rev variable in _run_one_diff must use chart_revision when provided.
    assert "pr_rev = chart_revision or main_rev" in src, (
        "_run_one_diff must use chart_revision for the PR render when provided"
    )
    assert "_ensure_chart(registry, chart_name, pr_rev)" in src, (
        "_run_one_diff must pull the PR chart version (pr_rev) from OCI"
    )


def test_interleave_by_agent():
    """_interleave_by_agent round-robins apps across agents, dropping none."""
    mod = _import_module()
    mod._app_agent_map = {
        "a1": "agentA", "a2": "agentA", "a3": "agentA",
        "b1": "agentB", "b2": "agentB",
    }
    order = mod._interleave_by_agent(["a1", "a2", "a3", "b1", "b2"])
    assert set(order) == {"a1", "a2", "a3", "b1", "b2"}, "no app dropped/duplicated"
    # First two must be from different agents (spread, not piled on agentA).
    assert mod._app_agent_map[order[0]] != mod._app_agent_map[order[1]], (
        "interleave must alternate agents, not group them"
    )


# ── JFrog webhook + dedicated account ────────────────────────────────────────

def test_argocd_uses_diff_preview_account():
    """argocd_login must use ARGOCD_USER (diff-preview), never hardcoded admin.

    The diff-preview local account has limited RBAC (applications:*, repos/projects/clusters:get).
    Using admin would give unnecessary access to account management, RBAC config, etc.
    Password independence: rotating admin does not break the service.
    """
    src = _source()
    assert '"--username", ARGOCD_USER' in src, (
        "argocd_login must use ARGOCD_USER variable, not hardcoded 'admin'"
    )
    assert '"--username", "admin"' not in src, (
        "REGRESSION: hardcoded admin username found — use ARGOCD_USER instead"
    )
    assert 'os.environ.get("ARGOCD_USER", "diff-preview")' in src, (
        "ARGOCD_USER must default to 'diff-preview'"
    )


def test_jfrog_webhook_secret_constant():
    """All JFrog webhook constants must be present and configurable."""
    src = _source()
    assert 'JFROG_WEBHOOK_SECRET' in src, "Missing JFROG_WEBHOOK_SECRET constant"
    assert 'JFROG_MAX_BODY_BYTES' in src, "Missing body size limit constant"
    assert 'JFROG_DEDUP_WINDOW' in src, "Missing dedup window constant"
    assert 'JFROG_REFRESH_WORKERS' in src, "Missing parallel workers constant"
    assert 'JFROG_WEBHOOK_SECRET' in src


def test_jfrog_webhook_endpoint():
    """/jfrog-webhook route must exist in do_POST."""
    src = _source()
    assert '"/jfrog-webhook"' in src, "Missing /jfrog-webhook route in do_POST"
    assert 'X-JFrog-Event-Auth' in src, "Missing X-JFrog-Event-Auth header check"
    assert '202' in src, "JFrog webhook must respond 202 Accepted before background processing"
    assert '401' in src, "JFrog webhook must return 401 on HMAC failure"


def test_hmac_verification():
    """_verify_jfrog_hmac function must exist and use HMAC-SHA256."""
    src = _source()
    assert "def _verify_jfrog_hmac" in src, "Missing _verify_jfrog_hmac function"
    assert "hmac" in src.lower(), "HMAC verification must use Python hmac module"
    assert "compare_digest" in src, "Must use hmac.compare_digest to prevent timing attacks"


def test_jfrog_hard_refresh_function():
    """_jfrog_hard_refresh function must list apps and call hard-refresh."""
    src = _source()
    assert "def _jfrog_hard_refresh" in src, "Missing _jfrog_hard_refresh function"
    assert "ARGOCD_PROJECTS" in src, "Must use configurable project list"
    assert "ARGOCD_PROJECTS" in src or "--project" in src, "Must target ArgoCD projects"
    assert "--hard-refresh" in src, "Must call argocd app get --hard-refresh"
    assert "image_name" in src, "Must parse data.image_name from JFrog payload"
    assert "tag" in src, "Must parse data.tag from JFrog payload"


# ── Improvements: security, performance, dedup, observability ────────────────

def test_body_size_limit_constant():
    """JFROG_MAX_BODY_BYTES constant must exist."""
    src = _source()
    assert 'JFROG_MAX_BODY_BYTES' in src, "Missing JFROG_MAX_BODY_BYTES constant"
    assert '65536' in src, "Default must be 64KB"

def test_body_size_check_before_hmac():
    """Size check must appear before HMAC check in do_POST."""
    src = _source()
    size_pos = src.find('JFROG_MAX_BODY_BYTES')
    hmac_pos = src.find('_verify_jfrog_hmac')
    assert size_pos > 0 and hmac_pos > 0
    # The 413 response (size rejection) must appear before the 401 (HMAC rejection)
    pos_413 = src.find('send_response(413)')
    pos_401 = src.find('send_response(401)')
    assert pos_413 < pos_401, "Size check (413) must appear before HMAC check (401)"

def test_content_length_safe_parse():
    """Content-Length parsing must be inside a try/except to prevent ValueError crash."""
    src = _source()
    # The try/except block wrapping Content-Length must be present
    assert "except (ValueError, TypeError):" in src, \
        "Content-Length int() must be inside a try/except (ValueError, TypeError)"

def test_parallel_hard_refresh():
    """Hard-refresh must use ThreadPoolExecutor, not a sequential for-loop."""
    src = _source()
    assert 'ThreadPoolExecutor' in src, "Hard-refresh must be parallelized with ThreadPoolExecutor"
    assert 'JFROG_REFRESH_WORKERS' in src, "Worker count must be configurable"

def test_dedup_constants():
    """Deduplication window constant and state must exist."""
    src = _source()
    assert 'JFROG_DEDUP_WINDOW' in src, "Missing JFROG_DEDUP_WINDOW constant"
    assert '_jfrog_recent' in src, "Missing _jfrog_recent dedup state"
    assert '_jfrog_dedup_lock' in src, "Missing _jfrog_dedup_lock"
    assert 'dedup_skipped' in src, "Missing dedup_skipped stats counter"

def test_dedup_logic():
    """Dedup check must happen AFTER 202 response and before spawning thread."""
    src = _source()
    # Find the 202 send, the dedup_key assignment, and the thread name in do_POST
    idx_202    = src.find('send_response(202)')
    idx_dedup  = src.find('dedup_key = f"')
    idx_thread = src.find('jfrog-refresh-{chart_name}:{chart_ver}')
    assert idx_202 > 0 and idx_dedup > 0 and idx_thread > 0
    assert idx_202 < idx_dedup < idx_thread, \
        "Order must be: 202 response -> dedup check -> thread spawn"

def test_stats_counters():
    """Stats counters must exist and cover key events."""
    src = _source()
    for key in ('received', 'rejected_hmac', 'rejected_format',
                'dedup_skipped', 'refreshes_ok', 'refreshes_failed'):
        assert f'"{key}"' in src, f"Missing stats counter: {key}"

def test_stats_endpoint():
    """GET /jfrog-webhook/stats endpoint must exist."""
    src = _source()
    assert '"/jfrog-webhook/stats"' in src, "Missing /jfrog-webhook/stats route"
    assert '"Content-Type", "application/json"' in src, "Stats must return JSON"


def test_body_size_limit():
    """JFROG_MAX_BODY_BYTES must be used to reject oversized bodies before reading."""
    src = _source()
    assert "JFROG_MAX_BODY_BYTES" in src
    assert "413" in src, "Must return HTTP 413 for oversized bodies"
    # Size check must happen before HMAC (HMAC check happens after reading body)
    size_pos = src.find("JFROG_MAX_BODY_BYTES")
    hmac_pos = src.find("_verify_jfrog_hmac")
    assert size_pos < hmac_pos, "Body size check must come before HMAC verification"


def test_content_length_error_defaults_to_zero():
    """Malformed Content-Length must not return 400 — should default to 0."""
    src = _source()
    # The except block should assign length = 0, not send_response(400)
    idx = src.find("length = 0  # malformed header")
    assert idx > 0, "Content-Length parse error must default to length=0, not return 400"


def test_dedup_state_and_lock():
    """Dedup state (_jfrog_recent, _jfrog_dedup_lock, JFROG_DEDUP_WINDOW) must exist."""
    src = _source()
    assert "_jfrog_recent" in src, "Missing dedup dict"
    assert "_jfrog_dedup_lock" in src, "Missing dedup lock"
    assert "JFROG_DEDUP_WINDOW" in src, "Missing dedup window"
    assert "dedup_key" in src, "Missing dedup key construction"


def test_stats_counters():
    """All expected stats counters must be present."""
    src = _source()
    for key in ("received", "rejected_hmac", "rejected_format",
                "dedup_skipped", "refreshes_ok", "refreshes_failed"):
        assert f'"{key}"' in src, f"Missing stats counter: {key}"


def test_stats_endpoint():
    """GET /jfrog-webhook/stats must exist and return JSON."""
    src = _source()
    assert '"/jfrog-webhook/stats"' in src, "Missing /jfrog-webhook/stats route"
    assert '"application/json"' in src, "Stats endpoint must set Content-Type: application/json"


def test_parallel_refresh():
    """Hard-refresh must use ThreadPoolExecutor for parallel app refreshes."""
    src = _source()
    assert "ThreadPoolExecutor" in src, "Missing ThreadPoolExecutor"
    assert "JFROG_REFRESH_WORKERS" in src or "REFRESH_WORKERS" in src, "Missing refresh workers constant"
    # Pool must be used inside _jfrog_hard_refresh
    func_start = src.find("def _jfrog_hard_refresh")
    next_def   = src.find("\ndef ", func_start + 1)
    func_body  = src[func_start:next_def] if next_def > 0 else src[func_start:]
    assert "ThreadPoolExecutor" in func_body or "_TPE" in func_body, \
        "_jfrog_hard_refresh must parallelize with ThreadPoolExecutor"


# ── Rebrand: ArgoCD Diff Preview -> ACME Diff Preview ────────────────────────

def test_status_name_is_acme():
    """The Bitbucket-visible status name and comment header must say ACME."""
    mod = _import_module()
    assert mod.STATUS_NAME == "ACME Diff Preview"
    src = _source()
    # No leftover 'ArgoCD Diff Preview' display string (the product was renamed).
    assert "ArgoCD Diff Preview" not in src, (
        "Leftover 'ArgoCD Diff Preview' display string — rename to 'ACME Diff Preview'"
    )


def test_build_status_name_uses_status_name():
    """post_build_status must send STATUS_NAME, not a hardcoded ArgoCD label."""
    src = _source()
    assert '"name": STATUS_NAME' in src, (
        "Build status must use STATUS_NAME so the PR shows 'ACME Diff Preview'"
    )


def test_build_key_is_stable():
    """BUILD_KEY must stay 'argocd-diff-preview' so existing PR statuses are reused.

    The key identifies the build-status row in Bitbucket; changing it would
    orphan the old status and create a duplicate row on every open PR.
    """
    mod = _import_module()
    assert mod.BUILD_KEY == "argocd-diff-preview"


def test_comment_marker_matches_legacy():
    """find_existing_comment must match the new and the legacy marker.

    During rollout, comments written by old pods carry 'argocd-diff-preview';
    they must still be found and updated in place (no duplicate comment).
    """
    mod = _import_module()
    assert mod.COMMENT_MARKER == "acme-diff-preview"
    assert "argocd-diff-preview" in mod._COMMENT_MARKERS
    assert "acme-diff-preview" in mod._COMMENT_MARKERS


def test_comment_header_renders_acme():
    """format_comment output must carry the ACME header and footer marker."""
    mod = _import_module()
    # A clean no-diff result for one app.
    results = {"env-a-ms": mod.DiffResult("", False, None, mod.OUT_NO_DIFF, "clean")}
    body = mod.format_comment("abcdef1234567890", results)
    assert "ACME Diff Preview" in body
    assert "acme-diff-preview" in body  # footer marker
    assert "No manifest changes" in body


def test_indeterminate_comment_is_not_green():
    """An indeterminate result must NOT render as 'No manifest changes'."""
    mod = _import_module()
    results = {
        "env-a-glb": mod.DiffResult("", False, "401 Bad Credentials",
                                    mod.OUT_INDETERMINATE, "oci_login"),
    }
    body = mod.format_comment("abcdef1234567890", results)
    assert "diff unavailable" in body.lower()
    assert "Diff incomplete" in body
    # Must not falsely claim the app is unchanged.
    assert "No manifest changes" not in body


# ── Helm-template diff + OCI error handling ───────────────────────────────────

def test_helm_diff_architecture():
    """Helm template diff must be the primary path; argocd manifests is fallback."""
    src = _source()
    assert "_helm_template" in src, "missing _helm_template function"
    assert "_ensure_chart" in src, "missing _ensure_chart (chart caching) function"
    assert "_fetch_value_files" in src, "missing _fetch_value_files function"
    assert "_helm_login" in src, "missing _helm_login (OCI registry auth) function"
    assert "OciChartNotFound" in src, "missing OciChartNotFound exception class"
    # Both dev and release registries must be handled via the same credential pair
    assert "_app_chart_registry_map" in src, "must track per-app OCI registry URL"
    assert "OCI_USER" in src, "OCI_USER env var not wired"
    assert "OCI_PASS" in src, "OCI_PASS env var not wired"


def test_oci_not_found_error_classification():
    """OCI chart version not found must surface as oci_not_found, not a generic error."""
    mod = _import_module()
    for pattern in ("not found in", "chart not found",
                    "unexpected status code: 404", "does not exist in oci registry"):
        outcome, reason, detail = mod.classify_diff_error(pattern)
        assert reason == "oci_not_found", (
            f"Pattern {pattern!r} should map to oci_not_found, got {reason}")
        assert outcome == mod.OUT_INDETERMINATE


def test_oci_not_found_is_not_retried():
    """OciChartNotFound must not be retried (it is permanent, not transient)."""
    src = _source()
    assert 'reason == "oci_not_found"' in src, (
        "oci_not_found must be caught before the retry loop to avoid wasting retries"
    )


def test_oci_not_found_in_reason_hints():
    """oci_not_found must have a human-readable hint for the PR comment."""
    mod = _import_module()
    assert "oci_not_found" in mod._REASON_HINTS, "missing hint for oci_not_found reason"
    hint = mod._REASON_HINTS["oci_not_found"]
    assert "OCI" in hint or "oci" in hint.lower() or "registry" in hint.lower(), (
        f"Hint should mention the OCI registry: {hint!r}")


def test_parse_manifest_resources():
    """_parse_manifest_resources must split multi-doc YAML by kind/namespace/name."""
    mod = _import_module()
    yaml_text = """---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-service
  namespace: test-ns
spec: {}
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: my-config
  namespace: test-ns
data: {}
"""
    resources = mod._parse_manifest_resources(yaml_text)
    assert len(resources) == 2, f"expected 2 resources, got {len(resources)}"
    keys = set(k[2] for k in resources)
    assert "my-service" in keys
    assert "my-config" in keys


def test_diff_manifests_detects_change():
    """_diff_manifests must return non-empty string when resource content differs."""
    mod = _import_module()
    main_yaml = """---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: svc
  namespace: env-a
spec:
  image: myapp:1.0.0
"""
    pr_yaml = """---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: svc
  namespace: env-a
spec:
  image: myapp:2.0.0
"""
    diff = mod._diff_manifests(main_yaml, pr_yaml)
    assert diff, "diff should be non-empty when resource content changed"
    assert "===== " in diff, "diff output must use ArgoCD header format"
    assert "1.0.0" in diff or "2.0.0" in diff, "diff must contain the version strings"


def test_diff_manifests_no_change():
    """_diff_manifests must return empty string when manifests are identical."""
    mod = _import_module()
    yaml = """---
apiVersion: v1
kind: ConfigMap
metadata:
  name: cfg
  namespace: ns
data:
  key: value
"""
    diff = mod._diff_manifests(yaml, yaml)
    assert diff == "", "identical manifests must produce empty diff"


def test_diff_manifests_new_resource():
    """_diff_manifests must detect a brand new resource added in PR."""
    mod = _import_module()
    main_yaml = "---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: old\n  namespace: ns\n"
    pr_yaml = (main_yaml +
               "---\napiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: new-svc\n  namespace: ns\n")
    diff = mod._diff_manifests(main_yaml, pr_yaml)
    assert diff, "new resource in PR must produce a diff"
    assert "new-svc" in diff


def test_ai_prompt_sre_format():
    """AI prompt must use the SRE format with 🌍/📊/⚠️ sections."""
    src = _source()
    assert "Senior SRE" in src, "AI prompt must reference Senior SRE role"
    assert "AFFECTED ENVIRONMENTS" in src, "AI prompt must have environments section"
    assert "SUMMARY" in src, "AI prompt must have summary section"
    assert "CRITICAL CHANGES" in src, "AI prompt must have critical changes section"
    assert "Version downgrade" in src or "downgrade" in src, (
        "AI prompt must instruct model to flag version downgrades"
    )
