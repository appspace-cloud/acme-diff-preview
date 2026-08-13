"""COPS-2576 - a standby's 5s reactive wait must not count as safety-net ticks.

COPS-2575 added iteration-trigger counters so a silently dead Bitbucket
webhook shows up as a climbing safety-net-to-webhook ratio. A standby,
though, waits 5s (a leadership-handoff poll) instead of the leader's 60s
safety net, so its timeouts inflated iters_safetynet_triggered about twelve
times faster than the leader's real ticks (measured live on the na1-a hub:
standby 414, leader 48, same 34-minute window). A webhook wake on a standby
has the mirror problem: it inflates iters_webhook_triggered and makes the
ratio look healthier than it is.

The invariant this file defends: the two leader counters are leader-only.
A standby pass, however its wait ended, counts once in iters_standby_wait
and nowhere else, so the ratio is meaningful on any pod with no is_leader
filter.
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m  # noqa: E402
import logsink

COUNTERS = ("iters_webhook_triggered", "iters_safetynet_triggered",
            "iters_standby_wait")


class _Elector:
    def __init__(self, leading):
        self.leading = leading

    def is_leader(self):
        return self.leading

    def start(self):
        pass

    def release(self):
        pass


class _Wake:
    """Doubles _wake: ends the loop after one wait, mimicking a timeout
    (woken=False) or a webhook wake (woken=True)."""

    def __init__(self, woken):
        self.woken = woken
        self.timeouts = []

    def wait(self, timeout=None):
        self.timeouts.append(timeout)
        m._shutdown = True
        return self.woken

    def clear(self):
        pass


def _run_one_pass(monkeypatch, leading, woken):
    """Run main() for exactly one loop pass and return counter deltas."""
    monkeypatch.setattr(
        m, "_start_health_server",
        lambda *a, **k: type("S", (), {"shutdown": lambda self: None})())
    monkeypatch.setattr(m, "_start_heartbeat", lambda: None)
    monkeypatch.setattr(m, "argocd_login", lambda: None)
    monkeypatch.setattr(m, "OCI_USER", "user")
    monkeypatch.setattr(m, "OCI_PASS", "secret")
    monkeypatch.setattr(m, "_start_oci_selfcheck_loop", lambda: None)
    monkeypatch.setattr(logsink, "log", lambda *a, **k: None)
    monkeypatch.setattr(m, "_make_leader_elector", lambda: _Elector(leading))
    wake = _Wake(woken)
    monkeypatch.setattr(m, "_wake", wake)
    monkeypatch.setattr(m, "main_iteration", lambda: None)
    with m._diff_stats_lock:
        before = {k: m._diff_stats.get(k, 0) or 0 for k in COUNTERS}
    saved = m._shutdown
    m._shutdown = False
    try:
        m.main()
    finally:
        m._shutdown = saved
    with m._diff_stats_lock:
        after = {k: m._diff_stats.get(k, 0) or 0 for k in COUNTERS}
        trigger = m._diff_stats.get("last_iteration_trigger")
    delta = {k: after[k] - before[k] for k in COUNTERS}
    return delta, trigger, wake


def test_standby_timeout_is_not_a_safety_net_tick(monkeypatch):
    delta, trigger, wake = _run_one_pass(monkeypatch, leading=False,
                                         woken=False)
    assert wake.timeouts == [5]
    assert delta["iters_safetynet_triggered"] == 0
    assert delta["iters_webhook_triggered"] == 0
    assert delta["iters_standby_wait"] == 1
    assert trigger == "standby_wait"


def test_standby_webhook_wake_is_not_a_webhook_iteration(monkeypatch):
    delta, trigger, _ = _run_one_pass(monkeypatch, leading=False, woken=True)
    assert delta["iters_webhook_triggered"] == 0
    assert delta["iters_safetynet_triggered"] == 0
    assert delta["iters_standby_wait"] == 1
    assert trigger == "standby_wait"


def test_leader_timeout_still_counts_as_safety_net(monkeypatch):
    delta, trigger, wake = _run_one_pass(monkeypatch, leading=True,
                                         woken=False)
    assert wake.timeouts == [60]
    assert delta["iters_safetynet_triggered"] == 1
    assert delta["iters_webhook_triggered"] == 0
    assert delta["iters_standby_wait"] == 0
    assert trigger == "safety_net"


def test_leader_webhook_wake_still_counts_as_webhook(monkeypatch):
    delta, trigger, _ = _run_one_pass(monkeypatch, leading=True, woken=True)
    assert delta["iters_webhook_triggered"] == 1
    assert delta["iters_safetynet_triggered"] == 0
    assert delta["iters_standby_wait"] == 0
    assert trigger == "webhook"


def test_stats_endpoint_exposes_the_standby_counter():
    srv = m._start_health_server(0)
    try:
        port = srv.server_address[1]
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/diff-preview/stats",
                timeout=10) as r:
            payload = json.loads(r.read())
        assert "iters_standby_wait" in payload, (
            "/diff-preview/stats must expose the standby wait counter")
    finally:
        srv.shutdown()
