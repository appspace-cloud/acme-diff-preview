"""Unit tests for diff_preview.py — syntax, key functions, and bug-regression checks."""
import ast
import os
import sys

SRC = os.path.join(os.path.dirname(__file__), "..", "src", "diff_preview.py")


def _source():
    with open(SRC) as f:
        return f.read()


def _tree():
    return ast.parse(_source())


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
    """_wake threading.Event must be present (COPS-2497 webhook support)."""
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


def test_redis_timeout_not_in_managed_mode_errors():
    """BUG: Redis i/o timeout must NOT be silently treated as no-diff.

    'error getting cached app managed resources' without 'i/o timeout' is a
    true argocd-agent managed-mode error (no-diff is correct).

    'error getting cached app managed resources' WITH 'i/o timeout' is a Redis
    infrastructure failure — the diff is indeterminate, must report as error
    so the retry logic fires on the next iteration.
    """
    src = _source()
    # The i/o timeout branch must exist
    assert "i/o timeout" in src, (
        "Fix missing: 'i/o timeout' must be checked to separate Redis failures "
        "from managed-mode no-diff."
    )
    # Redis timeout must return an error message, not None
    assert "Redis timeout" in src, (
        "Fix missing: Redis timeout must produce an error string (not None) "
        "so the comment gets ❌ and the retry logic fires."
    )
    # The error getting cached... string must still be handled (not removed)
    assert "error getting cached app managed resources" in src, (
        "managed-mode error handling must still be present."
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


def test_managed_mode_errors_list_intact():
    """The MANAGED_MODE_ERRORS list must still contain expected entries.

    Fixing the Redis timeout bug must not accidentally remove legitimate
    managed-mode error patterns that should produce silent no-diff.
    """
    src = _source()
    for pattern in [
        "error getting server version",
        "the server is not currently accepting requests",
        "rpc error: code = PermissionDenied",
        "context canceled",
    ]:
        assert pattern in src, (
            f"REGRESSION: managed-mode error pattern missing: {pattern!r}"
        )


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


def test_diff_timeout_reduced():
    """Per-attempt diff timeout must be 60s not 120s.

    120s was too long — a diff command that hasn't finished in 60s is unlikely
    to succeed. Shorter timeout means faster failure detection and faster retries.
    """
    src = _source()
    assert "timeout=60" in src, "Per-attempt diff timeout must be 60s"
    assert "timeout=120" not in src, (
        "REGRESSION: 120s timeout still present — must use 60s per attempt"
    )


def test_diff_workers_reduced():
    """DIFF_WORKERS must be 3, not 4.

    Reduced to limit concurrent gRPC connections to ArgoCD server under load.
    With MAX_PR_WORKERS=3: max concurrent diffs = 3 * 3 = 9 (down from 12).
    """
    src = _source()
    assert "DIFF_WORKERS       = 3" in src, (
        "DIFF_WORKERS must be 3 to limit concurrent ArgoCD gRPC load"
    )


def test_retry_loop_in_argocd_diff():
    """argocd_diff must use a retry loop, not a single subprocess call."""
    src = _source()
    # The loop exists
    assert "for attempt in range(2):" in src, (
        "argocd_diff must use 'for attempt in range(2)' retry loop"
    )
    # Retry on transient errors
    assert "retrying in 3s" in src, (
        "Retry log message missing — 3s backoff between attempts"
    )
    # argocd_diff loop is exactly 2 attempts
    # (range(3) at file level is the Bitbucket http() helper, separate function)
    fn_start = src.index("def argocd_diff(")
    fn_end   = src.index("\ndef parse_diff_sections", fn_start)
    fn_body  = src[fn_start:fn_end]
    import re
    loops = re.findall(r"for attempt in range\((\d+)\)", fn_body)
    assert loops and int(loops[0]) == 2, (
        f"argocd_diff retry loop must be 2 attempts, found: {loops}"
    )


# ── COPS-2500: JFrog webhook + account fix ───────────────────────────────────

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
