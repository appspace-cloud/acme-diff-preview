"""v2.5.22 (P3-fix) — proxy detection must ignore non-proxy *_proxy env vars.

The v2.5.21 P3 guard used urllib.request.getproxies(), which on Linux
lowercases EVERY env var ending in `_proxy` and treats it as a proxy.
Kubernetes injects service-discovery vars like
ARGOCD_AGENT_REDIS_PROXY_SERVICE_PORT_REDIS=6379 — ending in `_proxy`
after lowercasing — so getproxies() returned bogus entries and the guard
silently disabled ALL pooling in production (observed live: /stats showed
http_pool_fallbacks climbing while reuses/fresh stayed at 0). No functional
break (fallback == old urlopen path) but the entire E1 perf win was lost.

Fix: only treat the real, documented proxy vars as proxies —
HTTP_PROXY/HTTPS_PROXY/ALL_PROXY (and lowercase), honoring NO_PROXY for
the target host. Confirmed RED against v2.5.21.
"""
import os
import sys
import urllib.request

import pytest

os.environ.setdefault("BB_USER", "test-user")
os.environ.setdefault("BB_TOKEN", "test-token")
os.environ.setdefault("ARGOCD_PASS", "test-pass")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m


@pytest.fixture(autouse=True)
def _pooling_on(monkeypatch):
    monkeypatch.setattr(m, "HTTP_POOLING_ENABLED", True)
    # Clear any proxy-ish vars the host/CI might carry.
    for k in list(os.environ):
        if k.lower().endswith("_proxy") or k.lower() == "no_proxy":
            monkeypatch.delenv(k, raising=False)


def _seed_success(monkeypatch):
    class _Resp:
        status = 200
        import email.message
        headers = email.message.Message()
        def read(self): return b"{}"

    conns = []

    class _Conn:
        def __init__(self, *a, **k): conns.append(self)
        def request(self, *a, **k): pass
        def getresponse(self): return _Resp()
        def close(self): pass

    monkeypatch.setattr(m._http_client, "HTTPSConnection", _Conn)
    if hasattr(m._http_conn_local, "conns"):
        del m._http_conn_local.conns
    return conns


def test_p3fix_k8s_service_discovery_proxy_var_does_not_disable_pooling(monkeypatch):
    """The exact production trigger: a K8s service-discovery var that ends in
    `_PROXY` (so stdlib getproxies() wrongly treats it as a proxy) but is NOT
    a real HTTP proxy must NOT push requests to the urllib fallback.

    K8s injects, for a Service named `argocd-agent-redis-proxy`, an env var
    `ARGOCD_AGENT_REDIS_PROXY` (=host IP) plus the _SERVICE_* family. The
    bare one ends in `_PROXY`, and getproxies() maps scheme
    `argocd_agent_redis` -> that IP. Reproduce that exact shape."""
    monkeypatch.setenv("ARGOCD_AGENT_REDIS_PROXY", "10.32.5.7")
    monkeypatch.setenv("ARGOCD_AGENT_RESOURCE_PROXY", "10.32.5.8")
    # Sanity: stdlib really does see these as "proxies".
    assert urllib.request.getproxies_environment(), \
        "test precondition: stdlib must treat these as proxies"
    conns = _seed_success(monkeypatch)

    req = urllib.request.Request("https://api.bitbucket.org/2.0/x")
    before = m._diff_stats["http_pool_fallbacks"]
    with m._pooled_urlopen(req, timeout=60) as r:
        assert r.read() == b"{}"
    assert m._diff_stats["http_pool_fallbacks"] == before, \
        "a non-proxy *_proxy var wrongly forced the urllib fallback"
    assert len(conns) == 1, "pooling did not engage"


def test_p3fix_real_https_proxy_still_forces_fallback(monkeypatch):
    """A genuine HTTPS_PROXY must still defer to urlopen (P3 intent kept)."""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    sentinel = object()
    monkeypatch.setattr(m.urllib.request, "urlopen",
                        lambda req, context=None, timeout=None: sentinel)
    monkeypatch.setattr(
        m._http_client, "HTTPSConnection",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not connect directly under a real proxy")))
    if hasattr(m._http_conn_local, "conns"):
        del m._http_conn_local.conns
    req = urllib.request.Request("https://api.bitbucket.org/2.0/x")
    before = m._diff_stats["http_pool_fallbacks"]
    assert m._pooled_urlopen(req, timeout=60) is sentinel
    assert m._diff_stats["http_pool_fallbacks"] == before + 1


def test_p3fix_lowercase_https_proxy_also_honored(monkeypatch):
    monkeypatch.setenv("https_proxy", "http://proxy.internal:3128")
    sentinel = object()
    monkeypatch.setattr(m.urllib.request, "urlopen",
                        lambda req, context=None, timeout=None: sentinel)
    if hasattr(m._http_conn_local, "conns"):
        del m._http_conn_local.conns
    req = urllib.request.Request("https://api.bitbucket.org/2.0/x")
    assert m._pooled_urlopen(req, timeout=60) is sentinel


def test_p3fix_no_proxy_env_bypasses_the_proxy_for_that_host(monkeypatch):
    """If HTTPS_PROXY is set but NO_PROXY covers the target host, pool
    directly (urlopen would too)."""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    monkeypatch.setenv("NO_PROXY", "api.bitbucket.org,.internal")
    conns = _seed_success(monkeypatch)
    req = urllib.request.Request("https://api.bitbucket.org/2.0/x")
    before = m._diff_stats["http_pool_fallbacks"]
    with m._pooled_urlopen(req, timeout=60) as r:
        assert r.read() == b"{}"
    assert m._diff_stats["http_pool_fallbacks"] == before, \
        "NO_PROXY host should still pool directly"
    assert len(conns) == 1
