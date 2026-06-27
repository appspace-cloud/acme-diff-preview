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
    """Must use dedicated diff-preview local account. argocd_login now uses
    the REST API (no CLI args) so ARGOCD_USER is used in the JSON body."""
    src = _source()
    assert "ARGOCD_PASS" in src
    assert "ARGOCD_USER" in src
    # argocd login now goes via REST API — no --password CLI arg
    assert '"--password", ARGOCD_PASS' not in src, (
        "ARGOCD_PASS must not appear as a CLI arg (visible in ps aux)"
    )
    assert "admin" not in src.split("ARGOCD_USER")[0].split("def argocd_login")[-1][:200]


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


def test_seen_not_cached_on_transient_error():
    """_seen must NOT be set on transient failures (allows retry next iteration).

    Permanent failures (oci_not_found) SHOULD mark seen to avoid spamming the
    PR with repeated 'not found' comments every 60 seconds.
    Transient failures (timeout, auth) must NOT mark seen so they are retried.
    """
    src = _source()
    assert "is_transient_failure" in src, (
        "Must distinguish transient vs permanent failures for _seen caching"
    )
    assert "if not is_transient_failure:" in src, (
        "_seen must only be set for non-transient outcomes (clean OR permanent error)"
    )


# ── Diff failure-reason model (functional) ───────────────────────────────────
# The helm-template engine sets an explicit REASON_* code per failure (no stderr
# regex). These tests drive argocd_diff with a stubbed _run_one_diff to verify the
# retry/outcome behaviour for each reason class.

def _fast_no_backoff(mod, monkeypatch_attr=True):
    """Make retries instant and bounded so functional tests don't sleep."""
    mod.DIFF_RETRIES = 2
    mod._diff_backoff = lambda attempt: 0.0


def test_reason_sets_are_coherent():
    """Reason constants must be partitioned into retryable vs permanent."""
    mod = _import_module()
    assert mod.REASON_OCI_NOT_FOUND in mod.PERMANENT_REASONS
    assert mod.REASON_OCI_NOT_FOUND not in mod.RETRYABLE_REASONS
    for r in (mod.REASON_OCI_PULL, mod.REASON_METADATA, mod.REASON_TIMEOUT):
        assert r in mod.RETRYABLE_REASONS
    # render_failed is a soft (non-retryable, non-permanent) indeterminate.
    assert mod.REASON_RENDER not in mod.RETRYABLE_REASONS
    assert mod.REASON_RENDER not in mod.PERMANENT_REASONS


def test_oci_not_found_is_permanent_indeterminate():
    """oci_not_found must resolve to INDETERMINATE/oci_not_found with no retry."""
    mod = _import_module()
    calls = {"n": 0}
    def _stub(app, pr_sha, main_sha, chart_revision=None, **_kw):
        calls["n"] += 1
        return None, mod.REASON_OCI_NOT_FOUND, "Chart x:9.9.9 not found in registry"
    mod._run_one_diff = _stub
    _fast_no_backoff(mod)
    res = mod.argocd_diff("env-a-ms", "prsha", "mainsha")
    assert res.outcome == mod.OUT_INDETERMINATE
    assert res.reason == mod.REASON_OCI_NOT_FOUND
    assert calls["n"] == 1, "permanent reason must not be retried"


def test_transient_reason_retries_then_indeterminate():
    """A transient reason is retried up to DIFF_RETRIES then ends INDETERMINATE.

    Critically it must NOT become OUT_ERROR (which would FAIL the PR on a blip).
    """
    mod = _import_module()
    calls = {"n": 0}
    def _stub(app, pr_sha, main_sha, chart_revision=None, **_kw):
        calls["n"] += 1
        return None, mod.REASON_OCI_PULL, "helm pull failed"
    mod._run_one_diff = _stub
    _fast_no_backoff(mod)
    res = mod.argocd_diff("env-a-ms", "prsha", "mainsha")
    assert res.outcome == mod.OUT_INDETERMINATE
    assert res.reason == mod.REASON_OCI_PULL
    assert calls["n"] == mod.DIFF_RETRIES, "transient reason must use all attempts"


def test_render_failed_is_soft_no_retry():
    """render_failed is indeterminate but must not be retried (not transient)."""
    mod = _import_module()
    calls = {"n": 0}
    def _stub(app, pr_sha, main_sha, chart_revision=None, **_kw):
        calls["n"] += 1
        return None, mod.REASON_RENDER, "Error: execution error: missing value"
    mod._run_one_diff = _stub
    _fast_no_backoff(mod)
    res = mod.argocd_diff("env-a-ms", "prsha", "mainsha")
    assert res.outcome == mod.OUT_INDETERMINATE
    assert res.reason == mod.REASON_RENDER
    assert calls["n"] == 1, "render_failed must not be retried"


def test_success_path_returns_diff_or_no_diff():
    """reason=None with diff text -> OUT_DIFF; with empty text -> OUT_NO_DIFF."""
    mod = _import_module()
    _fast_no_backoff(mod)

    mod._run_one_diff = lambda *a, **k: (
        "===== /Deployment ns/svc ======\n--- \n+++ \n@@ -1 +1 @@\n-image: a\n+image: b\n",
        None, None)
    res = mod.argocd_diff("env-a-ms", "prsha", "mainsha")
    assert res.outcome == mod.OUT_DIFF and res.has_diff

    mod._run_one_diff = lambda *a, **k: ("", None, None)
    res = mod.argocd_diff("env-a-ms", "prsha", "mainsha")
    assert res.outcome == mod.OUT_NO_DIFF


def test_classify_diff_error_removed():
    """The argocd-era stderr classifier and its constants must be gone."""
    src = _source()
    assert "def classify_diff_error" not in src, "classify_diff_error must be removed"
    assert "_RETRYABLE_DIFF_ERRORS" not in src, "stale retryable-stderr list must be removed"
    assert "_DIFF_ERROR_RULES" not in src, "stale stderr rule table must be removed"


# ── Retry and resilience tests ───────────────────────────────────────────────

def test_retryable_reasons_defined():
    """RETRYABLE_REASONS must hold the transient helm-path reasons (and not permanent)."""
    mod = _import_module()
    assert mod.REASON_OCI_PULL in mod.RETRYABLE_REASONS
    assert mod.REASON_METADATA in mod.RETRYABLE_REASONS
    assert mod.REASON_TIMEOUT in mod.RETRYABLE_REASONS
    assert mod.REASON_OCI_NOT_FOUND not in mod.RETRYABLE_REASONS


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


def test_cache_warm_selection():
    """_select_warm_apps was removed in 1.9.2 (helm pre-pull replaced it).
    Charts are now pre-pulled before the diff fan-out — no warm-up diff pass."""
    src = _source()
    assert "_select_warm_apps" not in src, (
        "_select_warm_apps must be removed — replaced by helm pre-pull phase"
    )


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
    assert "_bb_fetch_status" in src, "missing Bitbucket file fetch helper"
    # With pure helm path, the new chart version is passed to _ensure_chart / _helm_template.
    # The pr_rev variable in _run_one_diff must use chart_revision when provided.
    assert "pr_rev = chart_revision or main_rev" in src, (
        "_run_one_diff must use chart_revision for the PR render when provided"
    )
    assert "_ensure_chart" in src and "pr_rev" in src, (
        "_run_one_diff must pull the PR chart version (pr_rev) from OCI"
    )


# ── JFrog webhook + dedicated account ────────────────────────────────────────

def test_argocd_uses_diff_preview_account():
    """argocd_login must use ARGOCD_USER (diff-preview), never hardcoded admin.
    Login now uses REST API — no --password on CLI arg (prevents ps aux exposure).
    """
    src = _source()
    assert "ARGOCD_USER" in src, "ARGOCD_USER variable must be present"
    assert '"--username", "admin"' not in src, (
        "REGRESSION: hardcoded admin username on CLI — use ARGOCD_USER"
    )
    assert 'os.environ.get("ARGOCD_USER", "diff-preview")' in src, (
        "ARGOCD_USER must default to 'diff-preview'"
    )
    # argocd_login must not pass ARGOCD_PASS as CLI argument
    assert '"--password", ARGOCD_PASS' not in src, (
        "ARGOCD_PASS must not appear as a CLI arg (visible in ps aux); use REST API"
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
    results = {"env-a-ms": mod.DiffResult("", [], 0, False, None, mod.OUT_NO_DIFF, "clean")}
    body = mod.format_comment("abcdef1234567890", results)
    assert "ACME Diff Preview" in body
    assert "acme-diff-preview" in body  # footer marker
    assert "No manifest changes" in body


def test_indeterminate_comment_is_not_green():
    """An indeterminate result must NOT render as 'No manifest changes'."""
    mod = _import_module()
    results = {
        "env-a-glb": mod.DiffResult("", [], 0, False, "401 Bad Credentials",
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


def test_ensure_chart_raises_on_missing_version():
    """_ensure_chart must raise OciChartNotFound when the registry returns 404.

    This is what surfaces as the permanent oci_not_found reason.
    """
    mod = _import_module()
    import subprocess as _sp

    class _R:
        returncode = 1
        stdout = ""
        stderr = "Error: chart not found: unexpected status code: 404"

    import time as _time
    mod._helm_logged_in.add("reg.example.com")
    mod._helm_login_ts["reg.example.com"] = _time.monotonic()   # mark as recently logged in
    orig_run = _sp.run
    _sp.run = lambda *a, **k: _R()
    try:
        import pytest
        with pytest.raises(mod.OciChartNotFound):
            mod._ensure_chart("reg.example.com", "some-chart", "9.9.9-missing")
    finally:
        _sp.run = orig_run


def test_oci_not_found_is_not_retried():
    """The permanent reason must be caught before the retry loop."""
    src = _source()
    assert "reason in PERMANENT_REASONS" in src, (
        "oci_not_found (PERMANENT_REASONS) must short-circuit the retry loop"
    )


def test_oci_not_found_posts_failed_build_status():
    """oci_not_found must post FAILED build status, not SUCCESSFUL.

    PR #6451 bug: when all apps had oci_not_found, the status was SUCCESSFUL.
    The deployer would fail at sync time with the same error, so the diff
    must proactively block the PR.
    """
    src = _source()
    # The code must check for oci_not_found specifically and post FAILED
    assert "oci_not_found_count" in src, (
        "must track oci_not_found count separately to post FAILED status"
    )
    assert '"FAILED"' in src and "oci_not_found" in src, (
        "oci_not_found must result in FAILED build status, not SUCCESSFUL"
    )
    assert "has_blocking_indet" in src, (
        "must distinguish blocking (oci_not_found) from soft indeterminate reasons"
    )


def test_oci_not_found_marks_seen_no_retry():
    """After oci_not_found, PR must NOT be re-processed every 60s.

    Unlike soft indeterminate errors (transient timeouts), oci_not_found is
    permanent: the version will not appear on its own. Re-processing would
    just spam the PR comment every iteration.
    """
    src = _source()
    assert "is_permanent_failure" in src, (
        "must identify permanent failures (oci_not_found) to prevent retry spam"
    )
    # permanent failures should mark _seen so the next iteration skips them
    assert "is_transient_failure" in src and "is_permanent_failure" in src


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


# ── appspace.version detection (scoped, not first version:) ──────────────────

def test_extract_chart_version_scoped_to_appspace():
    """_extract_chart_version must return ONLY the direct appspace.version child,
    ignoring decoy version: keys at other depths (real-config shape)."""
    mod = _import_module()
    content = (
        "apiextensions:\n"
        "  version: 0.0.1\n"            # decoy: top-level sibling
        "appspace:\n"
        "  region: eu\n"
        "  version: 2603.1.0-dev\n"     # the real chart targetRevision (direct child)
        "  elastic:\n"
        "    version: 8.15.1\n"          # deep decoy: appspace.elastic.version
    )
    assert mod._extract_chart_version(content) == "2603.1.0-dev"


def test_extract_chart_version_ignores_deep_version_when_no_direct_child():
    """A config with only a deep appspace.elastic.version (no appspace.version)
    must return None — never the unrelated elastic version (this was the bug)."""
    mod = _import_module()
    content = (
        "appspace:\n"
        "  elastic:\n"
        "    version: 8.15.1\n"
    )
    assert mod._extract_chart_version(content) is None


def test_extract_chart_version_quoted_and_absent():
    mod = _import_module()
    assert mod._extract_chart_version("appspace:\n  version: '2603.1.0-dev'\n") == "2603.1.0-dev"
    assert mod._extract_chart_version("foo: bar\nbaz: qux\n") is None


# ── Noise filter works on difflib unified-diff output ────────────────────────

def test_checksum_only_section_difflib_format():
    """A unified-diff section that only flips a checksum annotation is noise."""
    mod = _import_module()
    body = (
        "--- \n"
        "+++ \n"
        "@@ -3,3 +3,3 @@\n"
        "     annotations:\n"
        "-      checksum/config: aaaaaaaa\n"
        "+      checksum/config: bbbbbbbb\n"
    )
    assert mod._is_checksum_only_section(body) is True


def test_real_change_not_treated_as_noise():
    """A genuine image change in unified-diff form must NOT be filtered as noise."""
    mod = _import_module()
    body = (
        "--- \n"
        "+++ \n"
        "@@ -5,3 +5,3 @@\n"
        "-        image: app:1.0.0\n"
        "+        image: app:2.0.0\n"
    )
    assert mod._is_checksum_only_section(body) is False


# ── Bitbucket fetch status + safe caching ────────────────────────────────────

def test_bb_fetch_status_constants():
    """Distinct OK / NOT_FOUND / ERROR statuses must exist so only definitive
    results are cached (transient errors must never poison the value cache)."""
    mod = _import_module()
    assert mod.BB_OK and mod.BB_NOT_FOUND and mod.BB_ERROR
    src = _source()
    assert "if status in (BB_OK, BB_NOT_FOUND):" in src, (
        "value cache must store only OK / NOT_FOUND, never transient BB_ERROR"
    )
    assert "def _bb_fetch_status" in src


# ── helm --kube-version, OCI startup self-check, cache eviction ──────────────

def test_kube_version_passed_to_helm_template():
    mod = _import_module()
    src = _source()
    assert 'KUBE_VERSION    = os.environ.get("KUBE_VERSION"' in src
    assert '"--kube-version", KUBE_VERSION' in src, (
        "helm template must pin --kube-version for capability-stable renders"
    )


def test_oci_pass_startup_self_check():
    """main() must loudly flag an empty OCI_PASS (otherwise every diff is unavailable)."""
    src = _source()
    assert "OCI_PASS is empty" in src, "startup must log an ERROR when OCI_PASS is unset"


def test_helm_cache_pruning_exists():
    """The on-disk chart cache must be bounded and pruned each iteration."""
    src = _source()
    assert "def _prune_helm_cache" in src, "missing on-disk chart cache prune"
    assert "_prune_helm_cache()" in src, "prune must be called from the loop"
    assert "HELM_CACHE_MAX_CHARTS" in src
    assert "def _bound_vf_cache" in src, "value-file cache must be bounded too"


def test_dead_agent_era_code_removed():
    """Agent-era machinery must be fully gone (it no longer runs in the helm path)."""
    src = _source()
    for symbol in ("AGENT_MAX_CONCURRENCY", "_interleave_by_agent",
                   "_app_agent_map", "_agent_semaphore", "_async_relogin"):
        assert symbol not in src, f"dead agent-era symbol still present: {symbol}"


def test_no_per_pr_vf_cache_clear():
    """The cross-PR _vf_cache.clear() must be gone (it thrashed concurrent PRs)."""
    src = _source()
    assert "_vf_cache.clear()" not in src, (
        "value cache must not be cleared per PR (keys are immutable shas)"
    )


# ── New behaviours (1.9.1+) ──────────────────────────────────────────────────

def test_helm_login_ttl():
    """_helm_login must re-login after HELM_LOGIN_TTL for credential rotation."""
    src = _source()
    assert "HELM_LOGIN_TTL" in src, "missing HELM_LOGIN_TTL constant"
    assert "_helm_login_ts" in src, "missing login timestamp tracker"


def test_status_token_in_comment_footer():
    """format_comment must embed [clean|permanent|transient] token in footer."""
    mod = _import_module()
    results_clean = {"env-a-ms": mod.DiffResult("", [], 0, False, None, mod.OUT_NO_DIFF, "clean")}
    body_clean = mod.format_comment("abc1234", results_clean)
    assert "[clean]" in body_clean, "clean run must embed [clean] token"

    results_indet = {
        "env-a-ms": mod.DiffResult("", [], 0, False, "oci err", mod.OUT_INDETERMINATE, mod.REASON_OCI_NOT_FOUND)
    }
    body_indet = mod.format_comment("abc1234", results_indet)
    assert "[permanent]" in body_indet, "oci_not_found must embed [permanent] token"

    results_transient = {
        "env-a-ms": mod.DiffResult("", [], 0, False, "timeout", mod.OUT_INDETERMINATE, mod.REASON_TIMEOUT)
    }
    body_transient = mod.format_comment("abc1234", results_transient)
    assert "[transient]" in body_transient, "transient reason must embed [transient] token"


def test_diff_stats_endpoint():
    """GET /diff-preview/stats endpoint must expose diff operation counters."""
    src = _source()
    assert '"/diff-preview/stats"' in src, "missing /diff-preview/stats route"
    assert "_diff_stats" in src, "missing _diff_stats counter dict"
    assert '"prs_processed"' in src
    assert '"apps_oci_not_found"' in src


def test_no_warm_diff_pass():
    """_select_warm_apps warm-diff pass must be gone (charts are pre-pulled)."""
    src = _source()
    # The warm_apps/rest_apps split must no longer drive process_batch
    assert "process_batch(warm_apps" not in src, (
        "warm diff pass must be removed — charts are pre-pulled before the fan-out"
    )


def test_pr_chart_revision_uses_vf_cache():
    """_pr_chart_revision must route file fetches through _vf_cache."""
    src = _source()
    # _pr_chart_revision should use _bb_fetch_status + _vf_cache, not
    # calling _bb_fetch_file_at_sha directly (which bypasses the cache).
    assert "_bb_fetch_status(clean, pr_sha)" in src, (
        "_pr_chart_revision must call _bb_fetch_status for cache-routed fetches"
    )
