"""Bitbucket requests must carry an explicit User-Agent (COPS-2550).

Root cause, confirmed against a real Bitbucket support export (9270 requests,
2026-07-29): 7009 of them (all of diff-preview's own traffic) arrived at
Bitbucket with NO User-Agent at all, and Bitbucket's CloudFront front end
stamps those as `User-Agent: Amazon CloudFront`. When support was asked to
help find what was consuming the shared token's budget, our own traffic came
back labelled as a CDN, and the first reading blamed an unrelated AWS service.

_user_agent() and the header already existed and were already correct on the
http()/bb() path (PR listing, comments, build status, base sha), which is why
those calls were not the problem. The gap is `_bb_fetch_status`, the value-file
read hot path (6255 of the 9270 requests, 67%): it builds its own
urllib.request.Request directly, bypassing http() on purpose (to avoid
json.loads() on YAML/text content), and never carried the header.

Mechanism, confirmed by reading _pooled_urlopen: it does
`conn.request(..., headers=dict(req.header_items()))`, and Request.header_items()
returns ONLY headers set explicitly on the Request object -- the default
`Python-urllib/x.y` User-Agent is added by urlopen's opener, not by the
Request, so a request built with only {"Authorization": ...} and sent through
the pooled path carries no User-Agent whatsoever.
"""
import os
import sys

import pytest

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m


def test_user_agent_has_the_expected_name_and_version():
    ua = m._user_agent()
    assert ua == f"AppspaceAcmeDiffPreview/{m.APP_VERSION}"


def test_bb_fetch_status_request_carries_an_explicit_user_agent(monkeypatch):
    """The hot path: 67% of the traffic in the incident, and the one gap.
    Capture the actual Request object passed to the pooled opener and assert
    the header survives EXACTLY the code path Bitbucket's server would see."""
    seen = {}

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"x: 1"

    def fake_pooled(req, timeout=None):
        seen["headers"] = dict(req.header_items())
        return FakeResp()
    monkeypatch.setattr(m, "_pooled_urlopen", fake_pooled)

    m._bb_fetch_status("gcp/config.yaml", "sha1")
    # header_items() keys are Python's internal capitalisation; HTTP headers
    # are case-insensitive on the wire, but assert case-insensitively here so
    # this test does not silently depend on urllib's internal convention.
    lower = {k.lower(): v for k, v in seen["headers"].items()}
    assert "user-agent" in lower, f"no User-Agent sent: {seen['headers']}"
    assert lower["user-agent"] == m._user_agent()


def test_bb_fetch_status_user_agent_survives_the_urlopen_fallback(monkeypatch):
    """_pooled_urlopen falls back to plain urllib.request.urlopen for several
    reasons (pooling off, non-https, a proxy, a broken connection). The header
    must survive that path too, not just the pooled one."""
    seen = {}

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"x: 1"

    def fake_urlopen(req, context=None, timeout=None):
        seen["headers"] = dict(req.header_items())
        return FakeResp()
    monkeypatch.setattr(m.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(m, "HTTP_POOLING_ENABLED", False)

    m._bb_fetch_status("gcp/config.yaml", "sha1")
    lower = {k.lower(): v for k, v in seen["headers"].items()}
    assert lower.get("user-agent") == m._user_agent()


def test_http_helper_still_sets_user_agent_no_regression(monkeypatch):
    """http()/bb() already did this correctly before COPS-2550; pin it so a
    future refactor cannot quietly remove it while 'fixing' the other path."""
    seen = {}

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"{}"
        status = 200

    def fake_pooled(req, timeout=None):
        seen["headers"] = dict(req.header_items())
        return FakeResp()
    monkeypatch.setattr(m, "_pooled_urlopen", fake_pooled)

    m.http("GET", "https://api.bitbucket.org/2.0/repositories/x/y")
    lower = {k.lower(): v for k, v in seen["headers"].items()}
    assert lower.get("user-agent") == m._user_agent()


def test_caller_supplied_user_agent_is_not_overridden():
    """hdrs.setdefault must stay a setdefault: a caller-supplied UA (there is
    none today, but the contract matters) must win over the default."""
    import inspect
    src = inspect.getsource(m.http)
    assert 'hdrs.setdefault("User-Agent"' in src


def test_pod_to_pod_webhook_relay_is_unaffected():
    """The standby-to-leader relay (line ~347) is internal cluster traffic,
    never Bitbucket, and must not gain a Bitbucket User-Agent -- pinned so
    nobody 'fixes' it by mistake while touching this area."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "src",
                            "diff_preview.py")).read()
    i = src.index("X-ADP-Forwarded")
    block = src[max(0, i - 400):i + 200]
    assert "User-Agent" not in block


def test_argocd_session_fetch_is_unaffected():
    """_argocd_fetch_token talks to ArgoCD, not Bitbucket; out of scope."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "src",
                            "diff_preview.py")).read()
    i = src.index("def _argocd_fetch_token")
    block = src[i:i + 700]
    assert "User-Agent" not in block
