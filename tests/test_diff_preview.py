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
