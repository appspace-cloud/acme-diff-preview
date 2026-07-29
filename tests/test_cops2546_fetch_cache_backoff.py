"""Fetch caching and transient retry backoff (COPS-2546).

Live incident 2026-07-29: the v2.13.2/v2.13.3 additions call
_bb_fetch_status directly, bypassing the singleflight cache value files
have used for months, and the retry-until-determinate loop reprocesses a
120-app PR every iteration when rate limiting makes some fetches
unreadable. The two together exhausted the Bitbucket API budget and kept
acme-config-dev PR 6938 permanently INPROGRESS with 1449s iterations.

Fix one: _bb_fetch_cached, a thin wrapper over _bb_fetch_status that
caches BB_OK and BB_NOT_FOUND per (sha, path) with singleflight, and
never caches transient errors. All read-at-sha call sites use it.

Fix two: exponential retry backoff for transient PR failures: 1, 2, 4,
then capped at 8 iterations between retries, reset by a new push (sha
change) and cleared by a clean or permanent completion.
"""
import sys, os
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m


SHA = "cafe0001"


@pytest.fixture(autouse=True)
def _isolate_caches():
    """The fetch cache is process-lifetime by design (content at a sha is
    immutable). Tests reuse fake shas and paths, so without clearing it a
    result cached by one test silently satisfies the next and the mocked
    fetcher is never called. Scoped to this module only."""
    for d in (m._vf_cache, m._vf_inflight, m._retry_backoff):
        d.clear()
    yield
    for d in (m._vf_cache, m._vf_inflight, m._retry_backoff):
        d.clear()


def _counting_fetch(monkeypatch, result):
    calls = {"n": 0}
    def fake(filepath, sha, repo=None):
        calls["n"] += 1
        return result
    monkeypatch.setattr(m, "_bb_fetch_status", fake)
    return calls


# ── fetch cache ──────────────────────────────────────────────────────────────

def test_ok_result_is_cached(monkeypatch):
    calls = _counting_fetch(monkeypatch, ("content", m.BB_OK))
    r1 = m._bb_fetch_cached("gcp/config.yaml", SHA)
    r2 = m._bb_fetch_cached("gcp/config.yaml", SHA)
    assert r1 == r2 == ("content", m.BB_OK)
    assert calls["n"] == 1


def test_not_found_is_cached(monkeypatch):
    calls = _counting_fetch(monkeypatch, (None, m.BB_NOT_FOUND))
    m._bb_fetch_cached("gcp/x/config.yaml", SHA)
    m._bb_fetch_cached("gcp/x/config.yaml", SHA)
    assert calls["n"] == 1


def test_transient_error_is_never_cached(monkeypatch):
    calls = _counting_fetch(monkeypatch, (None, m.BB_ERROR))
    m._bb_fetch_cached("gcp/y/config.yaml", SHA)
    m._bb_fetch_cached("gcp/y/config.yaml", SHA)
    assert calls["n"] == 2


def test_different_sha_is_a_different_key(monkeypatch):
    calls = _counting_fetch(monkeypatch, ("c", m.BB_OK))
    m._bb_fetch_cached("gcp/config.yaml", "cafe0001")
    m._bb_fetch_cached("gcp/config.yaml", "cafe0002")
    assert calls["n"] == 2


def test_hot_call_sites_use_the_cached_wrapper():
    src = open(os.path.join(os.path.dirname(__file__), "..", "src",
                            "diff_preview.py")).read()
    body = src.replace("def _bb_fetch_cached(", "", 1)
    n = body.count("_bb_fetch_cached(")
    assert n >= 10, (
        f"expected the read-at-sha call sites (identity checks, cohort "
        f"guard, augmenter, new-env render chain, rename identity, "
        f"decommission identity, chart revision) to use _bb_fetch_cached; "
        f"found only {n} uses")


# ── retry backoff ────────────────────────────────────────────────────────────

SK = ("acme-config-dev", "6938")
PR_SHA_1 = "aaaa0001"
PR_SHA_2 = "aaaa0002"


def test_backoff_escalates_and_caps():
    assert m._backoff_register_transient(SK, PR_SHA_1) == 1
    m._retry_backoff[SK][0] = 0  # simulate the skips being consumed
    assert m._backoff_register_transient(SK, PR_SHA_1) == 2
    m._retry_backoff[SK][0] = 0
    assert m._backoff_register_transient(SK, PR_SHA_1) == 4
    m._retry_backoff[SK][0] = 0
    assert m._backoff_register_transient(SK, PR_SHA_1) == 8
    m._retry_backoff[SK][0] = 0
    assert m._backoff_register_transient(SK, PR_SHA_1) == 8  # capped


def test_backoff_skip_decrements_then_allows():
    m._backoff_register_transient(SK, PR_SHA_1)  # 1 skip
    assert m._backoff_should_skip(SK, PR_SHA_1) is True
    assert m._backoff_should_skip(SK, PR_SHA_1) is False


def test_new_push_resets_backoff():
    m._backoff_register_transient(SK, PR_SHA_1)
    m._retry_backoff[SK][0] = 0
    m._backoff_register_transient(SK, PR_SHA_1)  # escalated to 2 skips
    assert m._backoff_should_skip(SK, PR_SHA_2) is False  # new sha: no skip
    assert m._backoff_register_transient(SK, PR_SHA_2) == 1  # escalation reset


def test_clear_removes_entry():
    m._backoff_register_transient(SK, PR_SHA_1)
    m._backoff_clear(SK)
    assert m._backoff_should_skip(SK, PR_SHA_1) is False
    assert SK not in m._retry_backoff


def test_process_pr_wires_the_backoff():
    src = open(os.path.join(os.path.dirname(__file__), "..", "src",
                            "diff_preview.py")).read()
    assert "_backoff_should_skip(" in src.replace(
        "def _backoff_should_skip(", "", 1)
    assert "_backoff_register_transient(" in src.replace(
        "def _backoff_register_transient(", "", 1)
    assert "_backoff_clear(" in src.replace(
        "def _backoff_clear(", "", 1)


# ── design guards: shared bounded cache, not a second private dict ───────────

def test_cache_is_shared_with_value_file_fetches(monkeypatch):
    """A path already fetched as a helm value file must be a cache HIT here.
    The new-env ancestor chain reads the same config.yaml files the value-file
    cascade reads; if the two used separate caches every one of those would be
    a duplicate Bitbucket call."""
    calls = _counting_fetch(monkeypatch, ("cascade content", m.BB_OK))
    got = m._fetch_value_files(["$config/gcp/config.yaml"], SHA)
    assert got and calls["n"] == 1
    content, status = m._bb_fetch_cached("gcp/config.yaml", SHA)
    assert (content, status) == ("cascade content", m.BB_OK)
    assert calls["n"] == 1, "second read should be served from the shared cache"


def test_cache_is_bounded_no_second_unbounded_dict():
    """Regression guard for the memory side of this fix: everything cached at
    a sha must live in _vf_cache, which _bound_vf_cache() evicts once per
    iteration. A private module-level dict here would grow forever in a pod
    that runs for weeks."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "src",
                            "diff_preview.py")).read()
    body = src[src.index("def _bb_fetch_cached("):]
    body = body[:body.index("\ndef ", 1)]
    assert "_vf_cache" in body, "the cached fetcher must use the bounded _vf_cache"
    # Every module-level dict whose name looks like a cache must be on this
    # list, and each entry must have a documented bound. Adding a new one
    # without bounding it is how a pod that runs for weeks starts leaking.
    import re
    declared = set(re.findall(r"^(_\w*cache\w*)\s*[:=]", src, re.MULTILINE))
    bounded_or_intentional = {
        # name                            how its growth is bounded
        "_vf_cache",                      # _bound_vf_cache(), every iteration
        "_main_render_cache",             # MAIN_RENDER_CACHE_MAX, evict-half
        "_identity_rename_verdict_cache", # _IDENTITY_RENAME_CACHE_MAX, evict-half
        "_comment_id_cache",              # pruned with _seen by the stale sweep
        "_helm_chart_cache",              # popped by the chart prune
        "_path_map_cache",                # snapshot, replaced wholesale on refresh
        # locks, not caches
        "_vf_cache_lock", "_comment_id_cache_lock", "_helm_cache_lock",
    }
    unexpected = declared - bounded_or_intentional
    assert not unexpected, (
        f"unbounded cache dict(s) added without a bound: {sorted(unexpected)}. "
        f"Route sha-keyed reads through _bb_fetch_cached/_vf_cache instead.")
    m._vf_cache.clear()
    for i in range(m.VF_CACHE_MAX + 100):
        m._vf_cache[("sha", f"p{i}")] = "x"
    m._bound_vf_cache()
    assert len(m._vf_cache) <= m.VF_CACHE_MAX
    m._vf_cache.clear()


def test_empty_file_is_not_confused_with_a_missing_one(monkeypatch):
    """An empty config.yaml caches as BB_OK with empty content, not as a 404.
    The cohort guard branches on exactly that difference."""
    _counting_fetch(monkeypatch, ("", m.BB_OK))
    assert m._bb_fetch_cached("gcp/empty.yaml", SHA) == ("", m.BB_OK)
    assert m._bb_fetch_cached("gcp/empty.yaml", SHA) == ("", m.BB_OK)


def test_wrapper_is_a_drop_in_for_two_arg_callers(monkeypatch):
    """The cached wrapper must not change the call shape the wrapped function
    sees. Dozens of existing test doubles are defined as (path, sha) only;
    forwarding repo=None unconditionally would break every one of them for no
    production benefit, since None is already that argument's default."""
    seen = {}
    def two_arg_only(path, sha):
        seen["called"] = (path, sha)
        return "content", m.BB_OK
    monkeypatch.setattr(m, "_bb_fetch_status", two_arg_only)
    assert m._bb_fetch_cached("gcp/x.yaml", SHA) == ("content", m.BB_OK)
    assert seen["called"] == ("gcp/x.yaml", SHA)


def test_repo_is_forwarded_when_set(monkeypatch):
    """Multi-repo callers must still reach the right repository."""
    seen = {}
    def with_repo(path, sha, repo=None):
        seen["repo"] = repo
        return "content", m.BB_OK
    monkeypatch.setattr(m, "_bb_fetch_status", with_repo)
    m._bb_fetch_cached("gcp/x.yaml", SHA, repo="acme-config-prod")
    assert seen["repo"] == "acme-config-prod"
