"""Leader election on a Kubernetes Lease (HA phase 0, v2.9.0).

Every test drives the elector one tick at a time with a fake clock and a
fake urlopen, so timing is deterministic and no test ever sleeps. The fake
API server keeps one lease object and enforces resourceVersion optimistic
concurrency exactly like the real API server (PUT with a stale
resourceVersion gets 409 Conflict, POST on an existing name gets 409
AlreadyExists).
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

import pytest

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import leader  # noqa: E402


MICROTIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")


class _FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _FakeResp:
    def __init__(self, payload=b"{}"):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeLeaseApi:
    """One lease object plus resourceVersion CAS, like the real API server."""

    def __init__(self):
        self.lease = None
        self.rv = 0
        self.calls = []          # (method, url, bearer)
        self.fail_with = None    # exception to raise for every call

    def urlopen(self, req, timeout=None):
        method = req.get_method()
        url = req.full_url
        self.calls.append((method, url, req.get_header("Authorization")))
        if self.fail_with is not None:
            raise self.fail_with
        if method == "GET":
            if self.lease is None:
                raise urllib.error.HTTPError(url, 404, "not found", {}, None)
            return _FakeResp(json.dumps(self.lease).encode())
        body = json.loads(req.data.decode())
        if method == "POST":
            if self.lease is not None:
                raise urllib.error.HTTPError(url, 409, "conflict", {}, None)
            self.rv += 1
            body.setdefault("metadata", {})["resourceVersion"] = str(self.rv)
            self.lease = body
            return _FakeResp(json.dumps(body).encode())
        if method == "PUT":
            sent_rv = body.get("metadata", {}).get("resourceVersion")
            if self.lease is None or sent_rv != str(self.rv):
                raise urllib.error.HTTPError(url, 409, "conflict", {}, None)
            self.rv += 1
            body["metadata"]["resourceVersion"] = str(self.rv)
            self.lease = body
            return _FakeResp(json.dumps(body).encode())
        raise AssertionError(f"unexpected method {method}")  # pragma: no cover

    @property
    def spec(self):
        return self.lease["spec"]


def _sa_dir(tmp_path, token="tok-1", namespace="argocd"):
    d = tmp_path / "sa"
    d.mkdir(exist_ok=True)
    (d / "token").write_text(token)
    (d / "namespace").write_text(namespace)
    return str(d)


def _elector(tmp_path, monkeypatch, api=None, clock=None, identity="pod-a",
             events=None, **kw):
    api = api if api is not None else _FakeLeaseApi()
    clock = clock or _FakeClock()
    monkeypatch.setattr(urllib.request, "urlopen", api.urlopen)
    el = leader.LeaderElector(
        "adp-leader", identity, sa_dir=_sa_dir(tmp_path), verify_tls=False,
        clock=clock, on_event=(events.append if events is not None else None),
        **kw)
    return el, api, clock


# -- single-process modes ---------------------------------------------------

def test_disabled_election_is_always_leader_without_network(tmp_path,
                                                            monkeypatch):
    def boom(*a, **kw):  # pragma: no cover - must never run
        raise AssertionError("network touched with election disabled")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    el = leader.LeaderElector("adp-leader", "pod-a",
                              sa_dir=_sa_dir(tmp_path), enabled=False)
    assert el.is_leader() is True
    el.start()   # must be a no-op, no thread
    assert el._thread is None
    el.stop()


def test_missing_serviceaccount_means_single_instance(tmp_path, monkeypatch):
    def boom(*a, **kw):  # pragma: no cover - must never run
        raise AssertionError("network touched with no ServiceAccount")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    warnings = []
    el = leader.LeaderElector("adp-leader", "pod-a",
                              sa_dir=str(tmp_path / "missing"),
                              on_event=warnings.append)
    assert el.is_leader() is True
    assert el.is_leader() is True   # second call: warning only once
    assert len([w for w in warnings if "single instance" in w]) == 1
    el.start()
    assert el._thread is None


# -- acquisition ------------------------------------------------------------

def test_acquires_lease_when_none_exists(tmp_path, monkeypatch):
    events = []
    el, api, clock = _elector(tmp_path, monkeypatch, events=events)
    el.tick()
    assert el.is_leader() is True
    assert api.spec["holderIdentity"] == "pod-a"
    assert api.spec["leaseDurationSeconds"] == 15
    assert api.spec["leaseTransitions"] == 0
    assert MICROTIME_RE.match(api.spec["acquireTime"])
    assert MICROTIME_RE.match(api.spec["renewTime"])
    assert any("acquired leadership" in e for e in events)


def test_create_race_loser_is_not_leader(tmp_path, monkeypatch):
    el, api, clock = _elector(tmp_path, monkeypatch)
    real_urlopen = api.urlopen

    def racing(req, timeout=None):
        if req.get_method() == "POST":
            raise urllib.error.HTTPError(req.full_url, 409, "exists", {}, None)
        return real_urlopen(req, timeout=timeout)
    monkeypatch.setattr(urllib.request, "urlopen", racing)
    el.tick()
    assert el.is_leader() is False


def test_takes_over_released_lease_with_empty_holder(tmp_path, monkeypatch):
    el, api, clock = _elector(tmp_path, monkeypatch)
    api.lease = {"metadata": {"name": "adp-leader", "namespace": "argocd",
                              "resourceVersion": "1"},
                 "spec": {"holderIdentity": "", "leaseDurationSeconds": 15,
                          "leaseTransitions": 3,
                          "renewTime": "2020-01-01T00:00:00.000000Z"}}
    api.rv = 1
    el.tick()
    assert el.is_leader() is True
    assert api.spec["holderIdentity"] == "pod-a"
    assert api.spec["leaseTransitions"] == 4


def test_does_not_steal_fresh_lease_from_other_holder(tmp_path, monkeypatch):
    el, api, clock = _elector(tmp_path, monkeypatch)
    el2, _, _ = _elector(tmp_path, monkeypatch, api=api, clock=clock,
                         identity="pod-b")
    el2.tick()                       # pod-b acquires first
    el.tick()                        # pod-a observes a fresh holder
    assert el.is_leader() is False
    assert api.spec["holderIdentity"] == "pod-b"


def test_takes_over_expired_lease_after_local_observation(tmp_path,
                                                          monkeypatch):
    el, api, clock = _elector(tmp_path, monkeypatch)
    el2, _, _ = _elector(tmp_path, monkeypatch, api=api, clock=clock,
                         identity="pod-b")
    el2.tick()                       # pod-b is leader, renewTime frozen now
    el.tick()                        # pod-a starts observing
    clock.advance(16)                # past leaseDurationSeconds without renew
    el.tick()
    assert el.is_leader() is True
    assert api.spec["holderIdentity"] == "pod-a"
    assert api.spec["leaseTransitions"] == 1


def test_observation_timer_resets_when_holder_renews(tmp_path, monkeypatch):
    el, api, clock = _elector(tmp_path, monkeypatch)
    el2, _, _ = _elector(tmp_path, monkeypatch, api=api, clock=clock,
                         identity="pod-b")
    el2.tick()
    el.tick()
    clock.advance(10)
    el2.tick()                       # pod-b renews in time
    el.tick()                        # pod-a sees a NEW renewTime: reset timer
    clock.advance(10)                # 20s since first obs, 10s since renewal
    el.tick()
    assert el.is_leader() is False
    assert api.spec["holderIdentity"] == "pod-b"


# -- renewal and self-demotion ----------------------------------------------

def test_renews_own_lease_and_keeps_acquire_time(tmp_path, monkeypatch):
    el, api, clock = _elector(tmp_path, monkeypatch)
    el.tick()
    first_acquire = api.spec["acquireTime"]
    first_renew = api.spec["renewTime"]
    clock.advance(2)
    el.tick()
    assert api.spec["acquireTime"] == first_acquire
    assert api.spec["renewTime"] != first_renew
    assert api.spec["leaseTransitions"] == 0
    assert el.is_leader() is True


def test_renew_conflict_means_leadership_lost(tmp_path, monkeypatch):
    events = []
    el, api, clock = _elector(tmp_path, monkeypatch, events=events)
    el.tick()
    real = api.urlopen

    def conflict_on_put(req, timeout=None):
        # Simulates a write landing between our GET and our PUT: the API
        # server answers the PUT with 409 Conflict.
        if req.get_method() == "PUT":
            raise urllib.error.HTTPError(req.full_url, 409, "conflict",
                                         {}, None)
        return real(req, timeout=timeout)
    monkeypatch.setattr(urllib.request, "urlopen", conflict_on_put)
    clock.advance(2)
    el.tick()                        # renew PUT hits 409: we lost the race
    assert el.is_leader() is False
    assert any("lost leadership" in e for e in events)


def test_self_demotes_when_renewals_keep_failing(tmp_path, monkeypatch):
    el, api, clock = _elector(tmp_path, monkeypatch)
    el.tick()
    assert el.is_leader() is True
    api.fail_with = urllib.error.URLError("api server down")
    clock.advance(2)
    el.tick()                        # tick fails, but deadline not passed yet
    assert el.is_leader() is True    # still within renew_deadline
    clock.advance(9)                 # 11s since last successful renew (>10)
    assert el.is_leader() is False   # self-demotion, no network needed


def test_tick_failures_never_raise_and_are_reported(tmp_path, monkeypatch):
    events = []
    el, api, clock = _elector(tmp_path, monkeypatch, events=events)
    api.fail_with = urllib.error.URLError("connection refused")
    el.tick()                        # must not raise
    assert el.is_leader() is False
    assert any("non-fatal" in e for e in events)


# -- auth and shutdown ------------------------------------------------------

def test_bearer_token_is_reread_from_disk_each_request(tmp_path, monkeypatch):
    el, api, clock = _elector(tmp_path, monkeypatch)
    el.tick()
    (tmp_path / "sa" / "token").write_text("tok-2")   # kubelet rotated it
    clock.advance(2)
    el.tick()
    bearers = {c[2] for c in api.calls}
    assert "Bearer tok-1" in bearers
    assert "Bearer tok-2" in bearers


def test_release_hands_over_and_clears_leadership(tmp_path, monkeypatch):
    events = []
    el, api, clock = _elector(tmp_path, monkeypatch, events=events)
    el.tick()
    assert el.is_leader() is True
    el.release()
    assert el.is_leader() is False
    assert api.spec["holderIdentity"] == ""
    assert any("released leadership" in e for e in events)


def test_release_when_not_leader_is_a_noop(tmp_path, monkeypatch):
    el, api, clock = _elector(tmp_path, monkeypatch)
    el.release()                     # never acquired anything
    assert api.lease is None
    assert [c for c in api.calls if c[0] in ("PUT", "POST")] == []


def test_release_failure_is_swallowed(tmp_path, monkeypatch):
    el, api, clock = _elector(tmp_path, monkeypatch)
    el.tick()
    api.fail_with = urllib.error.URLError("api down")
    el.release()                     # must not raise
    assert el.is_leader() is False   # locally demoted regardless


def test_event_hook_errors_never_propagate(tmp_path, monkeypatch):
    el, api, clock = _elector(tmp_path, monkeypatch)
    el._on_event = lambda m: 1 / 0
    el.tick()                        # acquisition fires the hook: must not raise
    assert el.is_leader() is True


def test_start_and_stop_run_real_thread(tmp_path, monkeypatch):
    el, api, clock = _elector(tmp_path, monkeypatch, retry_period=0.01)
    el.start()
    assert el._thread is not None
    for _ in range(200):
        if el.is_leader():
            break
        import time as _t
        _t.sleep(0.01)
    assert el.is_leader() is True
    el.stop()
    assert not el._thread.is_alive()


@pytest.mark.parametrize("method", ["GET", "POST", "PUT"])
def test_non_conflict_http_errors_hit_the_tick_guard(tmp_path, monkeypatch,
                                                     method):
    # 404 on GET is a miss and 409 on writes is the election itself; any
    # OTHER HTTP error must propagate to tick()'s guard and be reported.
    events = []
    el, api, clock = _elector(tmp_path, monkeypatch, events=events)
    if method == "PUT":
        el.tick()                    # become leader so the next tick renews
        clock.advance(2)
    real = api.urlopen

    def fail(req, timeout=None):
        if req.get_method() == method:
            raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, None)
        return real(req, timeout=timeout)
    monkeypatch.setattr(urllib.request, "urlopen", fail)
    el.tick()                        # must not raise
    assert any("non-fatal" in e for e in events)


# ── wiring into diff_preview ────────────────────────────────────────────────

import diff_preview as m  # noqa: E402


class _FakeElector:
    def __init__(self, leading):
        self.leading = leading
        self.released = 0

    def is_leader(self):
        return self.leading

    def release(self):
        self.released += 1


def test_factory_defaults(monkeypatch):
    monkeypatch.setenv("HOSTNAME", "pod-x")
    el = m._make_leader_elector()
    assert el._id == "pod-x"
    assert el._name == "acme-diff-preview-leader"
    assert el._enabled is True
    assert (el._duration, el._deadline, el._retry) == (15, 10, 2.0)


def test_factory_respects_env_overrides(monkeypatch):
    monkeypatch.setenv("LEADER_ELECTION_ENABLED", "false")
    monkeypatch.setenv("LEADER_LEASE_NAME", "custom-lease")
    monkeypatch.setenv("LEADER_LEASE_DURATION", "30")
    monkeypatch.setenv("LEADER_RENEW_DEADLINE", "20")
    monkeypatch.setenv("LEADER_RETRY_PERIOD", "5")
    el = m._make_leader_elector()
    assert el._enabled is False
    assert el._name == "custom-lease"
    assert (el._duration, el._deadline) == (30, 20)
    assert el._retry == 5.0


def test_iteration_gate_runs_only_for_the_leader():
    assert m._should_run_iteration(None) is True          # no elector wired
    assert m._should_run_iteration(_FakeElector(True)) is True
    assert m._should_run_iteration(_FakeElector(False)) is False


def test_sigterm_releases_leadership(monkeypatch):
    fake = _FakeElector(True)
    monkeypatch.setattr(m, "_leader", fake, raising=False)
    monkeypatch.setattr(m, "_shutdown", False)
    m._handle_sigterm(15, None)
    try:
        assert fake.released == 1
        assert m._shutdown is True
    finally:
        m._shutdown = False          # never poison the other tests


def test_stats_expose_leadership(health, monkeypatch):
    url, _ = health
    monkeypatch.setattr(m, "_leader", _FakeElector(False), raising=False)
    code, body = _req_stats(url)
    assert code == 200
    assert json.loads(body)["is_leader"] is False
    monkeypatch.setattr(m, "_leader", None, raising=False)
    code, body = _req_stats(url)
    assert json.loads(body)["is_leader"] is True   # no elector = single mode


def _req_stats(url):
    req = urllib.request.Request(f"{url}/diff-preview/stats")
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, r.read()


@pytest.fixture()
def health(monkeypatch):
    monkeypatch.setattr(m, "_jfrog_hard_refresh", lambda name, ver: None)
    srv = m._start_health_server(0)
    port = srv.server_address[1]
    yield f"http://127.0.0.1:{port}", None
    srv.shutdown()


def test_release_off_cluster_is_a_noop(tmp_path, monkeypatch):
    def boom(*a, **kw):  # pragma: no cover - must never run
        raise AssertionError("network touched off-cluster")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    el = leader.LeaderElector("adp-leader", "pod-a",
                              sa_dir=str(tmp_path / "missing"))
    el.release()                     # single-instance mode: nothing to do


def test_sigterm_without_elector_still_shuts_down(monkeypatch):
    monkeypatch.setattr(m, "_leader", None, raising=False)
    monkeypatch.setattr(m, "_shutdown", False)
    m._handle_sigterm(15, None)
    try:
        assert m._shutdown is True
    finally:
        m._shutdown = False


def test_main_standby_waits_short_then_takes_over(monkeypatch):
    """A non-leader main() pass must skip the iteration, log standby once,
    wait on the short 5s timeout, and run the iteration after takeover."""
    monkeypatch.setattr(m, "_start_health_server",
                        lambda *a, **k: type("S", (), {"shutdown": lambda self: None})())
    monkeypatch.setattr(m, "_start_heartbeat", lambda: None)
    monkeypatch.setattr(m, "argocd_login", lambda: None)
    monkeypatch.setattr(m, "OCI_USER", "user")
    monkeypatch.setattr(m, "OCI_PASS", "secret")
    monkeypatch.setattr(m, "_start_oci_selfcheck_loop", lambda: None)
    logs = []
    monkeypatch.setattr(m, "log", lambda msg, *a, **k: logs.append(str(msg)))

    answers = iter([False, True])
    fake = _FakeElector(False)
    fake.is_leader = lambda: next(answers)
    fake.start = lambda: None
    monkeypatch.setattr(m, "_make_leader_elector", lambda: fake)

    waits = []

    class _FakeWake:
        def wait(self, timeout=None):
            waits.append(timeout)

        def clear(self):
            pass
    monkeypatch.setattr(m, "_wake", _FakeWake())

    iterations = {"n": 0}

    def one_iteration():
        iterations["n"] += 1
        m._shutdown = True           # stop after the takeover pass
    monkeypatch.setattr(m, "main_iteration", one_iteration)
    saved = m._shutdown
    m._shutdown = False
    try:
        m.main()
    finally:
        m._shutdown = saved
    assert iterations["n"] == 1      # skipped as standby, ran as leader
    assert waits == [5]              # standby uses the short reactive wait
    assert any("standby: another replica owns the poll loop" in l
               for l in logs)
    assert any("now owns the poll loop" in l for l in logs)


def test_release_stops_the_election_for_good(tmp_path, monkeypatch):
    """After release() the elector must never re-acquire: the released
    lease belongs to the standby now. Without this, the dying pod's
    election thread would see the blank holder on its next tick and steal
    the lease right back while draining."""
    el, api, clock = _elector(tmp_path, monkeypatch)
    el.tick()
    assert el.is_leader() is True
    el.release()
    assert api.spec["holderIdentity"] == ""
    clock.advance(2)
    el.tick()                        # a stray tick after shutdown began
    assert el.is_leader() is False
    assert api.spec["holderIdentity"] == ""   # still free for the standby
    assert el._stop_event.is_set()   # the election loop is over


def test_release_on_a_standby_also_stops_the_election(tmp_path, monkeypatch):
    """A standby that got SIGTERM must stop competing: without this it
    could acquire leadership in the middle of its own drain."""
    el, api, clock = _elector(tmp_path, monkeypatch)
    el2, _, _ = _elector(tmp_path, monkeypatch, api=api, clock=clock,
                         identity="pod-b")
    el2.tick()                       # pod-b leads
    el.tick()                        # pod-a is standby
    el.release()                     # pod-a is shutting down
    el2.release()                    # pod-b releases: lease is now free
    clock.advance(2)
    el.tick()                        # stray tick on the dying standby
    assert el.is_leader() is False
    assert api.spec["holderIdentity"] == ""   # nobody stole it back


# ── leader identity lookup and webhook forwarding (HA hardening) ────────────

def test_current_holder_is_self_when_leading(tmp_path, monkeypatch):
    el, api, clock = _elector(tmp_path, monkeypatch)
    el.tick()
    assert el.current_holder() == "pod-a"


def test_current_holder_is_the_observed_leader_when_standby(tmp_path,
                                                            monkeypatch):
    el, api, clock = _elector(tmp_path, monkeypatch)
    el2, _, _ = _elector(tmp_path, monkeypatch, api=api, clock=clock,
                         identity="pod-b")
    el2.tick()                       # pod-b leads
    el.tick()                        # pod-a observes pod-b
    assert el.current_holder() == "pod-b"


def test_current_holder_is_empty_before_any_observation(tmp_path,
                                                        monkeypatch):
    el, api, clock = _elector(tmp_path, monkeypatch)
    assert el.current_holder() == ""


def test_pod_ip_fetches_the_pod_status(tmp_path, monkeypatch):
    calls = []

    def fake(req, timeout=None):
        calls.append(req.full_url)
        assert req.get_header("Authorization") == "Bearer tok-1"
        return _FakeResp(json.dumps(
            {"status": {"podIP": "10.32.6.99"}}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    el = leader.LeaderElector("adp-leader", "pod-a",
                              sa_dir=_sa_dir(tmp_path), verify_tls=False)
    assert el.pod_ip("pod-b") == "10.32.6.99"
    assert calls == [
        "https://kubernetes.default.svc/api/v1/namespaces/argocd/pods/pod-b"]


def test_pod_ip_rejects_empty_name(tmp_path, monkeypatch):
    el = leader.LeaderElector("adp-leader", "pod-a",
                              sa_dir=_sa_dir(tmp_path), verify_tls=False)
    with pytest.raises(ValueError):
        el.pod_ip("")


# ── standby forwards verified Bitbucket webhooks to the leader ──────────────

class _ForwardingElector(_FakeElector):
    def __init__(self, leading, holder="pod-l", ip="10.0.0.9"):
        super().__init__(leading)
        self.holder = holder
        self.ip = ip
        self.ip_lookups = []

    def current_holder(self):
        return self.holder

    def pod_ip(self, name):
        self.ip_lookups.append(name)
        if self.ip is None:
            raise urllib.error.URLError("pods get denied")
        return self.ip


def _post_webhook(url, marker=None):
    body = json.dumps({"pullrequest": {"id": 42}}).encode()
    headers = {"Content-Length": str(len(body)),
               "X-Event-Key": "pullrequest:updated",
               "X-Hub-Signature": "sha256=stub"}
    if marker:
        headers["X-ADP-Forwarded"] = marker
    req = urllib.request.Request(f"{url}/diff-preview/webhook", data=body,
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, body


def _capture_forwards(monkeypatch):
    real = urllib.request.urlopen
    captured = []

    def dispatch(req, timeout=None, **kw):
        url = req.full_url if hasattr(req, "full_url") else req
        if "10.0.0.9" in str(url):
            captured.append(req)
            return _FakeResp(b"")
        return real(req, timeout=timeout, **kw)
    monkeypatch.setattr(urllib.request, "urlopen", dispatch)
    return captured


def test_standby_forwards_webhook_to_leader_once(health, monkeypatch):
    url, _ = health
    monkeypatch.setattr(m, "BB_WEBHOOK_SECRET", "")
    monkeypatch.setattr(m, "_leader", _ForwardingElector(False),
                        raising=False)
    captured = _capture_forwards(monkeypatch)
    m._wake.clear()
    code, body = _post_webhook(url)
    assert code == 200
    assert m._wake.is_set()          # local wake still happens (harmless)
    assert len(captured) == 1
    fwd = captured[0]
    assert fwd.full_url == "http://10.0.0.9:8080/diff-preview/webhook"
    assert fwd.data == body          # exact original body, signature intact
    assert fwd.get_header("X-hub-signature") == "sha256=stub"
    assert fwd.get_header("X-adp-forwarded") == "1"
    m._wake.clear()


def test_leader_does_not_forward_its_own_webhooks(health, monkeypatch):
    url, _ = health
    monkeypatch.setattr(m, "BB_WEBHOOK_SECRET", "")
    monkeypatch.setattr(m, "_leader", _ForwardingElector(True),
                        raising=False)
    captured = _capture_forwards(monkeypatch)
    m._wake.clear()
    code, _ = _post_webhook(url)
    assert code == 200 and m._wake.is_set()
    assert captured == []
    m._wake.clear()


def test_forwarded_webhook_is_never_reforwarded(health, monkeypatch):
    url, _ = health
    monkeypatch.setattr(m, "BB_WEBHOOK_SECRET", "")
    monkeypatch.setattr(m, "_leader", _ForwardingElector(False),
                        raising=False)
    captured = _capture_forwards(monkeypatch)
    m._wake.clear()
    code, _ = _post_webhook(url, marker="1")
    assert code == 200 and m._wake.is_set()
    assert captured == []            # the marker breaks the relay chain
    m._wake.clear()


def test_forward_failure_falls_back_to_the_safety_net(health, monkeypatch):
    url, _ = health
    monkeypatch.setattr(m, "BB_WEBHOOK_SECRET", "")
    fake = _ForwardingElector(False, ip=None)   # pods get denied
    monkeypatch.setattr(m, "_leader", fake, raising=False)
    m._wake.clear()
    code, _ = _post_webhook(url)
    assert code == 200               # Bitbucket always gets its 200
    assert m._wake.is_set()          # local wake still set: net catches it
    assert fake.ip_lookups == ["pod-l"]
    m._wake.clear()


def test_no_elector_means_no_forwarding(health, monkeypatch):
    url, _ = health
    monkeypatch.setattr(m, "BB_WEBHOOK_SECRET", "")
    monkeypatch.setattr(m, "_leader", None, raising=False)
    m._wake.clear()
    code, _ = _post_webhook(url)
    assert code == 200 and m._wake.is_set()
    m._wake.clear()


def test_forwarder_declines_without_an_elector(monkeypatch):
    # Defensive guard: the handler never calls the forwarder with no
    # elector (the leadership gate short-circuits first), but the helper
    # must still be safe if called directly.
    monkeypatch.setattr(m, "_leader", None, raising=False)
    assert m._forward_webhook_to_leader(b"{}", {}) is False


def test_forwarder_declines_unknown_or_own_holder(monkeypatch):
    monkeypatch.setattr(m, "_leader", _ForwardingElector(False, holder=""),
                        raising=False)
    assert m._forward_webhook_to_leader(b"{}", {}) is False
    monkeypatch.setenv("HOSTNAME", "pod-self")
    monkeypatch.setattr(m, "_leader",
                        _ForwardingElector(False, holder="pod-self"),
                        raising=False)
    assert m._forward_webhook_to_leader(b"{}", {}) is False


# ── final review: release() must not race a concurrent tick() ──────────────

def test_release_joins_the_background_thread_before_touching_the_network(
        tmp_path, monkeypatch):
    """release() must synchronize with the background election thread
    BEFORE doing its own lease write. Without this, a tick() already in
    flight (e.g. a renewal) can win the resourceVersion race against
    release()'s write, leaving the lease renewed instead of released and
    forcing the standby to wait out the full lease duration instead of
    the instant handoff release() exists to provide.
    """
    el, api, clock = _elector(tmp_path, monkeypatch)
    el.tick()
    assert el.is_leader() is True

    class _FakeThread:
        def __init__(self):
            self.join_called = False

        def join(self, timeout=None):
            self.join_called = True

        def is_alive(self):
            return False

    fake_thread = _FakeThread()
    el._thread = fake_thread

    order = []
    real_get_lease = el._get_lease

    def spying_get_lease():
        order.append(("network", fake_thread.join_called))
        return real_get_lease()
    monkeypatch.setattr(el, "_get_lease", spying_get_lease)

    el.release()
    assert fake_thread.join_called is True
    assert order == [("network", True)]   # join happened BEFORE the GET


def test_release_stops_a_real_background_thread_before_returning(
        tmp_path, monkeypatch):
    """End-to-end companion to the ordering test above, with a real
    thread: once release() returns, the election thread must actually be
    gone, not just asked to stop."""
    el, api, clock = _elector(tmp_path, monkeypatch, retry_period=0.01)
    el.start()
    for _ in range(300):
        if el.is_leader():
            break
        time.sleep(0.01)
    assert el.is_leader() is True
    el.release()
    assert not el._thread.is_alive()
    assert api.spec["holderIdentity"] == ""


def test_release_from_within_the_election_thread_does_not_self_join(
        tmp_path, monkeypatch):
    """Guard against the failure mode the join fix could introduce: if
    release() is ever reached from inside the election thread itself, a
    self-join would raise RuntimeError. It must be skipped instead."""
    el, api, clock = _elector(tmp_path, monkeypatch)
    el.tick()
    el._thread = threading.current_thread()   # pretend we ARE that thread
    el.release()                              # must not raise
    assert el.is_leader() is False
    assert api.spec["holderIdentity"] == ""
