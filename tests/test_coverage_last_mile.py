"""Coverage campaign, pass J: the last mile (96.9% -> ~99%).

A quick re-audit after pass I showed ~50 of the 76 remaining lines were
not race-dependent at all — just ordinary error branches left behind once
the 90% goal was passed, plus three lines whose earlier tests passed
through SIBLING branches (an HTTPError where the uncovered arm wanted a
generic exception; a permanent reason where the uncovered arm wanted a
retryable one). Same seams as every previous pass. What stays out, still:
OS-level failure injection (rename EBUSY), hardcoded 60s/30s waits, and
the post-loop defensive raises.
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m  # noqa: E402
import logsink

from test_coverage_orchestration import world, _mk_pr, PATH_MAP, BASE_SHA  # noqa: E402,F401
from test_coverage_helm_layer import _mk_fake_helm, _calls, helm_world  # noqa: E402,F401


# ── branches whose earlier tests passed through a SIBLING arm ────────────

def test_bb_fetch_status_generic_exception_after_retries_is_bb_error(monkeypatch):
    # The HTTPError arm was covered; this is the plain-Exception arm
    # (network/timeout) that retries twice and then reports BB_ERROR.
    # NOTE: _bb_fetch_status deliberately uses urllib directly (bb()/http()
    # json-decode the body, which breaks on raw YAML) — so the seam to stub
    # is urllib.request.urlopen, not bb().
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError("connection reset")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    content, status = m._bb_fetch_status("path/x.yaml", "a" * 40)
    assert content is None and status == m.BB_ERROR
    assert calls["n"] == 3, "a network error must retry twice before giving up"


def test_argocd_diff_retryable_timeout_exhausts_the_loop(monkeypatch):
    # REASON_RENDER exits through the permanent-reason arm; a RETRYABLE
    # reason that never recovers must fall out of the loop's bottom.
    monkeypatch.setattr(m, "DIFF_RETRIES", 2)
    monkeypatch.setattr(m, "_diff_backoff", lambda a: 0.0)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    monkeypatch.setattr(m, "_run_one_diff",
                        lambda *a, **k: (None, m.REASON_TIMEOUT, "still timing out"))
    r = m.argocd_diff("app-t", "p" * 12, "m" * 12)
    assert r.outcome == m.OUT_INDETERMINATE and r.error == "still timing out"


def test_parse_diff_sections_flushes_on_the_next_header():
    text = ("===== apps/Deployment a =====\n+ x\n"
            "===== apps/Deployment b =====\n+ y\n")
    secs = m.parse_diff_sections(text)
    assert [h for h, _ in secs] == ["apps/Deployment a", "apps/Deployment b"]
    assert secs[0][1] == "+ x\n", "the first section must flush when the second header arrives"


# ── find_existing_comment: both raise contracts ──────────────────────────

def test_find_existing_comment_fast_path_transient_error_raises(monkeypatch):
    with m._comment_id_cache_lock:
        m._comment_id_cache[7701] = 4242

    def boom(method, path, **k):
        raise urllib.error.HTTPError("u", 500, "bb down", None, None)
    monkeypatch.setattr(m, "bb", boom)
    try:
        with pytest.raises(urllib.error.HTTPError):
            m.find_existing_comment(7701)
    finally:
        with m._comment_id_cache_lock:
            m._comment_id_cache.pop(7701, None)


def test_find_existing_comment_page_scan_error_raises_to_skip_the_pr(monkeypatch):
    # No cached id -> full scan; a transient error must RAISE so process_pr
    # skips the PR instead of posting a duplicate comment.
    def boom(method, path, **k):
        raise RuntimeError("bitbucket 502")
    monkeypatch.setattr(m, "bb", boom)
    with pytest.raises(RuntimeError):
        m.find_existing_comment(7702)


# ── JFrog hard refresh: malformed list, per-app failure counting ─────────

def test_jfrog_hard_refresh_malformed_app_list_and_failure_count(tmp_path, monkeypatch):
    import stat as stat_mod
    # Fake argocd whose `app list` emits garbage -> the JSON guard returns.
    bad = tmp_path / "argocd_bad"
    bad.write_text('#!/bin/bash\ncase "$*" in *"app list"*) printf "not json"; exit 0;; *) exit 0;; esac\n')
    bad.chmod(bad.stat().st_mode | stat_mod.S_IEXEC)
    logs = []
    monkeypatch.setattr(logsink, "log", lambda msg, *a, **k: logs.append(str(msg)))
    monkeypatch.setattr(m, "ARGOCD_BIN", str(bad))
    m._jfrog_hard_refresh("some-chart", "1.0.0")
    assert any("malformed app list JSON" in l for l in logs)

    # Now a list with one matching app whose hard-refresh FAILS -> failed += 1.
    good = tmp_path / "argocd_fail"
    good.write_text(
        '#!/bin/bash\n'
        'case "$*" in\n'
        '  *"app list"*) printf \'[{"metadata":{"name":"pv-x-a-ms"},"spec":{"sources":[{"chart":"some-chart","targetRevision":"1.0.0"}]}}]\'; exit 0;;\n'
        '  *"--hard-refresh"*) echo "refresh exploded" >&2; exit 1;;\n'
        '  *) exit 0;;\n'
        'esac\n')
    good.chmod(good.stat().st_mode | stat_mod.S_IEXEC)
    logs.clear()
    monkeypatch.setattr(m, "ARGOCD_BIN", str(good))
    m._jfrog_hard_refresh("some-chart", "1.0.0")
    assert any("hard-refresh FAILED" in l for l in logs)
    assert any("1 failed" in l or "failed" in l for l in logs)


# ── health server: degraded /healthz and the ancient-entry prune ─────────

@pytest.fixture()
def health(monkeypatch):
    monkeypatch.setattr(m, "_jfrog_hard_refresh", lambda name, ver: None)
    srv = m._start_health_server(0)
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _req(url, method="GET", body=None, headers=None):
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_healthz_alive_but_poll_failing_reports_degraded(health, monkeypatch):
    monkeypatch.setattr(m, "_last_ok", time.monotonic(), raising=False)
    monkeypatch.setattr(m, "_last_poll_ok", False, raising=False)
    monkeypatch.setattr(m, "_consecutive_poll_fails", 3, raising=False)
    code, body = _req(f"{health}/healthz")
    assert code == 200 and b"degraded" in body and b"poll_fails=3" in body


def test_jfrog_webhook_prunes_entries_far_outside_the_dedup_window(health, monkeypatch):
    import hashlib
    import hmac as hmac_mod
    monkeypatch.setattr(m, "JFROG_WEBHOOK_SECRET", "jfsecret")
    monkeypatch.setattr(m, "_invalidate_for_republish", lambda *a, **k: None)
    monkeypatch.setattr(m, "_jfrog_refresh_pool",
                        type("P", (), {"submit": staticmethod(lambda fn, *a: None)})())
    m._jfrog_recent["ancient-chart:0.0.1"] = time.monotonic() - m.JFROG_DEDUP_WINDOW * 200
    payload = json.dumps({"event_type": "pushed",
                          "data": {"image_name": "prune-chart", "tag": "1.0.0"}}).encode()
    sig = hmac_mod.new(b"jfsecret", payload, hashlib.sha256).hexdigest()
    code, _ = _req(f"{health}/jfrog-webhook", "POST", payload, {"X-JFrog-Event-Auth": sig})
    assert code == 202
    try:
        assert "ancient-chart:0.0.1" not in m._jfrog_recent, \
            "entries far outside the dedup window must be pruned"
    finally:
        m._jfrog_recent.pop("prune-chart:1.0.0", None)


# ── pure parsers: odd YAML documents and redaction passthroughs ──────────

def test_parse_manifest_resources_skips_odd_documents():
    manifest = "\n".join([
        "# just a comment doc",
        "---",
        "",                       # empty doc
        "---",
        "apiVersion: v1",         # apiVersion but no kind/name -> debug skip
        "data: {}",
        "---",
        "apiVersion: apps/v1",
        "kind: Deployment",
        "metadata:",
        "  name: real-one",
        "  namespace: ns",
        "---",
        "kind: ConfigMap",        # kind but no name -> also unidentifiable
        "data: {}",
    ])
    res = m._parse_manifest_resources(manifest)
    assert len(res) == 1 and any("real-one" in str(k) for k in res), \
        f"only the identifiable resource may survive: {list(res)}"


def test_redact_secret_section_passes_non_matching_lines_through():
    text = ("stringData:\n"
            "  password: hunter2\n"
            "just a plain line without a colon\n"
            "  another indented data line\n")
    out = m._redact_secret_section(text)
    assert "hunter2" not in out
    assert "just a plain line without a colon" in out, \
        "non key:value lines must pass through the section untouched"


def test_redact_k8s_env_pairs_passes_non_env_lines_through():
    text = ("- name: DB_PASSWORD\n"
            "  value: supersecret\n"
            "a completely unrelated line\n")
    out = m._redact_k8s_env_pairs(text)
    assert "supersecret" not in out
    assert "a completely unrelated line" in out


def test_pr_chart_revision_checked_branches(monkeypatch):
    monkeypatch.setitem(m._app_chart_revision_map, "app-norev2", "")
    assert m._pr_chart_revision_checked("app-norev2", ["f.yaml"], "s" * 12) == (None, False)
    monkeypatch.setitem(m._app_chart_revision_map, "app-c2", "1.0.0")
    with m._vf_cache_lock:
        m._vf_cache[("t" * 12, "same.yaml")] = "appspace:\n  version: 1.0.0\n"
        m._vf_cache[("u" * 12, "same.yaml")] = "appspace:\n  version: 1.0.0\n"
    try:
        out = m._pr_chart_revision_checked("app-c2", ["same.yaml"], "t" * 12,
                                           main_sha="u" * 12)
        assert out == (None, False), "an unchanged version is not a bump and not invalid"
    finally:
        with m._vf_cache_lock:
            m._vf_cache.pop(("t" * 12, "same.yaml"), None)
            m._vf_cache.pop(("u" * 12, "same.yaml"), None)


def test_evaluate_new_envs_truncates_long_file_lists_and_shows_error_detail(monkeypatch):
    files = [f"gcp/dev/x/pv-newenv-a/f{i}.yaml" for i in range(20)]
    cand = [{"name": "pv-newenv-a", "config_file": files[0],
             "env_dir": "gcp/dev/x/pv-newenv-a", "all_yaml_files": files}]
    monkeypatch.setattr(m, "_render_new_env_diff",
                        lambda info, sha: (None, "render exploded badly " * 10, 0, "1.0.0"))
    lines, structural, total = m._evaluate_new_envs(cand, "p" * 12)
    joined = "\n".join(lines)
    assert "more files" in joined, "a 20-file env must truncate its file list"
    assert "render exploded" in joined, "the render error must surface in the output"


def test_render_main_side_resources_pull_and_render_failures_raise(monkeypatch):
    monkeypatch.setitem(m._app_chart_map, "pv-lm-a-ms", "appspace-ms")
    monkeypatch.setitem(m._app_chart_revision_map, "pv-lm-a-ms", "1.0.0")
    monkeypatch.setitem(m._app_chart_registry_map, "pv-lm-a-ms", "reg.example.com")
    monkeypatch.setitem(m._app_value_files_map, "pv-lm-a-ms", ["$config/x/customer.yaml"])
    monkeypatch.setitem(m._app_namespace_map, "pv-lm-a-ms", "pv-lm-a")
    monkeypatch.setattr(m, "_fetch_value_files", lambda f, s: {v: "a: 1\n" for v in f})
    with m._main_render_lock:
        m._main_render_cache.clear()
    monkeypatch.setattr(m, "_ensure_chart", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="chart pull failed"):
        m._render_main_side_resources("pv-lm-a-ms", "m" * 12)
    monkeypatch.setattr(m, "_ensure_chart", lambda *a, **k: "/tmp/chartpath")
    monkeypatch.setattr(m, "_helm_template", lambda *a, **k: (None, "template exploded"))
    with pytest.raises(RuntimeError, match="template exploded"):
        m._render_main_side_resources("pv-lm-a-ms", "n" * 12)


def test_generate_ai_summary_trims_oversized_diffs_and_logs_max_tokens(monkeypatch):
    monkeypatch.setattr(m, "AI_MAX_BODY_CHARS", 40)
    seen = {}

    def fake_http(method, url, **kw):
        if "metadata.google.internal" in url:
            return {"access_token": "tok", "expires_in": 3600}
        seen["prompt"] = json.dumps(kw.get("body", ""))
        return {"candidates": [{"content": {"parts": [{"text": "**1 app(s) updated**"}]},
                                "finishReason": "MAX_TOKENS"}]}
    monkeypatch.setattr(m, "http", fake_http)
    saved = (getattr(m, "_gcp_token", None), getattr(m, "_gcp_token_exp", 0))
    m._gcp_token, m._gcp_token_exp = None, 0
    logs = []
    monkeypatch.setattr(logsink, "log", lambda msg, *a, **k: logs.append(str(msg)))
    diff = m.DiffResult("===== Deployment/webx =====\n" + "+ x\n" * 200,
                        [("Deployment/webx", "+ x\n" * 200)], 1, True, None,
                        m.OUT_DIFF, "changes")
    try:
        out = m.generate_ai_summary({"pv-x-a-ms": diff})
    finally:
        m._gcp_token, m._gcp_token_exp = saved
    assert out is not None
    assert "truncated" in seen["prompt"], "an oversized diff body must be trimmed in the prompt"
    assert any("MAX_TOKENS" in l for l in logs), "a MAX_TOKENS finish must be logged"


# ── process_pr: last orchestration branches ──────────────────────────────

def test_process_pr_revision_future_crash_defaults_to_no_bump(world, monkeypatch):
    sinks, plan = world

    def boom(app, files, pr_sha, main_sha=None, renames=None):
        raise RuntimeError("bitbucket hiccup mid-fetch")
    monkeypatch.setattr(m, "_pr_chart_revision_checked", boom)
    m.process_pr(_mk_pr(pr_id=701), PATH_MAP, base_sha=BASE_SHA)  # must not raise
    assert sinks.statuses, "the PR must still be processed to a terminal status"


def test_process_pr_legacy_incomplete_comment_forces_rerun(world, monkeypatch):
    sinks, plan = world
    pr = _mk_pr(pr_id=702)
    pr_sha = pr["source"]["commit"]["hash"]
    monkeypatch.setattr(m, "find_existing_comment",
                        lambda pr_id, repo=None: (555, pr_sha,
                                       "old comment\nDiff incomplete, could not evaluate.\n"))
    m.process_pr(pr, PATH_MAP, base_sha=BASE_SHA)
    assert sinks.diff_calls, \
        "a same-sha comment WITHOUT a status token but with legacy incomplete text must re-diff"


def test_process_pr_prewarm_skips_apps_without_chart_metadata(world, monkeypatch, tmp_path):
    sinks, plan = world
    helm, count = _mk_fake_helm(tmp_path)
    monkeypatch.setattr(m, "HELM_BIN", helm)
    monkeypatch.setattr(m, "HELM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(m, "OCI_USER", "u")
    monkeypatch.setattr(m, "OCI_PASS", "p")
    monkeypatch.setitem(m._app_chart_registry_map, "pv-orch-a-ms", "reg.example.com")
    # One affected app with NO chart-map entry -> the targets loop skips it
    # instead of crashing on a KeyError.
    monkeypatch.delitem(m._app_chart_map, "pv-orch-a-ss", raising=False)
    m.process_pr(_mk_pr(pr_id=703), PATH_MAP, base_sha=BASE_SHA)  # must not raise
    assert sinks.statuses, "the run must complete despite the metadata gap"


def test_process_pr_prewarm_unexpected_error_is_swallowed(world, monkeypatch, tmp_path):
    sinks, plan = world
    monkeypatch.setattr(m, "HELM_BIN", "/usr/bin/true")
    monkeypatch.setattr(m, "OCI_USER", "u")
    monkeypatch.setattr(m, "OCI_PASS", "p")
    monkeypatch.setitem(m._app_chart_registry_map, "pv-orch-a-ms", "reg.example.com")

    def boom(*a, **k):
        raise RuntimeError("pool exploded")
    monkeypatch.setattr(m, "_ensure_chart", boom)
    m.process_pr(_mk_pr(pr_id=704), PATH_MAP, base_sha=BASE_SHA)  # must not raise
    assert sinks.statuses, "a pre-warm crash must never take down the PR run"


def test_process_pr_oci_not_found_alone_desc(world, monkeypatch):
    sinks, plan = world
    plan["pv-orch-a-ms"] = m.DiffResult("", [], 0, False, "chart gone",
                                        m.OUT_INDETERMINATE, m.REASON_OCI_NOT_FOUND)
    m.process_pr(_mk_pr(pr_id=705), PATH_MAP, base_sha=BASE_SHA)
    state, desc = sinks.statuses[-1]
    assert state == "FAILED" and "chart version not found in OCI registry" in desc


def test_process_batch_survives_a_crashing_diff(world, monkeypatch):
    sinks, plan = world

    def crashing_diff(app, *a, **k):
        if app == "pv-orch-a-ms":
            raise RuntimeError("diff engine exploded")
        sinks.diff_calls.append(app)
        return m.DiffResult("", [], 0, False, "", m.OUT_NO_DIFF, "")
    monkeypatch.setattr(m, "argocd_diff", crashing_diff)
    m.process_pr(_mk_pr(pr_id=706), PATH_MAP, base_sha=BASE_SHA)  # must not raise
    assert sinks.upserts and "diff engine exploded" in sinks.upserts[-1][:5000], \
        "the crash must surface in the comment as that app's error"


# ── main() single-pass and main_iteration's login-recovery failure ───────

def test_main_single_iteration_and_unhandled_error_survival(monkeypatch):
    monkeypatch.setattr(m, "_start_health_server",
                        lambda *a, **k: type("S", (), {"shutdown": lambda self: None})())
    monkeypatch.setattr(m, "_start_heartbeat", lambda: None)
    monkeypatch.setattr(m, "argocd_login", lambda: None)
    monkeypatch.setattr(m, "OCI_USER", "user")
    monkeypatch.setattr(m, "OCI_PASS", "secret")
    logs = []
    monkeypatch.setattr(logsink, "log", lambda msg, *a, **k: logs.append(str(msg)))
    iterations = {"n": 0}

    def one_crashing_iteration():
        iterations["n"] += 1
        m._shutdown = True   # stop after this pass
        raise RuntimeError("iteration exploded")
    monkeypatch.setattr(m, "main_iteration", one_crashing_iteration)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    saved = m._shutdown
    m._shutdown = False
    try:
        m.main()
    finally:
        m._shutdown = saved
    assert iterations["n"] == 1
    assert any("OCI credentials present" in l for l in logs)
    assert any("Unhandled error in main loop" in l for l in logs), \
        "an iteration crash must be caught, logged, and the loop must go on"
    assert any("Shutdown complete" in l for l in logs)


def test_main_iteration_login_recovery_failure_is_swallowed(monkeypatch):
    monkeypatch.setattr(m, "_prune_helm_cache", lambda: None)
    monkeypatch.setattr(m, "_touch_progress", lambda: None)
    monkeypatch.setattr(m, "_argocd_token", "", raising=False)

    def discovery_boom():
        raise RuntimeError("argocd down")
    monkeypatch.setattr(m, "discover_path_app_map", discovery_boom)

    def login_boom():
        raise RuntimeError("login also down")
    monkeypatch.setattr(m, "argocd_login", login_boom)
    m.main_iteration()  # both failing must still return cleanly
