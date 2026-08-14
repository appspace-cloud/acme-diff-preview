import pytest
"""v2.6.4 coverage pass 3 — the last reachable lines from the full-suite run.

After passes 1 and 2, a fresh full-suite coverage run left exactly 11 lines
missing. Three are genuinely unreachable defensive guards (marked
`pragma: no cover` directly in src/diff_preview.py, same convention as the
lines pass 1 marked). The remaining reachable ones are covered here:
  - _oci_selfcheck: the helm-login-failure branch (distinct from the
    subprocess-failure branch other tests already cover)
  - _redact_error_detail: the except-Exception fallback, triggered by a
    non-str input the compiled (text) regex genuinely cannot process
  - _run_one_diff's _cancel_futs(): the counter increment path, forcing a
    real still-queued (cancellable) future to exist when a timeout fires
"""
import concurrent.futures
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m  # noqa: E402
import concurrency
import logsink


# ── _oci_selfcheck: login failure short-circuits before any helm call ────

def test_oci_selfcheck_login_failure_short_circuits(monkeypatch):
    monkeypatch.delenv("DIFF_OCI_SELFCHECK_REF", raising=False)
    monkeypatch.setattr(m, "_helm_login", lambda registry: False)
    m._record_pull_success("reg.example.com", "appspace-ms", "1.2.3")
    calls = []
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: calls.append(a))
    logged = []
    monkeypatch.setattr(logsink, "log",
                        lambda msg, sev="INFO", **k: logged.append((sev, msg)))
    assert m._oci_selfcheck() is False
    assert calls == [], "a failed login must never reach `helm show chart`"
    assert m._diff_stats["oci_selfcheck"] == "failed"
    assert any("login failed" in msg for _, msg in logged), logged


# ── _redact_error_detail: non-str input the regex cannot process ─────────

def test_redact_error_detail_non_str_input_falls_back_gracefully():
    # A compiled TEXT pattern raises TypeError against bytes ("cannot use a
    # string pattern on a bytes-like object") — the except must catch it and
    # return the safe fallback rather than propagate.
    out = m._redact_error_detail(b"password: hunter2")
    assert out == "(error detail withheld — could not be safely redacted)"


# ── _run_one_diff -> _cancel_futs(): a real queued future gets cancelled ──

APP = "pv-cancel-a-ms"
REG = "registry.example.com"
CHART = "appspace-ms"


@pytest.mark.realtime
def test_run_one_diff_timeout_cancels_a_still_queued_future(monkeypatch):
    monkeypatch.setitem(m._app_chart_map, APP, CHART)
    monkeypatch.setitem(m._app_chart_revision_map, APP, "2603.0.1")
    monkeypatch.setitem(m._app_chart_registry_map, APP, REG)
    monkeypatch.setitem(m._app_value_files_map, APP,
                        ["$config/gcp/dev/x/pv-cancel-a/customer.yaml"])
    monkeypatch.setitem(m._app_namespace_map, APP, "pv-cancel-a")
    with m._main_render_lock:
        m._main_render_cache.clear()

    # Bypass real helm entirely: chart "pull" is a no-op path string.
    monkeypatch.setattr(m, "_ensure_chart", lambda registry, chart, version: "/fake/chart")
    monkeypatch.setattr(m, "_fetch_value_files",
                        lambda value_files, sha: {vf: "appspace: {}\n" for vf in value_files})

    # A single-worker pool: the PR-side render occupies the only worker and
    # blocks past DIFF_TIMEOUT; the main-side render submitted right after
    # it must sit PENDING (genuinely cancellable) the whole time.
    small_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(concurrency, "_subtask_pool", small_pool)
    monkeypatch.setattr(m, "DIFF_TIMEOUT", 0.3)

    def slow_template(chart_path, release, namespace, value_files_content):
        import time as _t
        _t.sleep(2.0)   # well past DIFF_TIMEOUT
        return "kind: ConfigMap\n", None

    monkeypatch.setattr(m, "_helm_template", slow_template)

    before = m._diff_stats["futures_cancelled"]
    try:
        out = m._run_one_diff(APP, "prsha0000009", "mainsha00009")
        assert out[1] == m.REASON_TIMEOUT
        assert m._diff_stats["futures_cancelled"] > before, \
            "the still-queued main-side render future must have been cancelled"
    finally:
        small_pool.shutdown(wait=False, cancel_futures=True)
        with m._main_render_lock:
            m._main_render_cache.clear()
