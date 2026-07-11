"""v2.5.21 — pool hardening + ReDoS fix regression tests.

Findings from the five-pass adversarial review of v2.5.20:

F1 (HIGH, DoS) — _redact_error_detail ran its regex over the FULL untruncated
    helm stderr before the [:400] truncation at the call site. The prefix
    `[A-Za-z0-9_.\\-]*` before the alternation backtracks quadratically on a
    dashed near-miss run (`aaa-aaa-...`), trivially placed in a values file
    helm then echoes. Measured 20K->8s, 40K->33s, 80K->132s. A ~200KB blob
    pinned a worker thread for ~15 min. Fix: bound the input before the regex.

F2 (LOW-MED, FD leak) — ephemeral ThreadPoolExecutors spawn workers that each
    stash an HTTPSConnection in threading.local(); when the pool is torn down
    the threads die with sockets still open. Fix: _close_pooled_connections()
    the calling thread can run, wired into the per-diff pools.

F3 (LOW, correctness) — the pool used raw http.client, which ignores
    HTTPS_PROXY/HTTP_PROXY. urlopen honors them. Fix: when a proxy is
    configured, fall straight back to urlopen (counted as a fallback).

F4 (INFO, folded in) — keying by (host, timeout) held two live sockets per
    worker to the same host. Fix: key by host only, set timeout per-request.
"""
import os
import sys
import time
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
    """conftest.py disables pooling suite-wide; the pool tests re-enable it."""
    monkeypatch.setattr(m, "HTTP_POOLING_ENABLED", True)


# ── F1: ReDoS in _redact_error_detail ────────────────────────────────

def test_f1_redact_error_detail_bounds_input_size():
    """A large dashed near-miss run must NOT be regexed in full. With the
    input bounded, even a pathological 400KB blob returns near-instantly."""
    evil = "aaa-" * 100_000            # 400KB, the quadratic-worst shape
    t0 = time.monotonic()
    out = m._redact_error_detail(evil)
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"redaction took {elapsed:.1f}s — input not bounded"
    # And it still returns a bounded, safe string.
    assert len(out) <= m._REDACT_DETAIL_MAX_CHARS


def test_f1_redact_error_detail_still_masks_secret():
    """Bounding must not weaken redaction: a secret in the (bounded) head is
    still masked."""
    detail = "Error: yaml: line 5: password: hunter2trustno1 is invalid"
    out = m._redact_error_detail(detail)
    assert "hunter2trustno1" not in out
    assert "[REDACTED]" in out
    assert "password" in out            # key name kept for diagnosis


def test_f1_redact_error_detail_masks_secret_within_head():
    """A secret inside the retained head is masked even when a long benign
    tail follows (the tail is dropped before the regex)."""
    detail = "token=supersecretvalue " + ("x" * 300_000)
    t0 = time.monotonic()
    out = m._redact_error_detail(detail)
    assert time.monotonic() - t0 < 1.0
    assert "supersecretvalue" not in out


# ── F2: pooled-connection cleanup ────────────────────────────────────

def test_f2_close_pooled_connections_exists_and_clears():
    assert hasattr(m, "_close_pooled_connections")
    # Seed a fake connection into this thread's pool, then close.
    closed = []

    class _C:
        def close(self): closed.append(True)

    m._http_conn_local.conns = {"api.bitbucket.org": _C()}
    m._close_pooled_connections()
    assert closed == [True], "connection was not closed"
    assert getattr(m._http_conn_local, "conns", {}) == {}, "pool not cleared"


def test_f2_close_pooled_connections_safe_when_empty():
    """No pool on this thread yet -> no error."""
    if hasattr(m._http_conn_local, "conns"):
        del m._http_conn_local.conns
    m._close_pooled_connections()   # must not raise


def test_f2_close_survives_a_broken_close(monkeypatch):
    class _Bad:
        def close(self): raise OSError("already gone")

    m._http_conn_local.conns = {"h": _Bad()}
    m._close_pooled_connections()   # swallows the error
    assert getattr(m._http_conn_local, "conns", {}) == {}


# ── F3: proxy awareness ──────────────────────────────────────────────

def test_f3_proxy_env_forces_urllib_fallback(monkeypatch):
    """With HTTPS_PROXY set, the pool must defer to urlopen (which honors
    proxies) instead of connecting directly via http.client."""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")

    made_conn = []
    monkeypatch.setattr(
        m._http_client, "HTTPSConnection",
        lambda *a, **k: made_conn.append(a) or (_ for _ in ()).throw(
            AssertionError("must not open a direct connection under a proxy")))

    sentinel = object()
    monkeypatch.setattr(m.urllib.request, "urlopen",
                        lambda req, context=None, timeout=None: sentinel)
    if hasattr(m._http_conn_local, "conns"):
        del m._http_conn_local.conns

    req = urllib.request.Request("https://api.bitbucket.org/2.0/x")
    before = m._diff_stats["http_pool_fallbacks"]
    assert m._pooled_urlopen(req, timeout=60) is sentinel
    assert made_conn == []
    assert m._diff_stats["http_pool_fallbacks"] == before + 1


def test_f3_no_proxy_still_pools(monkeypatch):
    """Sanity: without a proxy, pooling still happens (no regression)."""
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)

    class _Resp:
        status = 200
        import email.message
        headers = email.message.Message()
        def read(self): return b"{}"

    class _Conn:
        def __init__(self, *a, **k): pass
        def request(self, *a, **k): pass
        def getresponse(self): return _Resp()
        def close(self): pass

    monkeypatch.setattr(m._http_client, "HTTPSConnection", _Conn)
    if hasattr(m._http_conn_local, "conns"):
        del m._http_conn_local.conns
    req = urllib.request.Request("https://api.bitbucket.org/2.0/x")
    with m._pooled_urlopen(req, timeout=60) as r:
        assert r.read() == b"{}"


# ── F4: one connection per host, not per (host, timeout) ─────────────

def test_f4_one_connection_per_host_regardless_of_timeout(monkeypatch):
    class _Resp:
        status = 200
        import email.message
        headers = email.message.Message()
        def read(self): return b"{}"

    conns = []

    class _Conn:
        def __init__(self, host, timeout=None, context=None):
            self.host = host
            self.timeout = timeout
            conns.append(self)
        def request(self, *a, **k): pass
        def getresponse(self): return _Resp()
        def close(self): pass

    monkeypatch.setattr(m._http_client, "HTTPSConnection", _Conn)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    if hasattr(m._http_conn_local, "conns"):
        del m._http_conn_local.conns

    req = urllib.request.Request("https://api.bitbucket.org/2.0/x")
    m._pooled_urlopen(req, timeout=60).read()
    m._pooled_urlopen(req, timeout=20).read()   # different timeout, same host

    pool = m._http_conn_local.conns
    assert len(pool) == 1, f"expected 1 connection per host, got {len(pool)}"
    assert len(conns) == 1, "a second socket was opened for a different timeout"
