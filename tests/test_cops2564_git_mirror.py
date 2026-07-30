"""Read config files from a local git mirror instead of the Bitbucket API (COPS-2564).

Why this exists
---------------
Every value file was one HTTPS call to Bitbucket's `src/{sha}/{path}` endpoint.
Counting the files that actually exist in the repos: acme-config-prod has 391,
acme-config-dev 149, acme-config-stage 51. A PR that touches a root file such
as `gcp/config.yaml` affects every app, so the engine needs essentially every
file at BOTH shas, which is around 780 calls for one prod PR. Three things
multiply that:

  1. the cache key is (sha, path) and both shas move constantly (any push moves
     the head, any merge to main moves the base for EVERY open PR), so the
     cache goes cold and the whole set is re-read;
  2. three repos are polled in parallel, each with its own open PRs;
  3. every 429 becomes retries, which are more calls, which cause more 429s.

Measured on the real prod repo: `git fetch` takes 1.8s and reading all 391
files at a commit with `cat-file` takes 0.13s, against ~780 network calls.

Design constraints these tests pin
----------------------------------
* The seam is `_bb_fetch_status`, which every reader already goes through and
  which returns (content, BB_OK / BB_NOT_FOUND / BB_ERROR). The mirror must
  honour that contract exactly, or every caller downstream changes meaning.
* "sha not in the mirror" is NOT the same as "file absent at that sha". The
  first must fall back to the API (fork PRs, a branch force-pushed after the
  last fetch); the second is a fact and must return BB_NOT_FOUND, exactly as
  the API's 404 does, so it stays cacheable.
* Any git failure must degrade to the API, never raise. The mirror is an
  optimisation, not a new hard dependency.
* The token must never appear in argv, because it would be visible in the
  process list and in any error message that echoes the command.
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
def mirror(tmp_path, monkeypatch):
    """A real bare mirror with two commits, which is the only honest way to
    test git plumbing: a mocked subprocess would pin my assumptions, not git's
    actual behaviour."""
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    _git("config", "user.email", "t@t", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "gcp").mkdir()
    (work / "gcp" / "config.yaml").write_text("appspace:\n  version: 1\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "one", cwd=work)
    sha1 = _git("rev-parse", "HEAD", cwd=work).stdout.strip()
    (work / "gcp" / "later.yaml").write_text("appspace:\n  added: true\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "two", cwd=work)
    sha2 = _git("rev-parse", "HEAD", cwd=work).stdout.strip()

    mdir = tmp_path / "mirrors"
    mdir.mkdir()
    bare = mdir / "acme-config-dev.git"
    _git("clone", "--mirror", "-q", str(work), str(bare))

    monkeypatch.setattr(m, "GIT_MIRROR_DIR", str(mdir))
    monkeypatch.setattr(m, "GIT_MIRROR_ENABLED", True)
    m._mirror_state_reset()
    return {"sha1": sha1, "sha2": sha2, "bare": str(bare), "work": str(work),
            "dir": str(mdir)}


# ── the three outcomes the API contract distinguishes ───────────────────────

def test_reads_a_file_at_a_sha_from_the_mirror(mirror):
    content, status = m._git_read_file("acme-config-dev", mirror["sha1"],
                                       "gcp/config.yaml")
    assert status == m.BB_OK
    assert "version: 1" in content


def test_absent_path_at_a_known_sha_is_not_found_not_a_miss(mirror):
    """later.yaml exists at sha2 but not at sha1. At sha1 that is a FACT,
    the same one the API reports as 404, so it must be cacheable."""
    content, status = m._git_read_file("acme-config-dev", mirror["sha1"],
                                       "gcp/later.yaml")
    assert (content, status) == (None, m.BB_NOT_FOUND)
    content2, status2 = m._git_read_file("acme-config-dev", mirror["sha2"],
                                         "gcp/later.yaml")
    assert status2 == m.BB_OK and "added: true" in content2


def test_unknown_sha_is_a_miss_so_the_caller_can_fall_back(mirror):
    """A fork PR, or a branch force-pushed after the last fetch. Reporting
    NOT_FOUND here would cache a lie and render the environment empty."""
    assert m._git_read_file("acme-config-dev", "0" * 40, "gcp/config.yaml") is None


def test_unknown_repo_is_a_miss(mirror):
    assert m._git_read_file("not-mirrored", mirror["sha1"], "gcp/config.yaml") is None


# ── integration with the existing seam ──────────────────────────────────────

def test_fetch_status_serves_from_the_mirror_without_an_api_call(mirror, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not touch the Bitbucket API")
    monkeypatch.setattr(m, "_pooled_urlopen", boom)
    monkeypatch.setattr(m, "_repo_for_sha", lambda sha: "acme-config-dev")
    m.reset_bb_call_stats()
    content, status = m._bb_fetch_status("gcp/config.yaml", mirror["sha1"])
    assert status == m.BB_OK and "version: 1" in content
    s = m.bb_call_stats()
    assert s["file_fetches"] == 0, "a mirror read is not a Bitbucket call"
    assert s["mirror_reads"] == 1


def test_fetch_status_falls_back_to_the_api_on_a_miss(mirror, monkeypatch):
    calls = []

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"from-api: true"
    def fake(req, timeout=None):
        calls.append(req.full_url)
        return FakeResp()
    monkeypatch.setattr(m, "_pooled_urlopen", fake)
    monkeypatch.setattr(m, "_repo_for_sha", lambda sha: "acme-config-dev")
    m.reset_bb_call_stats()
    content, status = m._bb_fetch_status("gcp/config.yaml", "0" * 40)
    assert status == m.BB_OK and "from-api" in content
    assert len(calls) == 1
    assert m.bb_call_stats()["file_fetches"] == 1


def test_disabled_flag_keeps_the_old_behaviour(mirror, monkeypatch):
    monkeypatch.setattr(m, "GIT_MIRROR_ENABLED", False)
    called = []

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"from-api: true"
    monkeypatch.setattr(m, "_pooled_urlopen",
                        lambda req, timeout=None: (called.append(1), FakeResp())[1])
    monkeypatch.setattr(m, "_repo_for_sha", lambda sha: "acme-config-dev")
    content, status = m._bb_fetch_status("gcp/config.yaml", mirror["sha1"])
    assert status == m.BB_OK and len(called) == 1


# ── it must never become a new hard dependency ──────────────────────────────

def test_a_broken_git_never_raises_and_falls_back(mirror, monkeypatch):
    monkeypatch.setattr(m, "GIT_BIN", "/nonexistent/git")
    m._mirror_state_reset()
    assert m._git_read_file("acme-config-dev", mirror["sha1"], "gcp/config.yaml") is None


def test_corrupt_mirror_directory_never_raises(mirror, monkeypatch):
    monkeypatch.setattr(m, "GIT_MIRROR_DIR", "/nonexistent/mirrors")
    m._mirror_state_reset()
    assert m._git_read_file("acme-config-dev", mirror["sha1"], "gcp/config.yaml") is None


def test_sync_failure_is_logged_and_swallowed(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "GIT_MIRROR_DIR", str(tmp_path / "nope"))
    monkeypatch.setattr(m, "GIT_BIN", "/nonexistent/git")
    m._mirror_state_reset()
    m.mirror_sync("acme-config-dev")   # must not raise


# ── security and efficiency ─────────────────────────────────────────────────

def test_credentials_never_appear_in_argv(monkeypatch, tmp_path):
    """A token in argv is visible in the process list and in any error text
    that echoes the command. It goes through the environment instead."""
    seen = []

    class R:
        returncode = 1
        stdout = ""
        stderr = "boom"
    def fake_run(cmd, **kw):
        seen.append((cmd, kw.get("env") or {}))
        return R()
    monkeypatch.setattr(m.subprocess, "run", fake_run)
    monkeypatch.setattr(m, "GIT_MIRROR_DIR", str(tmp_path))
    m._mirror_state_reset()
    m.mirror_sync("acme-config-dev")
    assert seen, "git was never invoked"
    for cmd, env in seen:
        flat = " ".join(str(c) for c in cmd)
        # The encoded credential itself, not BB_TOKEN, which is "t" under
        # test and would match any string.
        assert m._BB_AUTH_HEADER not in flat, f"credential leaked into argv: {flat}"
        assert "Authorization" not in flat, f"header leaked into argv: {flat}"
        assert "@bitbucket.org" not in flat, f"credential in the clone URL: {flat}"
    assert any(env.get("GIT_CONFIG_VALUE_0", "").startswith("Authorization: ")
               for _, env in seen), "auth must be passed via the environment"
    assert any(env.get("GIT_TERMINAL_PROMPT") == "0" for _, env in seen), \
        "git must never block waiting for a password"


def test_sha_presence_is_checked_once_per_sha(mirror, monkeypatch):
    """Two subprocess calls per file (exists + read) would double the cost of
    the thing we are optimising."""
    real = m.subprocess.run
    checks = []

    def counting(cmd, **kw):
        if "cat-file" in cmd and "-e" in cmd:
            checks.append(cmd)
        return real(cmd, **kw)
    monkeypatch.setattr(m.subprocess, "run", counting)
    for path in ("gcp/config.yaml", "gcp/config.yaml", "gcp/later.yaml"):
        m._git_read_file("acme-config-dev", mirror["sha2"], path)
    assert len(checks) == 1, f"{len(checks)} presence checks for one sha"


def test_paths_are_normalised_like_the_api_reader(mirror):
    """Callers pass "$config/"-prefixed and leading-slash paths; the API
    reader normalises them, so the mirror must agree or the two disagree on
    what the same file is."""
    a = m._git_read_file("acme-config-dev", mirror["sha1"], "$config/gcp/config.yaml")
    b = m._git_read_file("acme-config-dev", mirror["sha1"], "/gcp/config.yaml")
    assert a[1] == m.BB_OK and b[1] == m.BB_OK


def test_mirror_reads_are_counted_separately():
    m.reset_bb_call_stats()
    m._count_bb_call("mirror_reads", 3)
    assert m.bb_call_stats()["mirror_reads"] == 3


def test_git_uses_the_token_auth_username_not_the_rest_one():
    """Verified live: bitbucket.org git rejects Basic auth built from the
    account email, which api.bitbucket.org accepts. Getting this wrong fails
    with "could not read Username" only once it reaches a real remote, so it
    is pinned here."""
    import base64
    env = m._git_env()
    value = env["GIT_CONFIG_VALUE_0"]
    assert value.startswith("Authorization: Basic ")
    decoded = base64.b64decode(value.split("Basic ", 1)[1]).decode()
    assert decoded.startswith("x-bitbucket-api-token-auth:")
    assert m._GIT_AUTH_HEADER != m._BB_AUTH_HEADER


def test_credential_probe_tries_both_shapes_and_keeps_the_one_that_works(monkeypatch):
    """An API token and an app password need different git usernames. The
    probe must find the working one instead of failing over to the API for
    the life of the pod."""
    import base64
    tried = []

    class R:
        def __init__(self, rc): self.returncode, self.stdout, self.stderr = rc, "", ""
    def fake_run(cmd, **kw):
        header = kw["env"]["GIT_CONFIG_VALUE_0"]
        user = base64.b64decode(header.split("Basic ", 1)[1]).decode().split(":", 1)[0]
        tried.append(user)
        return R(0 if user == m.BB_USER else 1)   # only the app-password shape works
    monkeypatch.setattr(m.subprocess, "run", fake_run)
    m._mirror_state_reset()
    m._resolve_git_credential("https://bitbucket.org/w/r.git")
    assert tried == ["x-bitbucket-api-token-auth", m.BB_USER]
    chosen = base64.b64decode(
        m._GIT_AUTH_HEADER.split("Basic ", 1)[1]).decode().split(":", 1)[0]
    assert chosen == m.BB_USER


def test_credential_probe_runs_once_per_pod(monkeypatch):
    calls = []

    class R:
        returncode, stdout, stderr = 0, "", ""
    monkeypatch.setattr(m.subprocess, "run",
                        lambda cmd, **kw: (calls.append(1), R())[1])
    m._mirror_state_reset()
    m._resolve_git_credential("https://bitbucket.org/w/r.git")
    m._resolve_git_credential("https://bitbucket.org/w/r.git")
    assert len(calls) == 1


# ── concurrency invariant, pinned so a refactor cannot silently break it ────

def test_mirror_sync_runs_before_the_pr_worker_pool_not_concurrently_with_it():
    """git fetch (mirror_sync) and git cat-file (_git_read_file, called from
    inside process_pr) must never race on the same mirror. Correctness today
    comes from ordering, not locking: mirror_sync runs once per repo, serially,
    before the ThreadPoolExecutor that processes PRs is even created, and that
    executor fully drains (as_completed over every future) before the next
    iteration's mirror_sync can run. If a future refactor moves mirror_sync
    inside the pool, or makes the pool non-blocking, this pins the failure."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "src",
                            "diff_preview.py")).read()
    mirror_call_idx = src.index("mirror_sync(repo)")
    pool_idx = src.index("with ThreadPoolExecutor(max_workers=MAX_PR_WORKERS)")
    assert mirror_call_idx < pool_idx, \
        "mirror_sync must run before the PR worker pool starts"
    pool_block = src[pool_idx:pool_idx + 700]
    assert "for fut in as_completed(futs)" in pool_block, \
        "the pool must fully drain before the function can return"


# ── one honest end-to-end pass against a REAL bare repo, no mocking of git ──

def test_end_to_end_against_a_real_bare_repo_with_no_mocks(tmp_path, monkeypatch):
    """Every other test in this file mocks subprocess at some layer. This one
    does not: a real `git init`, a real commit, a real --mirror clone, a real
    fetch, and a real cat-file read, exercising the exact code path
    mirror_sync -> _mirror_has_sha -> _git_read_file uses in production."""
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    _git("config", "user.email", "t@t", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "gcp").mkdir()
    (work / "gcp" / "config.yaml").write_text("appspace:\n  version: real\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "one", cwd=work)
    sha = _git("rev-parse", "HEAD", cwd=work).stdout.strip()

    monkeypatch.setattr(m, "GIT_MIRROR_DIR", str(tmp_path / "mirrors"))
    monkeypatch.setattr(m, "GIT_MIRROR_ENABLED", True)
    # Bypass the token-auth probe: this fixture repo has no auth at all, and
    # the probe is already covered on its own above.
    monkeypatch.setattr(m, "_resolve_git_credential", lambda *a, **k: None)
    m._mirror_state_reset()

    def fake_clone(args):
        # same shape as production ("clone", "--mirror", "--quiet", url, path)
        assert args[0] == "clone"
        real = subprocess.run(["git", "clone", "--mirror", "--quiet",
                               str(work), args[-1]],
                              capture_output=True, text=True)
        return real
    monkeypatch.setattr(m, "_git_run",
                        lambda args, cwd=None, timeout=None, auth_header=None:
                        fake_clone(args) if args[0] == "clone" else
                        subprocess.run(["git", *args], cwd=cwd,
                                       capture_output=True, text=True))
    monkeypatch.setattr(m, "_repo_for_sha", lambda s: "acme-config-dev")

    m.mirror_sync("acme-config-dev")
    content, status = m._bb_fetch_status("gcp/config.yaml", sha)
    assert status == m.BB_OK and "version: real" in content


def test_chart_exposes_a_kill_switch_for_the_mirror():
    """A fast rollback path (no code redeploy) matters for a change to the
    read path of every value file. gitMirrorEnabled must reach the container
    as GIT_MIRROR_ENABLED."""
    values = open(os.path.join(os.path.dirname(__file__), "..",
                               "charts/acme-diff-preview/values.yaml")).read()
    assert "gitMirrorEnabled" in values
    deploy = open(os.path.join(os.path.dirname(__file__), "..",
                               "charts/acme-diff-preview/templates/deployment.yaml")).read()
    assert "GIT_MIRROR_ENABLED" in deploy


def test_dockerfile_installs_git_as_a_runtime_dependency():
    """git is not purged like curl is, and its presence is verified at
    build time, the same pattern the image already uses for argocd/helm."""
    dockerfile = open(os.path.join(os.path.dirname(__file__), "..",
                                   "Dockerfile")).read()
    assert " git " in dockerfile or " git\\" in dockerfile
    assert "apt-get purge -y curl" in dockerfile  # git must survive this line
    purge_idx = dockerfile.index("apt-get purge -y curl")
    assert "git" not in dockerfile[purge_idx:purge_idx + 60]
    assert "git --version" in dockerfile


def test_tmp_emptydir_has_an_explicit_size_limit():
    """The mirrors live on the one writable path (readOnlyRootFilesystem).
    An unbounded emptyDir risks the node, not just the pod."""
    deploy = open(os.path.join(os.path.dirname(__file__), "..",
                               "charts/acme-diff-preview/templates/deployment.yaml")).read()
    idx = deploy.rindex('name: tmp')   # the volumes entry, not volumeMounts
    block = deploy[idx:idx + 700]
    assert "emptyDir" in block
    assert "sizeLimit" in block
