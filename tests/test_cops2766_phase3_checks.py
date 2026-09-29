"""COPS-2766 block 2, items 4 and 5: Phase 3 checks on a cascade teardown.

A private-cloud folder removal with the cascade armed in config was green in
three cases where the cascade does not do what the panel says:

- the ArgoCD cascade finalizer is not on the Applications yet. The panel had
  a red note, the build was green. Now it is the gate `not_live`, with the
  token [transient], so the PR clears itself after ArgoCD syncs. When the
  lookup fails (None) the panel shows one warning line and the build stays
  green;
- the env is paused (`appspace.autosync: false` at base), so the finalizer
  never arrives. The gate `paused` has no trailer;
- zeroPods or decommission has been true on main for less than 7 days. The
  gate `hold` is lifted by `Confirm-Decommission: <env>`. The history comes
  from the git mirror, first parent, and an unreadable history is never
  "hold met".
"""
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import comment_render as cr  # noqa: E402
import diff_preview as m  # noqa: E402

from test_coverage_orchestration import (  # noqa: E402,F401
    world, _mk_pr, PATH_MAP, BASE_SHA, PR_SHA, IDENTITY, IDENTITY_YAML)

SK = ("acme-config-dev", 991)
NOW = 2_000_000_000
DAY = 86400
IDENT = "gcp/prod/private-cloud/na2-a/monthly/pv-foo-c/customer.yaml"
LIVE = "appspace:\n  customerName: foo\n"
ARMED = LIVE + "  decommission: true\n"
STOPPED = LIVE + "  zeroPods: true\n"
BOTH = STOPPED + "  decommission: true\n"


@pytest.fixture(autouse=True)
def no_vertex(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)


# ── (a) _teardown_hold_met reads the first-parent history of the mirror ───

def _git(*args, cwd=None, ts=None):
    env = {**os.environ, "GIT_AUTHOR_DATE": f"@{ts} +0000",
           "GIT_COMMITTER_DATE": f"@{ts} +0000"} if ts else None
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture
def history(tmp_path, monkeypatch):
    """A real repo used as the mirror. commit() writes the identity file (or
    removes it with None) at a time in the past; hold() mirrors the repo and
    asks at its tip."""
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    _git("config", "user.email", "t@t", cwd=work)
    _git("config", "user.name", "t", cwd=work)

    def commit(days_ago, text, path=IDENT):
        f = work / path
        if text is None:
            f.unlink()
        else:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        _git("add", "-A", cwd=work)
        _git("commit", "-qm", f"{days_ago} days ago", cwd=work,
             ts=int(NOW - days_ago * DAY))

    def hold(register=True):
        sha = _git("rev-parse", "HEAD", cwd=work)
        mdir = tmp_path / "mirrors"
        shutil.rmtree(mdir, ignore_errors=True)
        mdir.mkdir()
        _git("clone", "--mirror", "-q", str(work), str(mdir / "acme-config-dev.git"))
        monkeypatch.setattr(m, "GIT_MIRROR_DIR", str(mdir))
        monkeypatch.setattr(m, "GIT_MIRROR_ENABLED", True)
        m._mirror_state_reset()
        if register:
            m._register_sha_repo(sha, "acme-config-dev")
        return m._teardown_hold_met(IDENT, sha, now=NOW)

    return commit, hold, work


def test_flag_set_10_days_ago_meets_the_hold(history):
    commit, hold, _ = history
    commit(30, LIVE)
    commit(10, ARMED)
    commit(1, "x: 1\n", path="gcp/other.yaml")   # not the identity file
    assert hold() is True


@pytest.mark.parametrize("days,expected", [(3, False), (6.9, False), (7, True)])
def test_the_hold_is_seven_days(history, days, expected):
    commit, hold, _ = history
    commit(30, LIVE)
    commit(days, ARMED)
    assert hold() is expected


def test_zeropods_10_days_ago_meets_it_even_with_decommission_1_day_ago(history):
    commit, hold, _ = history
    commit(10, STOPPED)
    commit(1, BOTH)
    assert hold() is True


def test_the_flag_off_and_on_again_restarts_the_hold(history):
    commit, hold, _ = history
    commit(30, LIVE)
    commit(10, ARMED)
    commit(5, LIVE)
    commit(2, ARMED)
    assert hold() is False


def test_history_that_runs_out_is_not_the_hold(history):
    """The env was created 3 days ago with the flag already on."""
    commit, hold, _ = history
    commit(3, ARMED)
    assert hold() is False


def test_a_commit_where_the_file_is_absent_is_flags_off(history):
    commit, hold, _ = history
    commit(30, ARMED)
    commit(20, None)
    commit(2, ARMED)
    assert hold() is False


def test_the_time_is_when_it_landed_on_main(history):
    """A branch commit from 10 days ago merged yesterday: first parent sees
    the merge, so the hold starts yesterday."""
    commit, hold, work = history
    commit(30, LIVE)
    _git("checkout", "-q", "-b", "arm", cwd=work)
    commit(10, ARMED)
    _git("checkout", "-q", "main", cwd=work)
    _git("merge", "-q", "--no-ff", "-m", "merge", "arm", cwd=work, ts=NOW - DAY)
    assert hold() is False


def test_the_mirror_off_or_an_unknown_sha_is_none(history, monkeypatch):
    commit, hold, _ = history
    commit(11, ARMED)                  # a sha no other test registers
    assert hold(register=False) is None, "no repo for this sha"
    m._register_sha_repo("f" * 40, "acme-config-dev")
    assert m._teardown_hold_met(IDENT, "f" * 40, now=NOW) is None, "sha not in the mirror"
    assert m._teardown_hold_met(IDENT, "", now=NOW) is None
    assert hold() is True
    sha = m._git_run(["--git-dir", os.path.join(m.GIT_MIRROR_DIR, "acme-config-dev.git"),
                      "rev-parse", "HEAD"]).stdout.strip()
    monkeypatch.setattr(m, "GIT_MIRROR_ENABLED", False)
    assert m._teardown_hold_met(IDENT, sha, now=NOW) is None, "the mirror is off"


def test_a_git_error_is_none(history, monkeypatch):
    commit, hold, _ = history
    commit(10, ARMED)
    real = m._git_run

    def log_fails(rc):
        def run(args, **kw):
            if "log" not in args:
                return real(args, **kw)
            return None if rc is None else subprocess.CompletedProcess(args, rc, "", "boom")
        return run
    for rc in (None, 128):
        monkeypatch.setattr(m, "_git_run", log_fails(rc))
        assert hold() is None, rc


def test_a_read_miss_or_a_broken_file_is_none(history, monkeypatch):
    commit, hold, _ = history
    commit(10, ARMED)
    commit(2, "appspace: [\n")
    assert hold() is None, "unparseable at a commit"
    commit(1, ARMED)
    monkeypatch.setattr(m, "_git_read_file", lambda *a: None)
    assert hold() is None, "a read miss"


def test_now_defaults_to_the_clock(history):
    commit, hold, work = history
    commit((NOW - 1_000_000_000) / DAY, ARMED)      # in 2001
    assert hold() is True
    sha = _git("rev-parse", "HEAD", cwd=work)
    assert m._teardown_hold_met(IDENT, sha, now=1_000_000_000 + DAY) is False
    assert m._teardown_hold_met(IDENT, sha) is True


# ── (b) the finalizer note ───────────────────────────────────────────────

APPS = ["pv-doomed-a-ms", "pv-doomed-a-ss"]


def _note(monkeypatch, live, apps=APPS):
    monkeypatch.setattr(m, "_cascade_finalizer_live", lambda a: live)
    return m._cascade_mismatch_note("pv-doomed-a", apps, True)


def test_not_live_note_starts_with_its_header(monkeypatch):
    assert _note(monkeypatch, False)[0].startswith("🚨 " + cr._DECOM_CASCADE_NOT_LIVE_HDR)


def test_unknown_finalizer_is_one_visible_warning(monkeypatch):
    assert _note(monkeypatch, None) == [
        "⚠️ Could not verify the cascade finalizer on `pv-doomed-a` (ArgoCD lookup "
        "failed). Check that `argocd app get pv-doomed-a-ms` lists "
        "`resources-finalizer.argocd.argoproj.io` before merging.", ""]
    assert "`argocd app get pv-doomed-a`" in _note(monkeypatch, None, apps=[])[0]
    assert _note(monkeypatch, True) == []


# ── (c) the evaluator records the gates ──────────────────────────────────

def _evaluate(monkeypatch, base_yaml, live=True, hold=True, public=False):
    with m._vf_cache_lock:
        m._vf_cache.clear()
    m._yaml_cache.clear()
    calls = []
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda p, s, **kw: (None, m.BB_NOT_FOUND) if s == "prsha"
                        else (base_yaml, m.BB_OK))
    monkeypatch.setattr(m, "_render_main_side_resources", lambda app, sha: {})
    monkeypatch.setattr(m, "_cascade_finalizer_live", lambda apps: live)
    monkeypatch.setattr(m, "_teardown_hold_met",
                        lambda ident, sha: calls.append((ident, sha)) or hold)
    if public:
        cand = {"env_name": "cl-prod-b", "apps": ["cl-prod-b-api-glb"], "block": "api",
                "identity_file": "gcp/prod/public-cloud/na1/cl-prod-b/api/config.yaml"}
    else:
        cand = {"env_name": "pv-foo-c", "identity_file": IDENT, "apps": ["pv-foo-c-ms"]}
    lines, envs = m._evaluate_env_decommissions([cand], "prsha", "mainsha")
    assert envs == [cand["env_name"]]
    return cand["gates"], "\n".join(lines), calls


def test_armed_live_and_held_has_no_gate(monkeypatch):
    gates, text, calls = _evaluate(monkeypatch, ARMED)
    assert gates == [] and calls == [(IDENT, "mainsha")]
    assert "Could not verify" not in text


def test_finalizer_not_live_is_a_not_live_gate(monkeypatch):
    gates, text, _ = _evaluate(monkeypatch, ARMED, live=False)
    assert gates == [{"kind": "not_live", "env": "pv-foo-c"}]
    assert cr._DECOM_CASCADE_NOT_LIVE_HDR in text


def test_finalizer_unknown_is_a_line_not_a_gate(monkeypatch):
    gates, text, _ = _evaluate(monkeypatch, ARMED, live=None)
    assert gates == []
    assert "⚠️ Could not verify the cascade finalizer on `pv-foo-c`" in text


def test_paused_at_base_is_a_paused_gate(monkeypatch):
    gates, _, _ = _evaluate(monkeypatch, ARMED + "  autosync: false\n")
    assert gates == [{"kind": "paused", "env": "pv-foo-c"}]


@pytest.mark.parametrize("hold,gate", [
    (False, {"kind": "hold", "env": "pv-foo-c"}),
    (None, {"kind": "hold", "env": "pv-foo-c", "why": "history unreadable"}),
])
def test_hold_not_met_is_a_hold_gate(monkeypatch, hold, gate):
    gates, _, _ = _evaluate(monkeypatch, ARMED, hold=hold)
    assert gates == [gate]


def test_no_cascade_or_public_cloud_never_reads_the_history(monkeypatch):
    gates, _, calls = _evaluate(monkeypatch, LIVE, live=False, hold=None)
    assert [g["kind"] for g in gates] == ["orphan"] and calls == []
    gates, _, calls = _evaluate(monkeypatch, ARMED, live=False, hold=None, public=True)
    assert [g["kind"] for g in gates] == ["public"] and calls == []


# ── the gate text ────────────────────────────────────────────────────────

def test_gate_text_adds_the_reason():
    g = {"kind": "hold", "env": "pv-x-a", "why": "history unreadable", "lifted": False}
    assert cr.gate_text(g) == cr.GATES["hold"][0] + " (history unreadable)"
    assert cr.gate_text({"kind": "hold", "env": "pv-x-a"}) == cr.GATES["hold"][0]
    assert cr.GATES["paused"][0] == cr._DECOM_PAUSED_HDR
    desc = cr.gate_status_description([g])
    assert desc.startswith(f"Blocked - {cr.gate_text(g)} in pv-x-a.")
    assert "add 'Confirm-Decommission: pv-x-a'" in desc
    summary = "\n".join(cr._build_merge_summary({}, {}, None, None, None, None,
                                                False, gates=[g]))
    assert f"**{cr.gate_text(g)}** in `pv-x-a`" in summary


# ── (d) process_pr ───────────────────────────────────────────────────────

@pytest.fixture()
def phase3(world, monkeypatch):
    """The PR removes pv-orch-a/customer.yaml; main has the cascade armed."""
    sinks, _plan = world
    files = {}
    monkeypatch.setattr(m, "get_pr_changed_files",
                        lambda pr_id, repo=None: ([IDENTITY], {}))
    monkeypatch.setattr(m, "_bb_fetch_status", lambda path, sha, repo=None:
                        (files["base"], m.BB_OK) if path == IDENTITY and sha == BASE_SHA
                        else (None, m.BB_NOT_FOUND))
    monkeypatch.setattr(m, "_render_main_side_resources", lambda app, sha: {})
    monkeypatch.setattr(m, "_shared_user_content_lines", lambda *a, **k: [])

    def run(live=True, hold=True, messages=(), extra=""):
        m._seen.clear()             # a new push, for a second pass
        with m._vf_cache_lock:
            m._vf_cache.clear()
        m._yaml_cache.clear()
        files["base"] = IDENTITY_YAML + "  decommission: true\n" + extra
        monkeypatch.setattr(m, "_cascade_finalizer_live", lambda apps: live)
        monkeypatch.setattr(m, "_teardown_hold_met", lambda *a, **k: hold)
        monkeypatch.setattr(m, "_pr_commit_messages", lambda *a: list(messages))
        m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
        return sinks.upserts[-1], sinks.statuses[-1]
    return run


def test_a_held_live_cascade_is_clean(phase3):
    body, (state, desc) = phase3()
    assert m._extract_status_token(body) == "clean"
    assert state == "SUCCESSFUL", desc
    assert "ENVIRONMENT DECOMMISSION" in body
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA)


def test_finalizer_not_live_is_transient_then_clean_after_the_sync(phase3):
    body, (state, desc) = phase3(live=False)
    assert m._extract_status_token(body) == "transient"
    assert state == "FAILED" and cr.GATES["not_live"][0] in desc
    assert SK not in m._seen and SK in m._retry_backoff
    assert m._backoff_should_skip(SK, PR_SHA), "the next loop waits"
    body, (state, _desc) = phase3(live=True)
    assert m._extract_status_token(body) == "clean" and state == "SUCCESSFUL"


def test_finalizer_unknown_stays_green_with_the_line(phase3):
    body, (state, _desc) = phase3(live=None)
    assert m._extract_status_token(body) == "clean" and state == "SUCCESSFUL"
    assert "Could not verify the cascade finalizer on `pv-orch-a`" in body


def test_paused_at_base_is_blocked_and_no_trailer_lifts_it(phase3):
    body, (state, desc) = phase3(extra="  autosync: false\n", messages=[
        "Confirm-Decommission: pv-orch-a\nConfirm-Teardown: pv-orch-a"])
    assert m._extract_status_token(body) == "blocked" and state == "FAILED"
    assert desc.startswith(f"Blocked - {cr._DECOM_PAUSED_HDR} in pv-orch-a")
    assert f"⛔ **{cr._DECOM_PAUSED_HDR}** in `pv-orch-a`" in body
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA)


def test_hold_not_met_is_blocked_until_confirm_decommission(phase3):
    body, (state, desc) = phase3(hold=False, messages=["Confirm-Teardown: pv-orch-a"])
    assert m._extract_status_token(body) == "blocked" and state == "FAILED"
    assert "add 'Confirm-Decommission: pv-orch-a' to a commit message" in desc
    body, (state, desc) = phase3(hold=False, messages=[
        "Remove pv-orch-a\n\nConfirm-Decommission: pv-orch-a\n"])
    assert m._extract_status_token(body) == "clean"
    assert state == "SUCCESSFUL", desc
    assert "☑️ **Confirmed in a commit:** `Confirm-Decommission: pv-orch-a`" in body


def test_unreadable_history_blocks_visibly(phase3):
    body, (state, desc) = phase3(hold=None)
    assert m._extract_status_token(body) == "blocked" and state == "FAILED"
    assert "(history unreadable)" in desc and "(history unreadable)" in body
