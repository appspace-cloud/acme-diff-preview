"""Coverage campaign, pass I: the deep-branch sweep.

Exhaustive gap analysis (coverage.py analysis2, per-function) after pass H
left 236 uncovered statements. This file works through the ones that are
real logic behind reachable seams — error classification, cache eviction,
fallback paths, status-string derivation — using only the techniques the
suite already relies on (monkeypatch at module boundaries, the fake helm
binary, the scripted process_pr world, a local HTTP stub). Nothing here
asserts third-party behavior.
"""
import json
import os
import sys
import threading
import time
import urllib.error

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m  # noqa: E402
import logsink

from test_coverage_orchestration import world, _mk_pr, PATH_MAP, BASE_SHA  # noqa: E402,F401
from test_coverage_helm_layer import (  # noqa: E402
    _mk_fake_helm, _calls, helm_world, diff_world, _values_by_sha, APP, REG, CHART)


# ── small pure branches ──────────────────────────────────────────────────

def test_result_coerces_legacy_tuples():
    r = m._result(("===== Deployment/x =====\n+ a\n", True, None))
    assert r.outcome == m.OUT_DIFF and r.n_res == 1
    r = m._result(("", False, "some error"))
    assert r.outcome == m.OUT_INDETERMINATE and r.error == "some error"


def test_bound_vf_cache_evicts_oldest_half(monkeypatch):
    monkeypatch.setattr(m, "VF_CACHE_MAX", 4)
    with m._vf_cache_lock:
        saved = dict(m._vf_cache)
        m._vf_cache.clear()
        for i in range(10):
            m._vf_cache[("sha", f"f{i}.yaml")] = "x"
    m._bound_vf_cache()
    with m._vf_cache_lock:
        size = len(m._vf_cache)
        newest_kept = ("sha", "f9.yaml") in m._vf_cache
        m._vf_cache.clear()
        m._vf_cache.update(saved)
    assert size <= 4 and newest_kept, "eviction must drop the OLDEST half"


def test_handle_sigterm_sets_shutdown_flag():
    saved = m._shutdown
    try:
        m._handle_sigterm(15, None)
        assert m._shutdown is True
    finally:
        m._shutdown = saved


def test_filter_diff_sections_drops_ignored_and_checksum_only():
    sections = [
        ("apps/Deployment micro-versions-info", "+ real change\n"),
        ("apps/Deployment webx", "-   checksum/config: aaa\n+   checksum/config: bbb\n"),
        ("apps/Deployment keeper", "+ replicas: 3\n"),
    ]
    out = m._filter_diff_sections(sections)
    assert [h for h, _ in out] == ["apps/Deployment keeper"]


def test_debug_logs_only_in_debug_mode(monkeypatch):
    logs = []
    monkeypatch.setattr(logsink, "log", lambda msg, *a, **k: logs.append(msg))
    monkeypatch.setattr(m, "DEBUG", True)
    m.debug("visible now")
    assert logs == ["visible now"]


def test_argocd_subprocess_env_carries_token(monkeypatch):
    monkeypatch.setattr(m, "_argocd_token", "tok-xyz", raising=False)
    env = m._argocd_subprocess_env()
    assert env["ARGOCD_AUTH_TOKEN"] == "tok-xyz"


def test_post_build_status_swallows_bb_errors(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bb down")
    monkeypatch.setattr(m, "bb", boom)
    logs = []
    monkeypatch.setattr(logsink, "log", lambda msg, *a, **k: logs.append(str(msg)))
    m.post_build_status("a" * 40, "SUCCESSFUL", "desc", pr_id=1)  # must not raise
    assert any("failed to set" in l for l in logs)


def test_bb_fetch_status_maps_non_404_errors_to_bb_error(monkeypatch):
    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 500, "server error", None, None)
    monkeypatch.setattr(m, "bb", boom)
    content, status = m._bb_fetch_status("path/x.yaml", "sha1")
    assert content is None and status == m.BB_ERROR


def test_invalidate_for_republish_clears_pull_locks(monkeypatch):
    key = f"{REG}/{CHART}:7.7.7"
    with m._helm_pull_locks_lock:
        m._helm_pull_locks[key] = threading.Lock()
    m._invalidate_for_republish(CHART, "7.7.7")
    with m._helm_pull_locks_lock:
        gone = key not in m._helm_pull_locks
    assert gone, "a republished chart:version must drop its stale pull lock"


def test_pr_chart_revision_branches(monkeypatch):
    # No cached current revision -> immediate None.
    monkeypatch.setitem(m._app_chart_revision_map, "app-norev", "")
    assert m._pr_chart_revision("app-norev", ["f.yaml"], "sha1") is None
    # Cache hit path + a None (404-cached) file skipped + no bump found.
    monkeypatch.setitem(m._app_chart_revision_map, "app-x", "1.0.0")
    with m._vf_cache_lock:
        m._vf_cache[("sha2", "gone.yaml")] = None
        m._vf_cache[("sha2", "same.yaml")] = "appspace:\n  version: 1.0.0\n"
    try:
        out = m._pr_chart_revision("app-x", ["gone.yaml", "same.yaml"], "sha2")
        assert out is None, "an unchanged version is not a bump"
    finally:
        with m._vf_cache_lock:
            m._vf_cache.pop(("sha2", "gone.yaml"), None)
            m._vf_cache.pop(("sha2", "same.yaml"), None)


# ── fix_stuck_inprogress: every state/desc derivation string ─────────────

@pytest.fixture()
def stuck_world(monkeypatch):
    captured = []
    monkeypatch.setattr(m, "http", lambda *a, **k: {"state": "INPROGRESS"})
    monkeypatch.setattr(m, "post_build_status",
                        lambda sha, state, desc, pr_id=None, repo=None: captured.append((state, desc)))
    return captured


@pytest.mark.parametrize("comment,state,desc_frag", [
    ("New Environment(s) Detected ~12 resource(s) to create\n"
     "*ts \u2014 acme-diff-preview [clean]*",
     "SUCCESSFUL", "~12 resource(s) to create"),
    ("No ArcoCD text; No ArgoCD apps affected by this change\n"
     "*ts \u2014 acme-diff-preview [clean]*",
     "SUCCESSFUL", "No ArgoCD apps affected"),
    ("legacy text \u274c Error running diff",
     "FAILED", "Diff failed"),
    ("legacy: chart not found in OCI registry",
     "FAILED", "not found in OCI registry"),
    ("legacy: 7 resource(s) will change overall",
     "SUCCESSFUL", "7 resource(s) will change"),
    ("legacy comment with no markers at all",
     "SUCCESSFUL", "No manifest changes"),
])
def test_fix_stuck_inprogress_state_derivations(stuck_world, comment, state, desc_frag):
    m.fix_stuck_inprogress("a" * 40, 42, comment)
    assert stuck_world[-1][0] == state
    assert desc_frag in stuck_world[-1][1]


# ── upsert_comment: deleted-comment fallback path ────────────────────────

def test_upsert_comment_recreates_after_put_404(monkeypatch):
    calls = []

    def fake_bb(method, path, body=None, **kw):
        calls.append(method)
        if method == "PUT":
            raise urllib.error.HTTPError("u", 404, "gone", None, None)
        return {"id": 999}
    monkeypatch.setattr(m, "bb", fake_bb)
    m.upsert_comment(77, "body text", existing_id=123)
    assert calls == ["PUT", "POST"], "a 404 on PUT means deleted -> re-create via POST"


def test_upsert_comment_survives_fallback_post_failure(monkeypatch):
    def fake_bb(method, path, body=None, **kw):
        if method == "PUT":
            raise urllib.error.HTTPError("u", 404, "gone", None, None)
        raise RuntimeError("POST also down")
    monkeypatch.setattr(m, "bb", fake_bb)
    logs = []
    monkeypatch.setattr(logsink, "log", lambda msg, *a, **k: logs.append(str(msg)))
    m.upsert_comment(78, "body", existing_id=124)  # must not raise
    assert any("fallback POST also failed" in l for l in logs)


# ── http(): malformed Retry-After + exhausted retries ────────────────────

def test_http_malformed_retry_after_does_not_crash(monkeypatch):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    hits = {"n": 0}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits["n"] += 1
            if hits["n"] == 1:
                self.send_response(429)
                self.send_header("Retry-After", "not-a-number")
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    try:
        out = m.http("GET", f"http://127.0.0.1:{srv.server_address[1]}/x")
        assert out == {"ok": True} and hits["n"] == 2
    finally:
        srv.shutdown()


def test_http_raises_last_exception_when_retries_exhausted(monkeypatch):
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    with pytest.raises(Exception):
        m.http("GET", "http://127.0.0.1:1/unreachable")


# ── _fetch_value_files: the singleflight WAITER path ─────────────────────

def test_fetch_value_files_waiter_joins_inflight_fetch(monkeypatch):
    vf = "$config/gcp/dev/x/pv-sf-a/customer.yaml"
    clean = "gcp/dev/x/pv-sf-a/customer.yaml"
    sha = "sfsha0000001"
    evt = threading.Event()
    with m._vf_cache_lock:
        m._vf_inflight[(sha, clean)] = evt
    result = {}

    def waiter():
        result.update(m._fetch_value_files([vf], sha))

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.05)  # let the waiter block on the event
    with m._vf_cache_lock:
        m._vf_cache[(sha, clean)] = "appspace: {}\n"
    evt.set()
    t.join(timeout=5)
    try:
        assert result == {vf: "appspace: {}\n"}, \
            "the waiter must receive the fetcher's cached value, not fetch again"
    finally:
        with m._vf_cache_lock:
            m._vf_cache.pop((sha, clean), None)
            m._vf_inflight.pop((sha, clean), None)


# ── argocd_diff: noise-only and retry-exhausted terminal branches ────────

def test_argocd_diff_noise_only_sections_report_no_diff(monkeypatch):
    noise = ("===== apps/Deployment webx =====\n"
             "-   checksum/config: aaa\n+   checksum/config: bbb\n")
    monkeypatch.setattr(m, "_run_one_diff", lambda *a, **k: (noise, None, None, None))
    r = m.argocd_diff("app-n", "p" * 12, "m" * 12)
    assert r.outcome == m.OUT_NO_DIFF and r.reason == "noise_only"


def test_argocd_diff_retryable_reason_exhausts_to_indeterminate(monkeypatch):
    monkeypatch.setattr(m, "DIFF_RETRIES", 2)
    monkeypatch.setattr(m, "_diff_backoff", lambda a: 0.0)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    monkeypatch.setattr(m, "_run_one_diff",
                        lambda *a, **k: (None, m.REASON_RENDER, "persistent failure"))
    r = m.argocd_diff("app-r", "p" * 12, "m" * 12)
    assert r.outcome == m.OUT_INDETERMINATE and r.error == "persistent failure"


# ── _run_one_diff: deep error branches (fake helm from pass H) ───────────

def test_run_one_diff_pull_timeout(diff_world, monkeypatch):
    helm, _ = _mk_fake_helm(diff_world, pull_sleep=2)
    monkeypatch.setattr(m, "HELM_BIN", helm)
    monkeypatch.setattr(m, "DIFF_TIMEOUT", 1)
    monkeypatch.setattr(m, "_fetch_value_files",
                        _values_by_sha("replicas_marker: 2", "replicas_marker: 2"))
    out = m._run_one_diff(APP, "prsha0000001", "mainsha00010")
    assert out[0] is None and out[1] == m.REASON_TIMEOUT and "chart pull" in out[2]


def test_run_one_diff_render_timeout(diff_world, monkeypatch):
    helm, _ = _mk_fake_helm(diff_world, template_sleep=2)
    monkeypatch.setattr(m, "HELM_BIN", helm)
    monkeypatch.setattr(m, "DIFF_TIMEOUT", 1)
    monkeypatch.setattr(m, "_fetch_value_files",
                        _values_by_sha("replicas_marker: 2", "replicas_marker: 2"))
    out = m._run_one_diff(APP, "prsha0000001", "mainsha00011")
    assert out[0] is None and out[1] == m.REASON_TIMEOUT and "render" in out[2]


def test_run_one_diff_unwraps_chained_oci_not_found(diff_world, monkeypatch):
    def raiser(*a, **k):
        try:
            raise m.OciChartNotFound("chart x:1 not found")
        except m.OciChartNotFound as inner:
            raise RuntimeError("executor wrapper") from inner
    monkeypatch.setattr(m, "_ensure_chart", raiser)
    out = m._run_one_diff(APP, "prsha0000001", "mainsha00012")
    assert out[1] == m.REASON_OCI_NOT_FOUND and "not found" in out[2]


def test_run_one_diff_generic_pull_exception_is_oci_pull(diff_world, monkeypatch):
    def raiser(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(m, "_ensure_chart", raiser)
    out = m._run_one_diff(APP, "prsha0000001", "mainsha00013")
    assert out[1] == m.REASON_OCI_PULL and "disk full" in out[2]


def test_run_one_diff_pull_returning_none_per_side(diff_world, monkeypatch):
    # PR side None (pr_rev differs from main_rev via chart_revision)
    monkeypatch.setattr(m, "_ensure_chart",
                        lambda reg, chart, ver: None if ver == "9.9.9" else "/tmp/chart")
    out = m._run_one_diff(APP, "prsha0000001", "mainsha00014", chart_revision="9.9.9")
    assert out[1] == m.REASON_OCI_PULL and "9.9.9" in out[2]
    # Main side None
    monkeypatch.setattr(m, "_ensure_chart",
                        lambda reg, chart, ver: "/tmp/chart" if ver == "9.9.9" else None)
    out = m._run_one_diff(APP, "prsha0000001", "mainsha00015", chart_revision="9.9.9")
    assert out[1] == m.REASON_OCI_PULL and "2603.0.1" in out[2]


def test_run_one_diff_folder_move_value_fetch_failure(diff_world, monkeypatch):
    monkeypatch.setattr(m, "_detect_env_move",
                        lambda *a, **k: ("gcp/dev/x/pv-helm-a", "gcp/dev/y/pv-helm-a"))
    monkeypatch.setattr(m, "_rebase_value_files",
                        lambda vfs, old, new: [v.replace(old, new) for v in vfs])

    def boom(files, sha):
        raise RuntimeError("bitbucket 500")
    monkeypatch.setattr(m, "_fetch_value_files", boom)
    out = m._run_one_diff(APP, "prsha0000001", "mainsha00016",
                          renames={"a": "b"})
    assert out[1] == m.REASON_UNEXPECTED and "folder move" in out[2]


def test_run_one_diff_folder_move_empty_values_is_render_failure(diff_world, monkeypatch):
    monkeypatch.setattr(m, "_detect_env_move",
                        lambda *a, **k: ("gcp/dev/x/pv-helm-a", "gcp/dev/y/pv-helm-a"))
    monkeypatch.setattr(m, "_rebase_value_files", lambda vfs, old, new: vfs)
    monkeypatch.setattr(m, "_fetch_value_files", lambda files, sha: {})
    out = m._run_one_diff(APP, "prsha0000001", "mainsha00017", renames={"a": "b"})
    assert out[1] == m.REASON_RENDER and "moved location" in out[2]


def test_run_one_diff_main_side_render_failure(diff_world, monkeypatch):
    helm, _ = _mk_fake_helm(diff_world)
    monkeypatch.setattr(m, "HELM_BIN", helm)
    monkeypatch.setattr(m, "_fetch_value_files",
                        _values_by_sha("replicas_marker: 2", "MARKER_BOOM: 1"))
    out = m._run_one_diff(APP, "prsha0000001", "mainsha00018")
    assert out[0] is None and out[1] == m.REASON_RENDER


def test_run_one_diff_prunes_main_render_cache(diff_world, monkeypatch):
    helm, _ = _mk_fake_helm(diff_world)
    monkeypatch.setattr(m, "HELM_BIN", helm)
    monkeypatch.setattr(m, "MAIN_RENDER_CACHE_MAX", 1)
    monkeypatch.setattr(m, "_fetch_value_files",
                        _values_by_sha("replicas_marker: 2", "replicas_marker: 2"))
    m._run_one_diff(APP, "prsha0000001", "mainsha00019")
    m._run_one_diff(APP, "prsha0000001", "mainsha00020")
    with m._main_render_lock:
        size = len(m._main_render_cache)
    assert size <= 1, "the main-render cache must honor its cap"


def test_run_one_diff_generic_value_fetch_exception(diff_world, monkeypatch):
    helm, _ = _mk_fake_helm(diff_world)
    monkeypatch.setattr(m, "HELM_BIN", helm)

    def boom(files, sha):
        raise ValueError("unexpected parse explosion")
    monkeypatch.setattr(m, "_fetch_value_files", boom)
    out = m._run_one_diff(APP, "prsha0000001", "mainsha00021")
    assert out[1] == m.REASON_RENDER and "explosion" in out[2]


VF = "$config/gcp/dev/x/pv-helm-a/customer.yaml"
VF_CLEAN = "gcp/dev/x/pv-helm-a/customer.yaml"
VF_NEW = "gcp/dev/y/pv-helm-a/customer.yaml"


def test_run_one_diff_follows_trusted_rename_in_changed_paths(diff_world, monkeypatch):
    helm, _ = _mk_fake_helm(diff_world)
    monkeypatch.setattr(m, "HELM_BIN", helm)
    # This exercises the per-FILE rename follow inside the changed_paths
    # split — not the whole-folder move path, so neutralize that detector.
    monkeypatch.setattr(m, "_detect_env_move", lambda *a, **k: None)
    monkeypatch.setattr(m, "_trusted_rename_dirs", lambda *a, **k: {"x"})
    monkeypatch.setattr(m, "_is_trusted_rename", lambda *a, **k: True)

    def fake_fetch(files, sha):
        if files == [VF_NEW]:                       # the followed rename target
            return {VF_NEW: "appspace: {}\nreplicas_marker: 3\n"}
        if sha == "prsha0000001":                    # changed file 404s at pr sha
            return {}
        return {vf: "appspace: {}\nreplicas_marker: 2\n" for vf in files}
    monkeypatch.setattr(m, "_fetch_value_files", fake_fetch)

    out = m._run_one_diff(APP, "prsha0000001", "mainsha00022",
                          changed_paths=[VF_CLEAN],
                          renames={VF_CLEAN: VF_NEW})
    diff_text, reason = out[0], out[1]
    assert reason is None
    assert "replicas: 2" in diff_text and "replicas: 3" in diff_text, \
        "the renamed file's overrides must reach the PR-side render"


def test_run_one_diff_unchanged_files_reuse_main_fetch(diff_world, monkeypatch):
    helm, _ = _mk_fake_helm(diff_world)
    monkeypatch.setattr(m, "HELM_BIN", helm)
    fetches = []

    def fake_fetch(files, sha):
        fetches.append((tuple(files), sha))
        return {vf: "appspace: {}\nreplicas_marker: 2\n" for vf in files}
    monkeypatch.setattr(m, "_fetch_value_files", fake_fetch)

    # changed_paths touches an unrelated file -> the app's own value file is
    # unchanged and must be fetched ONCE (at main sha) and reused for PR side.
    out = m._run_one_diff(APP, "prsha0000001", "mainsha00023",
                          changed_paths=["gcp/dev/x/other-env/config.yaml"])
    assert out[1] is None and out[0] == ""
    pr_side_fetches = [f for f in fetches if f[1] == "prsha0000001" and f[0]]
    assert pr_side_fetches == [], \
        "an unchanged value file must never be re-fetched at the PR sha"


# ── _ensure_chart: dev-registry TTL eviction and stale-dir parking ───────

DEV_REG = "helm-oci-dev.repo.example.com"


def test_ensure_chart_dev_memory_cache_expires_and_repulls(helm_world, monkeypatch):
    helm, count = _mk_fake_helm(helm_world)
    monkeypatch.setattr(m, "HELM_BIN", helm)
    path1 = m._ensure_chart(DEV_REG, CHART, "1.0.0-dev")
    assert path1 and _calls(count, "pull") == 1
    # Age the pull past the dev TTL: memory hit is now stale -> evict + re-pull.
    key = f"{DEV_REG}/{CHART}:1.0.0-dev"
    m._helm_chart_pull_ts[key] = time.monotonic() - m.DEV_CHART_TTL - 5
    path2 = m._ensure_chart(DEV_REG, CHART, "1.0.0-dev")
    assert path2 and _calls(count, "pull") == 2, \
        "a dev tag past its TTL must be re-pulled (mutable tags republish)"


def test_ensure_chart_dev_stale_disk_dir_is_parked_not_deleted(helm_world, monkeypatch):
    helm, count = _mk_fake_helm(helm_world)
    monkeypatch.setattr(m, "HELM_BIN", helm)
    m._ensure_chart(DEV_REG, CHART, "2.0.0-dev")
    key = f"{DEV_REG}/{CHART}:2.0.0-dev"
    # Simulate a later iteration: memory cache empty, disk dir present, pull old.
    with m._helm_cache_lock:
        m._helm_chart_cache.clear()
    with m._helm_pull_locks_lock:
        m._helm_pull_locks.clear()
    m._helm_chart_pull_ts[key] = time.monotonic() - m.DEV_CHART_TTL - 5
    path = m._ensure_chart(DEV_REG, CHART, "2.0.0-dev")
    assert path and _calls(count, "pull") == 2
    parent = os.path.join(m.HELM_CACHE_DIR, DEV_REG, CHART)
    parked = [d for d in os.listdir(parent) if ".stale-" in d]
    assert parked, ("the stale build must be PARKED aside (in-flight renders may "
                    "still hold paths into it), not rmtree'd from under them")


def test_ensure_chart_lock_recheck_finds_disk_filled_by_concurrent_thread(helm_world, monkeypatch):
    # A second thread waiting on the per-key pull lock must, after acquiring
    # it, notice the first thread already landed the chart on disk and reuse
    # it instead of pulling again.
    helm, count = _mk_fake_helm(helm_world)
    monkeypatch.setattr(m, "HELM_BIN", helm)
    key = f"{REG}/{CHART}:6.0.0"
    held = threading.Lock()
    held.acquire()
    with m._helm_pull_locks_lock:
        m._helm_pull_locks[key] = held
    result = {}

    def runner():
        result["path"] = m._ensure_chart(REG, CHART, "6.0.0")

    t = threading.Thread(target=runner)
    t.start()
    time.sleep(0.1)  # runner is now blocked on the held pull lock
    # "The other thread" lands the chart on disk + records the pull.
    disk = os.path.join(m.HELM_CACHE_DIR, REG, CHART, "6.0.0", CHART)
    os.makedirs(disk)
    open(os.path.join(disk, "Chart.yaml"), "w").write("apiVersion: v2\n")
    m._helm_chart_pull_ts[key] = time.monotonic()
    held.release()
    t.join(timeout=10)
    assert result["path"] == disk
    assert _calls(count, "pull") == 0, "the waiter must reuse the concurrent pull"


# ── _render_new_env_diff ─────────────────────────────────────────────────

ENV_INFO = {"name": "pv-newenv-a",
            "config_file": "gcp/dev/x/pv-newenv-a/customer.yaml",
            "env_dir": "gcp/dev/x/pv-newenv-a",
            "all_yaml_files": ["gcp/dev/x/pv-newenv-a/customer.yaml"]}


def test_render_new_env_config_fetch_failure(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_status", lambda p, s: (None, m.BB_ERROR))
    text, err, n, ver = m._render_new_env_diff(dict(ENV_INFO), "prsha")
    assert text is None and "could not fetch" in err and ver is None


def test_render_new_env_missing_version(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda p, s: ("appspace:\n  customerName: x\n", m.BB_OK))
    text, err, n, ver = m._render_new_env_diff(dict(ENV_INFO), "prsha")
    assert text is None and "no appspace.version" in err


def _cfg_ok(version="2603.0.1-dev"):
    return lambda p, s: (f"appspace:\n  version: {version}\n", m.BB_OK)


def test_render_new_env_chart_not_found(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_status", _cfg_ok())

    def nf(*a, **k):
        raise m.OciChartNotFound("boom 404")
    monkeypatch.setattr(m, "_ensure_chart", nf)
    text, err, n, ver = m._render_new_env_diff(dict(ENV_INFO), "prsha")
    assert text is None and "chart not found in OCI" in err and ver == "2603.0.1-dev"


def test_render_new_env_chart_pull_exception_and_none(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_status", _cfg_ok())

    def boom(*a, **k):
        raise RuntimeError("net down")
    monkeypatch.setattr(m, "_ensure_chart", boom)
    text, err, *_ = m._render_new_env_diff(dict(ENV_INFO), "prsha")
    assert "chart pull failed" in err
    monkeypatch.setattr(m, "_ensure_chart", lambda *a, **k: None)
    text, err, *_ = m._render_new_env_diff(dict(ENV_INFO), "prsha")
    assert "returned None" in err


def test_render_new_env_value_files_fetch_failure_and_fallback_list(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_status", _cfg_ok())
    captured = {}
    monkeypatch.setattr(m, "_ensure_chart", lambda *a, **k: "/tmp/chartpath")

    def fetch(files, sha):
        captured["files"] = files
        return {}
    monkeypatch.setattr(m, "_fetch_value_files", fetch)
    info = dict(ENV_INFO, all_yaml_files=[])  # forces the config-file fallback
    text, err, *_ = m._render_new_env_diff(info, "prsha")
    assert "could not fetch value files" in err
    # COPS-2545 (F1): the value list now carries the full root-to-leaf
    # ancestor chain before the env's own file; the leaf stays last so
    # helm override order is preserved.
    assert captured["files"][-1] == f"$config/{ENV_INFO['config_file']}"
    assert captured["files"][0] == "$config/gcp/config.yaml"
    assert all(f.endswith("/config.yaml") for f in captured["files"][:-1])


def test_render_new_env_happy_path_counts_resources(helm_world, monkeypatch):
    helm, _ = _mk_fake_helm(helm_world)
    monkeypatch.setattr(m, "HELM_BIN", helm)
    monkeypatch.setattr(m, "_bb_fetch_status", _cfg_ok())
    monkeypatch.setattr(m, "_fetch_value_files",
                        lambda files, sha: {f: "replicas_marker: 4\n" for f in files})
    monkeypatch.setitem(m._app_chart_registry_map, "seed-app",
                        "helm-oci-dev.repo.appspace.com")
    text, err, n, ver = m._render_new_env_diff(dict(ENV_INFO), "prsha")
    assert err is None and n == 1 and "replicas: 4" in text
    assert ver == "2603.0.1-dev"


# ── process_pr: remaining orchestration branches ─────────────────────────

def test_process_pr_confirmed_decommission_skips_normal_diff(world, monkeypatch):
    sinks, plan = world
    cand = [{"env_name": "pv-orch-a", "identity_file": "x/customer.yaml",
             "apps": ["pv-orch-a-ms"]}]
    monkeypatch.setattr(m, "_detect_env_decommission_candidates", lambda *a, **k: cand)
    monkeypatch.setattr(m, "_evaluate_env_decommissions",
                        lambda *a, **k: (["# ENVIRONMENT DECOMMISSION",
                                          "pv-orch-a is being deleted"], ["pv-orch-a"], []))
    monkeypatch.setattr(m, "_apps_to_skip_for_decommission",
                        lambda *a, **k: {"pv-orch-a-ms"})
    m.process_pr(_mk_pr(pr_id=601), PATH_MAP, base_sha=BASE_SHA)
    assert "pv-orch-a-ms" not in sinks.diff_calls, \
        "a confirmed-decommissioned app must never enter the normal diff pipeline"
    assert sinks.upserts and "DECOMMISSION" in sinks.upserts[-1]


def test_process_pr_prewarns_charts_and_tolerates_missing_versions(world, monkeypatch, tmp_path):
    sinks, plan = world
    helm, count = _mk_fake_helm(tmp_path)
    monkeypatch.setattr(m, "HELM_BIN", helm)
    monkeypatch.setattr(m, "HELM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(m, "OCI_USER", "u")
    monkeypatch.setattr(m, "OCI_PASS", "p")
    monkeypatch.setitem(m._app_chart_registry_map, "pv-orch-a-ms", REG)
    monkeypatch.setitem(m._app_chart_registry_map, "pv-orch-a-ss", REG)
    monkeypatch.setattr(m, "_pr_chart_revision_checked",
                        lambda app, files, pr_sha, main_sha=None, renames=None:
                        ("2604.0.0-dev", False))
    with m._helm_cache_lock:
        m._helm_chart_cache.clear()
    m._helm_chart_pull_ts.clear()
    m.process_pr(_mk_pr(pr_id=602), PATH_MAP, base_sha=BASE_SHA)
    assert _calls(count, "pull") >= 2, \
        "the pre-warm must pull both the main and the bumped chart versions"


def test_process_pr_prewarm_not_found_is_warning_not_crash(world, monkeypatch, tmp_path):
    sinks, plan = world
    helm, _ = _mk_fake_helm(tmp_path, pull_mode="notfound")
    monkeypatch.setattr(m, "HELM_BIN", helm)
    monkeypatch.setattr(m, "HELM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(m, "OCI_USER", "u")
    monkeypatch.setattr(m, "OCI_PASS", "p")
    monkeypatch.setitem(m._app_chart_registry_map, "pv-orch-a-ms", REG)
    monkeypatch.setitem(m._app_chart_registry_map, "pv-orch-a-ss", REG)
    logs = []
    monkeypatch.setattr(logsink, "log", lambda msg, *a, **k: logs.append(str(msg)))
    with m._helm_cache_lock:
        m._helm_chart_cache.clear()
    m.process_pr(_mk_pr(pr_id=603), PATH_MAP, base_sha=BASE_SHA)
    assert any("not found" in l for l in logs)
    assert sinks.statuses, "the run must continue past a pre-warm not-found"


def test_process_pr_structural_plus_hard_error_desc(world, monkeypatch):
    sinks, plan = world
    monkeypatch.setattr(m, "_detect_new_env_candidates",
                        lambda *a, **k: [{"name": "pv-newenv-a"}])
    monkeypatch.setattr(m, "_evaluate_new_envs",
                        lambda *a, **k: ([], ["pv-newenv-a"], 0, []))
    plan["pv-orch-a-ms"] = m.DiffResult("", [], 0, False, "boom",
                                        m.OUT_ERROR, "unexpected")
    m.process_pr(_mk_pr(pr_id=604), PATH_MAP, base_sha=BASE_SHA)
    state, desc = sinks.statuses[-1]
    assert state == "FAILED" and "existing app diff also failed" in desc


def test_process_pr_structural_plus_invalid_config_desc(world, monkeypatch):
    sinks, plan = world
    monkeypatch.setattr(m, "_detect_new_env_candidates",
                        lambda *a, **k: [{"name": "pv-newenv-a"}])
    monkeypatch.setattr(m, "_evaluate_new_envs",
                        lambda *a, **k: ([], ["pv-newenv-a"], 0, []))
    plan["pv-orch-a-ms"] = m.DiffResult("", [], 0, False, "bad yaml",
                                        m.OUT_INDETERMINATE, m.REASON_INVALID_YAML)
    m.process_pr(_mk_pr(pr_id=605), PATH_MAP, base_sha=BASE_SHA)
    state, desc = sinks.statuses[-1]
    assert state == "FAILED" and "invalid config" in desc


def test_process_pr_structural_alone_desc(world, monkeypatch):
    sinks, plan = world
    monkeypatch.setattr(m, "_detect_new_env_candidates",
                        lambda *a, **k: [{"name": "pv-newenv-a"}])
    monkeypatch.setattr(m, "_evaluate_new_envs",
                        lambda *a, **k: ([], ["pv-newenv-a"], 0, []))
    m.process_pr(_mk_pr(pr_id=606), PATH_MAP, base_sha=BASE_SHA)
    state, desc = sinks.statuses[-1]
    assert state == "FAILED" and desc.endswith("pv-newenv-a")


def test_process_pr_hard_error_without_structural_desc(world, monkeypatch):
    sinks, plan = world
    plan["pv-orch-a-ms"] = m.DiffResult("", [], 0, False, "boom",
                                        m.OUT_ERROR, "unexpected")
    m.process_pr(_mk_pr(pr_id=607), PATH_MAP, base_sha=BASE_SHA)
    state, desc = sinks.statuses[-1]
    assert state == "FAILED" and desc == "Diff failed - check PR comment"


def test_process_pr_clean_with_new_envs_success_desc(world, monkeypatch):
    sinks, plan = world
    monkeypatch.setattr(m, "_detect_new_env_candidates",
                        lambda *a, **k: [{"name": "pv-newenv-a"}])
    monkeypatch.setattr(m, "_evaluate_new_envs",
                        lambda *a, **k: (["### New Environment(s) Detected"], [], 9, []))
    m.process_pr(_mk_pr(pr_id=608), PATH_MAP, base_sha=BASE_SHA)
    state, desc = sinks.statuses[-1]
    assert state == "SUCCESSFUL" and "new environment(s) will be created" in desc


def test_process_pr_sigterm_mid_diff_drains_without_marking_seen(world, monkeypatch):
    sinks, plan = world
    saved = m._shutdown

    def diff_and_shutdown(app, *a, **k):
        sinks.diff_calls.append(app)
        m._shutdown = True  # SIGTERM arrives while diffs are in flight
        return m.DiffResult("", [], 0, False, "", m.OUT_NO_DIFF, "")
    monkeypatch.setattr(m, "argocd_diff", diff_and_shutdown)
    try:
        m.process_pr(_mk_pr(pr_id=609), PATH_MAP, base_sha=BASE_SHA)
    finally:
        m._shutdown = saved
    assert 609 not in m._seen, \
        "a SIGTERM-drained PR must NOT be marked seen — the next pod re-diffs it"


# ── main_iteration ───────────────────────────────────────────────────────

def test_main_iteration_discovery_failure_relogs_and_returns(monkeypatch):
    monkeypatch.setattr(m, "_prune_helm_cache", lambda: None)
    monkeypatch.setattr(m, "_touch_progress", lambda: None)
    # Stale token forces the proactive JWT refresh branch first. The age must
    # be expressed RELATIVE to time.monotonic(): an absolute 0.0 only looks
    # old on a long-uptime machine (monotonic counts from boot) — on a
    # freshly booted CI runner monotonic() can be smaller than the 12h TTL,
    # silently skipping the branch. Caught live: passed on the Mac, failed
    # on the GitHub runner.
    monkeypatch.setattr(m, "_argocd_token", "tok", raising=False)
    monkeypatch.setattr(m, "_argocd_token_ts",
                        time.monotonic() - m.ARGOCD_TOKEN_TTL - 10, raising=False)
    logins = []
    monkeypatch.setattr(m, "argocd_login", lambda: logins.append(1))

    def boom():
        raise RuntimeError("argocd unreachable")
    monkeypatch.setattr(m, "discover_path_app_map", boom)
    called = []
    monkeypatch.setattr(m, "get_open_prs", lambda repo=None: called.append(1) or [])
    m.main_iteration()
    assert called == [], "a discovery failure must return before polling Bitbucket"
    assert len(logins) == 2, "proactive JWT refresh + the recovery re-login"


def test_main_iteration_jwt_refresh_failure_is_nonfatal(monkeypatch):
    monkeypatch.setattr(m, "_prune_helm_cache", lambda: None)
    monkeypatch.setattr(m, "_touch_progress", lambda: None)
    monkeypatch.setattr(m, "_argocd_token", "tok", raising=False)
    # Relative age, same uptime caveat as the test above.
    monkeypatch.setattr(m, "_argocd_token_ts",
                        time.monotonic() - m.ARGOCD_TOKEN_TTL - 10, raising=False)
    calls = {"n": 0}

    def flaky_login():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("session api hiccup")
    monkeypatch.setattr(m, "argocd_login", flaky_login)
    monkeypatch.setattr(m, "discover_path_app_map", lambda: PATH_MAP)
    monkeypatch.setattr(m, "http", lambda *a, **k: {"target": {"hash": BASE_SHA}})
    monkeypatch.setattr(m, "get_open_prs", lambda repo=None: [])
    m.main_iteration()  # must not raise


def test_main_iteration_prunes_stale_state_and_logs_rollup(monkeypatch):
    monkeypatch.setattr(m, "_prune_helm_cache", lambda: None)
    monkeypatch.setattr(m, "_touch_progress", lambda: None)
    monkeypatch.setattr(m, "_argocd_token", "", raising=False)
    monkeypatch.setattr(m, "discover_path_app_map", lambda: PATH_MAP)
    monkeypatch.setattr(m, "http", lambda *a, **k: {"target": {"hash": BASE_SHA}})
    pr = _mk_pr(pr_id=610)
    monkeypatch.setattr(m, "get_open_prs", lambda repo=None: [pr])
    monkeypatch.setattr(m, "process_pr",
                        lambda p, pm, base_sha=None, repo=None: {m.OUT_DIFF: 2, m.OUT_INDETERMINATE: 1})
    with m._seen_lock:
        m._seen[("acme-config-dev", 99999)] = ("dead", "dead")  # a PR that is no longer open
    with m._comment_id_cache_lock:
        m._comment_id_cache[("acme-config-dev", 99999)] = 1
    logs = []
    monkeypatch.setattr(logsink, "log", lambda msg, *a, **k: logs.append(str(msg)))
    m.main_iteration()
    with m._seen_lock:
        pruned = ("acme-config-dev", 99999) not in m._seen
    with m._comment_id_cache_lock:
        pruned_c = ("acme-config-dev", 99999) not in m._comment_id_cache
    assert pruned and pruned_c, "state for closed PRs must be evicted"
    assert any("diff outcomes" in l and "could not be computed" in l for l in logs)


# ── format_comment: remaining rows and status combinations ───────────────

def _dr(outcome, reason="x", n=0, text="", error=None):
    secs = m.parse_diff_sections(text) if text else []
    return m.DiffResult(text, secs, n, outcome == m.OUT_DIFF, error, outcome, reason)


def test_format_comment_large_mode_rows_for_every_outcome(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda *a, **k: None)
    results = {f"chg-{i}": _dr(m.OUT_DIFF, n=1,
                               text="===== /v1/ConfigMap ns/x =====\n+ a\n")
               for i in range(6)}  # > LARGE_PR_APP_THRESHOLD -> table mode
    results["decom-app"] = _dr(m.OUT_DECOMMISSIONED, "confirmed_decommission")
    results["indet-app"] = _dr(m.OUT_INDETERMINATE, m.REASON_RENDER, error="e")
    results["error-app"] = _dr(m.OUT_ERROR, "unexpected", error="crash")
    body = m.format_comment("a" * 40, results, base_sha="b" * 40)
    assert "decommissioned" in body
    assert "diff unavailable" in body
    assert "| `error-app` | \u274c error" in body


def test_format_comment_small_mode_error_block_and_status(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda *a, **k: None)
    results = {"error-app": _dr(m.OUT_ERROR, "unexpected", error="stack overflow in x")}
    body = m.format_comment("a" * 40, results, base_sha="b" * 40)
    assert "error: stack overflow in x" in body
    assert "\u274c Error running diff" in body


def test_format_comment_error_plus_structural_combined_status(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda *a, **k: None)
    results = {"error-app": _dr(m.OUT_ERROR, "unexpected", error="boom")}
    body = m.format_comment("a" * 40, results, base_sha="b" * 40,
                            new_env_lines=["### New Environment(s) Detected"],
                            new_env_structural=True,
                            new_env_desc="new env pv-x-a has a structural problem")
    assert "Error running diff, AND new env pv-x-a" in body


def test_format_comment_includes_ai_summary_when_available(monkeypatch):
    # COPS-2612 moved the AI Analysis block to the full-diff page only. It is
    # model output that partly restates the deterministic merge summary, and
    # the comment is now a decision summary where the deterministic narrative
    # is the one that belongs. It is still generated and still rendered, on
    # the page, so this test follows it there rather than being deleted.
    monkeypatch.setattr(m, "generate_ai_summary",
                        lambda *a, **k: "**1 app(s) updated**\n- synthetic AI line")
    results = {"chg": _dr(m.OUT_DIFF, n=1,
                          text="===== /v1/ConfigMap ns/x =====\n+ a\n")}
    body = m.format_comment("a" * 40, results, base_sha="b" * 40)
    assert "synthetic AI line" not in body
    page = m.format_comment("a" * 40, results, base_sha="b" * 40,
                            profile=m.FULL_PROFILE)
    assert "synthetic AI line" in page
