"""COPS-2766: a failed comment or status write must not mark the PR seen.

post_build_status and upsert_comment swallowed every error and returned
nothing, so process_pr set _seen after a write that never landed. The PR
then kept a stale comment, or had no merge gate, until the next push.

Both now return one result:
  ok         the write landed
  transient  429, 5xx or network: no _seen, back off and retry
  permanent  any other 4xx: _seen (a retry fails the same way), ERROR log
  skipped    not the leader: nothing, the new leader owns the PR
A failed write (transient or permanent) counts in bb_write_failures.
"""
import ast
import glob
import os
import urllib.error

import pytest

import diff_preview as m
import logsink

from test_coverage_orchestration import world, _mk_pr, PATH_MAP, BASE_SHA, PR_SHA  # noqa: E402,F401

SK = ("acme-config-dev", 991)


def _http_error(code):
    return urllib.error.HTTPError("https://api.bitbucket.org/x", code,
                                  "boom", {}, None)


@pytest.fixture()
def failures(monkeypatch):
    """The write-failure counter, from zero, and the ERROR log lines."""
    monkeypatch.setitem(m._diff_stats, "bb_write_failures", 0)
    errors = []
    real_log = logsink.log

    def log(msg, level="INFO", *a, **k):
        if level == "ERROR":
            errors.append(str(msg))
        return real_log(msg, level, *a, **k)
    monkeypatch.setattr(logsink, "log", log)
    return errors


def _bb_raising(exc, calls=None):
    def fake_bb(method, path, **kw):
        if calls is not None:
            calls.append(method)
        raise exc
    return fake_bb


# ── post_build_status ────────────────────────────────────────────────────

def test_status_write_that_lands_is_ok(monkeypatch, failures):
    monkeypatch.setattr(m, "bb", lambda *a, **k: {})
    assert m.post_build_status("a" * 40, "SUCCESSFUL", "d", pr_id=1) == "ok"
    assert m._diff_stats["bb_write_failures"] == 0


@pytest.mark.parametrize("exc", [
    _http_error(429), _http_error(500), _http_error(503),
    urllib.error.URLError("connection reset"), TimeoutError("read timed out"),
])
def test_status_write_outage_is_transient_and_counted(monkeypatch, failures, exc):
    monkeypatch.setattr(m, "bb", _bb_raising(exc))
    assert m.post_build_status("a" * 40, "FAILED", "d", pr_id=1) == "transient"
    assert m._diff_stats["bb_write_failures"] == 1


@pytest.mark.parametrize("code", [400, 403, 404, 413])
def test_status_write_rejected_is_permanent_counted_and_loud(monkeypatch, failures, code):
    monkeypatch.setattr(m, "bb", _bb_raising(_http_error(code)))
    assert m.post_build_status("a" * 40, "FAILED", "d", pr_id=1) == "permanent"
    assert m._diff_stats["bb_write_failures"] == 1
    assert any("failed to set FAILED" in e for e in failures), failures


def test_status_write_skipped_when_not_leader(monkeypatch, failures):
    calls = []
    monkeypatch.setattr(m, "_still_leader", lambda: False)
    monkeypatch.setattr(m, "bb", _bb_raising(_http_error(500), calls))
    assert m.post_build_status("a" * 40, "FAILED", "d", pr_id=1) == "skipped"
    assert calls == [] and m._diff_stats["bb_write_failures"] == 0


# ── upsert_comment ───────────────────────────────────────────────────────

def test_comment_post_and_put_that_land_are_ok(monkeypatch, failures):
    monkeypatch.setattr(m, "bb", lambda *a, **k: {"id": 5})
    assert m.upsert_comment(10, "body") == "ok"
    assert m.upsert_comment(10, "body", existing_id=5) == "ok"
    assert m._diff_stats["bb_write_failures"] == 0


def test_comment_outage_is_transient_and_counted(monkeypatch, failures):
    monkeypatch.setattr(m, "bb", _bb_raising(_http_error(503)))
    assert m.upsert_comment(10, "body", existing_id=5) == "transient"
    assert m._diff_stats["bb_write_failures"] == 1


def test_comment_rejected_is_permanent_and_counted(monkeypatch, failures):
    """A 403 on a comment another bot account owns fails the same way on
    every retry."""
    monkeypatch.setattr(m, "bb", _bb_raising(_http_error(403)))
    assert m.upsert_comment(10, "body", existing_id=5) == "permanent"
    assert m._diff_stats["bb_write_failures"] == 1
    assert failures


def test_comment_deleted_then_recreated_is_ok(monkeypatch, failures):
    def fake_bb(method, path, **kw):
        if method == "PUT":
            raise _http_error(404)
        return {"id": 6}
    monkeypatch.setattr(m, "bb", fake_bb)
    assert m.upsert_comment(10, "body", existing_id=5) == "ok"
    assert m._diff_stats["bb_write_failures"] == 0


def test_comment_recreate_that_fails_is_classified_and_counted_once(monkeypatch, failures):
    def fake_bb(method, path, **kw):
        raise _http_error(404 if method == "PUT" else 502)
    monkeypatch.setattr(m, "bb", fake_bb)
    assert m.upsert_comment(10, "body", existing_id=5) == "transient"
    assert m._diff_stats["bb_write_failures"] == 1


def test_comment_skipped_when_not_leader(monkeypatch, failures):
    calls = []
    monkeypatch.setattr(m, "_still_leader", lambda: False)
    monkeypatch.setattr(m, "bb", _bb_raising(_http_error(500), calls))
    assert m.upsert_comment(10, "body") == "skipped"
    assert calls == [] and m._diff_stats["bb_write_failures"] == 0


# ── process_pr: an early exit (the merge conflict block) ─────────────────

def _conflict_world(world, monkeypatch, status="ok", comment="ok"):
    sinks, _plan = world
    monkeypatch.setattr(m, "_merge_preview",
                        lambda repo, base, sha: (None, ["gcp/x/values.yaml"]))
    monkeypatch.setattr(m, "post_build_status",
                        lambda pr_sha, state, description, pr_id=None, repo=None:
                        sinks.statuses.append((state, description)) or status)
    monkeypatch.setattr(m, "upsert_comment",
                        lambda pr_id, body, existing_id=None, repo=None, **kw:
                        sinks.upserts.append(body) or comment)
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    assert "[conflict]" in sinks.upserts[-1]
    return sinks


@pytest.mark.parametrize("status,comment", [("transient", "ok"), ("ok", "transient")])
def test_early_exit_with_a_transient_write_retries(world, monkeypatch, status, comment):
    _conflict_world(world, monkeypatch, status=status, comment=comment)
    assert SK not in m._seen, "a write that did not land must not mark the PR seen"
    assert SK in m._retry_backoff, "and the retry must back off"


@pytest.mark.parametrize("status,comment", [("ok", "ok"), ("permanent", "ok"),
                                            ("ok", "permanent")])
def test_early_exit_with_landed_or_rejected_writes_is_seen(world, monkeypatch, status, comment):
    m._retry_backoff[SK] = [0, 4, PR_SHA]     # an earlier transient pass
    monkeypatch.setitem(m._pr_supersede_aborts, SK, 1)
    _conflict_world(world, monkeypatch, status=status, comment=comment)
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA)
    assert SK not in m._retry_backoff, "a published result ends the backoff"
    assert SK not in m._pr_supersede_aborts, "and the supersede abort streak"


def test_early_exit_not_leader_neither_seen_nor_backed_off(world, monkeypatch):
    _conflict_world(world, monkeypatch, status="skipped", comment="skipped")
    assert SK not in m._seen and SK not in m._retry_backoff


def test_early_exit_status_carries_the_repo(world, monkeypatch):
    sinks, _plan = world
    repos = []
    monkeypatch.setattr(m, "_merge_preview",
                        lambda repo, base, sha: (None, ["gcp/x/values.yaml"]))
    monkeypatch.setattr(m, "post_build_status",
                        lambda pr_sha, state, description, pr_id=None, repo=None:
                        repos.append(repo) or "ok")
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    assert repos == ["acme-config-dev"]


# ── process_pr: the main diff path ───────────────────────────────────────

def _main_world(world, monkeypatch, final="ok", comment="ok", inprogress="ok"):
    sinks, _plan = world
    repos = []

    def post(pr_sha, state, description, pr_id=None, repo=None):
        sinks.statuses.append((state, description))
        repos.append(repo)
        return inprogress if state == "INPROGRESS" else final
    monkeypatch.setattr(m, "post_build_status", post)
    monkeypatch.setattr(m, "upsert_comment",
                        lambda pr_id, body, existing_id=None, repo=None, **kw:
                        sinks.upserts.append(body) or comment)
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    assert m._extract_status_token(sinks.upserts[-1]) == "clean"
    assert [s for s, _ in sinks.statuses] == ["INPROGRESS", "SUCCESSFUL"]
    assert repos == ["acme-config-dev"] * 2
    return sinks


@pytest.mark.parametrize("final,comment", [("transient", "ok"), ("ok", "transient")])
def test_main_path_with_a_transient_write_retries(world, monkeypatch, final, comment):
    _main_world(world, monkeypatch, final=final, comment=comment)
    assert SK not in m._seen
    assert SK in m._retry_backoff


def test_main_path_success_is_seen_and_clears_the_backoff(world, monkeypatch):
    m._retry_backoff[SK] = [0, 2, PR_SHA]     # an earlier transient pass
    _main_world(world, monkeypatch)
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA)
    assert SK not in m._retry_backoff


def test_main_path_rejected_write_is_seen(world, monkeypatch):
    _main_world(world, monkeypatch, final="permanent")
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA)


def test_main_path_ignores_the_inprogress_result(world, monkeypatch):
    """The INPROGRESS post is a hint. Only the final writes gate _seen."""
    _main_world(world, monkeypatch, inprogress="transient")
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA)


def test_main_path_kcc_nil_block_keeps_its_description(world, monkeypatch):
    """The status chain now ends in one post. Its text must not change."""
    sinks, plan = world
    hdr = "/compute.cnrm.cloud.google.com/ComputeInstance vm-a"
    plan["pv-orch-a-ms"] = m.DiffResult(
        "--- main\n+++ pr", [(hdr, "+  hosting-id: hst-%!s(<nil>)")],
        1, True, "", m.OUT_DIFF, "", template_artifacts=[hdr])
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    assert sinks.statuses[-1] == (
        "FAILED", "Unresolved KCC value - Compute* resources render "
                  "%!s(<nil>) / <no value>; set hostingID (or the missing "
                  "field) before merging (see PR comment)")
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA), "a permanent block is not retried"


# ── fix_stuck_inprogress ─────────────────────────────────────────────────

CLEAN = f"3 resource(s) will change\n*ts — {m.COMMENT_MARKER} [clean]*"


def _stuck(monkeypatch, get, result="ok"):
    posted = []

    def fake_http(*a, **k):
        if isinstance(get, Exception):
            raise get
        return get
    monkeypatch.setattr(m, "http", fake_http)
    monkeypatch.setattr(m, "post_build_status",
                        lambda sha, state, desc, pr_id=None, repo=None:
                        posted.append((state, desc, repo)) or result)
    return posted


def test_fix_stuck_posts_from_the_comment_when_no_status_exists(monkeypatch):
    """Both posts failed, so the GET is a 404. Before, the except only
    logged, and the commit never got a merge gate."""
    posted = _stuck(monkeypatch, _http_error(404))
    assert m.fix_stuck_inprogress("a" * 12, 10, CLEAN, repo="acme-config-stage") == "ok"
    assert posted == [("SUCCESSFUL", "3 resource(s) will change - review comment",
                       "acme-config-stage")]


@pytest.mark.parametrize("result", ["transient", "permanent", "skipped"])
def test_fix_stuck_returns_the_write_result(monkeypatch, result):
    _stuck(monkeypatch, {"state": "INPROGRESS"}, result=result)
    assert m.fix_stuck_inprogress("a" * 12, 10, CLEAN) == result


def test_fix_stuck_with_a_final_status_has_nothing_to_write(monkeypatch):
    posted = _stuck(monkeypatch, {"state": "SUCCESSFUL"})
    assert m.fix_stuck_inprogress("a" * 12, 10, CLEAN) == "ok"
    assert posted == []


@pytest.mark.parametrize("exc,result", [(_http_error(503), "transient"),
                                        (urllib.error.URLError("dns"), "transient"),
                                        (_http_error(403), "permanent")])
def test_fix_stuck_classifies_a_failed_status_read(monkeypatch, exc, result):
    posted = _stuck(monkeypatch, exc)
    assert m.fix_stuck_inprogress("a" * 12, 10, CLEAN) == result
    assert posted == []


# ── process_pr: the cross-pod dedup gates _seen on fix_stuck ─────────────

def _dedup_world(world, monkeypatch, result):
    comment = (f"x\n*ts — {m.COMMENT_MARKER} [clean] [base:{BASE_SHA[:8]}]*")
    monkeypatch.setattr(m, "find_existing_comment",
                        lambda pr_id, repo=None: (7, PR_SHA[:8], comment))
    calls = []
    monkeypatch.setattr(m, "fix_stuck_inprogress",
                        lambda sha, pr_id, raw, repo=None: calls.append(repo) or result)
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    assert calls == ["acme-config-dev"]
    assert world[0].diff_calls == [], "an up-to-date comment is not re-rendered"


def test_dedup_retries_when_the_status_fix_did_not_land(world, monkeypatch):
    _dedup_world(world, monkeypatch, "transient")
    assert SK not in m._seen
    assert SK in m._retry_backoff


@pytest.mark.parametrize("result", ["ok", "permanent"])
def test_dedup_is_seen_once_the_status_is_settled(world, monkeypatch, result):
    m._retry_backoff[SK] = [0, 8, PR_SHA]     # a status write that failed before
    _dedup_world(world, monkeypatch, result)
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA)
    assert SK not in m._retry_backoff, "the next failure starts at 1 again"


# ── a failed write, then the real recovery: red stays red ────────────────

REAL_FIX_STUCK = m.fix_stuck_inprogress     # the world fixture stubs it


def _bitbucket(world, monkeypatch, state, fail):
    """The commit's build status as Bitbucket holds it (None: no status).
    The first post of each state in `fail` does not land."""
    sinks, _plan = world
    bb = {"state": state}
    fail = list(fail)

    def post(pr_sha, st, description, pr_id=None, repo=None):
        sinks.statuses.append((st, description))
        if st in fail:
            fail.remove(st)
            return "transient"
        bb["state"] = st
        return "ok"

    def get(url, *a, **k):
        assert "/statuses/build/" in url, url
        if bb["state"] is None:
            raise _http_error(404)
        return {"state": bb["state"]}
    monkeypatch.setattr(m, "post_build_status", post)
    monkeypatch.setattr(m, "http", lambda method, url, *a, **k: get(url))
    monkeypatch.setattr(m, "fix_stuck_inprogress", REAL_FIX_STUCK)
    return bb


def _render_then_recover(world, monkeypatch, passes=4):
    """One render, then the next passes find its comment up to date."""
    sinks = world[0]
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    body = sinks.upserts[-1]
    monkeypatch.setattr(m, "find_existing_comment",
                        lambda pr_id, repo=None: (5, PR_SHA[:8], body))
    for _ in range(passes):
        m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    return body


@pytest.mark.parametrize("start,fail", [
    ("INPROGRESS", ["FAILED"]),                  # the FAILED post was lost
    (None, ["INPROGRESS", "FAILED"]),            # both posts were lost
])
def test_apps_over_the_cap_stay_red_after_a_failed_status_write(
        world, monkeypatch, start, fail):
    """The cap posts FAILED, but the comment said [clean], so the recovery
    posted SUCCESSFUL "No manifest changes"."""
    monkeypatch.setattr(m, "MAX_APPS_PER_RUN", 1)
    bb = _bitbucket(world, monkeypatch, start, fail)
    body = _render_then_recover(world, monkeypatch)
    assert m._extract_status_token(body) == "permanent"
    assert "over the cap" in body
    assert [s for s, _ in world[0].statuses].count("SUCCESSFUL") == 0
    assert bb["state"] == "FAILED"
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA)


def test_a_block_whose_status_write_fails_turns_an_old_green_red(world, monkeypatch):
    """The early exits post no INPROGRESS. When their FAILED did not land,
    the commit kept the SUCCESSFUL of a render against an older main, and
    the recovery left it: a green gate under a [blocked] comment."""
    from test_coverage_orchestration import IDENTITY
    monkeypatch.setattr(m, "_detect_wiped_definitions", lambda *a, **k: [IDENTITY])
    bb = _bitbucket(world, monkeypatch, "SUCCESSFUL", ["FAILED"])
    body = _render_then_recover(world, monkeypatch)
    assert m._extract_status_token(body) == "blocked"
    assert bb["state"] == "FAILED"
    assert world[0].statuses[-1] == (
        "FAILED", "Blocked - merging would break the environment (see comment)")
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA)


_BLOCKED = f"x\n*ts — {m.COMMENT_MARKER} [blocked]*"
_PERMANENT = f"x\n*ts — {m.COMMENT_MARKER} [permanent]*"


@pytest.mark.parametrize("comment", [_BLOCKED, _PERMANENT])
def test_fix_stuck_turns_a_green_status_under_a_red_comment_red(monkeypatch, comment):
    posted = _stuck(monkeypatch, {"state": "SUCCESSFUL"})
    assert m.fix_stuck_inprogress("a" * 12, 10, comment) == "ok"
    assert [s for s, _d, _r in posted] == ["FAILED"]


@pytest.mark.parametrize("state,comment", [
    ("FAILED", CLEAN),        # never unblock from text rebuilt from a comment
    ("FAILED", _BLOCKED),
    ("STOPPED", _BLOCKED),
])
def test_fix_stuck_never_touches_a_red_or_stopped_status(monkeypatch, state, comment):
    posted = _stuck(monkeypatch, {"state": state})
    assert m.fix_stuck_inprogress("a" * 12, 10, comment) == "ok"
    assert posted == []


# ── every status write names its repo ────────────────────────────────────

def test_every_post_build_status_call_passes_repo():
    """Without repo= the status falls back to _repo_for_sha, a bounded map
    that is cleared when full. A PR mid-run can then post to BB_REPO: a 404
    that nobody sees, and a link to another repo's PR."""
    src = os.path.join(os.path.dirname(__file__), "..", "src")
    missing, calls = [], 0
    for path in sorted(glob.glob(os.path.join(src, "*.py"))):
        for node in ast.walk(ast.parse(open(path).read())):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name != "post_build_status":
                continue
            calls += 1
            if not any(k.arg == "repo" for k in node.keywords):
                missing.append(f"{os.path.basename(path)}:{node.lineno}")
    assert calls >= 10, "the scan found no calls; did the function move?"
    assert not missing, f"post_build_status without repo=: {missing}"
