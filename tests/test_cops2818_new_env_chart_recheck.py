"""COPS-2818: a new environment re-checks its chart until it is published.

Live on acme-config-prod PR 4770: pv-oncor-a pinned a dev chart that was
pushed 35s after the first render. The render said "chart not found in OCI",
the comment got [blocked], and the leader skipped the PR until a human pushed
an empty commit. The JFrog push landed on the standby, which knew nothing
about the PR.

COPS-2696 already made this case retry for existing apps. Here the new-env
path gets the same rule: the build stays FAILED, the token is [transient], so
the COPS-2546 backoff checks again. The new-env chart is also registered for
the JFrog fast path, and a standby relays the push to the leader.
"""
import hashlib
import hmac
import json
import os
import sys
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import comment_render as cr  # noqa: E402
import diff_preview as m  # noqa: E402

from test_coverage_orchestration import (  # noqa: E402,F401
    world, _mk_pr, PATH_MAP, BASE_SHA, PR_SHA, ANCILLARY, IDENTITY, IDENTITY_YAML)
from test_cops2766_new_env_gates import (  # noqa: E402,F401
    newenv, ENV, ENV_DIR, IDENT, FILES, CAND, RENDERED, IP, SK, REAL_FIX_STUCK)

CHART = "appspace-micro-services"
VER = "2603.2.22-rev1-copr-32580-dev"
NOT_FOUND = (None, f"chart not found in OCI: {CHART}:{VER}", 0, VER)
PULL_FAILED = (None, "chart pull failed: read tcp: i/o timeout", 0, VER)
PULL_NONE = (None, "chart pull returned None (registry login may have failed)", 0, VER)
GREEN = (RENDERED, None, 3, VER)
HARD = (None, "template: boom at line 3", 0, VER)

ENV2 = "pv-new-y-a"
IDENT2 = f"{ENV_DIR.rsplit('/', 1)[0]}/{ENV2}/customer.yaml"
CAND2 = {"name": ENV2, "config_file": IDENT2,
         "env_dir": IDENT2.rsplit("/", 1)[0], "all_yaml_files": [IDENT2]}
FILES2 = {**FILES, IDENT2: "appspace:\n  customerName: new-y\n  version: 2603.0.1-dev\n"}


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)
    for d in (m._retry_backoff, m._pr_chart_targets, m._seen, m._force_recompute):
        d.clear()
    yield
    for d in (m._retry_backoff, m._pr_chart_targets, m._seen, m._force_recompute):
        d.clear()


@pytest.fixture()
def comments(world, monkeypatch):
    """One durable comment, like Bitbucket: found again, replaced in place."""
    sinks, _plan = world
    store = {"ids": []}

    def find(pr_id, repo=None):
        if "raw" not in store:
            return None, "", ""
        return store["id"], m._extract_comment_sha(store["raw"]), store["raw"]

    def upsert(pr_id, body, existing_id=None, repo=None, **kw):
        if existing_id is None:
            store["id"] = 500 + len(store["ids"])
            store["ids"].append(store["id"])
        else:
            assert existing_id == store["id"]
        store["raw"] = body
        sinks.upserts.append(body)
        return "ok"
    monkeypatch.setattr(m, "find_existing_comment", find)
    monkeypatch.setattr(m, "upsert_comment", upsert)
    return store


# ── new-env-only path ────────────────────────────────────────────────────

def test_a_missing_chart_is_failed_but_transient(newenv):
    body, (state, desc) = newenv(render=NOT_FOUND)
    assert m._extract_status_token(body) == "transient"
    assert state == "FAILED"
    assert "wait for their chart in the registry" in desc and ENV in desc
    assert SK not in m._seen and SK in m._retry_backoff
    assert f"`{CHART}:{VER}` is not in the registry yet" in body
    assert "this comment is updated once the chart is published" in body
    assert "this preview checks again automatically" in body
    assert "its configuration did not validate" not in body
    assert "structural problem" not in body
    assert "missing required value" not in body.lower()


@pytest.mark.parametrize("render", [PULL_FAILED, PULL_NONE])
def test_a_failed_chart_pull_stays_blocked(newenv, render):
    """Only "not found" resolves by itself. helm answers a bad version such
    as 1.2.3.4 with "improper constraint", and _ensure_chart returns None."""
    body, (state, desc) = newenv(render=render)
    assert m._extract_status_token(body) == "blocked"
    assert state == "FAILED" and "structural config problem" in desc
    assert "not in the registry yet" not in body
    assert "its configuration did not validate" in body
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA) and SK not in m._retry_backoff


def test_the_next_pass_turns_the_same_comment_green(newenv, comments, world):
    sinks, _ = world
    newenv(render=NOT_FOUND)
    assert len(sinks.upserts) == 1
    for _ in range(3):          # one backoff skip, then the transient rerun
        body, (state, _desc) = newenv(render=GREEN)
    assert state == "SUCCESSFUL" and m._extract_status_token(body) == "clean"
    assert len(sinks.upserts) == 2, "rendered once more, then marked seen"
    assert len(comments["ids"]) == 1, "the same comment is replaced in place"
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA)


def test_a_fresh_pod_reruns_a_transient_comment(newenv, comments, world):
    sinks, _ = world
    newenv(render=NOT_FOUND)
    m._seen.clear()
    m._retry_backoff.clear()     # a restart or leader flip: no memory at all
    body, (state, _desc) = newenv(render=GREEN)
    assert len(sinks.upserts) == 2 and state == "SUCCESSFUL"
    assert len(comments["ids"]) == 1


def test_a_forced_recompute_skips_the_backoff(newenv, comments, world):
    sinks, _ = world
    newenv(render=NOT_FOUND)
    assert SK in m._retry_backoff
    m._force_recompute.add(SK)
    body, (state, _desc) = newenv(render=GREEN)
    assert len(sinks.upserts) == 2 and state == "SUCCESSFUL"


def _two_envs(monkeypatch, renders):
    monkeypatch.setattr(m, "_detect_new_env_candidates",
                        lambda *a, **k: [dict(CAND), dict(CAND2)])
    monkeypatch.setattr(m, "_bb_fetch_status", lambda path, sha, repo=None:
                        (FILES2[path], m.BB_OK) if sha == PR_SHA and path in FILES2
                        else (None, m.BB_NOT_FOUND))
    monkeypatch.setattr(m, "_render_new_env_diff",
                        lambda env_info, pr_sha: renders[env_info["name"]])


def test_a_hard_env_next_to_an_awaiting_one_stays_blocked(newenv, monkeypatch):
    _two_envs(monkeypatch, {ENV: NOT_FOUND, ENV2: HARD})
    body, (state, desc) = newenv()
    assert m._extract_status_token(body) == "blocked"
    assert state == "FAILED" and "structural config problem" in desc
    assert "its configuration did not validate" in body
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA)


def test_a_blocked_gate_keeps_blocked(newenv):
    body, (state, desc) = newenv(extra=[IP], messages=[], render=NOT_FOUND)
    assert m._extract_status_token(body) == "blocked"
    assert state == "FAILED"
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA)
    # The gate blocks, so no promise of a re-check, and the gate is named.
    assert "checks again automatically" not in body
    assert desc and "wait for their chart" not in desc


def test_a_transient_gate_with_an_awaiting_env_stays_transient(newenv):
    body, (state, _desc) = newenv(extra=[{"kind": "not_live", "env": ENV}],
                                  render=NOT_FOUND)
    assert m._extract_status_token(body) == "transient" and state == "FAILED"


def test_status_recovery_of_an_awaiting_comment_is_failed(newenv, monkeypatch):
    body, (state, _desc) = newenv(render=NOT_FOUND)
    posted = []
    monkeypatch.setattr(m, "http", lambda *a, **k: {"state": "INPROGRESS"})
    monkeypatch.setattr(m, "post_build_status",
                        lambda sha, st, d, pr_id=None, repo=None:
                        posted.append(st) or "ok")
    REAL_FIX_STUCK(PR_SHA, 991, body)
    assert posted == ["FAILED"]


def test_the_merge_summary_says_the_chart_is_not_there_yet(newenv):
    body, _ = newenv(render=NOT_FOUND)
    assert "its chart is not in the registry yet" in body
    summary = body[body.index(cr.MERGE_SUMMARY_HDR):body.index("New Environment(s) Detected")]
    assert cr._VERDICTS[cr._SEV_BLOCK] in summary


# ── JFrog fast path: the new-env chart is registered ─────────────────────

def test_the_new_env_chart_is_registered_and_a_push_forces_the_pr(newenv):
    newenv(render=NOT_FOUND)
    assert m._pr_chart_targets[SK] == {(CHART, VER)}
    m._invalidate_for_republish(CHART, VER)
    assert SK in m._force_recompute and SK not in m._seen


# ── mixed path: existing apps plus a new env ─────────────────────────────

@pytest.fixture()
def mixed(world, monkeypatch):
    sinks, plan = world
    monkeypatch.setattr(m, "get_pr_changed_files",
                        lambda pr_id, repo=None: ([ANCILLARY, IDENT], {}))
    monkeypatch.setattr(m, "_detect_new_env_candidates", lambda *a, **k: [dict(CAND)])
    monkeypatch.setattr(m, "_pr_commit_messages", lambda *a: [])
    real_merge = m._merge_gates

    def run(structural=(ENV,), awaiting=(ENV,), gates=(), state_lines=()):
        def evaluate(*a, awaiting=None, chart_targets=None, **k):
            if awaiting is not None:
                awaiting.update(_aw)
            if chart_targets is not None:
                chart_targets.add((CHART, VER))
            return ["### \U0001f195 New Environment(s) Detected", ""], list(structural), 0, []
        _aw = set(awaiting)
        monkeypatch.setattr(m, "_evaluate_new_envs", evaluate)
        monkeypatch.setattr(m, "_merge_gates", lambda *a, **k: real_merge(
            *a[:5], extra=[dict(g) for g in gates]))
        monkeypatch.setattr(m, "_summarize_appspace_state_changes",
                            lambda *a, **k: list(state_lines))
        m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
        return sinks.upserts[-1], sinks.statuses[-1]
    return run, plan


def test_mixed_awaiting_env_is_transient_and_backs_off(mixed):
    run, _plan = mixed
    body, (state, desc) = run()
    assert m._extract_status_token(body) == "transient"
    assert state == "FAILED" and "wait for their chart" in desc
    assert SK in m._retry_backoff and SK not in m._seen
    assert (CHART, VER) in m._pr_chart_targets[SK]
    assert ("appspace-ms", "2603.0.1-dev") in m._pr_chart_targets[SK]


def test_mixed_hard_env_is_permanent(mixed):
    run, _plan = mixed
    body, (state, _desc) = run(awaiting=())
    assert m._extract_status_token(body) == "permanent" and state == "FAILED"
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA)


@pytest.mark.parametrize("case", [
    "awaiting", "awaiting+hard", "awaiting+blocked_gate", "awaiting+oci_app",
    "awaiting+flag_typo"])
def test_mixed_token_and_scheduling_agree(mixed, case):
    run, plan = mixed
    kw = {}
    if case == "awaiting+hard":
        kw["structural"] = (ENV, ENV2)
    elif case == "awaiting+blocked_gate":
        kw["gates"] = [IP]
    elif case == "awaiting+oci_app":
        plan["pv-orch-a-ms"] = m.DiffResult("", [], 0, False, "not found",
                                            m.OUT_INDETERMINATE, m.REASON_OCI_NOT_FOUND)
    elif case == "awaiting+flag_typo":
        kw["state_lines"] = [cr._DECOM_FLAG_TYPO_HDR]
    body, (state, _desc) = run(**kw)
    token = m._extract_status_token(body)
    assert state == "FAILED"
    assert (token == "transient") == (SK in m._retry_backoff), token
    assert (token == "transient") == (SK not in m._seen), token
    assert token == {"awaiting": "transient", "awaiting+hard": "permanent",
                     "awaiting+blocked_gate": "blocked",
                     "awaiting+oci_app": "transient",
                     "awaiting+flag_typo": "permanent"}[case]


# ── JFrog relay from the standby to the leader ───────────────────────────

class _Elector:
    def __init__(self, leading):
        self.leading = leading

    def is_leader(self):
        return self.leading

    def current_holder(self):
        return "pod-l"

    def pod_ip(self, name):
        return "10.0.0.9"


@pytest.fixture()
def jfrog(monkeypatch):
    monkeypatch.setattr(m, "JFROG_WEBHOOK_SECRET", "jfsecret")
    calls = {"invalidate": [], "submit": []}
    monkeypatch.setattr(m, "_invalidate_for_republish",
                        lambda c, v: calls["invalidate"].append((c, v)))
    monkeypatch.setattr(m, "_jfrog_refresh_pool", type("P", (), {
        "submit": staticmethod(lambda fn, *a: calls["submit"].append((fn, a)))})())
    srv = m._start_health_server(0)
    url = f"http://127.0.0.1:{srv.server_address[1]}/jfrog-webhook"

    def post(tag, leading, marker=None):
        monkeypatch.setattr(m, "_leader", _Elector(leading) if leading is not None
                            else None, raising=False)
        body = json.dumps({"event_type": "pushed", "data": {
            "image_name": CHART, "tag": tag}}).encode()
        sig = hmac.new(b"jfsecret", body, hashlib.sha256).hexdigest()
        headers = {"X-JFrog-Event-Auth": sig}
        if marker:
            headers["X-ADP-Forwarded"] = marker
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
            assert r.status == 202
        m._jfrog_recent.pop(f"{CHART}:{tag}", None)
        return body, sig
    yield post, calls
    srv.shutdown()


def _fns(calls):
    return [fn for fn, _a in calls["submit"]]


def test_the_standby_processes_locally_and_relays(jfrog):
    post, calls = jfrog
    body, sig = post("1.0.0-dev", leading=False)
    assert calls["invalidate"] == [(CHART, "1.0.0-dev")]
    assert _fns(calls) == [m._jfrog_refresh_guarded, m._relay_jfrog_push]
    fwd_body, fwd_headers, label = calls["submit"][1][1]
    assert fwd_body == body and label == f"{CHART}:1.0.0-dev"
    assert fwd_headers == {"X-JFrog-Event-Auth": sig, "X-ADP-Forwarded": "1"}


@pytest.mark.parametrize("leading", [True, None])
def test_the_leader_does_not_relay(jfrog, leading):
    post, calls = jfrog
    post("1.0.1-dev", leading=leading)
    assert calls["invalidate"] == [(CHART, "1.0.1-dev")]
    assert _fns(calls) == [m._jfrog_refresh_guarded]


@pytest.mark.parametrize("leading", [True, False])
def test_a_relayed_push_invalidates_once_and_is_never_relayed(jfrog, leading):
    post, calls = jfrog
    post("1.0.2-dev", leading=leading, marker="1")
    assert calls["invalidate"] == [(CHART, "1.0.2-dev")]
    assert _fns(calls) == [], "the standby already did the ArgoCD refresh"


def test_the_relay_posts_to_the_leader_jfrog_path(monkeypatch):
    sent = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(m, "_leader", _Elector(False), raising=False)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: sent.append(req) or _Resp())
    assert m._forward_webhook_to_leader(
        b"{}", {"X-JFrog-Event-Auth": "sig", "X-ADP-Forwarded": "1"},
        "/jfrog-webhook") is True
    req = sent[0]
    assert req.full_url == "http://10.0.0.9:8080/jfrog-webhook"
    assert req.get_header("X-jfrog-event-auth") == "sig"
    assert req.get_header("X-adp-forwarded") == "1"


@pytest.mark.parametrize("ok", [True, False])
def test_the_jfrog_relay_logs_its_result(monkeypatch, ok):
    sent, logged = [], []
    monkeypatch.setattr(m, "_forward_webhook_to_leader",
                        lambda body, headers, path: sent.append(path) or ok)
    monkeypatch.setattr(m.logsink, "log", lambda msg, sev="INFO", **k: logged.append(msg))
    assert m._relay_jfrog_push(b"{}", {}, f"{CHART}:{VER}") is ok
    assert sent == ["/jfrog-webhook"]
    assert logged == [f"JFrog webhook {CHART}:{VER} (standby): "
                      + ("relayed to the leader" if ok else
                         "relay unavailable, the leader does not force its PRs")]


def test_a_failed_jfrog_relay_does_not_claim_a_safety_net(monkeypatch):
    logged = []
    monkeypatch.setattr(m, "_leader", _Elector(False), raising=False)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: (_ for _ in ()).throw(OSError("down")))
    monkeypatch.setattr(m.logsink, "log", lambda msg, sev="INFO", **k: logged.append(msg))
    assert m._forward_webhook_to_leader(b"{}", {}, "/jfrog-webhook") is False
    assert m._forward_webhook_to_leader(b"{}", {}) is False
    assert "safety net" not in logged[0] and "safety net covers it" in logged[1]


def test_a_real_push_payload_forces_the_new_env_pr(newenv, monkeypatch):
    """The real handler and the real invalidation, end to end."""
    newenv(render=NOT_FOUND)
    m._seen[SK] = (PR_SHA, BASE_SHA)
    monkeypatch.setattr(m, "JFROG_WEBHOOK_SECRET", "jfsecret")
    monkeypatch.setattr(m, "_leader", None, raising=False)
    monkeypatch.setattr(m, "_jfrog_refresh_pool", type("P", (), {
        "submit": staticmethod(lambda fn, *a: None)})())
    srv = m._start_health_server(0)
    try:
        body = json.dumps({"event_type": "pushed", "data": {
            "image_name": CHART, "tag": VER}}).encode()
        sig = hmac.new(b"jfsecret", body, hashlib.sha256).hexdigest()
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.server_address[1]}/jfrog-webhook", data=body,
            headers={"X-JFrog-Event-Auth": sig}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
    finally:
        srv.shutdown()
        m._jfrog_recent.pop(f"{CHART}:{VER}", None)
    assert SK in m._force_recompute and SK not in m._seen
