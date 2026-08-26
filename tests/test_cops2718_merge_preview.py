"""The PR side is the merge of main and the branch, not the branch (COPS-2718).

Why this exists
---------------
The comment's question is "what will the cluster do when this merges", and
ArgoCD deploys MAIN. The base side was already honest — the poll re-reads
main's tip and syncs the mirror every iteration — but the PR side rendered
the source branch as it was when somebody cut it. Three paths read the whole
value-file chain at pr_sha (effective-chart-version resolution, env moves,
new-environment previews), so a version bump merged to main AFTER the branch
point was invisible and the preview showed the OLD chart: a downgrade that
the real merge would never produce. Marcos reported exactly that on the
acme-config repos: "el PR muestra cosas que son certeras en base a lo que el
commit nuestro no está al tanto".

The mechanism is one git command in the mirror that already exists:
`git merge-tree --write-tree` computes the merge without a worktree and
reports conflicts. The tree is wrapped in a synthetic commit with pinned
identity and epoch dates, so the sha is a PURE FUNCTION of (base, pr) —
every pod mints the same sha, `_mirror_has_sha` accepts it (^{commit}),
and every (sha, path) cache keeps working unchanged.

A CONFLICT is the one case where the merge — and therefore THE diff — cannot
be computed. Per the tool's one unbreakable rule, that must be said in red,
never approximated: no fallback diff, because any fallback describes a merge
that will never happen.

These tests use real git, like the mirror tests: a mocked subprocess would
pin assumptions, not git's behaviour.
"""
import os
import subprocess
import sys

import pytest

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """main and a PR branch that DIVERGE: after the branch point, main bumps
    the version of an environment the PR never touches. That is the exact
    shape of the reported bug."""
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    _git("config", "user.email", "t@t", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "gcp").mkdir()
    (work / "gcp" / "config.yaml").write_text("appspace:\n  version: 2603.1.0\n")
    (work / "gcp" / "other.yaml").write_text("appspace:\n  replicas: 1\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "branch point", cwd=work)
    fork = _git("rev-parse", "HEAD", cwd=work).stdout.strip()

    # The PR: changes other.yaml only.
    _git("checkout", "-qb", "pr", cwd=work)
    (work / "gcp" / "other.yaml").write_text("appspace:\n  replicas: 2\n")
    _git("commit", "-aqm", "pr change", cwd=work)
    pr_sha = _git("rev-parse", "HEAD", cwd=work).stdout.strip()

    # main moves on: the version bump the PR knows nothing about.
    _git("checkout", "-q", "main", cwd=work)
    (work / "gcp" / "config.yaml").write_text("appspace:\n  version: 2603.2.0\n")
    _git("commit", "-aqm", "main bumps the version", cwd=work)
    base_sha = _git("rev-parse", "HEAD", cwd=work).stdout.strip()

    mdir = tmp_path / "mirrors"
    mdir.mkdir()
    _git("clone", "--mirror", "-q", str(work), str(mdir / "acme-config-dev.git"))

    monkeypatch.setattr(m, "GIT_MIRROR_DIR", str(mdir))
    monkeypatch.setattr(m, "GIT_MIRROR_ENABLED", True)
    m._mirror_state_reset()
    m._merge_preview_cache.clear()
    return {"work": work, "fork": fork, "pr": pr_sha, "base": base_sha}


def test_the_preview_sees_what_main_learned_after_the_branch_point(repo):
    """The reported bug, reduced: the PR branched before main's version bump.
    Reading the chain at pr_sha shows 2603.1.0 — a phantom downgrade. The
    merge preview must show 2603.2.0 AND the PR's own change."""
    sha, conflicts = m._merge_preview("acme-config-dev", repo["base"], repo["pr"])
    assert conflicts == [] and sha

    content, status = m._git_read_file("acme-config-dev", sha, "gcp/config.yaml")
    assert status == m.BB_OK and "2603.2.0" in content, (
        "main's bump is invisible: this is the stale-output bug itself")
    content, status = m._git_read_file("acme-config-dev", sha, "gcp/other.yaml")
    assert status == m.BB_OK and "replicas: 2" in content, (
        "the PR's own change was lost in the merge preview")


def test_the_synthetic_sha_is_deterministic_across_pods(repo):
    """Two pods (or two iterations) must mint the SAME sha for the same
    (base, pr), or every (sha, path) cache and the cross-pod dedup would
    see phantom changes."""
    a, _ = m._merge_preview("acme-config-dev", repo["base"], repo["pr"])
    m._merge_preview_cache.clear()   # simulate the other pod: no warm cache
    b, _ = m._merge_preview("acme-config-dev", repo["base"], repo["pr"])
    assert a == b


def test_a_real_conflict_names_its_files_and_yields_no_sha(repo):
    work = repo["work"]
    _git("checkout", "-q", "pr", cwd=work)
    (work / "gcp" / "config.yaml").write_text("appspace:\n  version: 9999\n")
    _git("commit", "-aqm", "pr edits the same line main edited", cwd=work)
    pr2 = _git("rev-parse", "HEAD", cwd=work).stdout.strip()
    _git("--git-dir", str(work / ".git"), "push", "-q", "--mirror",
         os.path.join(m.GIT_MIRROR_DIR, "acme-config-dev.git"))
    m._mirror_state_reset()

    sha, conflicts = m._merge_preview("acme-config-dev", repo["base"], pr2)
    assert sha is None
    assert conflicts and "gcp/config.yaml" in conflicts, (
        f"the conflicted file must be named, got {conflicts}")


def test_conflict_and_cannot_compute_are_different_answers(repo):
    """A conflict is a fact about the PR; a sha the mirror lacks is a fact
    about the mirror. Conflating them would paint red over a fork PR."""
    sha, conflicts = m._merge_preview("acme-config-dev", repo["base"],
                                      "0" * 40)
    assert (sha, conflicts) == (None, None)


def test_mirror_off_degrades_to_none_none(repo, monkeypatch):
    monkeypatch.setattr(m, "GIT_MIRROR_ENABLED", False)
    assert m._merge_preview("acme-config-dev", repo["base"], repo["pr"]) == (None, None)


def test_the_preview_commit_never_reaches_the_bitbucket_api(repo, monkeypatch):
    """The synthetic sha exists only in the mirror. If a read for it ever
    fell through to the REST API it would 404 and poison the cache with a
    'file absent' that is false — so the mirror must ALWAYS answer for it."""
    sha, _ = m._merge_preview("acme-config-dev", repo["base"], repo["pr"])

    def boom(*a, **k):
        raise AssertionError("a merge-preview read fell through to the API")
    monkeypatch.setattr(m, "_pooled_urlopen", boom)
    monkeypatch.setattr(m.urllib.request, "urlopen", boom)

    content, status = m._bb_fetch_status("gcp/config.yaml", sha,
                                         repo="acme-config-dev")
    assert status == m.BB_OK and "2603.2.0" in content
    # And a path genuinely absent at the preview is a cacheable fact, not
    # an API fallback.
    content, status = m._bb_fetch_status("gcp/missing.yaml", sha,
                                         repo="acme-config-dev")
    assert (content, status) == (None, m.BB_NOT_FOUND)


def test_merge_tree_blowing_up_degrades_never_raises(repo, monkeypatch):
    """Exit code 2 (or a pre-2.38 git that lacks --write-tree) is not a
    conflict and not an answer: degrade to the branch tip and say so in the
    log, exactly like every other mirror failure."""
    real = m._git_run

    def broken(args, **kw):
        if "merge-tree" in args:
            r = real(["--version"], **{k: v for k, v in kw.items()
                                        if k != "env_extra"})
            r.returncode = 2
            r.stdout = "fatal: unknown option"
            return r
        return real(args, **kw)
    monkeypatch.setattr(m, "_git_run", broken)
    m._merge_preview_cache.clear()
    assert m._merge_preview("acme-config-dev", repo["base"], repo["pr"]) == (None, None)


def test_commit_tree_blowing_up_degrades_never_raises(repo, monkeypatch):
    real = m._git_run

    def broken(args, **kw):
        if "commit-tree" in args:
            return None
        return real(args, **kw)
    monkeypatch.setattr(m, "_git_run", broken)
    m._merge_preview_cache.clear()
    assert m._merge_preview("acme-config-dev", repo["base"], repo["pr"]) == (None, None)


def test_the_cache_is_bounded(repo):
    """The bounded-cache guard test knows this dict by name; this pins the
    bound it declares actually firing."""
    m._merge_preview_cache.clear()
    for i in range(513):
        m._merge_preview_cache[("r", str(i), "x")] = ("s", [])
    m._merge_preview("acme-config-dev", repo["base"], repo["pr"])
    assert len(m._merge_preview_cache) <= 2
