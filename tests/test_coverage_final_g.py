"""Coverage campaign, pass G (final): pure-logic edges, both webhook error
paths, the AI summary call (mocked at the same http() boundary as every
other external call in this codebase), and process_pr's remaining
observable branches: MAX_APPS_PER_RUN capping, invalid-version accounting,
the structural-new-env FAILED description matrix, and the outer
exception-safety-net that must never let one PR's crash affect the others.

Nothing here fakes helm. Everything is either pure string/YAML parsing, or
mocks the exact same http()/bb() boundary the rest of the suite already
relies on.
"""
import json
import os
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m  # noqa: E402
import logsink

from test_coverage_orchestration import world, _mk_pr, PATH_MAP, BASE_SHA  # noqa: E402,F401


# ── pure parsers: blank-line-inside-block and dedent-exit branches ───────

def test_extract_chart_version_checked_blank_line_inside_appspace_block():
    content = "appspace:\n  customerName: x\n\n  version: 2603.0.1-dev\n"
    version, status = m._extract_chart_version_checked(content)
    assert version == "2603.0.1-dev" and status == "ok"


def test_extract_chart_version_checked_dedent_exits_appspace_block():
    # A version declared AFTER dedenting out of appspace: must not be read
    # as the chart revision (regression class: deeper unrelated `version:`
    # keys, e.g. appspace.elastic.version).
    content = "appspace:\n  version: 2603.0.1-dev\nelastic:\n  version: 8.15.1\n"
    version, status = m._extract_chart_version_checked(content)
    assert version == "2603.0.1-dev" and status == "ok"


def test_extract_appspace_identity_blank_line_and_dedent():
    content = "appspace:\n  customerName: acme\n\n  suffix: a\nother:\n  suffix: z\n"
    name, suffix = m._extract_appspace_identity(content)
    assert name == "acme" and suffix == "a"


def test_rename_identity_verdict_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(m, "_IDENTITY_RENAME_CACHE_MAX", 4)
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda path, sha: ("appspace:\n  customerName: x\n", m.BB_OK))
    with m._identity_rename_verdict_lock:
        m._identity_rename_verdict_cache.clear()
    for i in range(10):
        m._rename_identity_confirmed(f"old{i}.yaml", f"new{i}.yaml", "m" * 12, "p" * 12)
    with m._identity_rename_verdict_lock:
        size = len(m._identity_rename_verdict_cache)
        m._identity_rename_verdict_cache.clear()
    assert size <= 4, f"cache must self-prune, had {size} entries"


# ── health server: remaining GET/POST branches ───────────────────────────

@pytest.fixture()
def health(monkeypatch):
    monkeypatch.setattr(m, "_jfrog_hard_refresh", lambda name, ver: None)
    srv = m._start_health_server(0)
    port = srv.server_address[1]
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


def _req(url, method="GET", body=None, headers=None):
    import urllib.request
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_healthz_ok_and_stale_branches(health, monkeypatch):
    monkeypatch.setattr(m, "_last_ok", __import__("time").monotonic(), raising=False)
    code, body = _req(f"{health}/healthz")
    assert code == 200 and body == b"ok"
    monkeypatch.setattr(m, "_last_ok", __import__("time").monotonic() - 99999, raising=False)
    code, body = _req(f"{health}/healthz")
    assert code == 503 and b"stale" in body


def test_readyz_reports_each_not_ready_reason(health, monkeypatch):
    monkeypatch.setattr(m, "_ready", False, raising=False)
    monkeypatch.setattr(m, "OCI_PASS", "", raising=False)
    monkeypatch.setattr(m, "_consecutive_login_fails", m.LOGIN_FAIL_THRESHOLD, raising=False)
    monkeypatch.setattr(m, "_consecutive_poll_fails", m.POLL_FAIL_THRESHOLD, raising=False)
    monkeypatch.setattr(m, "_last_poll_ok", False, raising=False)
    code, body = _req(f"{health}/readyz")
    assert code == 503
    for token in (b"not_started", b"oci_missing", b"login_fails", b"poll_fails"):
        assert token in body, f"missing {token!r} in {body!r}"


def test_get_unknown_path_is_404(health):
    code, _ = _req(f"{health}/nonexistent")
    assert code == 404


def test_post_unknown_path_is_404(health):
    # No body: the server's final "unknown path" branch decides purely on
    # self.path before ever reading Content-Length, so sending one adds
    # nothing to what this test checks and only risks an incidental
    # connection-reset race if the body is left unread on close.
    code, _ = _req(f"{health}/nonexistent", "POST")
    assert code == 404


def test_bb_webhook_malformed_content_length_treated_as_zero(health):
    # A non-numeric Content-Length must degrade to length=0, not crash.
    code, _ = _req(f"{health}/diff-preview/webhook", "POST", b"",
                   {"Content-Length": "not-a-number"})
    assert code == 413


def test_jfrog_webhook_malformed_content_length_is_no_body(health, monkeypatch):
    monkeypatch.setattr(m, "JFROG_WEBHOOK_SECRET", "")
    # length becomes 0 (not negative) -> falls through to "no body" -> bad
    # HMAC/JSON on empty body -> rejected downstream, but must NOT crash on
    # the header parse itself.
    code, _ = _req(f"{health}/jfrog-webhook", "POST", b"",
                   {"Content-Length": "garbage"})
    assert code in (400, 401, 413)


def test_jfrog_webhook_dedup_skips_recent_duplicate(health, monkeypatch):
    import hashlib
    import hmac as hmac_mod
    monkeypatch.setattr(m, "JFROG_WEBHOOK_SECRET", "jfsecret")
    monkeypatch.setattr(m, "_invalidate_for_republish", lambda *a, **k: None)
    refreshes = []
    monkeypatch.setattr(m, "_jfrog_refresh_pool",
                        type("P", (), {"submit": staticmethod(lambda fn, *a: refreshes.append(a))})())
    payload = json.dumps({"event_type": "pushed",
                          "data": {"image_name": "dedup-chart", "tag": "1.0.0"}}).encode()
    sig = hmac_mod.new(b"jfsecret", payload, hashlib.sha256).hexdigest()
    code1, _ = _req(f"{health}/jfrog-webhook", "POST", payload, {"X-JFrog-Event-Auth": sig})
    code2, _ = _req(f"{health}/jfrog-webhook", "POST", payload, {"X-JFrog-Event-Auth": sig})
    assert code1 == code2 == 202
    assert len(refreshes) == 1, "the second push within the dedup window must be skipped"
    m._jfrog_recent.pop("dedup-chart:1.0.0", None)


def test_jfrog_webhook_invalidation_exception_is_swallowed(health, monkeypatch):
    import hashlib
    import hmac as hmac_mod
    monkeypatch.setattr(m, "JFROG_WEBHOOK_SECRET", "jfsecret")

    def boom(*a, **k):
        raise RuntimeError("cache corrupted")
    monkeypatch.setattr(m, "_invalidate_for_republish", boom)
    monkeypatch.setattr(m, "_jfrog_refresh_pool",
                        type("P", (), {"submit": staticmethod(lambda fn, *a: None)})())
    payload = json.dumps({"event_type": "pushed",
                          "data": {"image_name": "boom-chart", "tag": "9.9.9"}}).encode()
    sig = hmac_mod.new(b"jfsecret", payload, hashlib.sha256).hexdigest()
    code, _ = _req(f"{health}/jfrog-webhook", "POST", payload, {"X-JFrog-Event-Auth": sig})
    assert code == 202, "a local-invalidation crash must not fail the webhook response"
    m._jfrog_recent.pop("boom-chart:9.9.9", None)


# ── generate_ai_summary: mocked at the same http() boundary as everything else ──

@pytest.fixture()
def fresh_gcp_token():
    """Isolate the module-level GCP token cache. Other test files call
    generate_ai_summary too, and a token cached by an earlier test (or by a
    sibling module) would make our http() mock's metadata branch never run,
    so the Vertex response shape here would not be reached. Force a cold
    cache before, restore after."""
    saved = (getattr(m, "_gcp_token", None), getattr(m, "_gcp_token_exp", 0))
    m._gcp_token = None
    m._gcp_token_exp = 0
    yield
    m._gcp_token, m._gcp_token_exp = saved


def test_generate_ai_summary_no_changes_skips_the_call(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(m, "http", lambda *a, **kw: called.append(1))
    out = m.generate_ai_summary({"pv-x-a-ms": ("", False, None)})
    assert out is None and called == []


def test_generate_ai_summary_success_returns_gemini_text(monkeypatch, fresh_gcp_token):
    def fake_http(method, url, **kw):
        if "metadata.google.internal" in url:
            return {"access_token": "tok-abc", "expires_in": 3600}
        return {"candidates": [{"content": {"parts": [{"text": "**1 app(s) updated**\n- pv-x-a-ms: bumped replicas"}]},
                                "finishReason": "STOP"}]}
    monkeypatch.setattr(m, "http", fake_http)
    # A DiffResult with a real parsed section (===== header =====), so the
    # summary's `changed` map is non-empty and the Vertex call is reached.
    diff = m.DiffResult("===== Deployment/webx =====\n- replicas: 2\n+ replicas: 3\n",
                        [("Deployment/webx", "- replicas: 2\n+ replicas: 3\n")],
                        1, True, None, m.OUT_DIFF, "changes")
    out = m.generate_ai_summary({"pv-x-a-ms": diff})
    assert out and "app(s) updated" in out


def test_generate_ai_summary_model_garden_not_enabled_logs_hint(monkeypatch, fresh_gcp_token):
    def fake_http(method, url, **kw):
        if "metadata.google.internal" in url:
            return {"access_token": "tok-abc", "expires_in": 3600}
        raise urllib.error.HTTPError("u", 404, "does not have access to the model", None, None)
    monkeypatch.setattr(m, "http", fake_http)
    logs = []
    monkeypatch.setattr(logsink, "log", lambda msg, *a, **k: logs.append(str(msg)))
    diff = m.DiffResult("===== Deployment/webx =====\n+ x\n",
                        [("Deployment/webx", "+ x\n")], 1, True, None, m.OUT_DIFF, "changes")
    out = m.generate_ai_summary({"pv-x-a-ms": diff})
    assert out is None
    assert any("Model Garden" in l for l in logs)


def test_generate_ai_summary_generic_failure_returns_none(monkeypatch, fresh_gcp_token):
    def fake_http(method, url, **kw):
        if "metadata.google.internal" in url:
            return {"access_token": "tok-abc", "expires_in": 3600}
        raise RuntimeError("network exploded")
    monkeypatch.setattr(m, "http", fake_http)
    diff = m.DiffResult("===== Deployment/webx =====\n+ x\n",
                        [("Deployment/webx", "+ x\n")], 1, True, None, m.OUT_DIFF, "changes")
    out = m.generate_ai_summary({"pv-x-a-ms": diff})
    assert out is None


# ── process_pr: remaining observable branches ────────────────────────────

def test_process_pr_ignores_prs_targeting_a_non_main_branch(world):
    sinks, plan = world
    pr = _mk_pr(pr_id=501)
    pr["destination"]["branch"]["name"] = "develop"
    m.process_pr(pr, PATH_MAP, base_sha=BASE_SHA)
    assert sinks.diff_calls == [] and sinks.upserts == []


def test_process_pr_caps_at_max_apps_per_run(world, monkeypatch):
    sinks, plan = world
    monkeypatch.setattr(m, "MAX_APPS_PER_RUN", 1)
    m.process_pr(_mk_pr(pr_id=502), PATH_MAP, base_sha=BASE_SHA)
    # PATH_MAP affects 2 apps (ms, ss); capped to 1 -> only 1 diffed.
    assert len(set(sinks.diff_calls)) == 1


def test_process_pr_invalid_version_is_logged_but_does_not_crash(world, monkeypatch):
    sinks, plan = world
    monkeypatch.setattr(m, "_pr_chart_revision_checked",
                        lambda app, files, pr_sha, main_sha=None, renames=None: (None, True))
    logs = []
    monkeypatch.setattr(logsink, "log", lambda msg, *a, **k: logs.append(str(msg)))
    m.process_pr(_mk_pr(pr_id=503), PATH_MAP, base_sha=BASE_SHA)
    assert any("rejected as unsafe" in l for l in logs)


def test_process_pr_structural_new_env_with_oci_not_found_composes_combined_desc(world, monkeypatch):
    sinks, plan = world
    monkeypatch.setattr(m, "_detect_new_env_candidates", lambda *a, **k: [{"name": "pv-newenv-a"}])
    monkeypatch.setattr(m, "_evaluate_new_envs", lambda *a, **k:
                        ([], ["pv-newenv-a"], 0, []))
    plan["pv-orch-a-ms"] = m.DiffResult("", [], 0, False, "chart not found",
                                        m.OUT_INDETERMINATE, m.REASON_OCI_NOT_FOUND)
    m.process_pr(_mk_pr(pr_id=504), PATH_MAP, base_sha=BASE_SHA)
    states = [s for s, _ in sinks.statuses]
    descs = [d for _, d in sinks.statuses]
    assert states[-1] == "FAILED"
    # COPS-2709: "structural config problem" was a category, not a problem,
    # so the new-env half now says it cannot render and, when the render
    # named a reason, carries it. Both halves must still be present.
    assert "new environment(s) cannot render" in descs[-1], descs[-1]
    assert "pv-newenv-a" in descs[-1]
    assert "chart version not found" in descs[-1], descs[-1]


def test_process_pr_outer_exception_is_caught_posts_failed_and_fallback_comment(world, monkeypatch):
    sinks, plan = world

    def boom(pr_id):
        raise RuntimeError("bitbucket api exploded")
    monkeypatch.setattr(m, "get_pr_changed_files", boom)
    m.process_pr(_mk_pr(pr_id=505), PATH_MAP, base_sha=BASE_SHA)  # must not raise
    assert sinks.statuses and sinks.statuses[-1][0] == "FAILED"
    assert sinks.upserts and "Error processing diff" in sinks.upserts[-1]


def test_process_pr_outer_exception_survives_a_failing_fallback_too(world, monkeypatch):
    # Belt-and-suspenders: even if post_build_status AND upsert_comment
    # themselves raise while handling the original crash, process_pr must
    # still return normally so the polling loop is unaffected.
    sinks, plan = world

    def boom(pr_id):
        raise RuntimeError("bitbucket api exploded")
    monkeypatch.setattr(m, "get_pr_changed_files", boom)

    def boom2(*a, **kw):
        raise RuntimeError("also broken")
    monkeypatch.setattr(m, "post_build_status", boom2)
    monkeypatch.setattr(m, "upsert_comment", boom2)
    m.process_pr(_mk_pr(pr_id=506), PATH_MAP, base_sha=BASE_SHA)  # must not raise
