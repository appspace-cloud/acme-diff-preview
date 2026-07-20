"""Direct unit tests for GitHubProvider and the VCSProvider contract
(COPS-2520). Sibling of test_bitbucket_provider.py, same style: GitHubProvider
is exercised standalone, with fakes for the HTTP transport, so nothing here
touches the network. These are the ONLY tests that cover github_provider.py,
so every method and branch is exercised here directly.
"""
import hashlib
import hmac as hmac_mod
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from github_provider import GitHubProvider  # noqa: E402
from vcs_provider import VCSProvider  # noqa: E402


def _provider():
    return GitHubProvider(
        owner="appspace-cloud", default_repo="acme-config-dev", token="t",
    )


def test_github_provider_satisfies_vcs_provider_protocol():
    # Structural typing check: GitHubProvider does not inherit from
    # VCSProvider but must expose the same method names.
    assert isinstance(_provider(), VCSProvider)


def test_api_base_default_and_explicit_repo():
    p = _provider()
    assert p.api_base() == "https://api.github.com/repos/appspace-cloud/acme-config-dev"
    assert p.api_base("acme-config-stage") == \
        "https://api.github.com/repos/appspace-cloud/acme-config-stage"


def test_call_builds_url_and_passes_bearer_headers():
    p = _provider()
    seen = {}

    def fake_http(method, url, **kw):
        seen["method"], seen["url"], seen["kw"] = method, url, kw
        return {"ok": True}

    result = p.call("GET", "pulls/1", None, fake_http)
    assert result == {"ok": True}
    assert seen["method"] == "GET"
    assert seen["url"] == "https://api.github.com/repos/appspace-cloud/acme-config-dev/pulls/1"
    assert seen["kw"]["headers"]["Authorization"] == "Bearer t"
    assert seen["kw"]["headers"]["Accept"] == "application/vnd.github+json"
    assert seen["kw"]["headers"]["X-GitHub-Api-Version"] == "2022-11-28"


# ── PR-shape accessors ───────────────────────────────────────────────────

def test_pr_shape_accessors_read_github_native_fields():
    p = _provider()
    pr = {"number": 77, "title": "Bump chart",
          "head": {"sha": "abc123", "ref": "feature/x"},
          "base": {"ref": "main", "sha": "def456"}}
    assert p.pr_id(pr) == 77
    assert p.pr_source_sha(pr) == "abc123"
    assert p.pr_dest_branch(pr) == "main"
    assert p.pr_title(pr) == "Bump chart"


# ── list_open_prs (page-number pagination) ───────────────────────────────

def test_list_open_prs_stops_on_short_page():
    p = _provider()
    pages = [
        [{"number": 1}, {"number": 2}],  # short (< 50) -> last page
    ]
    calls = {"n": 0}

    def fake_http(method, url, **kw):
        data = pages[calls["n"]]
        calls["n"] += 1
        return data

    prs, hit_limit = p.list_open_prs(None, fake_http, max_pages=100)
    assert [pr["number"] for pr in prs] == [1, 2]
    assert hit_limit is False


def test_list_open_prs_reports_hit_page_limit_on_full_pages():
    p = _provider()
    full = [{"number": i} for i in range(50)]  # exactly per_page -> keep paging

    def fake_http(method, url, **kw):
        return full

    prs, hit_limit = p.list_open_prs(None, fake_http, max_pages=3)
    assert len(prs) == 150
    assert hit_limit is True


def test_list_open_prs_tolerates_non_list_response():
    p = _provider()

    def fake_http(method, url, **kw):
        return {"message": "Not Found"}  # not a list -> treated as empty page

    prs, hit_limit = p.list_open_prs(None, fake_http, max_pages=5)
    assert prs == []
    assert hit_limit is False


# ── get_pr_diffstat (GitHub rename/copy semantics) ───────────────────────

def test_get_pr_diffstat_records_rename_keeps_copy_as_change():
    p = _provider()
    page = [
        {"filename": "a/new.yaml", "previous_filename": "a/old.yaml", "status": "renamed"},
        {"filename": "b/copy.yaml", "previous_filename": "b/src.yaml", "status": "copied"},
        {"filename": "c/added.yaml", "status": "added"},
    ]
    calls = {"n": 0}

    def fake_http(method, url, **kw):
        calls["n"] += 1
        return page if calls["n"] == 1 else []

    files, renames, hit_limit = p.get_pr_diffstat(42, None, fake_http, max_pages=10)
    # Both old and new paths of a rename are kept; the copy keeps both paths
    # too but only the rename produces an old->new edge.
    assert set(files) == {"a/new.yaml", "a/old.yaml",
                          "b/copy.yaml", "b/src.yaml", "c/added.yaml"}
    assert renames == {"a/old.yaml": "a/new.yaml"}
    assert hit_limit is False


def test_get_pr_diffstat_hit_page_limit_on_full_page():
    p = _provider()
    full = [{"filename": f"f{i}.yaml", "status": "modified"} for i in range(100)]

    def fake_http(method, url, **kw):
        return full  # always a full page -> more pages assumed

    _, _, hit_limit = p.get_pr_diffstat(42, None, fake_http, max_pages=1)
    assert hit_limit is True


def test_get_pr_diffstat_tolerates_non_list_response():
    p = _provider()

    def fake_http(method, url, **kw):
        return {"message": "boom"}

    files, renames, hit_limit = p.get_pr_diffstat(42, None, fake_http, max_pages=5)
    assert files == [] and renames == {} and hit_limit is False


# ── branch head + raw file ───────────────────────────────────────────────

def test_get_branch_head_sha_reads_commit_sha():
    p = _provider()

    def fake_http(method, url, **kw):
        assert url.endswith("/branches/main")
        return {"commit": {"sha": "headsha"}}

    assert p.get_branch_head_sha("main", None, fake_http) == "headsha"


def test_raw_file_url_and_headers_uses_contents_api_and_raw_accept():
    p = _provider()
    url, headers = p.raw_file_url_and_headers("apps/dev/values file.yaml", "SHA9", "acme-config-dev")
    # Path segments are percent-encoded but slashes survive as separators; the
    # ref is fully quoted; the media type asks for the raw bytes.
    assert url == ("https://api.github.com/repos/appspace-cloud/acme-config-dev"
                   "/contents/apps/dev/values%20file.yaml?ref=SHA9")
    assert headers["Accept"] == "application/vnd.github.raw"
    assert headers["Authorization"] == "Bearer t"


# ── comments (normalized {"id","body"}) ──────────────────────────────────

def test_get_comment_normalizes_shape():
    p = _provider()

    def fake_http(method, url, **kw):
        assert url.endswith("/issues/comments/555")
        return {"id": 555, "body": "hello", "user": {"login": "bot"}}

    assert p.get_comment(9, 555, None, fake_http) == {"id": 555, "body": "hello"}


def test_iter_comments_paginates_and_normalizes():
    p = _provider()
    pages = [
        [{"id": 1, "body": "a"}, {"id": 2, "body": "b"}],  # would need 100 to page
    ]
    calls = {"n": 0}

    def fake_http(method, url, **kw):
        data = pages[calls["n"]]
        calls["n"] += 1
        return data

    out = list(p.iter_comments(9, None, fake_http, max_pages=5))
    assert out == [{"id": 1, "body": "a"}, {"id": 2, "body": "b"}]
    assert calls["n"] == 1  # stopped after the short page


def test_iter_comments_continues_on_full_page_then_stops():
    p = _provider()
    full = [{"id": i, "body": str(i)} for i in range(100)]
    calls = {"n": 0}

    def fake_http(method, url, **kw):
        calls["n"] += 1
        return full if calls["n"] == 1 else []

    out = list(p.iter_comments(9, None, fake_http, max_pages=5))
    assert len(out) == 100
    assert calls["n"] == 2  # full page -> asked for a second (empty) page


def test_iter_comments_tolerates_non_list_page():
    p = _provider()

    def fake_http(method, url, **kw):
        return {"message": "nope"}

    assert list(p.iter_comments(9, None, fake_http, max_pages=5)) == []


def test_create_comment_posts_body_and_returns_id():
    p = _provider()
    seen = {}

    def fake_http(method, url, **kw):
        seen["method"], seen["url"], seen["body"] = method, url, kw.get("body")
        return {"id": 321}

    out = p.create_comment(9, "the body", None, fake_http)
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/issues/9/comments")
    assert seen["body"] == {"body": "the body"}
    assert out == {"id": 321, "body": "the body"}


def test_create_comment_handles_non_dict_response():
    p = _provider()

    def fake_http(method, url, **kw):
        return None  # some transports return no JSON

    assert p.create_comment(9, "b", None, fake_http) == {"id": None, "body": "b"}


def test_update_comment_patches_the_comment():
    p = _provider()
    seen = {}

    def fake_http(method, url, **kw):
        seen["method"], seen["url"], seen["body"] = method, url, kw.get("body")

    p.update_comment(9, 321, "new body", None, fake_http)
    assert seen["method"] == "PATCH"
    assert seen["url"].endswith("/issues/comments/321")
    assert seen["body"] == {"body": "new body"}


# ── build status (vocabulary translation) ────────────────────────────────

def test_post_build_status_translates_state_and_trims_description():
    p = _provider()
    calls = []

    def fake_http(method, url, **kw):
        calls.append((method, url, kw.get("body")))

    long_desc = "x" * 200
    p.post_build_status("SHA", "SUCCESSFUL", long_desc, "http://link", None,
                        fake_http, key="argocd-diff-preview", context="ACME Diff Preview")
    method, url, body = calls[0]
    assert method == "POST"
    assert url.endswith("/statuses/SHA")
    assert body["state"] == "success"               # SUCCESSFUL -> success
    assert body["target_url"] == "http://link"
    assert body["context"] == "ACME Diff Preview"
    assert len(body["description"]) == 140           # trimmed to GitHub's max


def test_post_build_status_maps_every_state_including_unknown():
    p = _provider()
    seen = {}

    def fake_http(method, url, **kw):
        seen["state"] = kw["body"]["state"]

    for core_state, gh_state in [("INPROGRESS", "pending"),
                                 ("SUCCESSFUL", "success"),
                                 ("FAILED", "failure")]:
        p.post_build_status("s", core_state, "d", "u", None, fake_http,
                            key="k", context="c")
        assert seen["state"] == gh_state
    # An unrecognized core state degrades to GitHub's "error".
    p.post_build_status("s", "WEIRD", "d", "u", None, fake_http, key="k", context="c")
    assert seen["state"] == "error"


def test_get_build_status_returns_core_vocabulary_for_matching_context():
    p = _provider()

    def fake_http(method, url, **kw):
        assert url.endswith("/commits/SHA/statuses")
        return [
            {"context": "other", "state": "success"},
            {"context": "ACME Diff Preview", "state": "failure"},
        ]

    out = p.get_build_status("SHA", None, fake_http,
                             key="k", context="ACME Diff Preview")
    assert out == {"state": "FAILED"}  # failure -> FAILED


def test_get_build_status_maps_error_and_unknown_to_failed():
    p = _provider()

    def make(state):
        def fake_http(method, url, **kw):
            return [{"context": "ctx", "state": state}]
        return fake_http

    assert p.get_build_status("s", None, make("error"), key="k", context="ctx") == {"state": "FAILED"}
    assert p.get_build_status("s", None, make("mystery"), key="k", context="ctx") == {"state": "FAILED"}
    assert p.get_build_status("s", None, make("pending"), key="k", context="ctx") == {"state": "INPROGRESS"}


def test_get_build_status_returns_empty_when_no_matching_status():
    p = _provider()

    def fake_http(method, url, **kw):
        return [{"context": "someone-else", "state": "success"}]

    assert p.get_build_status("s", None, fake_http, key="k", context="ours") == {}


def test_get_build_status_tolerates_non_list_response():
    p = _provider()

    def fake_http(method, url, **kw):
        return {"message": "boom"}

    assert p.get_build_status("s", None, fake_http, key="k", context="ours") == {}


# ── URLs / limits ─────────────────────────────────────────────────────────

def test_pr_web_url_default_and_explicit_repo():
    p = _provider()
    assert p.pr_web_url(77, None) == "https://github.com/appspace-cloud/acme-config-dev/pull/77"
    assert p.pr_web_url(77, "acme-config-stage") == \
        "https://github.com/appspace-cloud/acme-config-stage/pull/77"


def test_comment_anchor_uses_issuecomment_fragment():
    assert _provider().comment_anchor(999) == "#issuecomment-999"


def test_max_comment_bytes_matches_bitbucket_headroom():
    assert _provider().max_comment_bytes == 245_000


# ── webhook ────────────────────────────────────────────────────────────────

def test_is_pr_event_only_for_pull_request():
    p = _provider()
    assert p.is_pr_event("pull_request") is True
    assert p.is_pr_event("push") is False
    assert p.is_pr_event("") is False


def test_signature_and_event_header_names():
    p = _provider()
    assert p.signature_header == "X-Hub-Signature-256"
    assert p.event_header == "X-GitHub-Event"


def test_verify_webhook_signature_permissive_when_no_secret():
    p = _provider()
    assert p.verify_webhook_signature(b"body", "", webhook_secret="") is True
    assert p.verify_webhook_signature(b"body", "sha256=whatever", webhook_secret="") is True


def test_verify_webhook_signature_rejects_missing_header_when_secret_set():
    p = _provider()
    assert p.verify_webhook_signature(b"body", "", webhook_secret="s3cret") is False


def test_verify_webhook_signature_accepts_correct_hmac_with_sha256_prefix():
    p = _provider()
    secret = "s3cret"
    body = b'{"action": "opened"}'
    digest = hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert p.verify_webhook_signature(body, f"sha256={digest}", webhook_secret=secret) is True


def test_verify_webhook_signature_rejects_wrong_and_non_ascii_header():
    p = _provider()
    assert p.verify_webhook_signature(b"body", "sha256=deadbeef", webhook_secret="s3cret") is False
    # A non-ASCII signature header must be a plain mismatch, never a TypeError
    # (bytes comparison, same v2.5.3 CRIT-2 rationale as BitbucketProvider).
    assert p.verify_webhook_signature(b"body", "sha256=\u00f1", webhook_secret="s3cret") is False
