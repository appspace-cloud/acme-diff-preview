"""COPS-2766 block 3, item 6 (row 34, C24): an env that no ApplicationSet reads.

ArgoCD makes the apps of an env from the ApplicationSets: a git files generator
lists the identity files it reads. A new env, or a live env moved, where no glob
matches the file makes no apps, so nothing deploys on merge (#3423 in the
replay, the nachaos spoke). This is the `appset_miss` gate, and nothing lifts
it. The globs come from `argocd appset list` at PR time, cached like the path
map, and a miss lists them again first. aws/ and a list that is not proven give
a warning line, never a gate.
"""
import json
import os
import posixpath
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import comment_render as cr  # noqa: E402
import diff_preview as m  # noqa: E402
import logsink  # noqa: E402

from test_coverage_orchestration import (  # noqa: E402,F401
    world, _mk_pr, PATH_MAP, IDENTITY, IDENTITY_YAML, BASE_SHA, PR_SHA)

REPO = "acme-config-dev"
URL = "git@bitbucket.org:appspace-cloud/acme-config-dev"
PV = "gcp/dev/private-cloud"
CL = "gcp/dev/public-cloud/ap1"
GLOBS = sorted([f"{PV}/ap1/**/customer.yaml", f"{PV}/ap1-b/**/customer.yaml",
                f"{CL}/cl-*/config.yaml", f"{CL}/cl-*/app1/customer.yaml"])
NEW = f"{PV}/ap1/custom/pv-dev-b3glob-a/customer.yaml"
FAKE = f"{PV}/ap9/custom/pv-dev-b3glob-a/customer.yaml"
TEXT = cr.GATES["appset_miss"][0]
GATE_FIX = ". Check the cloud, tier and spoke folders"
CHECK = cr._NEW_ENV_CHECK_PREFIX
NONE = "ApplicationSet check unavailable (could not list ApplicationSets)"


def _gen(path, url=URL, **kw):
    return {"git": {"files": [{"path": path, **kw}], "repoURL": url, "revision": "main"}}


def _pv(glob, url=URL):
    """The private-cloud shape: the env files, then their cohort, times the charts."""
    return {"metadata": {"name": "pv"}, "spec": {"generators": [{"matrix": {"generators": [
        {"matrix": {"generators": [_gen(glob, url),
                                   _gen("{{.path.path}}/../config.yaml", url)]}},
        {"list": {"elements": [{"chart": "appspace-supporting-services"}]}}]}}]}}


def _cl(glob):
    return {"metadata": {"name": "cl"}, "spec": {"generators": [{"matrix": {"generators": [
        _gen(glob), {"list": {"elements": [{"chart": "appspace-micro-services"}]}}]}}]}}


APPSETS = [
    _pv(f"{PV}/ap1/**/customer.yaml"),
    _pv(f"{PV}/ap1-b/**/customer.yaml", "https://bitbucket.org/appspace-cloud/acme-config-dev.git/"),
    _cl(f"{CL}/cl-*/config.yaml"),
    _cl(f"{CL}/cl-*/app1/customer.yaml"),
    _cl(f"{CL}/cl-*/app1/customer.yaml"),                                   # a duplicate
    {"metadata": {"name": "old"}, "spec": {"generators": [
        _gen(f"{PV}/ap7/**/customer.yaml", exclude=True)]}},              # deploys nothing
    _pv("gcp/prod/private-cloud/na1-b/**/customer.yaml",
        "git@bitbucket.org:appspace-cloud/acme-config-prod"),              # another repo
    {"metadata": {"name": "bare"}},
]
LIVE = {
    "pv-dev-a-ss": ["$config/gcp/config.yaml", f"$config/{PV}/ap1/custom/config.yaml",
                    f"$config/{PV}/ap1/custom/pv-dev-a/customer.yaml"],
    "cl-dev11-a-api-glb": [f"$config/{CL}/cl-dev11-a/config.yaml",
                           f"$config/{CL}/cl-dev11-a/api/customer.yaml"],   # api is not checked
    "cl-dev11-a-app1-ms": [f"$config/{CL}/cl-dev11-a/app1/customer.yaml"],
    "pv-amzn-a-ss": ["$config/aws/prod/private-cloud/na1-a/pv-amzn-a/customer.yaml"],
    "pv-prod-x-ss": ["$config/gcp/prod/private-cloud/na9-a/pv-x/customer.yaml"],
    "pv-no-files": None,
}
REPOS = {"pv-dev-a-ss": REPO, "cl-dev11-a-app1-ms": REPO, "pv-prod-x-ss": "acme-config-prod"}


# ── the matcher ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("glob,path,hit", [
    (f"{PV}/ap1/**/customer.yaml", f"{PV}/ap1/pv-a/customer.yaml", True),
    (f"{PV}/ap1/**/customer.yaml", f"{PV}/ap1/custom/pv-a/customer.yaml", True),
    (f"{PV}/ap1/**/customer.yaml", f"{PV}/ap1/a/b/c/customer.yaml", True),
    (f"{PV}/ap1/**/customer.yaml", f"{PV}/ap1/customer.yaml", True),        # zero folders
    (f"{PV}/ap1/**/customer.yaml", f"{PV}/ap1-b/pv-a/customer.yaml", False),
    (f"{PV}/ap1/**/customer.yaml", f"{PV}/ap1/pv-a/customer.yaml.bak", False),
    (f"{PV}/**", f"{PV}/ap1/pv-a/customer.yaml", True),
    (f"{CL}/cl-*/app3/customer.yaml", f"{CL}/cl-dev11-a/app3/customer.yaml", True),
    (f"{CL}/cl-*/app4/customer.yaml", f"{CL}/cl-dev11-a/app41/customer.yaml", False),
    (f"{CL}/cl-*/app3/customer.yaml", f"{CL}/cl-a/x/app3/customer.yaml", False),
    (f"{CL}/cl-*/config.yaml", f"{CL}/cl-dev11-a/config.yaml", True),
    (f"{CL}/cl-*/config.yaml", f"{CL}/cl-dev11-a/api/config.yaml", False),
    (f"{CL}/cl-?/config.yaml", f"{CL}/cl-a/config.yaml", True),
    ("gcp/cl-?/config.yaml", "gcp/cl-//config.yaml", False),
    ("gcp/dev/config.yaml", "gcp/dev/configXyaml", False),
])
def test_the_glob_matcher(glob, path, hit):
    assert bool(m._glob_re(glob).fullmatch(path)) is hit


@pytest.mark.parametrize("path,checked", [
    (NEW, True),
    ("azure/prod/private-cloud/na1-a/pv-x-a/customer.yaml", True),
    (f"{CL}/cl-dev11-a/config.yaml", True),
    (f"{CL}/cl-dev11-a/app3/customer.yaml", True),
    (f"{CL}/cl-dev11-a/api/customer.yaml", False),     # rendered by the cl-*/config.yaml sets
    (f"{PV}/ap1/custom/config.yaml", False),           # a cohort
    (f"{PV}/ap1/custom/pv-dev-b3glob-a/cicd-versions.yaml", False),
    ("aws/prod/private-cloud/na1-a/pv-amzn-a/customer.yaml", False),
])
def test_the_checked_shapes(path, checked):
    assert m._appset_checked(path) is checked


# ── the ApplicationSet list ──────────────────────────────────────────────

@pytest.fixture()
def argocd(monkeypatch):
    """`argocd appset list` answers from `out`; `calls` records each run."""
    calls, out = [], {"rc": 0, "stdout": json.dumps(APPSETS), "stderr": ""}

    def run(args, **kw):
        calls.append((args, kw))
        if "raise" in out:
            raise out["raise"]
        return subprocess.CompletedProcess(args, out["rc"], out["stdout"], out["stderr"])
    monkeypatch.setattr(m.subprocess, "run", run)
    monkeypatch.setattr(m, "_app_value_files_map", dict(LIVE))
    monkeypatch.setattr(m, "_app_repo_map", dict(REPOS))
    return calls, out


@pytest.fixture()
def warnings(monkeypatch):
    seen = []
    real = logsink.log
    monkeypatch.setattr(logsink, "log", lambda msg, severity="INFO", **kw: (
        seen.append(msg) if severity == "WARNING" else None) or real(msg, severity, **kw))
    return seen


@pytest.mark.appset_reads
def test_the_globs_of_this_repo_and_how_they_are_listed(argocd, monkeypatch):
    calls, _out = argocd
    monkeypatch.setattr(m, "_argocd_token", "jwt")
    assert m._appset_file_globs(REPO) == GLOBS
    (args, kw), = calls
    assert args == [m.ARGOCD_BIN, "appset", "list", "-o", "json"] + m._auth_flags()
    assert kw["env"]["ARGOCD_AUTH_TOKEN"] == "jwt" and kw["timeout"] == 90


@pytest.mark.appset_reads
def test_an_items_wrapper_is_read_too(argocd):
    _calls, out = argocd
    out["stdout"] = json.dumps({"items": APPSETS})
    assert m._appset_file_globs(REPO) == GLOBS


@pytest.mark.appset_reads
def test_a_proven_list_is_cached_until_the_ttl_or_a_fresh_call(argocd):
    calls, _out = argocd
    assert m._appset_file_globs(REPO) == m._appset_file_globs(REPO) == GLOBS
    assert len(calls) == 1
    assert m._appset_file_globs(REPO, fresh=True) == GLOBS and len(calls) == 2
    ts, globs = m._appset_globs_cache[REPO]
    m._appset_globs_cache[REPO] = (ts - m.PATH_MAP_TTL - 1, globs)
    assert m._appset_file_globs(REPO) == GLOBS and len(calls) == 3


@pytest.mark.appset_reads
@pytest.mark.parametrize("change,why", [
    ({"rc": 1, "stderr": "permission denied"}, "rc 1: permission denied"),
    ({"stdout": "{not json"}, "Expecting property name"),
    ({"raise": subprocess.TimeoutExpired("argocd", 90)}, "timed out"),
    ({"raise": FileNotFoundError("argocd")}, "argocd"),
])
def test_a_failed_list_is_none_and_is_not_cached(argocd, warnings, change, why):
    calls, out = argocd
    out.update(change)
    assert m._appset_file_globs(REPO) is None
    assert m._appset_file_globs(REPO) is None and len(calls) == 2
    assert why in warnings[0] and warnings[0].startswith(NONE)


@pytest.mark.appset_reads
@pytest.mark.parametrize("stdout", ["[]", "null", "5", '{"items": 3}', json.dumps(APPSETS[-2:])])
def test_no_glob_for_this_repo_is_none(argocd, warnings, stdout):
    """An RBAC filter lists nothing and exits 0."""
    argocd[1]["stdout"] = stdout
    assert m._appset_file_globs(REPO) is None
    assert warnings == [f"{NONE}: no git files glob reads {REPO}"]


@pytest.mark.appset_reads
def test_a_live_file_that_matches_no_glob_is_none(argocd, warnings, monkeypatch):
    """The matcher does not agree with ArgoCD, so no miss can be trusted."""
    live = f"{PV}/ap7/pv-z/customer.yaml"
    m._app_value_files_map["pv-z-ss"] = [f"$config/{live}"]
    assert m._appset_file_globs(REPO) is None
    assert warnings == [f"{NONE}: live {live} matches no glob"]
    assert REPO not in m._appset_globs_cache


@pytest.mark.appset_reads
def test_the_live_files_of_another_repo_are_not_checked(argocd):
    m._app_value_files_map["pv-z-ss"] = [f"$config/{PV}/ap7/pv-z/customer.yaml"]
    m._app_repo_map["pv-z-ss"] = "acme-config-stage"
    assert m._appset_file_globs(REPO) == GLOBS


# ── the misses ───────────────────────────────────────────────────────────

def _globs(monkeypatch, *answers):
    """_appset_file_globs gives `answers` in turn, the last one again after that."""
    calls = []

    def fake(repo, fresh=False):
        calls.append((repo, fresh))
        return answers[min(len(calls), len(answers)) - 1]
    monkeypatch.setattr(m, "_appset_file_globs", fake)
    return calls


def _cand(*paths, name=None):
    first = paths[0]
    return {"name": name or first.split("/")[-2], "config_file": first,
            "env_dir": posixpath.dirname(first), "all_yaml_files": list(paths)}


def _gate(env, *paths):
    return {"kind": "appset_miss", "env": env, "paths": list(paths)}


def test_a_new_env_in_a_spoke_an_appset_reads_is_fine(monkeypatch):
    calls = _globs(monkeypatch, GLOBS)
    assert m._appset_misses([_cand(NEW)], {}, REPO) == ([], {}, [])
    assert calls == [(REPO, False)]


def test_a_new_env_in_a_fake_spoke_is_a_gate_after_a_fresh_list(monkeypatch):
    calls = _globs(monkeypatch, GLOBS)
    assert m._appset_misses([_cand(FAKE)], {}, REPO) == ([_gate("pv-dev-b3glob-a", FAKE)], {}, [])
    assert calls == [(REPO, False), (REPO, True)]


def test_an_appset_applied_just_now_counts(monkeypatch):
    calls = _globs(monkeypatch, GLOBS, GLOBS + [f"{PV}/ap9/**/customer.yaml"])
    assert m._appset_misses([_cand(FAKE)], {}, REPO) == ([], {}, [])
    assert calls == [(REPO, False), (REPO, True)]


@pytest.mark.parametrize("answers", [(None,), (GLOBS, None)])
def test_globs_that_are_not_proven_give_a_check_and_no_gate(monkeypatch, answers):
    _globs(monkeypatch, *answers)
    line = (f"{CHECK}`pv-dev-b3glob-a`: {NONE}, so it is not proven that ArgoCD makes "
            "apps for it.")
    extra = f"{posixpath.dirname(FAKE)}/cicd-versions.yaml"
    assert m._appset_misses([_cand(FAKE, extra)], {}, REPO) == ([], {FAKE: [line]}, [])


def test_a_cl_env_checks_its_config_and_its_numbered_apps(monkeypatch):
    """api and the other sub-app folders are rendered by the cl-*/config.yaml sets."""
    d = f"{CL}/cl-dev99-a"
    files = [f"{d}/config.yaml", f"{d}/app1/customer.yaml", f"{d}/api/customer.yaml",
             f"{d}/app41/customer.yaml", f"{d}/app42/customer.yaml"]
    _globs(monkeypatch, GLOBS)
    assert m._appset_misses([_cand(*files, name="cl-dev99-a")], {}, REPO) == (
        [_gate("cl-dev99-a", f"{d}/app41/customer.yaml", f"{d}/app42/customer.yaml")], {}, [])


def test_an_aws_env_is_a_check_and_lists_nothing(monkeypatch):
    aws = "aws/prod/private-cloud/na1-a/pv-amzn-b/customer.yaml"
    monkeypatch.setattr(m, "_appset_file_globs", lambda *a, **k: pytest.fail("listed"))
    assert m._appset_misses([_cand(aws)], {}, REPO) == ([], {aws: [
        f"{CHECK}`pv-amzn-b`: `aws/` is deployed by the legacy pipeline, not by ArgoCD, so "
        "no ApplicationSet check ran."]}, [])


def test_a_move_out_of_every_appset_is_a_gate(monkeypatch):
    old = f"{PV}/ap1/custom/pv-dev-a/customer.yaml"
    new = f"{PV}/ap9/custom/pv-dev-a/customer.yaml"
    cl_old, cl_new = f"{CL}/cl-dev11-a/config.yaml", "gcp/dev/public-cloud/ap9/cl-dev11-a/config.yaml"
    _globs(monkeypatch, GLOBS)
    assert m._appset_misses([], {old: new, cl_old: cl_new}, REPO) == (
        [_gate("cl-dev11-a", cl_new), _gate("pv-dev-a", new)], {}, [])


@pytest.mark.parametrize("old,new", [
    (f"{PV}/ap1/custom/pv-dev-a/customer.yaml", f"{PV}/ap1-b/custom/pv-dev-a/customer.yaml"),
    (f"{PV}/ap7/custom/pv-dev-a/customer.yaml", f"{PV}/ap9/custom/pv-dev-a/customer.yaml"),
])
def test_a_move_that_an_appset_still_reads_or_never_read_is_fine(monkeypatch, old, new):
    """The second one deployed nothing on main either: not a new error."""
    _globs(monkeypatch, GLOBS)
    assert m._appset_misses([], {old: new}, REPO) == ([], {}, [])


def test_a_move_with_globs_not_proven_gives_a_note(monkeypatch):
    new = f"{PV}/ap9/custom/pv-dev-a/customer.yaml"
    _globs(monkeypatch, None)
    assert m._appset_misses([], {f"{PV}/ap1/custom/pv-dev-a/customer.yaml": new}, REPO) == (
        [], {}, [f"⚠️ {NONE}, so it is not proven that ArgoCD makes apps for the "
                 f"moved `{new}`."])


@pytest.mark.parametrize("cands,renames", [
    ([], {}),
    ([{"name": "pv-x"}], {}),                            # an old fake candidate
    ([_cand(f"{PV}/ap9/custom/config.yaml")], {}),       # a cohort
    ([], {f"{PV}/ap1/a/pv-x/cicd-versions.yaml": f"{PV}/ap9/a/pv-x/cicd-versions.yaml"}),
])
def test_no_identity_file_lists_nothing(monkeypatch, cands, renames):
    monkeypatch.setattr(m, "_appset_file_globs", lambda *a, **k: pytest.fail("listed"))
    assert m._appset_misses(cands, renames, REPO) == ([], {}, [])


def test_no_repo_is_the_default_repo(monkeypatch):
    calls = _globs(monkeypatch, [f"{PV}/ap9/**/customer.yaml"])
    assert m._appset_misses([_cand(FAKE)], None) == ([], {}, [])
    assert calls == [(m.BB_REPO, False)]


# ── the gate and the panel ───────────────────────────────────────────────

def test_nothing_lifts_the_gate(monkeypatch):
    monkeypatch.setattr(m, "_pr_commit_messages", lambda *a: pytest.fail("no trailer to read"))
    gates = m._lift_gates(m._merge_gates((), extra=[_gate("pv-dev-b3glob-a", FAKE)]),
                          "r", 7, "b", "h")
    assert cr.GATES["appset_miss"] == ("No ApplicationSet reads this folder, nothing deploys",
                                       None, "blocked",
                                       "Check the cloud, tier and spoke folders")
    assert cr.gate_token(gates) == "blocked" and not gates[0]["lifted"]
    assert cr.gate_status_description(gates) == (
        f"Blocked - {TEXT} in pv-dev-b3glob-a{GATE_FIX} (see PR comment)")


def test_the_panel_names_each_path_and_the_way_out():
    gates = [_gate("cl-dev99-a", "a/app41/customer.yaml", "a/app42/customer.yaml"),
             _gate("pv-x", FAKE), {"kind": "shrink", "env": ""}]
    assert m._appset_miss_lines(gates, ["note"]) == [
        "## ⛔ NO APPLICATIONSET READS THIS FOLDER", "",
        "- `a/app41/customer.yaml` matches no ApplicationSet, so ArgoCD makes no apps for it "
        "and nothing deploys on merge.",
        "- `a/app42/customer.yaml` matches no ApplicationSet, so ArgoCD makes no apps for it "
        "and nothing deploys on merge.",
        f"- `{FAKE}` matches no ApplicationSet, so ArgoCD makes no apps for it and nothing "
        "deploys on merge.", "",
        "Check the cloud, tier and spoke folders. If the ApplicationSet is being added in "
        "acme-infrastructure, apply it first, then push again here (an empty commit is "
        "enough).", "",
        "note", ""]
    assert m._appset_miss_lines([{"kind": "shrink", "env": ""}]) == []
    assert m._appset_miss_lines(None, ["note"]) == ["note", ""]


# ── process_pr ───────────────────────────────────────────────────────────

CUSTOM = posixpath.dirname(posixpath.dirname(IDENTITY))       # gcp/dev/private-cloud/ap1/custom
FAKE_CUSTOM = f"{PV}/ap9/custom"
MOVED = f"{FAKE_CUSTOM}/pv-orch-a/customer.yaml"
NEW_DOC = "appspace:\n  customerName: b3glob\n  version: 2603.0.1-dev\n"
COHORT_DOC = "appspace:\n  version: 2603.0.1-dev\n"
RENDERED = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: platform-config\n"
SK = (m.BB_REPO, 991)


@pytest.fixture()
def pr(world, monkeypatch):
    """pv-orch-a is live in ap1. `run` serves `head` at the PR and `base` at main."""
    sinks, _plan = world
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)
    monkeypatch.setattr(m, "_render_new_env_diff",
                        lambda env_info, pr_sha: (RENDERED, None, 1, "2603.0.1-dev"))
    monkeypatch.setattr(m, "_pr_commit_messages",
                        lambda *a: pytest.fail("commits read with no trailer to find"))
    cohorts = {f"{CUSTOM}/config.yaml": COHORT_DOC, f"{FAKE_CUSTOM}/config.yaml": COHORT_DOC}

    def run(changed, head, renames=None, globs=GLOBS, base=None, messages=None):
        _globs(monkeypatch, globs)
        if messages is not None:
            monkeypatch.setattr(m, "_pr_commit_messages", lambda *a: list(messages))
        sides = {PR_SHA: {**cohorts, **head}, BASE_SHA: {**cohorts, IDENTITY: IDENTITY_YAML,
                                                         **(base or {})}}
        monkeypatch.setattr(m, "_bb_fetch_status", lambda path, sha, repo=None:
                            (sides[sha][path], m.BB_OK) if path in sides.get(sha, {})
                            else (None, m.BB_NOT_FOUND))
        monkeypatch.setattr(m, "get_pr_changed_files",
                            lambda pr_id, repo=None: (changed, renames or {}))
        m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
        return sinks.upserts[-1], sinks.statuses[-1]
    return run


def test_a_new_env_in_a_fake_spoke_blocks_the_new_env_path(pr):
    body, (state, desc) = pr([FAKE], {FAKE: NEW_DOC})
    assert m._extract_status_token(body) == "blocked"
    assert state == "FAILED"
    assert desc == f"Blocked - {TEXT} in pv-dev-b3glob-a{GATE_FIX} (see PR comment)"
    assert f"- ⛔ **{TEXT}** in `pv-dev-b3glob-a`" in body and "to merge anyway" not in body
    assert body.index(cr.MERGE_SUMMARY_HDR) < body.index("NO APPLICATIONSET READS") \
        < body.index("New Environment(s) Detected")
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA)


def test_a_new_env_in_a_real_spoke_is_clean(pr):
    body, (state, _desc) = pr([NEW], {NEW: NEW_DOC})
    assert m._extract_status_token(body) == "clean" and state == "SUCCESSFUL"
    assert "APPLICATIONSET" not in body and NONE not in body


def test_a_list_that_fails_is_a_check_and_stays_green(pr):
    body, (state, desc) = pr([FAKE], {FAKE: NEW_DOC}, globs=None)
    assert m._extract_status_token(body) == "clean" and state == "SUCCESSFUL"
    assert f"{CHECK}`pv-dev-b3glob-a`: {NONE}" in body and "NO APPLICATIONSET" not in body
    assert "New environment: 1 check(s) to review" in body
    assert NONE in desc


def _confirmed_move(pr, **kw):
    """A move to another spoke is a live identity change: it passes that guard
    only when a commit confirms it and autosync stays off on both sides."""
    paused = IDENTITY_YAML + "  autosync: false\n"
    return pr([IDENTITY, MOVED], {MOVED: paused}, {IDENTITY: MOVED}, base={IDENTITY: paused},
              messages=["Confirm-Rename: ap1/pv-orch-a -> ap9/pv-orch-a"], **kw)


def test_the_rename_guard_still_speaks_first_on_a_move_to_another_spoke(pr):
    body, (state, _desc) = pr([IDENTITY, MOVED], {MOVED: IDENTITY_YAML}, {IDENTITY: MOVED},
                              messages=[])
    assert state == "FAILED" and "APPLICATIONSET" not in body


def test_a_confirmed_move_to_a_fake_spoke_blocks_the_diff_path(pr):
    body, (state, desc) = _confirmed_move(pr)
    assert m._extract_status_token(body) == "blocked"
    assert state == "FAILED" and desc == f"Blocked - {TEXT} in pv-orch-a{GATE_FIX} (see PR comment)"
    assert body.index(cr.MERGE_SUMMARY_HDR) < body.index("NO APPLICATIONSET READS")
    assert f"- `{MOVED}` matches no ApplicationSet" in body


def test_a_move_inside_the_spoke_is_clean(pr):
    moved = f"{CUSTOM}/other/pv-orch-a/customer.yaml"
    body, (state, _desc) = pr([IDENTITY, moved], {moved: IDENTITY_YAML,
                                                  f"{CUSTOM}/other/config.yaml": COHORT_DOC},
                              {IDENTITY: moved})
    assert m._extract_status_token(body) == "clean" and state == "SUCCESSFUL"
    assert "APPLICATIONSET" not in body


def test_a_move_with_a_failed_list_shows_the_note_and_stays_green(pr):
    body, (state, _desc) = _confirmed_move(pr, globs=None)
    assert m._extract_status_token(body) == "clean" and state == "SUCCESSFUL"
    assert f"⚠️ {NONE}, so it is not proven that ArgoCD makes apps for the moved " \
           f"`{MOVED}`." in body
