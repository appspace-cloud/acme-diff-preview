"""A PR whose head is already in main is not rendered again (COPS-2766).

A merged PR can still be in the open list when main already has its merge
commit. The poll then rendered it against that merge commit and replaced
the verdict the reviewers saw (#4504 got a false FAILED). process_pr now
asks the mirror `git merge-base --is-ancestor pr_sha base_sha` and skips
on True. False and None (mirror off, sha not fetched, git error) go on as
before.

The helper is tested against real git, like the other mirror tests.
"""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
import diff_preview as m  # noqa: E402
import logsink  # noqa: E402

from test_coverage_orchestration import world, _mk_pr, PATH_MAP, BASE_SHA, PR_SHA  # noqa: E402,F401


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


@pytest.fixture(autouse=True)
def _clean():
    m._mirror_ancestor_cache.clear()
    m._already_merged_logged.clear()
    yield
    m._mirror_ancestor_cache.clear()
    m._already_merged_logged.clear()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """main and a PR branch that diverge after the branch point."""
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    _git("config", "user.email", "t@t", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "a.yaml").write_text("a: 1\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "branch point", cwd=work)
    fork = _git("rev-parse", "HEAD", cwd=work).stdout.strip()
    _git("checkout", "-qb", "pr", cwd=work)
    (work / "b.yaml").write_text("b: 1\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "pr change", cwd=work)
    pr_sha = _git("rev-parse", "HEAD", cwd=work).stdout.strip()
    _git("checkout", "-q", "main", cwd=work)
    (work / "a.yaml").write_text("a: 2\n")
    _git("commit", "-aqm", "main moves on", cwd=work)
    base_sha = _git("rev-parse", "HEAD", cwd=work).stdout.strip()

    mdir = tmp_path / "mirrors"
    mdir.mkdir()
    mirror = mdir / f"{m.BB_REPO}.git"
    _git("clone", "--mirror", "-q", str(work), str(mirror))
    monkeypatch.setattr(m, "GIT_MIRROR_DIR", str(mdir))
    monkeypatch.setattr(m, "GIT_MIRROR_ENABLED", True)
    m._mirror_state_reset()
    return {"work": work, "mirror": mirror, "fork": fork,
            "pr": pr_sha, "base": base_sha}


def _merge_pr_no_ff(repo):
    """Merge the PR into main the way Bitbucket does, then fetch the mirror."""
    _git("merge", "--no-ff", "-qm", "Merged in pr (pull request #1)", "pr",
         cwd=repo["work"])
    merged = _git("rev-parse", "HEAD", cwd=repo["work"]).stdout.strip()
    _git("--git-dir", str(repo["mirror"]), "fetch", "-q", "origin", cwd=repo["work"])
    m._mirror_state_reset()
    return merged


# ── the helper, on real git ─────────────────────────────────────────────

def test_a_commit_already_in_main_is_an_ancestor(repo):
    assert m._mirror_is_ancestor(m.BB_REPO, repo["fork"], repo["base"]) is True


def test_a_diverged_branch_is_not_an_ancestor(repo):
    assert m._mirror_is_ancestor(m.BB_REPO, repo["pr"], repo["base"]) is False


def test_after_a_no_ff_merge_the_pr_head_is_an_ancestor(repo):
    """The #4504 shape: main's tip is the PR's own merge commit."""
    merged = _merge_pr_no_ff(repo)
    assert m._mirror_is_ancestor(m.BB_REPO, repo["pr"], merged) is True


def test_an_unknown_sha_is_none_and_is_not_cached(repo):
    """The sha may arrive with the next fetch, so None is never cached."""
    assert m._mirror_is_ancestor(m.BB_REPO, "0" * 40, repo["base"]) is None
    assert m._mirror_ancestor_cache == {}


def test_mirror_disabled_is_none(repo, monkeypatch):
    monkeypatch.setattr(m, "GIT_MIRROR_ENABLED", False)
    assert m._mirror_is_ancestor(m.BB_REPO, repo["fork"], repo["base"]) is None


def test_a_mirror_not_cloned_yet_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "GIT_MIRROR_DIR", str(tmp_path))
    monkeypatch.setattr(m, "GIT_MIRROR_ENABLED", True)
    assert m._mirror_is_ancestor(m.BB_REPO, PR_SHA, BASE_SHA) is None


@pytest.mark.parametrize("result", [None, subprocess.CompletedProcess([], 128)])
def test_a_git_error_is_none_and_is_not_cached(repo, monkeypatch, result):
    """git not runnable, or an exit code that is neither 0 nor 1."""
    monkeypatch.setattr(m, "_mirror_has_sha", lambda r, s: True)
    monkeypatch.setattr(m, "_git_run", lambda *a, **k: result)
    assert m._mirror_is_ancestor(m.BB_REPO, repo["fork"], repo["base"]) is None
    assert m._mirror_ancestor_cache == {}


def test_true_and_false_are_cached(repo, monkeypatch):
    assert m._mirror_is_ancestor(m.BB_REPO, repo["fork"], repo["base"]) is True
    assert m._mirror_is_ancestor(m.BB_REPO, repo["pr"], repo["base"]) is False
    with monkeypatch.context() as mp:
        mp.setattr(m, "_git_run", lambda *a, **k: pytest.fail("git was called"))
        assert m._mirror_is_ancestor(m.BB_REPO, repo["fork"], repo["base"]) is True
        assert m._mirror_is_ancestor(m.BB_REPO, repo["pr"], repo["base"]) is False


def test_the_cache_is_bounded(repo):
    for i in range(600):
        m._mirror_ancestor_cache[("r", str(i), "x")] = True
    assert m._mirror_is_ancestor(m.BB_REPO, repo["fork"], repo["base"]) is True
    assert len(m._mirror_ancestor_cache) == 1


# ── process_pr ──────────────────────────────────────────────────────────

def _events(monkeypatch):
    seen = []
    real = logsink.log

    def spy(msg, *a, **k):
        seen.append(k.get("event"))
        return real(msg, *a, **k)
    monkeypatch.setattr(logsink, "log", spy)
    return seen


def test_process_pr_skips_a_pr_already_in_main(world, monkeypatch):
    """No render, no comment, no status, no _seen, and one log line per sha."""
    sinks, _plan = world
    asked = []
    monkeypatch.setattr(m, "_mirror_is_ancestor",
                        lambda repo, anc, desc: asked.append((repo, anc, desc)) or True)
    events = _events(monkeypatch)
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)

    assert asked == [(m.BB_REPO, PR_SHA, BASE_SHA)] * 2
    assert sinks.upserts == [] and sinks.statuses == [] and sinks.diff_calls == []
    with m._seen_lock:
        assert (m.BB_REPO, 991) not in m._seen
    assert events.count("pr_skipped_already_merged") == 1


def test_the_logged_once_set_is_bounded(world, monkeypatch):
    monkeypatch.setattr(m, "_mirror_is_ancestor", lambda *a: True)
    m._already_merged_logged.update(("sk", str(i)) for i in range(600))
    events = _events(monkeypatch)
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    assert m._already_merged_logged == {((m.BB_REPO, 991), PR_SHA)}
    assert events.count("pr_skipped_already_merged") == 1


@pytest.mark.parametrize("answer", [False, None])
def test_process_pr_goes_on_when_not_known_to_be_merged(world, monkeypatch, answer):
    """A diverged head (False) or no answer (None) renders as before."""
    sinks, _plan = world
    monkeypatch.setattr(m, "_mirror_is_ancestor", lambda *a: answer)
    events = _events(monkeypatch)
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)

    assert "pr_skipped_already_merged" not in events
    assert len(sinks.upserts) == 1
    assert m._extract_status_token(sinks.upserts[0]) == "clean", "not the transient path"
    assert [s for s, _ in sinks.statuses][-1] == "SUCCESSFUL"
    with m._seen_lock:
        assert (m.BB_REPO, 991) in m._seen


def test_process_pr_skips_its_own_merge_commit_on_real_git(world, repo):
    """End to end on a real mirror: the #4504 shape is skipped."""
    sinks, _plan = world
    merged = _merge_pr_no_ff(repo)
    pr = _mk_pr()
    pr["source"]["commit"]["hash"] = repo["pr"]
    m.process_pr(pr, PATH_MAP, base_sha=merged)
    assert sinks.upserts == [] and sinks.statuses == [] and sinks.diff_calls == []
