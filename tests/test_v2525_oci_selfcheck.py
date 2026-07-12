"""v2.5.25 — OCI self-check + failure escalation regression tests.

Post-incident review L1/L2: the OCI-pull path had ZERO health coverage.
A pod could be Ready with 100% of diffs failing (proven live: the 403
incident ran invisible — probes green, logs WARNING-only). Two fixes:

1. _oci_selfcheck(): a cheap `helm show chart` against a known-good ref,
   run periodically, using the SAME env pattern as real pulls (cache homes
   isolated, config home inherited). Result surfaced in /stats and logged
   at ERROR on failure.
2. Escalation: consecutive systemic pull failures (auth/network, NOT 404s)
   log at ERROR once past a threshold, so log-based alerting can fire.

Confirmed RED against v2.5.24.
"""
import os
import sys

import pytest

os.environ.setdefault("BB_USER", "test-user")
os.environ.setdefault("BB_TOKEN", "test-token")
os.environ.setdefault("ARGOCD_PASS", "test-pass")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m


@pytest.fixture(autouse=True)
def _reset_state():
    m._diff_stats["oci_consecutive_pull_failures"] = 0
    m._diff_stats["oci_selfcheck"] = None
    m._last_pull_ok_ref = None
    yield


def test_stats_keys_exist():
    for k in ("oci_selfcheck", "oci_selfcheck_at",
              "oci_consecutive_pull_failures"):
        assert k in m._diff_stats, f"missing /stats key: {k}"


def test_record_pull_failure_escalates_to_error_at_threshold():
    """First failures are WARNING; from the threshold on, ERROR — that is
    the alertable signal the 403 incident lacked."""
    sevs = [m._record_pull_failure("reg/chart:v") for _ in range(4)]
    thr = m.OCI_FAIL_ERROR_THRESHOLD
    assert sevs[:thr - 1] == ["WARNING"] * (thr - 1)
    assert all(s == "ERROR" for s in sevs[thr - 1:]), sevs
    assert m._diff_stats["oci_consecutive_pull_failures"] == 4


def test_record_pull_success_resets_counter_and_remembers_ref():
    m._record_pull_failure("reg/chart:v")
    m._record_pull_failure("reg/chart:v")
    m._record_pull_success("reg.example.com", "appspace-ms", "1.2.3")
    assert m._diff_stats["oci_consecutive_pull_failures"] == 0
    assert m._last_pull_ok_ref == ("reg.example.com", "appspace-ms", "1.2.3")


def test_selfcheck_uses_last_successful_ref_and_pull_env_pattern(monkeypatch):
    """No env ref configured -> use the last successful pull's ref. The
    subprocess env must inherit HELM_CONFIG_HOME (credentials!) and isolate
    the cache homes — the exact contract the 403 incident taught us."""
    monkeypatch.delenv("DIFF_OCI_SELFCHECK_REF", raising=False)
    monkeypatch.setattr(m, "_helm_login", lambda registry: True)
    m._record_pull_success("reg.example.com", "appspace-ms", "1.2.3")
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["env"] = kw.get("env")
        class R: returncode = 0; stdout = "name: appspace-ms"; stderr = ""
        return R()

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    ok = m._oci_selfcheck()
    assert ok is True
    assert m._diff_stats["oci_selfcheck"] == "ok"
    cmd = captured["cmd"]
    assert cmd[:3] == [m.HELM_BIN, "show", "chart"]
    assert "oci://reg.example.com/appspace-ms" in cmd
    assert "1.2.3" in cmd
    env = captured["env"]
    assert env.get("HELM_CONFIG_HOME") == os.environ.get("HELM_CONFIG_HOME")
    for isolated in ("HELM_CACHE_HOME", "HELM_REPOSITORY_CACHE",
                     "HELM_DATA_HOME"):
        assert env.get(isolated) and env[isolated] != os.environ.get(isolated)


def test_selfcheck_env_ref_takes_priority(monkeypatch):
    monkeypatch.setenv("DIFF_OCI_SELFCHECK_REF",
                       "envreg.example.com/envchart:9.9.9")
    monkeypatch.setattr(m, "_helm_login", lambda registry: True)
    m._record_pull_success("other.example.com", "other", "1.0.0")
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        class R: returncode = 0; stdout = "ok"; stderr = ""
        return R()

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    assert m._oci_selfcheck() is True
    assert "oci://envreg.example.com/envchart" in captured["cmd"]
    assert "9.9.9" in captured["cmd"]


def test_selfcheck_failure_sets_stats_and_logs_error(monkeypatch):
    monkeypatch.delenv("DIFF_OCI_SELFCHECK_REF", raising=False)
    m._record_pull_success("reg.example.com", "appspace-ms", "1.2.3")
    logged = []
    monkeypatch.setattr(m, "log",
                        lambda msg, sev="INFO", **kw: logged.append((sev, msg)))

    def fake_run(cmd, **kw):
        class R: returncode = 1; stdout = ""; stderr = "403 unauthorized"
        return R()

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    assert m._oci_selfcheck() is False
    assert m._diff_stats["oci_selfcheck"] == "failed"
    assert any(s == "ERROR" and "self-check" in msg.lower()
               for s, msg in logged), logged


def test_selfcheck_skips_when_no_ref_available(monkeypatch):
    monkeypatch.delenv("DIFF_OCI_SELFCHECK_REF", raising=False)
    calls = []
    monkeypatch.setattr(m.subprocess, "run",
                        lambda *a, **k: calls.append(a))
    assert m._oci_selfcheck() is None
    assert m._diff_stats["oci_selfcheck"] == "skipped"
    assert calls == [], "must not run helm without a reference"


def test_selfcheck_never_raises(monkeypatch):
    m._record_pull_success("reg.example.com", "appspace-ms", "1.2.3")

    def boom(*a, **k):
        raise OSError("helm binary vanished")

    monkeypatch.setattr(m.subprocess, "run", boom)
    assert m._oci_selfcheck() is False   # swallowed, reported, not raised
    assert m._diff_stats["oci_selfcheck"] == "failed"
