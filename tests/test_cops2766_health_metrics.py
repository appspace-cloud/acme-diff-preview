"""COPS-2766: poll, write and OCI self-check health as metrics.

Three failures were only in the logs or in /healthz text:
  bb_write_failures_total    a comment or status write failed (counter)
  poll_consecutive_failures  leader polls where Bitbucket failed for every
                             repo, 0 again after one good poll (gauge)
  oci_selfcheck_ok           1 or 0 after each OCI self-check, and absent
                             before the first one (gauge)
"""
import json
import urllib.error

import pytest

import diff_preview as m

from test_coverage_orchestration import _quiet_iteration_edges  # noqa: E402
from test_cops2671_cov_dp_a import health, _get, _metric  # noqa: E402,F401

P = "acme_diff_preview_"


def _prom():
    with m._diff_stats_lock:
        return m.render_prometheus(dict(m._diff_stats))


def _type_of(text, name):
    for ln in text.split("\n"):
        if ln.startswith("# TYPE %s " % name):
            return ln.rsplit(" ", 1)[1]
    return None


@pytest.fixture()
def fresh(monkeypatch):
    for k, v in (("bb_write_failures", 0), ("poll_consecutive_failures", 0),
                 ("oci_selfcheck", None), ("oci_selfcheck_ok", None),
                 ("oci_selfcheck_at", None)):
        monkeypatch.setitem(m._diff_stats, k, v)


def test_the_three_metrics_are_declared_with_their_type():
    rows = {r[0]: (r[1], r[2]) for r in m._PROM_REGISTRY}
    assert rows["bb_write_failures"] == ("bb_write_failures_total", "counter")
    assert rows["poll_consecutive_failures"] == ("poll_consecutive_failures", "gauge")
    assert rows["oci_selfcheck_ok"] == ("oci_selfcheck_ok", "gauge")
    for key in ("bb_write_failures", "poll_consecutive_failures", "oci_selfcheck_ok"):
        assert key in m._diff_stats, key


# ── bb_write_failures_total ──────────────────────────────────────────────

def test_a_failed_write_counts_and_a_not_leader_skip_does_not(monkeypatch, fresh):
    def bb_500(method, path, **kw):
        raise urllib.error.HTTPError("https://api.bitbucket.org/x", 500,
                                     "boom", {}, None)
    monkeypatch.setattr(m, "bb", bb_500)
    assert m.post_build_status("a" * 40, "FAILED", "d", pr_id=1) == "transient"
    out = _prom()
    assert _metric(out, P + "bb_write_failures_total") == 1
    assert _type_of(out, P + "bb_write_failures_total") == "counter"

    monkeypatch.setattr(m, "_still_leader", lambda: False)
    assert m.post_build_status("a" * 40, "FAILED", "d", pr_id=1) == "skipped"
    assert m.upsert_comment(1, "body", existing_id=5) == "skipped"
    assert _metric(_prom(), P + "bb_write_failures_total") == 1


# ── oci_selfcheck_ok ─────────────────────────────────────────────────────

def test_the_selfcheck_gauge_is_absent_before_a_check(monkeypatch, fresh):
    """A skipped check (no chart reference yet) is not a check."""
    monkeypatch.delenv("DIFF_OCI_SELFCHECK_REF", raising=False)
    monkeypatch.setattr(m, "_last_pull_ok_ref", None)
    assert P + "oci_selfcheck_ok" not in _prom()
    assert m._oci_selfcheck() is None
    assert m._diff_stats["oci_selfcheck_ok"] is None
    assert P + "oci_selfcheck_ok" not in _prom()


@pytest.mark.parametrize("rc,want", [(0, 1), (1, 0)])
def test_the_selfcheck_gauge_is_one_or_zero_after_a_check(monkeypatch, fresh, rc, want):
    monkeypatch.delenv("DIFF_OCI_SELFCHECK_REF", raising=False)
    monkeypatch.setattr(m, "_last_pull_ok_ref", ("reg.example.com", "appspace-ms", "1.2.3"))
    monkeypatch.setattr(m, "_helm_login", lambda registry: True)

    class R:
        returncode = rc
        stdout = ""
        stderr = "403 unauthorized"
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: R())
    assert m._oci_selfcheck() is bool(rc == 0)
    assert m._diff_stats["oci_selfcheck_ok"] == want
    out = _prom()
    assert _metric(out, P + "oci_selfcheck_ok") == want
    assert _type_of(out, P + "oci_selfcheck_ok") == "gauge"


# ── poll_consecutive_failures ────────────────────────────────────────────

def test_the_poll_gauge_rises_on_failed_polls_and_resets(monkeypatch, fresh):
    monkeypatch.setattr(m, "_consecutive_poll_fails", 0)
    monkeypatch.setattr(m, "_last_poll_ok", True)
    monkeypatch.setattr(m, "mirror_sync", lambda repo: None)
    _quiet_iteration_edges(monkeypatch, [], [])
    good_http = m.http

    def down(*a, **kw):
        raise urllib.error.HTTPError("https://api.bitbucket.org/x", 503,
                                     "down", {}, None)
    monkeypatch.setattr(m, "http", down)
    m.main_iteration()
    m.main_iteration()
    out = _prom()
    assert _metric(out, P + "poll_consecutive_failures") == 2
    assert _type_of(out, P + "poll_consecutive_failures") == "gauge"

    monkeypatch.setattr(m, "http", good_http)
    m.main_iteration()
    assert _metric(_prom(), P + "poll_consecutive_failures") == 0


# ── the live endpoints ───────────────────────────────────────────────────

def test_both_endpoints_serve_the_new_values(health, monkeypatch, fresh):
    monkeypatch.setitem(m._diff_stats, "bb_write_failures", 3)
    monkeypatch.setitem(m._diff_stats, "poll_consecutive_failures", 2)
    monkeypatch.setitem(m._diff_stats, "oci_selfcheck_ok", 0)
    _c, _h, body = _get(health + "/metrics")
    text = body.decode()
    assert _metric(text, P + "bb_write_failures_total") == 3
    assert _metric(text, P + "poll_consecutive_failures") == 2
    assert _metric(text, P + "oci_selfcheck_ok") == 0
    _c, _h, body = _get(health + "/diff-preview/stats")
    stats = json.loads(body)
    assert (stats["bb_write_failures"], stats["poll_consecutive_failures"],
            stats["oci_selfcheck_ok"]) == (3, 2, 0)
