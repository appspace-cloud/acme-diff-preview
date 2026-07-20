"""Direct unit tests for BitbucketProvider and the VCSProvider contract
(COPS-2520). These exercise BitbucketProvider standalone, independent of
diff_preview.py's module-level wrappers (which are already covered via the
existing test suite, since every wrapper delegates to this class). Nothing
here touches the network: http_fn/bb_fn are always fakes.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from bitbucket_provider import BitbucketProvider  # noqa: E402
from vcs_provider import VCSProvider  # noqa: E402


def _provider():
    return BitbucketProvider(
        workspace="appspace-cloud", default_repo="acme-config-dev",
        user="u", token="t",
    )


def test_bitbucket_provider_satisfies_vcs_provider_protocol():
    # Structural typing check (runtime_checkable Protocol): BitbucketProvider
    # doesn't inherit from VCSProvider, but must have matching methods.
    assert isinstance(_provider(), VCSProvider)


def test_api_base_default_repo():
    p = _provider()
    assert p.api_base() == "https://api.bitbucket.org/2.0/repositories/appspace-cloud/acme-config-dev"


def test_api_base_explicit_repo():
    p = _provider()
    assert p.api_base("acme-config-stage") == \
        "https://api.bitbucket.org/2.0/repositories/appspace-cloud/acme-config-stage"


def test_call_builds_url_and_passes_auth():
    p = _provider()
    seen = {}

    def fake_http(method, url, **kw):
        seen["method"], seen["url"], seen["kw"] = method, url, kw
        return {"ok": True}

    result = p.call("GET", "pullrequests/1", None, fake_http)
    assert result == {"ok": True}
    assert seen["method"] == "GET"
    assert seen["url"] == "https://api.bitbucket.org/2.0/repositories/appspace-cloud/acme-config-dev/pullrequests/1"
    assert seen["kw"]["auth"] == ("u", "t")


def test_list_open_prs_paginates_until_next_is_empty():
    p = _provider()
    pages = [
        {"values": [{"id": 1}], "next": "page2"},
        {"values": [{"id": 2}], "next": ""},
    ]
    calls = {"n": 0}

    def fake_http(method, url, **kw):
        data = pages[calls["n"]]
        calls["n"] += 1
        return data

    prs, hit_limit = p.list_open_prs(None, fake_http, max_pages=100)
    assert [pr["id"] for pr in prs] == [1, 2]
    assert hit_limit is False


def test_list_open_prs_reports_hit_page_limit():
    p = _provider()

    def fake_http(method, url, **kw):
        return {"values": [{"id": 9}], "next": url}  # never-ending "next"

    prs, hit_limit = p.list_open_prs(None, fake_http, max_pages=3)
    assert len(prs) == 3
    assert hit_limit is True


def test_get_pr_diffstat_pairs_renames_and_keeps_both_paths():
    p = _provider()

    def fake_bb(method, path, repo=None):
        return {"values": [
            {"old": {"path": "a/old.yaml"}, "new": {"path": "a/new.yaml"}},
            {"old": None, "new": {"path": "b/added.yaml"}},
        ], "next": ""}

    files, renames, hit_limit = p.get_pr_diffstat(42, None, fake_bb, max_pages=10)
    assert set(files) == {"a/old.yaml", "a/new.yaml", "b/added.yaml"}
    assert renames == {"a/old.yaml": "a/new.yaml"}
    assert hit_limit is False


def test_get_pr_diffstat_hit_page_limit_only_when_more_pages_remain():
    p = _provider()

    def fake_bb(method, path, repo=None):
        return {"values": [], "next": "more"}  # always another page available

    _, _, hit_limit = p.get_pr_diffstat(42, None, fake_bb, max_pages=2)
    assert hit_limit is True


def test_verify_webhook_signature_permissive_when_no_secret():
    p = _provider()
    assert p.verify_webhook_signature(b"body", "", webhook_secret="") is True
    assert p.verify_webhook_signature(b"body", "sha256=whatever", webhook_secret="") is True


def test_verify_webhook_signature_rejects_missing_header_when_secret_set():
    p = _provider()
    assert p.verify_webhook_signature(b"body", "", webhook_secret="s3cret") is False


def test_verify_webhook_signature_accepts_correct_hmac():
    import hashlib
    import hmac as hmac_mod
    p = _provider()
    secret = "s3cret"
    body = b'{"pullrequest": {"id": 1}}'
    digest = hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert p.verify_webhook_signature(body, f"sha256={digest}", webhook_secret=secret) is True


def test_verify_webhook_signature_rejects_wrong_hmac():
    p = _provider()
    assert p.verify_webhook_signature(b"body", "sha256=deadbeef", webhook_secret="s3cret") is False


def test_max_comment_bytes_matches_the_tuned_headroom():
    # Exercised directly because the core truncation path reads the module-level
    # MAX_COMMENT_BYTES (which tests monkeypatch), not this property; both hosts
    # expose the same 245KB-class limit so truncation behaves identically.
    assert _provider().max_comment_bytes == 245_000


# ═══════════════════════════════════════════════════════════════════════
# COPS-2520 backward-compatibility wire pins. Each Bitbucket method must keep
# emitting the EXACT request it did before the provider extraction, or a
# deployed Bitbucket-only instance would break. These mirror the GitHubProvider
# suite so both hosts are pinned to the same level of detail.
# ═══════════════════════════════════════════════════════════════════════

def test_pr_shape_accessors_read_bitbucket_native_fields():
    p = _provider()
    pr = {"id": 7, "title": "T",
          "source": {"commit": {"hash": "abc123"}},
          "destination": {"branch": {"name": "main"}}}
    assert p.pr_id(pr) == 7
    assert p.pr_source_sha(pr) == "abc123"
    assert p.pr_dest_branch(pr) == "main"
    assert p.pr_title(pr) == "T"


def test_get_branch_head_sha_reads_target_hash():
    p = _provider()
    seen = {}

    def fake_http(method, url, **kw):
        seen["url"], seen["auth"] = url, kw.get("auth")
        return {"target": {"hash": "deadbeef"}}

    assert p.get_branch_head_sha("main", None, fake_http) == "deadbeef"
    assert seen["url"] == ("https://api.bitbucket.org/2.0/repositories/"
                           "appspace-cloud/acme-config-dev/refs/branches/main")
    assert seen["auth"] == ("u", "t")


def test_raw_file_url_and_headers_uses_src_api_and_basic_auth():
    import base64
    p = _provider()
    url, headers = p.raw_file_url_and_headers("apps/dev/values.yaml", "cafe", None)
    assert url == ("https://api.bitbucket.org/2.0/repositories/"
                   "appspace-cloud/acme-config-dev/src/cafe/apps/dev/values.yaml")
    expected = "Basic " + base64.b64encode(b"u:t").decode()
    assert headers == {"Authorization": expected}


def test_get_comment_normalizes_shape():
    p = _provider()

    def fake_bb(method, path, repo=None, **kw):
        assert method == "GET"
        assert path == "pullrequests/5/comments/99"
        return {"id": 99, "content": {"raw": "hello"}}

    assert p.get_comment(5, 99, None, fake_bb) == {"id": 99, "body": "hello"}


def test_iter_comments_paginates_base_stripped_and_normalizes():
    p = _provider()
    base = p.api_base(None)
    pages = [
        {"values": [{"id": 1, "content": {"raw": "a"}}],
         "next": f"{base}/pullrequests/5/comments?page=2"},
        {"values": [{"id": 2, "content": {"raw": "b"}}], "next": ""},
    ]
    seen_paths, calls = [], {"n": 0}

    def fake_bb(method, path, repo=None, **kw):
        seen_paths.append(path)
        data = pages[calls["n"]]
        calls["n"] += 1
        return data

    out = list(p.iter_comments(5, None, fake_bb, max_pages=10))
    assert out == [{"id": 1, "body": "a"}, {"id": 2, "body": "b"}]
    # the next-link is stored relative (base stripped), exactly as the old
    # inline find_existing_comment scan did.
    assert seen_paths == ["pullrequests/5/comments?pagelen=100",
                          "pullrequests/5/comments?page=2"]


def test_create_comment_posts_content_raw_body_and_returns_id():
    p = _provider()
    seen = {}

    def fake_bb(method, path, repo=None, **kw):
        seen["method"], seen["path"], seen["body"] = method, path, kw.get("body")
        return {"id": 321}

    out = p.create_comment(5, "the body", None, fake_bb)
    assert seen["method"] == "POST"
    assert seen["path"] == "pullrequests/5/comments"
    assert seen["body"] == {"content": {"raw": "the body"}}
    assert out == {"id": 321, "body": "the body"}


def test_create_comment_handles_non_dict_response():
    p = _provider()

    def fake_bb(method, path, repo=None, **kw):
        return None

    assert p.create_comment(5, "b", None, fake_bb) == {"id": None, "body": "b"}


def test_update_comment_puts_content_raw_body():
    p = _provider()
    seen = {}

    def fake_bb(method, path, repo=None, **kw):
        seen["method"], seen["path"], seen["body"] = method, path, kw.get("body")
        return {}

    p.update_comment(5, 99, "edited", None, fake_bb)
    assert seen["method"] == "PUT"
    assert seen["path"] == "pullrequests/5/comments/99"
    assert seen["body"] == {"content": {"raw": "edited"}}


def test_post_build_status_posts_native_state_and_trims_description():
    p = _provider()
    seen = {}

    def fake_bb(method, path, repo=None, **kw):
        seen["method"], seen["path"], seen["body"] = method, path, kw.get("body")
        return {}

    p.post_build_status("sha1", "SUCCESSFUL", "x" * 400, "http://u", None, fake_bb,
                        key="argocd-diff-preview", context="ACME Diff Preview")
    assert seen["method"] == "POST"
    assert seen["path"] == "commit/sha1/statuses/build"
    assert seen["body"]["state"] == "SUCCESSFUL"          # native vocab, not translated
    assert seen["body"]["key"] == "argocd-diff-preview"
    assert seen["body"]["name"] == "ACME Diff Preview"
    assert seen["body"]["url"] == "http://u"
    assert len(seen["body"]["description"]) == 255          # trimmed at 255


def test_post_build_status_handles_none_description():
    p = _provider()
    seen = {}

    def fake_bb(method, path, repo=None, **kw):
        seen["body"] = kw.get("body")
        return {}

    p.post_build_status("sha1", "FAILED", None, "http://u", None, fake_bb,
                        key="k", context="c")
    assert seen["body"]["description"] == ""                # None coerced to ""


def test_get_build_status_hits_build_key_endpoint_with_auth():
    p = _provider()
    seen = {}

    def fake_http(method, url, **kw):
        seen["url"], seen["auth"] = url, kw.get("auth")
        return {"state": "INPROGRESS"}

    st = p.get_build_status("sha1", None, fake_http,
                            key="argocd-diff-preview", context="ACME Diff Preview")
    assert st == {"state": "INPROGRESS"}
    assert seen["url"] == ("https://api.bitbucket.org/2.0/repositories/"
                           "appspace-cloud/acme-config-dev/commit/sha1/statuses/build/"
                           "argocd-diff-preview")
    assert seen["auth"] == ("u", "t")


def test_pr_web_url_default_and_explicit_repo():
    p = _provider()
    assert p.pr_web_url(12, None) == \
        "https://bitbucket.org/appspace-cloud/acme-config-dev/pull-requests/12"
    assert p.pr_web_url(12, "acme-config-stage") == \
        "https://bitbucket.org/appspace-cloud/acme-config-stage/pull-requests/12"


def test_comment_anchor_uses_comment_fragment():
    assert _provider().comment_anchor(456) == "#comment-456"


def test_verify_webhook_signature_rejects_non_ascii_header_without_raising():
    # v2.5.3 CRIT-2: the comparison is done in bytes so a non-ASCII signature
    # header can never raise TypeError on this pre-auth path; it just fails.
    p = _provider()
    assert p.verify_webhook_signature(
        b"body", "sha256=\u00e9\u00e9", webhook_secret="s3cret") is False


def test_webhook_header_names_and_pr_event_matcher():
    p = _provider()
    assert p.signature_header == "X-Hub-Signature"
    assert p.event_header == "X-Event-Key"
    assert p.is_pr_event("pullrequest:created") is True
    assert p.is_pr_event("pullrequest:updated") is True
    assert p.is_pr_event("repo:push") is False
    assert p.is_pr_event("") is False
