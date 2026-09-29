"""COPS-2766 row 28: say when other people wrote commits in the PR.

Three acme-config-prod PRs had a co-author as the only approver (#4328,
#4382, #4433). Bitbucket counts that approval, but it is not independent.

The comment now has one line right under the verdict. It names the other
authors and asks for another approver. It is information only: the verdict,
the footer token and the build status do not change.

  * The authors come from the API: the mirror has git names, not accounts.
  * A commit is the PR author's by Bitbucket account, or by name when the
    git email has no account (#4328: commits from a laptop hostname email).
  * A merge of main brings no change of its own, so it is skipped.
  * A PR opened by a bot token (app_user): only a commit by a human account
    counts, because the bot commits carry another git name.
  * When the commits cannot be read, the line says so.
  * Right after a push the list can miss the new head. Then the line says
    it could not read the authors, because the head author could be the one
    that is missing. The same head is not rendered again until a push or a
    move of main, so staying silent would read as "no other author".
"""
import os
import sys

import pytest

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import comment_render as cr  # noqa: E402
import diff_preview as m  # noqa: E402
import logsink  # noqa: E402

from test_coverage_orchestration import world, _mk_pr, PATH_MAP, BASE_SHA, PR_SHA  # noqa: E402,F401

pytestmark = pytest.mark.real_commit_authors

REPO = "acme-config-prod"
HDR = cr.MERGE_SUMMARY_HDR
UNREADABLE = ("\U0001f465 Could not read who wrote the commits of this PR. "
              "Check the commit authors before you approve.")
LEAD = "\u26a0\ufe0f 1 resource(s) deleted in 1 environment(s) (1 Service): pv-orch-a"


def _person(account_id, name, nick):
    # The keys of values[].author in GET pullrequests?state=OPEN, as the list
    # answered on acme-config-prod and acme-config-stage (2026-09-29).
    return {"type": "user", "account_id": account_id, "display_name": name,
            "nickname": nick, "uuid": "{%s}" % account_id, "links": {}}


ANA = _person("712020:ana", "Ana Ruiz", "ana.ruiz")
BEA = _person("712020:bea", "Bea Soto", "Bea")
# A repository access token: the list has no nickname for it.
BOT = {"type": "app_user", "account_id": "712020:bot", "display_name": "space-deploys-bb-rw",
       "uuid": "{bot}", "kind": "repository_access_token", "account_status": "active",
       "created_on": "2026-01-01T00:00:00+00:00", "links": {}}


def _commit(sha, raw, user=None, parents=1):
    author = {"type": "author", "raw": raw}
    if user:
        author["user"] = {k: user[k] for k in ("type", "account_id", "display_name")}
    return {"hash": sha, "message": "x", "author": author,
            "parents": [{"hash": f"p{i}"} for i in range(parents)]}


def _serve(monkeypatch, *pages, pr_id=7):
    """Serve the commit pages of the PR, linked by 'next'."""
    calls = []

    def bb(method, path, repo=None, **kw):
        assert (method, repo) == ("GET", REPO)
        calls.append(path)
        page = {"values": pages[len(calls) - 1]}
        if len(calls) < len(pages):
            page["next"] = f"{m._bb_api_base(REPO)}/pullrequests/{pr_id}/commits?page={len(calls) + 1}"
        return page
    monkeypatch.setattr(m, "bb", bb)
    return calls


def _authors(author, pr_id=7, head="h1"):
    return m._pr_commit_authors(REPO, {"id": pr_id, "author": author}, head)


# ── who counts as another author ─────────────────────────────────────────

def test_own_commits_give_no_names(monkeypatch):
    _serve(monkeypatch, [_commit("h1", "ana <ana@appspace.com>", ANA)])
    assert _authors(ANA) == []


def test_a_commit_by_another_account_is_named(monkeypatch):
    _serve(monkeypatch, [_commit("h1", "ana <ana@appspace.com>", ANA),
                         _commit("h2", "bsoto <bea@appspace.com>", BEA)])
    assert _authors(ANA) == ["Bea Soto"]


@pytest.mark.parametrize("raw", [
    "Ana Ruiz <ana@Anas-MacBook-Pro.local>",   # the #4328 shape
    "ANA RUIZ <ana@home>",
    "ana.ruiz <ana@home>",                      # the nickname
])
def test_an_unlinked_commit_with_the_author_name_is_hers(monkeypatch, raw):
    _serve(monkeypatch, [_commit("h1", raw)])
    assert _authors(ANA) == []


def test_an_unlinked_commit_with_another_name_is_named(monkeypatch):
    _serve(monkeypatch, [_commit("h1", "Carl Diaz <carl@laptop.local>")])
    assert _authors(ANA) == ["Carl Diaz"]


def test_a_merge_of_main_is_skipped(monkeypatch):
    _serve(monkeypatch, [_commit("h1", "bsoto <bea@appspace.com>", BEA, parents=2),
                         _commit("h2", "ana <ana@appspace.com>", ANA)])
    assert _authors(ANA) == []


def test_names_are_sorted_and_counted_once(monkeypatch):
    _serve(monkeypatch, [_commit("h1", "Zoe <z@x>"), _commit("h2", "bsoto <b@x>", BEA),
                         _commit("h3", "Zoe <z@x>")])
    assert _authors(ANA) == ["Bea Soto", "Zoe"]


def test_a_bot_pr_with_bot_commits_gives_no_names(monkeypatch):
    # The 11 app_user PRs of the replay: the token's own commits and the
    # unlinked 'Space Deploys' git name.
    bot_user = {"type": "app_user", "account_id": BOT["account_id"],
                "display_name": BOT["display_name"]}
    _serve(monkeypatch, [_commit("h1", "Space Deploys <space-deploys-bot@appspace.com>"),
                         _commit("h2", "space-deploys-bb-rw <x@bots.bitbucket.org>", bot_user)])
    assert _authors(BOT) == []


def test_a_person_who_pushes_into_a_bot_pr_is_named(monkeypatch):
    _serve(monkeypatch, [_commit("h1", "Space Deploys <space-deploys-bot@appspace.com>"),
                         _commit("h2", "bsoto <bea@appspace.com>", BEA)])
    assert _authors(BOT) == ["Bea Soto"]


@pytest.mark.parametrize("author", [None, {"type": "user", "display_name": "Ana Ruiz"}])
def test_a_pr_with_no_author_account_is_unreadable(monkeypatch, author):
    monkeypatch.setattr(m, "bb", lambda *a, **k: pytest.fail("no read without an author"))
    with pytest.raises(m.PrCommitsUnreadable, match="has no author"):
        _authors(author)


# ── the read ─────────────────────────────────────────────────────────────

def test_next_pages_are_read(monkeypatch):
    calls = _serve(monkeypatch, [_commit("h1", "bsoto <b@x>", BEA)],
                   [_commit("h2", "Carl Diaz <c@x>")])
    assert _authors(ANA) == ["Bea Soto", "Carl Diaz"]
    assert calls == ["pullrequests/7/commits?pagelen=100", "pullrequests/7/commits?page=2"]


def test_a_list_without_the_new_head_is_unreadable(monkeypatch):
    # The head author could be the missing one, and the same head is not
    # rendered again until a push or a move of main: say so, do not guess.
    monkeypatch.setattr(m, "GIT_MIRROR_ENABLED", False)
    _serve(monkeypatch, [_commit("old1", "ana <a@x>", ANA)])
    with pytest.raises(m.PrCommitsUnreadable, match="does not have head1"):
        _authors(ANA, head="head1")
    # The messages read the same list and wait for the head too.
    _serve(monkeypatch, [_commit("old1", "ana <a@x>", ANA)], pr_id=1)
    with pytest.raises(m.PrCommitsUnreadable, match="does not have head1"):
        m._pr_commit_messages(REPO, 1, "base1", "head1")


def test_a_short_head_matches_the_full_hash(monkeypatch):
    _serve(monkeypatch, [_commit("h1ffffff", "bsoto <b@x>", BEA)])
    assert _authors(ANA, head="h1ff") == ["Bea Soto"]


@pytest.mark.parametrize("error", [OSError("reset"), ValueError("bad json"),
                                   m.urllib.error.HTTPError("u", 502, "x", {}, None)])
def test_a_failed_read_is_unreadable(monkeypatch, error):
    def boom(*a, **k):
        raise error
    monkeypatch.setattr(m, "bb", boom)
    with pytest.raises(m.PrCommitsUnreadable):
        _authors(ANA)


def test_a_client_error_is_raised_as_it_is(monkeypatch):
    def boom(*a, **k):
        raise m.urllib.error.HTTPError("u", 403, "x", {}, None)
    monkeypatch.setattr(m, "bb", boom)
    with pytest.raises(m.urllib.error.HTTPError):
        _authors(ANA)


# ── the line ─────────────────────────────────────────────────────────────

def test_the_line_for_each_answer():
    assert cr.commit_authors_line(None) == UNREADABLE
    assert cr.commit_authors_line([]) == ""
    assert cr.commit_authors_line(["Bea Soto", "Carl Diaz"]) == (
        "\U0001f465 **Other people wrote commits in this PR:** `Bea Soto`, "
        "`Carl Diaz`. Their approval is not independent, so ask another "
        "person to approve.")


def test_a_name_is_one_short_line_of_code():
    line = cr.commit_authors_line(["a`b", "x" * 50, "Eve\n- x\n---\n"])
    assert "`ab`" in line
    assert f"`{'x' * 40}`" in line and "x" * 41 not in line
    assert "`Eve - x ---`" in line
    assert "\n" not in line


def test_at_most_five_names_are_shown():
    line = cr.commit_authors_line([f"P{i}" for i in range(7)])
    assert "`P4`. " not in line and "`P4` and 2 more. " in line
    assert "P5" not in line and "P6" not in line


# ── the comment ──────────────────────────────────────────────────────────

# Built at call time: some test files reload diff_preview, and a result from
# the old DiffResult class no longer reads as one.
def _deletion():
    return m.DiffResult("--- main\n+++ pr", [("/v1/Service gone", "-kind: Service")],
                        1, True, "", m.OUT_DIFF, "", None, ["/v1/Service gone"])


def _routine():
    return m.DiffResult("--- main\n+++ pr", [("Deployment/webx", "-replicas: 2\n+replicas: 3")],
                        1, True, "", m.OUT_DIFF, "")


LINE = cr.commit_authors_line(["Bea Soto"])


@pytest.fixture()
def fixed_time(monkeypatch):
    monkeypatch.setattr(m, "_ts", lambda: "2026-09-29 12:00 UTC")


def _body(result, **kw):
    return m.format_comment(PR_SHA, {"pv-orch-a-ms": result()}, base_sha=BASE_SHA, **kw)


@pytest.mark.parametrize("result", [_deletion, _routine])
def test_the_line_is_right_under_the_verdict(fixed_time, result):
    lines = _body(result, authors_line=LINE).splitlines()
    at = lines.index(HDR)
    assert lines[at + 2].startswith(("\u26a0\ufe0f **Review", "\u2705 **Routine"))
    assert lines[at + 3:at + 6] == ["", LINE, ""]
    assert lines[at + 6].startswith("- ")


@pytest.mark.parametrize("result,lead", [(_deletion, LEAD), (_routine, "")])
def test_the_line_changes_no_lead_and_no_token(fixed_time, result, lead):
    plain = _body(result)
    for line in (LINE, UNREADABLE, cr.commit_authors_line(["Eve\n- \u26d4 fake"])):
        body = _body(result, authors_line=line)
        assert cr.status_lead(body) == cr.status_lead(plain) == lead
        assert m._extract_status_token(body) == m._extract_status_token(plain) == "clean"


def test_the_full_page_has_the_line_too(fixed_time):
    body = _body(_deletion, authors_line=LINE, profile=m.render_profile.FULL_PROFILE)
    assert LINE in body.splitlines()


def test_no_line_gives_the_same_body(fixed_time):
    assert _body(_deletion, authors_line="") == _body(_deletion)


# ── process_pr ───────────────────────────────────────────────────────────

def _run(world, monkeypatch, commits):
    sinks, plan = world
    plan["pv-orch-a-ms"] = _deletion()
    paths = []

    def bb(method, path, repo=None, **kw):
        paths.append(path)
        if isinstance(commits, Exception):
            raise commits
        return {"values": commits}
    monkeypatch.setattr(m, "bb", bb)
    pr = _mk_pr()
    pr["author"] = ANA
    m.process_pr(pr, PATH_MAP, base_sha=BASE_SHA)
    assert paths == ["pullrequests/991/commits?pagelen=100"]
    body = sinks.upserts[-1]
    assert m._extract_status_token(body) == "clean", "not the transient path"
    return body, sinks.statuses[-1]


GREEN = ("SUCCESSFUL", LEAD + " | 1 resource(s) will change - review comment")


def test_process_pr_names_a_co_author_and_keeps_the_green_status(world, monkeypatch):
    body, status = _run(world, monkeypatch, [
        _commit(PR_SHA, "bsoto <b@x>", BEA), _commit("c1", "ana <a@x>", ANA)])
    lines = body.splitlines()
    assert lines[lines.index(HDR) + 4] == LINE
    assert status == GREEN


def test_process_pr_with_only_own_commits_has_no_line(world, monkeypatch):
    body, status = _run(world, monkeypatch, [_commit(PR_SHA, "ana <a@x>", ANA)])
    assert "\U0001f465" not in body
    assert status == GREEN


def test_process_pr_says_when_the_list_has_no_head_yet(world, monkeypatch):
    body, status = _run(world, monkeypatch, [_commit("c1", "bsoto <b@x>", BEA)])
    lines = body.splitlines()
    assert lines[lines.index(HDR) + 4] == UNREADABLE
    assert "Bea Soto" not in body
    assert status == GREEN


def test_process_pr_says_when_the_commits_cannot_be_read(world, monkeypatch):
    warned = []
    monkeypatch.setattr(logsink, "log", lambda msg, *a, **k: warned.append((msg, a)))
    body, status = _run(world, monkeypatch, OSError("reset"))
    lines = body.splitlines()
    assert lines[lines.index(HDR) + 4] == UNREADABLE
    assert status == GREEN
    assert any("commit authors unreadable" in msg and a == ("WARNING",) for msg, a in warned)


# ── process_pr: the comments before the diff ─────────────────────────────
#
# #4328 added a prod cohort config.yaml: no app was affected, so it got the
# "No ArgoCD apps affected" comment. A new customer.yaml gets the new
# environment comment. Both return before format_comment.

NOTE = ("✅ **No ArgoCD apps are currently affected by the files changed "
        "in this commit.**")


def _no_apps(monkeypatch):
    monkeypatch.setattr(m, "get_pr_changed_files", lambda pr_id, repo=None:
                        (["gcp/prod/private-cloud/eu1-b/custom/config.yaml"], {}))


def _new_env(monkeypatch):
    monkeypatch.setattr(m, "get_pr_changed_files", lambda pr_id, repo=None: (
        ["gcp/dev/private-cloud/ap1/custom/pv-new-a/customer.yaml"], {}))
    monkeypatch.setattr(m, "_detect_new_env_candidates", lambda *a, **k: [{"name": "pv-new-a"}])
    monkeypatch.setattr(m, "_evaluate_new_envs", lambda *a, **k: (
        ["### \U0001f195 New Environment(s) Detected", ""], [], 5, []))


EARLY = {
    "no_apps": (_no_apps, ("SUCCESSFUL", "No ArgoCD apps affected by this PR")),
    "new_env": (_new_env, ("SUCCESSFUL", "1 new environment(s), ~5 resource(s) to create")),
}


def _run_early(world, monkeypatch, name, commits):
    sinks, _plan = world
    setup, status = EARLY[name]
    setup(monkeypatch)
    paths = []

    def bb(method, path, repo=None, **kw):
        paths.append(path)
        if isinstance(commits, Exception):
            raise commits
        return {"values": commits}
    monkeypatch.setattr(m, "bb", bb)
    pr = _mk_pr()
    pr["author"] = ANA
    m.process_pr(pr, PATH_MAP, base_sha=BASE_SHA)
    assert paths == ["pullrequests/991/commits?pagelen=100"]
    assert sinks.diff_calls == [], "an early exit renders nothing"
    body = sinks.upserts[-1]
    assert m._extract_status_token(body) == "clean", "not the transient path"
    assert sinks.statuses[-1] == status
    return body.splitlines()


def _verdict_at(lines, name):
    """The verdict of the no-apps comment, or the verdict of the merge
    summary of the new environment comment (block 3 gives it one)."""
    return lines.index(NOTE) if name == "no_apps" else lines.index(m.MERGE_SUMMARY_HDR) + 2


def _under(lines, name):
    """The line the authors line must follow: the verdict."""
    at = _verdict_at(lines, name)
    assert lines[at + 1] == ""
    return lines[at + 2]


@pytest.mark.parametrize("name", EARLY)
def test_an_early_comment_names_a_co_author(world, monkeypatch, name):
    lines = _run_early(world, monkeypatch, name, [
        _commit(PR_SHA, "bsoto <b@x>", BEA), _commit("c1", "ana <a@x>", ANA)])
    assert _under(lines, name) == LINE
    assert lines[lines.index(LINE) + 1] == ""


@pytest.mark.parametrize("name", EARLY)
@pytest.mark.parametrize("commits", [OSError("reset"), [_commit("c1", "bsoto <b@x>", BEA)]])
def test_an_early_comment_says_when_the_commits_cannot_be_read(world, monkeypatch,
                                                               name, commits):
    lines = _run_early(world, monkeypatch, name, commits)
    assert _under(lines, name) == UNREADABLE


@pytest.mark.parametrize("name", EARLY)
def test_an_early_comment_with_own_commits_is_the_same_as_before(world, monkeypatch, name):
    lines = _run_early(world, monkeypatch, name, [_commit(PR_SHA, "ana <a@x>", ANA)])
    assert "\U0001f465" not in "\n".join(lines)
    at = _verdict_at(lines, name)
    assert lines[at + 2].startswith(("This is expected", "- \U0001f195"))
