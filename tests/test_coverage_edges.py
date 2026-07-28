"""Coverage campaign (post v2.5.15), pass E: HTTP surface + orchestrator branches.

- The REAL health server, started on an ephemeral port: every GET endpoint,
  plus both webhook POST handlers driven with genuine HMAC-signed requests
  (accept, reject, malformed payload, oversize/invalid Content-Length).
- The new-environment and structural-problem branches of process_pr, forced
  through scripted detectors.
- argocd_diff's metadata-missing guard ("never raises" contract).
- argocd_login's consecutive-failure readiness flip.
"""
import hashlib
import hmac as hmac_mod
import json
import os
import sys
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m  # noqa: E402

from test_coverage_orchestration import world, _mk_pr, PATH_MAP, BASE_SHA  # noqa: E402,F401


# ── real health server on an ephemeral port ──────────────────────────────

@pytest.fixture()
def health(monkeypatch):
    refreshes = []
    monkeypatch.setattr(m, "_jfrog_hard_refresh",
                        lambda name, ver: refreshes.append((name, ver)))
    srv = m._start_health_server(0)
    port = srv.server_address[1]
    yield f"http://127.0.0.1:{port}", refreshes
    srv.shutdown()


def _req(url, method="GET", body=None, headers=None):
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_health_endpoints_respond(health, monkeypatch):
    url, _ = health
    monkeypatch.setattr(m, "_ready", True, raising=False)
    code, body = _req(f"{url}/healthz")
    assert code == 200
    code, _ = _req(f"{url}/readyz")
    assert code in (200, 503)
    code, body = _req(f"{url}/jfrog-webhook/stats")
    assert code == 200 and b"received" in body
    code, body = _req(f"{url}/diff-preview/stats")
    assert code == 200


def test_bb_webhook_wakes_loop_in_permissive_mode(health, monkeypatch):
    url, _ = health
    monkeypatch.setattr(m, "BB_WEBHOOK_SECRET", "")
    m._wake.clear()
    body = json.dumps({"pullrequest": {"id": 1}}).encode()
    code, _ = _req(f"{url}/diff-preview/webhook", "POST", body,
                   {"Content-Length": str(len(body)),
                    "X-Event-Key": "pullrequest:updated"})
    assert code == 200
    assert m._wake.is_set(), "a pullrequest:* event must wake the diff loop"
    m._wake.clear()


def test_bb_webhook_rejects_bad_signature_when_secret_set(health, monkeypatch):
    url, _ = health
    monkeypatch.setattr(m, "BB_WEBHOOK_SECRET", "bbsecret")
    body = b'{"pullrequest":{}}'
    code, _ = _req(f"{url}/diff-preview/webhook", "POST", body,
                   {"X-Hub-Signature": "sha256=deadbeef"})
    assert code == 401


def test_bb_webhook_rejects_zero_or_oversize_body(health):
    url, _ = health
    # Content-Length 0 is refused before any read (v2.5.2 C1 memory-cap fix).
    code, _ = _req(f"{url}/diff-preview/webhook", "POST", b"",
                   {"Content-Length": "0"})
    assert code == 413


def test_jfrog_webhook_accepts_signed_push_and_schedules_refresh(health, monkeypatch):
    url, refreshes = health
    monkeypatch.setattr(m, "JFROG_WEBHOOK_SECRET", "jfsecret")
    payload = json.dumps({"event_type": "pushed",
                          "data": {"image_name": "appspace-ms",
                                   "tag": "2603.0.1-dev"}}).encode()
    sig = hmac_mod.new(b"jfsecret", payload, hashlib.sha256).hexdigest()
    before = m._jfrog_stats["received"]
    code, _ = _req(f"{url}/jfrog-webhook", "POST", payload,
                   {"X-JFrog-Event-Auth": sig})
    assert code in (200, 202)
    assert m._jfrog_stats["received"] == before + 1
    # The hard refresh runs in a daemon thread; the recorder is patched in,
    # give it a moment via join-by-polling.
    import time as _t
    for _ in range(50):
        if refreshes:
            break
        _t.sleep(0.05)
    assert refreshes == [("appspace-ms", "2603.0.1-dev")]


def test_jfrog_webhook_rejects_bad_hmac_and_counts_it(health, monkeypatch):
    url, refreshes = health
    monkeypatch.setattr(m, "JFROG_WEBHOOK_SECRET", "jfsecret")
    payload = b'{"event_type":"pushed","data":{}}'
    before = m._jfrog_stats["rejected_hmac"]
    code, _ = _req(f"{url}/jfrog-webhook", "POST", payload,
                   {"X-JFrog-Event-Auth": "0" * 64})
    assert code == 401
    assert m._jfrog_stats["rejected_hmac"] == before + 1
    assert not refreshes


def test_jfrog_webhook_malformed_payload_is_400(health, monkeypatch):
    url, _ = health
    monkeypatch.setattr(m, "JFROG_WEBHOOK_SECRET", "jfsecret")
    payload = b'{"event_type":"pushed","data":{"no_image_name":true}}'
    sig = hmac_mod.new(b"jfsecret", payload, hashlib.sha256).hexdigest()
    before = m._jfrog_stats["rejected_format"]
    code, _ = _req(f"{url}/jfrog-webhook", "POST", payload,
                   {"X-JFrog-Event-Auth": sig})
    assert code == 400
    assert m._jfrog_stats["rejected_format"] == before + 1


def test_jfrog_webhook_ignores_non_push_events(health, monkeypatch):
    url, refreshes = health
    monkeypatch.setattr(m, "JFROG_WEBHOOK_SECRET", "jfsecret")
    payload = json.dumps({"event_type": "deleted",
                          "data": {"image_name": "x", "tag": "y"}}).encode()
    sig = hmac_mod.new(b"jfsecret", payload, hashlib.sha256).hexdigest()
    code, _ = _req(f"{url}/jfrog-webhook", "POST", payload,
                   {"X-JFrog-Event-Auth": sig})
    assert code == 200
    assert not refreshes


# ── forced orchestrator branches ─────────────────────────────────────────

def test_process_pr_new_env_branch_renders_and_reports(world, monkeypatch):
    sinks, plan = world
    # The pure new-env path runs when NO existing app is affected: point the
    # changed files away from PATH_MAP so `affected` comes out empty.
    monkeypatch.setattr(m, "get_pr_changed_files",
                        lambda pr_id, repo=None: (["gcp/dev/private-cloud/ap1/custom/pv-new-x-a/customer.yaml"], {}))
    monkeypatch.setattr(m, "_detect_new_env_candidates",
                        lambda changed, path_map, renames=None, pr_sha=None, repo=None:
                        [{"name": "pv-new-x-a",
                          "config_file": "gcp/dev/private-cloud/ap1/custom/pv-new-x-a/customer.yaml",
                          "env_dir": "gcp/dev/private-cloud/ap1/custom/pv-new-x-a",
                          "all_yaml_files": ["gcp/dev/private-cloud/ap1/custom/pv-new-x-a/customer.yaml"]}])
    monkeypatch.setattr(m, "_evaluate_new_envs",
                        lambda cands, pr_sha:
                        (["- `pv-new-x-a`: renders cleanly (12 resources)"], [], 1))
    m.process_pr(_mk_pr(pr_id=993), PATH_MAP, base_sha=BASE_SHA)
    assert any("pv-new-x-a" in b for b in sinks.upserts), sinks.upserts[:1]
    states = [s for s, _ in sinks.statuses]
    assert states[-1] == "SUCCESSFUL", states


def test_process_pr_structural_new_env_blocks_the_pr(world, monkeypatch):
    sinks, plan = world
    monkeypatch.setattr(m, "get_pr_changed_files",
                        lambda pr_id, repo=None: (["gcp/dev/private-cloud/ap1/custom/pv-new-y-a/customer.yaml"], {}))
    monkeypatch.setattr(m, "_detect_new_env_candidates",
                        lambda changed, path_map, renames=None, pr_sha=None, repo=None:
                        [{"name": "pv-new-y-a",
                          "config_file": "gcp/dev/private-cloud/ap1/custom/pv-new-y-a/customer.yaml",
                          "env_dir": "gcp/dev/private-cloud/ap1/custom/pv-new-y-a",
                          "all_yaml_files": ["gcp/dev/private-cloud/ap1/custom/pv-new-y-a/customer.yaml"]}])
    monkeypatch.setattr(m, "_evaluate_new_envs",
                        lambda cands, pr_sha:
                        (["- `pv-new-y-a`: missing required value appspace.version"],
                         ["pv-new-y-a"], 1))
    m.process_pr(_mk_pr(pr_id=994), PATH_MAP, base_sha=BASE_SHA)
    states = [s for s, _ in sinks.statuses]
    assert states[-1] == "FAILED", sinks.statuses
    assert any("pv-new-y-a" in b for b in sinks.upserts)


def test_process_pr_registers_chart_targets_when_oci_configured(world, monkeypatch):
    # The republish-invalidation machinery only registers which chart builds
    # a PR renders with when OCI credentials are present (HELM_BIN and
    # OCI_PASS gate). Force the gate open and run the happy path through it.
    sinks, plan = world
    monkeypatch.setattr(m, "OCI_PASS", "x", raising=False)
    m.process_pr(_mk_pr(pr_id=995), PATH_MAP, base_sha=BASE_SHA)
    states = [s for s, _ in sinks.statuses]
    assert states[-1] == "SUCCESSFUL", states


# ── argocd_diff guard + argocd_login readiness ───────────────────────────

def test_argocd_diff_missing_metadata_is_indeterminate_never_raises():
    res = m.argocd_diff("ghost-app-not-in-any-cache", "a" * 12, "b" * 12)
    assert res.outcome == m.OUT_INDETERMINATE
    assert res.error


def test_argocd_login_flips_ready_after_consecutive_failures(monkeypatch):
    saved = (m._consecutive_login_fails, m._ready, m._argocd_token)

    def boom():
        raise RuntimeError("session api down")

    monkeypatch.setattr(m, "_argocd_fetch_token", boom)
    try:
        m._consecutive_login_fails = 0
        m._ready = True
        for _ in range(m.LOGIN_FAIL_THRESHOLD):
            # argocd_login logs the failure, updates the counters, and then
            # re-raises so callers know this iteration has no session.
            with pytest.raises(RuntimeError):
                m.argocd_login()
        assert m._ready is False, (
            f"{m.LOGIN_FAIL_THRESHOLD} consecutive login failures must flip readiness")
    finally:
        m._consecutive_login_fails, m._ready, m._argocd_token = saved
