"""v2.5.20 (E1) — HTTP connection pooling regression tests.

E1 from bughunt/FINDINGS_IMPROVEMENTS.md: http() and the raw Bitbucket
file fetch open a fresh TCP+TLS connection per call (urllib.request.urlopen).
A mass-PR pass makes ~2-3K Bitbucket calls, each paying ~100-300ms of pure
handshake overhead.

Fix under test: _pooled_urlopen(), a per-thread http.client.HTTPSConnection
kept in threading.local(), with transparent reconnect-on-error and a plain
urllib fallback on anything unexpected. Confirmed RED against v2.5.19
before the implementation existed.
"""
import io
import os
import sys
import threading
import urllib.error
import urllib.request

import pytest

os.environ.setdefault("BB_USER", "test-user")
os.environ.setdefault("BB_TOKEN", "test-token")
os.environ.setdefault("ARGOCD_PASS", "test-pass")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m


@pytest.fixture(autouse=True)
def _pooling_on(monkeypatch):
    """conftest.py turns pooling OFF suite-wide (see its docstring); the
    tests in THIS file are the ones exercising the pool, so re-enable it.
    test_e1_pooling_can_be_disabled_by_env overrides it back to False."""
    monkeypatch.setattr(m, "HTTP_POOLING_ENABLED", True)


class _FakeHTTPResponse:
    """Mimics http.client.HTTPResponse just enough for _pooled_urlopen."""
    def __init__(self, status=200, body=b'{"ok": true}', headers=None,
                 reason="OK"):
        self.status = status
        self.reason = reason
        import email.message
        msg = email.message.Message()
        for k, v in (headers or {}).items():
            msg[k] = v
        self.headers = msg
        self._body = body

    def read(self):
        return self._body


class _FakeConn:
    """Mimics http.client.HTTPSConnection. Records requests; can be told
    to fail N times before succeeding."""
    instances = []

    def __init__(self, host, timeout=None, context=None, fail_times=0,
                 response=None):
        self.host = host
        self.timeout = timeout
        self.requests = []
        self.closed = False
        self.fail_times = fail_times
        self.response = response or _FakeHTTPResponse()
        _FakeConn.instances.append(self)

    def request(self, method, path, body=None, headers=None):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionResetError("stale keep-alive")
        self.requests.append((method, path, body, dict(headers or {})))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def _fresh_pool():
    """Reset the per-thread pool so each test starts clean."""
    if hasattr(m._http_conn_local, "conns"):
        del m._http_conn_local.conns


def _req(url="https://api.bitbucket.org/2.0/repositories/x/y/pullrequests",
         method="GET", data=None, headers=None):
    return urllib.request.Request(url, data=data, headers=headers or {},
                                  method=method)


def test_e1_pooled_urlopen_exists():
    assert hasattr(m, "_pooled_urlopen"), \
        "E1 fix missing: _pooled_urlopen() not implemented"
    assert hasattr(m, "_http_conn_local"), \
        "E1 fix missing: per-thread pool _http_conn_local not implemented"


def test_e1_connection_reused_within_thread(monkeypatch):
    _FakeConn.instances = []
    monkeypatch.setattr(m._http_client, "HTTPSConnection", _FakeConn)
    _fresh_pool()

    with m._pooled_urlopen(_req(), timeout=60) as r1:
        assert r1.read() == b'{"ok": true}'
    with m._pooled_urlopen(_req(), timeout=60) as r2:
        assert r2.read() == b'{"ok": true}'

    assert len(_FakeConn.instances) == 1, \
        f"expected 1 pooled connection, got {len(_FakeConn.instances)}"
    assert len(_FakeConn.instances[0].requests) == 2


def test_e1_reconnect_on_stale_connection(monkeypatch):
    """First request on a reused connection dies (stale keep-alive) —
    must retry transparently on a fresh connection, not raise."""
    _FakeConn.instances = []

    def factory(host, timeout=None, context=None):
        # first instance fails its first request, the replacement succeeds
        fail = 1 if not _FakeConn.instances else 0
        return _FakeConn(host, timeout=timeout, fail_times=fail)

    monkeypatch.setattr(m._http_client, "HTTPSConnection", factory)
    _fresh_pool()

    with m._pooled_urlopen(_req(), timeout=60) as r:
        assert r.read() == b'{"ok": true}'
    assert len(_FakeConn.instances) == 2
    assert _FakeConn.instances[0].closed, "stale connection must be closed"


def test_e1_fallback_to_urllib_when_pool_broken(monkeypatch):
    """If a fresh connection ALSO fails, fall back to plain urlopen —
    pooling must never make a request fail that urllib could serve."""
    _FakeConn.instances = []
    monkeypatch.setattr(
        m._http_client, "HTTPSConnection",
        lambda host, timeout=None, context=None:
            _FakeConn(host, timeout=timeout, fail_times=99))

    sentinel = object()
    calls = []

    def fake_urlopen(req, context=None, timeout=None):
        calls.append(req)
        return sentinel

    monkeypatch.setattr(m.urllib.request, "urlopen", fake_urlopen)
    _fresh_pool()

    assert m._pooled_urlopen(_req(), timeout=60) is sentinel
    assert len(calls) == 1


def test_e1_http_error_raised_with_code_and_headers(monkeypatch):
    """Non-2xx must surface as urllib.error.HTTPError so http()'s existing
    retry/Retry-After logic keeps working unchanged."""
    _FakeConn.instances = []
    resp = _FakeHTTPResponse(status=429, body=b"slow down",
                             headers={"Retry-After": "7"},
                             reason="Too Many Requests")
    monkeypatch.setattr(
        m._http_client, "HTTPSConnection",
        lambda host, timeout=None, context=None:
            _FakeConn(host, timeout=timeout, response=resp))
    _fresh_pool()

    try:
        m._pooled_urlopen(_req(), timeout=60)
        assert False, "expected HTTPError"
    except urllib.error.HTTPError as e:
        assert e.code == 429
        assert e.headers.get("Retry-After") == "7"
        assert e.read() == b"slow down"


def test_e1_redirects_fall_back_to_urllib(monkeypatch):
    """http.client does not follow redirects; a 3xx must be re-served via
    plain urlopen, which does."""
    _FakeConn.instances = []
    resp = _FakeHTTPResponse(status=302, body=b"",
                             headers={"Location": "https://elsewhere"})
    monkeypatch.setattr(
        m._http_client, "HTTPSConnection",
        lambda host, timeout=None, context=None:
            _FakeConn(host, timeout=timeout, response=resp))

    sentinel = object()
    monkeypatch.setattr(m.urllib.request, "urlopen",
                        lambda req, context=None, timeout=None: sentinel)
    _fresh_pool()

    assert m._pooled_urlopen(_req(), timeout=60) is sentinel


def test_e1_threads_get_isolated_connections(monkeypatch):
    _FakeConn.instances = []
    monkeypatch.setattr(m._http_client, "HTTPSConnection", _FakeConn)
    _fresh_pool()

    def worker():
        with m._pooled_urlopen(_req(), timeout=60) as r:
            r.read()

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(_FakeConn.instances) == 3, \
        "each thread must own its connection (threading.local isolation)"


def test_e1_pooling_can_be_disabled_by_env(monkeypatch):
    """DIFF_HTTP_POOLING=off is the operator escape hatch: everything goes
    straight to urllib, no pooled connections created."""
    _FakeConn.instances = []
    monkeypatch.setattr(m._http_client, "HTTPSConnection", _FakeConn)
    sentinel = object()
    monkeypatch.setattr(m.urllib.request, "urlopen",
                        lambda req, context=None, timeout=None: sentinel)
    monkeypatch.setattr(m, "HTTP_POOLING_ENABLED", False)
    _fresh_pool()

    assert m._pooled_urlopen(_req(), timeout=60) is sentinel
    assert len(_FakeConn.instances) == 0


def test_e1_http_uses_the_pool(monkeypatch):
    """End-to-end: http() must route through _pooled_urlopen."""
    called = []

    class _R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"routed": "pooled"}'

    def fake_pooled(req, timeout=60):
        called.append(req.full_url)
        return _R()

    monkeypatch.setattr(m, "_pooled_urlopen", fake_pooled)
    out = m.http("GET", "https://api.bitbucket.org/2.0/x")
    assert out == {"routed": "pooled"}
    assert called == ["https://api.bitbucket.org/2.0/x"]


def test_e1_stats_counters_exist():
    """Pool observability in /stats from day one (same spirit as M8)."""
    for key in ("http_pool_reuses", "http_pool_fresh_conns",
                "http_pool_fallbacks"):
        assert key in m._diff_stats, f"missing /stats counter: {key}"
