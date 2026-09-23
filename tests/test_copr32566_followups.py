"""COPR-32566 follow-ups, found while reviewing the clone-token guard.

1. The merge preview is a commit only our git mirror has. When the mirror
   cannot answer, Bitbucket says 404 "Commit not found". That is a failed read,
   not an absent file, and must never be cached as one.
2. A transient error that reaches the process_pr catch-all gets the COPS-2546
   backoff, like one in the normal path.
"""
import http.client
import io
import os
import sys
import urllib.error

import pytest

os.environ.setdefault("BB_USER", "test-user")
os.environ.setdefault("BB_TOKEN", "test-token")
os.environ.setdefault("ARGOCD_PASS", "test-pass")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as dp  # noqa: E402
from test_coverage_orchestration import world, _mk_pr, BASE_SHA  # noqa: E402,F401

MERGED = "f00d" * 10
UNKNOWN_COMMIT = (b'{"type":"error","error":{"message":"Commit not found",'
                  b'"data":{"shas":["' + MERGED.encode() + b'"]}}}')


def _bitbucket_404(body, calls=None):
    def fake(req, timeout=None):
        if calls is not None:
            calls.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {},
                                     None if body is None else io.BytesIO(body))
    return fake


@pytest.fixture()
def mirror_miss(monkeypatch):
    # The local mirror cannot answer, so the read falls back to the API.
    monkeypatch.setattr(dp, "_git_read_file", lambda repo, sha, path: None)


# --- 1. an unknown commit is not an absent file -----------------------------

@pytest.mark.parametrize("body,status", [
    (UNKNOWN_COMMIT, dp.BB_ERROR),
    (b'{"type":"error","error":{"message":"No such file or directory: gcp/x.yaml"}}', dp.BB_NOT_FOUND),
    (b"", dp.BB_NOT_FOUND),
    (None, dp.BB_NOT_FOUND),
])
def test_an_unknown_commit_is_a_failed_read(mirror_miss, monkeypatch, body, status):
    monkeypatch.setattr(dp, "_pooled_urlopen", _bitbucket_404(body))
    assert dp._bb_fetch_status("gcp/x.yaml", MERGED) == (None, status)


class _BrokenBody(io.RawIOBase):
    def __init__(self, exc):
        self.exc = exc

    def read(self, n=-1):
        raise self.exc


@pytest.mark.parametrize("exc", [http.client.IncompleteRead(b""), ConnectionResetError(), ValueError()])
def test_a_404_body_that_cannot_be_read_is_a_failed_read(mirror_miss, monkeypatch, exc):
    # It cannot be told apart from an absent file, so it is retried, never cached.
    monkeypatch.setattr(dp, "_pooled_urlopen", lambda req, timeout=None: (_ for _ in ()).throw(
        urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, _BrokenBody(exc))))
    assert dp._bb_fetch_status("gcp/x.yaml", MERGED) == (None, dp.BB_ERROR)


@pytest.mark.parametrize("body,status", [(UNKNOWN_COMMIT, dp.BB_ERROR),
                                         (b'{"error":{"message":"No such file or directory: x"}}', dp.BB_NOT_FOUND)])
def test_the_pooled_production_path_keeps_the_404_body(mirror_miss, monkeypatch, body, status):
    # Production runs with HTTP pooling on; the suite turns it off (conftest.py).
    import email.message

    class Resp:
        status, reason, headers = 404, "Not Found", email.message.Message()

        def read(self):
            return body

    class Conn:
        def __init__(self, host, timeout=None, context=None):
            pass

        def request(self, *a, **k):
            pass

        def getresponse(self):
            return Resp()

        def close(self):
            pass

    monkeypatch.setattr(dp, "HTTP_POOLING_ENABLED", True)
    monkeypatch.setattr(dp._http_client, "HTTPSConnection", Conn)
    if hasattr(dp._http_conn_local, "conns"):
        del dp._http_conn_local.conns
    assert dp._bb_fetch_status("gcp/x.yaml", MERGED) == (None, status)
    del dp._http_conn_local.conns


def test_an_unknown_commit_is_never_cached(mirror_miss, monkeypatch):
    calls = []
    monkeypatch.setattr(dp, "_pooled_urlopen", _bitbucket_404(UNKNOWN_COMMIT, calls))
    for _ in range(2):
        assert dp._bb_fetch_cached("gcp/x.yaml", MERGED) == (None, dp.BB_ERROR)
    assert len(calls) == 2, "a failed read must be asked again, not remembered"


def test_a_missing_file_is_still_cached(mirror_miss, monkeypatch):
    calls = []
    monkeypatch.setattr(dp, "_pooled_urlopen", _bitbucket_404(
        b'{"type":"error","error":{"message":"No such file or directory: gcp/x.yaml"}}', calls))
    for _ in range(2):
        assert dp._bb_fetch_cached("gcp/x.yaml", MERGED) == (None, dp.BB_NOT_FOUND)
    assert len(calls) == 1


def test_the_clone_guard_retries_instead_of_passing(mirror_miss, monkeypatch):
    monkeypatch.setattr(dp, "_pooled_urlopen", _bitbucket_404(UNKNOWN_COMMIT))
    path = "gcp/aec/private-cloud/na1-b/pv-usbank-c/customer.yaml"
    with pytest.raises(dp.ValueFileUnreadable):
        dp._detect_partition_token_misses([path], MERGED)


# --- 2. the catch-all spaces out transient retries --------------------------

def _raise(exc):
    def fake(*a, **k):
        raise exc
    return fake


def test_a_transient_error_in_the_catch_all_backs_off(world, monkeypatch):
    sinks, _ = world
    monkeypatch.setattr(dp, "_retry_backoff", {})
    monkeypatch.setattr(dp, "get_pr_changed_files", _raise(dp.ValueFileUnreadable(
        "value file unreadable at sha aabbccdd (Bitbucket transport, not absence): x")))
    pr = _mk_pr(pr_id=5001)
    dp.process_pr(pr, {}, base_sha=BASE_SHA)
    state, desc = sinks.statuses[-1]
    assert state == "FAILED" and desc.startswith("Diff unavailable (infrastructure) - will retry")
    assert list(dp._retry_backoff.values()) == [[1, 2, pr["source"]["commit"]["hash"]]]
    posted = len(sinks.statuses)
    dp.process_pr(pr, {}, base_sha=BASE_SHA)
    assert len(sinks.statuses) == posted, "the next iteration waits instead of retrying"


def test_a_permanent_error_in_the_catch_all_does_not_back_off(world, monkeypatch):
    sinks, _ = world
    monkeypatch.setattr(dp, "_retry_backoff", {})
    monkeypatch.setattr(dp, "get_pr_changed_files", _raise(KeyError("appspace")))
    dp.process_pr(_mk_pr(pr_id=5002), {}, base_sha=BASE_SHA)
    assert sinks.statuses[-1][1].startswith("Diff error")
    assert dp._retry_backoff == {}


# --- 3. other readers treat a failed read as a failed read ------------------

def _unreadable(paths, files=None):
    """_bb_fetch_status that fails for `paths`, serves `files`, and says absent for the rest."""
    files = files or {}
    return lambda path, sha, repo=None: (
        (files[path], dp.BB_OK) if path in files else (None, dp.BB_ERROR if path in paths else dp.BB_NOT_FOUND))


def test_a_new_env_whose_config_cannot_be_read_is_retried_not_green(world, monkeypatch):
    sinks, _ = world
    cf = "gcp/dev/public-cloud/na1/cl-newenv-a/config.yaml"
    monkeypatch.setattr(dp, "_retry_backoff", {})
    monkeypatch.setattr(dp, "get_pr_changed_files", lambda pr_id, repo=None: ([cf], {}))
    monkeypatch.setattr(dp, "_bb_fetch_status", _unreadable({cf}))
    dp.process_pr(_mk_pr(pr_id=5003), {}, base_sha=BASE_SHA)
    state, desc = sinks.statuses[-1]
    assert state == "FAILED" and desc.startswith("Diff unavailable (infrastructure) - will retry"), desc
    assert not dp._seen


def test_a_new_env_whose_customer_file_cannot_be_read_is_retried_not_blocked(world, monkeypatch):
    sinks, _ = world
    cust = "gcp/dev/private-cloud/ap1/custom/pv-brandnew-a/customer.yaml"
    monkeypatch.setattr(dp, "_retry_backoff", {})
    monkeypatch.setattr(dp, "get_pr_changed_files", lambda pr_id, repo=None: ([cust], {}))
    cohort = "gcp/dev/private-cloud/ap1/custom/config.yaml"  # present, so COPS-2552 passes
    monkeypatch.setattr(dp, "_bb_fetch_status", _unreadable({cust}, {cohort: "appspace:\n  version: 2603.2.19\n"}))
    dp.process_pr(_mk_pr(pr_id=5004), {}, base_sha=BASE_SHA)
    state, desc = sinks.statuses[-1]
    assert state == "FAILED" and desc.startswith("Diff unavailable (infrastructure) - will retry"), desc
    assert "structural" not in sinks.upserts[-1] and "[transient]" in sinks.upserts[-1]


def test_the_version_lookup_retries_when_an_ancestor_cannot_be_read(monkeypatch):
    env = {"name": "pv-brandnew-a", "config_file": "gcp/dev/private-cloud/ap1/custom/pv-brandnew-a/customer.yaml",
           "env_dir": "gcp/dev/private-cloud/ap1/custom/pv-brandnew-a", "all_yaml_files": []}
    anc = "gcp/dev/private-cloud/ap1/custom/config.yaml"
    monkeypatch.setattr(dp, "_bb_fetch_cached", lambda path, sha, repo=None: (
        ("appspace:\n  customerName: brandnew\n", dp.BB_OK) if path == env["config_file"]
        else (None, dp.BB_ERROR if path == anc else dp.BB_NOT_FOUND)))
    with pytest.raises(dp.ValueFileUnreadable):
        dp._render_new_env_diff(env, "sha1")


def test_a_rename_verdict_from_a_failed_read_is_not_cached(monkeypatch):
    monkeypatch.setattr(dp, "_identity_rename_verdict_cache", {})
    old, new = "gcp/prod/p/pv-manulife-a/customer.yaml", "gcp/prod/p/pv-manulife-b/customer.yaml"
    monkeypatch.setattr(dp, "_bb_fetch_cached", lambda path, sha, repo=None: (
        ("appspace:\n  customerName: manulife\n  suffix: a\n", dp.BB_OK) if path == old else (None, dp.BB_ERROR)))
    assert dp._rename_identity_confirmed(old, new, "main1", "pr1") is True  # permissive for this call
    assert dp._identity_rename_verdict_cache == {}, "a guess must not be remembered"
