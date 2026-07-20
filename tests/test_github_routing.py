"""diff_preview-level GitHub routing tests (COPS-2520).

These cover the provider-SELECTION branches the existing Bitbucket suite never
exercises: the GitHub-provider factory, _provider_for_repo / _provider_transport
when a GitHub repo is configured, and the webhook handler picking the right
provider by event header. The provider implementations themselves are covered
by test_github_provider.py; here we only prove the core routes to them.
"""
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
from github_provider import GitHubProvider  # noqa: E402


def _gh():
    return GitHubProvider(owner="appspace-cloud", default_repo="acme-config-dev", token="t")


# ── real health server on an ephemeral port (same pattern as edges) ───────

@pytest.fixture()
def health(monkeypatch):
    monkeypatch.setattr(m, "_jfrog_hard_refresh", lambda name, ver: None)
    srv = m._start_health_server(0)
    port = srv.server_address[1]
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


def _req(url, method="GET", body=None, headers=None):
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ── factory: configured vs unconfigured ──────────────────────────────────

def test_build_github_provider_none_when_unconfigured():
    assert m._build_github_provider(set(), "", "owner", "def-repo") is None


def test_build_github_provider_default_repo_from_repos_set():
    p = m._build_github_provider({"repo-a"}, "", "owner", "def-repo")
    assert isinstance(p, GitHubProvider)
    # default_repo is taken from the GITHUB_REPOS set when it is non-empty.
    assert p.api_base() == "https://api.github.com/repos/owner/repo-a"


def test_build_github_provider_token_only_uses_default_repo():
    p = m._build_github_provider(set(), "tok", "owner", "def-repo")
    assert isinstance(p, GitHubProvider)
    assert p.api_base() == "https://api.github.com/repos/owner/def-repo"


# ── _provider_for_repo / _provider_transport ─────────────────────────────

def test_provider_for_repo_routes_configured_github_repo(monkeypatch):
    gh = _gh()
    monkeypatch.setattr(m, "_github_provider", gh)
    monkeypatch.setattr(m, "_GITHUB_REPOS", {"acme-config-dev"})
    assert m._provider_for_repo("acme-config-dev") is gh
    # A repo NOT in the GitHub set stays on Bitbucket.
    assert m._provider_for_repo("acme-config-stage") is m._bitbucket_provider


def test_provider_for_repo_falls_back_when_github_unconfigured(monkeypatch):
    monkeypatch.setattr(m, "_github_provider", None)
    assert m._provider_for_repo("acme-config-dev") is m._bitbucket_provider


def test_provider_transport_http_for_github_bb_for_bitbucket():
    assert m._provider_transport(m._bitbucket_provider) is m.bb
    assert m._provider_transport(_gh()) is m.http


def test_get_open_prs_routes_to_github_provider(monkeypatch):
    gh = _gh()
    monkeypatch.setattr(m, "_github_provider", gh)
    monkeypatch.setattr(m, "_GITHUB_REPOS", {"acme-config-dev"})

    def fake_http(method, url, **kw):
        # GitHub open-PRs endpoint with page-number pagination; a short page
        # ends the scan. Headers (not auth tuple) prove the GitHub transport.
        assert "/repos/appspace-cloud/acme-config-dev/pulls" in url
        assert kw["headers"]["Authorization"] == "Bearer t"
        return [{"number": 11}]

    monkeypatch.setattr(m, "http", fake_http)
    prs = m.get_open_prs("acme-config-dev")
    assert [pr["number"] for pr in prs] == [11]


# ── webhook: provider selection by event header ──────────────────────────

def test_github_webhook_wakes_loop_when_configured(health, monkeypatch):
    monkeypatch.setattr(m, "_github_provider", _gh())
    monkeypatch.setattr(m, "GH_WEBHOOK_SECRET", "")  # permissive
    m._wake.clear()
    body = json.dumps({"action": "opened", "pull_request": {"number": 1}}).encode()
    code, _ = _req(f"{health}/diff-preview/webhook", "POST", body,
                   {"Content-Length": str(len(body)),
                    "X-GitHub-Event": "pull_request"})
    assert code == 200
    assert m._wake.is_set(), "a GitHub pull_request event must wake the diff loop"
    m._wake.clear()


def test_github_webhook_rejects_bad_signature_when_secret_set(health, monkeypatch):
    monkeypatch.setattr(m, "_github_provider", _gh())
    monkeypatch.setattr(m, "GH_WEBHOOK_SECRET", "s3cret")
    body = b'{"action":"opened"}'
    code, _ = _req(f"{health}/diff-preview/webhook", "POST", body,
                   {"Content-Length": str(len(body)),
                    "X-GitHub-Event": "pull_request",
                    "X-Hub-Signature-256": "sha256=deadbeef"})
    assert code == 401


def test_webhook_uses_bitbucket_when_github_configured_but_bb_request(health, monkeypatch):
    # GitHub provider is configured, but the request carries Bitbucket's
    # X-Event-Key and no X-GitHub-Event, so the handler must use Bitbucket.
    monkeypatch.setattr(m, "_github_provider", _gh())
    monkeypatch.setattr(m, "BB_WEBHOOK_SECRET", "")
    m._wake.clear()
    body = json.dumps({"pullrequest": {"id": 1}}).encode()
    code, _ = _req(f"{health}/diff-preview/webhook", "POST", body,
                   {"Content-Length": str(len(body)),
                    "X-Event-Key": "pullrequest:created"})
    assert code == 200
    assert m._wake.is_set()
    m._wake.clear()
