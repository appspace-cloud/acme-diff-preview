"""COPR-32578: block a PR that removes a cohort config.yaml that live envs use.

The private-cloud ApplicationSets make an env's apps from its customer.yaml and
the config.yaml one folder up. Without that file they make no apps, so ArgoCD
deletes their apps without pruning and the namespaces keep running (COPR-32565).
acme-config-prod #768 did it to 11 envs; every case in history was a mistake.
"""
import os
import subprocess
import sys

import pytest

os.environ.setdefault("BB_USER", "test-user")
os.environ.setdefault("BB_TOKEN", "test-token")
os.environ.setdefault("ARGOCD_PASS", "test-pass")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as dp  # noqa: E402
from app_meta import _extract_status_token  # noqa: E402
from test_coverage_orchestration import world, BASE_SHA, PR_SHA  # noqa: E402,F401
from test_copr32578_live_identity_guard import (  # noqa: E402,F401
    repo, rename_pr, _pr, ERR, AEC, COHORT, COHORT_BODY, NBC, NBC_MAIN,
    ENV, ENV_COHORT, ORCH, HEAD)

KID = f"{AEC}/pv-kid-a/customer.yaml"


def _removals(changed, renames=None, path_map=None):
    return dp._detect_orphaning_cohort_removals(changed, renames or {}, path_map or {}, HEAD)


def _live(*paths):
    return {p: [] for p in paths}


# --- the detector -------------------------------------------------------------------

def test_a_removed_cohort_with_live_envs_is_a_hit(repo):
    base, head, _ = repo
    del head[COHORT]
    head[NBC] = head[KID] = NBC_MAIN
    assert _removals([COHORT], path_map=_live(NBC, KID)) == [
        {"cohort": COHORT, "envs": ["pv-kid-a", "pv-nbc--aec1-a"]}]


@pytest.mark.parametrize("body", ["", "# no values\n", "appspace: {}\n", "appspace: [\n"])
def test_a_cohort_that_is_still_there_is_not_this_rule(repo, body):
    # Empty still makes the apps; bad YAML stops the ApplicationSet and the render reports it.
    base, head, calls = repo
    head[COHORT], head[NBC] = body, NBC_MAIN
    assert _removals([COHORT], path_map=_live(NBC)) == [] and calls == [COHORT]


def test_envs_removed_or_moved_in_the_same_pr_are_fine(repo):
    # 46 cohorts in history went away together with their envs.
    base, head, _ = repo
    del head[COHORT]
    moved = f"{AEC}/pv-moved-a/customer.yaml"
    assert _removals([COHORT, NBC, moved], {moved: "gcp/aec/private-cloud/na2-a/w/pv-moved-a/customer.yaml"},
                     _live(NBC, moved)) == []


def test_an_app_left_over_from_a_gone_env_does_not_count(repo):
    # path_map can still list apps whose customer.yaml is already gone.
    base, head, _ = repo
    del head[COHORT]
    assert _removals([COHORT], path_map=_live(NBC)) == []


def test_a_moved_cohort_leaves_the_envs_that_stay(repo):
    base, head, _ = repo
    del head[COHORT]
    head["gcp/aec/private-cloud/na2-a/weekly/config.yaml"] = COHORT_BODY
    head[NBC] = NBC_MAIN
    assert [h["envs"] for h in _removals(
        [COHORT, "gcp/aec/private-cloud/na2-a/weekly/config.yaml"],
        {COHORT: "gcp/aec/private-cloud/na2-a/weekly/config.yaml"}, _live(NBC))] == [["pv-nbc--aec1-a"]]


def test_a_spoke_config_is_the_cohort_of_a_flat_env(repo):
    base, head, _ = repo
    spoke, env = "gcp/aec/private-cloud/gb1-b/config.yaml", "gcp/aec/private-cloud/gb1-b/pv-h--aec1-a/customer.yaml"
    head[env] = NBC_MAIN
    assert _removals([spoke], path_map=_live(env)) == [{"cohort": spoke, "envs": ["pv-h--aec1-a"]}]


@pytest.mark.parametrize("path", [
    "gcp/prod/public-cloud/na1-a/cl-prod-a/config.yaml",   # a cl env, not a cohort
    "aws/prod/private-cloud/na1-a/config.yaml",              # no ApplicationSet
    "gcp/prod/private-cloud/config.yaml",                    # the tier file: Helm only
    f"{AEC}/cicd-versions.yaml",
])
def test_only_private_cloud_cohorts_are_read(repo, path):
    _, _, calls = repo
    assert _removals([path], path_map=_live(NBC)) == [] and calls == []


def test_a_bitbucket_error_is_retried_never_passed(repo):
    base, head, _ = repo
    head[COHORT] = ERR
    with pytest.raises(dp.ValueFileUnreadable):
        _removals([COHORT], path_map=_live(NBC))
    del head[COHORT]
    head[NBC] = ERR
    with pytest.raises(dp.ValueFileUnreadable):
        _removals([COHORT], path_map=_live(NBC))


# --- the message ----------------------------------------------------------------------

def test_the_blocked_comment_says_why_and_how_to_fix():
    hits = [{"cohort": COHORT, "envs": [f"pv-e{i}-a" for i in range(12)]}]
    desc, body = dp._cohort_removal_block(hits, PR_SHA, BASE_SHA)
    assert desc == f"BLOCKED: removes cohort {COHORT}, 12 live envs still use it — keep the file"
    assert "`pv-e9-a` (+2 more)" in body and "12 live environments still use it: " in body
    assert "**Fix:** keep the file where it is." in body and "COPR-32565" in body
    assert "removes a cohort `config.yaml` that live environments still use." in body
    # An empty cohort would drop appspace.version for the envs without their own.
    assert "empty" not in body
    assert _extract_status_token(body) == "blocked" and f"[base:{BASE_SHA[:8]}]" in body


def test_many_cohorts_fit_the_status():
    hits = [{"cohort": "gcp/prod/private-cloud/na3-a/" + "x" * 200 + "/config.yaml", "envs": ["pv-a-a"]},
            {"cohort": COHORT, "envs": ["pv-b-a"]}]
    desc, body = dp._cohort_removal_block(hits, PR_SHA, BASE_SHA)
    assert len(desc) <= 255 and desc.endswith("keep the files")
    assert "1 live environment still uses it: `pv-a-a`" in body
    assert "removes 2 cohort `config.yaml` files" in body and "removes cohorts `" in body


def test_one_env_reads_as_singular():
    desc, body = dp._cohort_removal_block([{"cohort": COHORT, "envs": ["pv-a-a"]}], PR_SHA, BASE_SHA)
    assert desc.endswith("1 live env still uses it — keep the file")
    assert "removes a cohort `config.yaml` that a live environment still uses." in body


def test_two_cohorts_say_them_and_files():
    hits = [{"cohort": COHORT, "envs": ["pv-a-a"]}, {"cohort": ENV_COHORT, "envs": ["pv-b-a"]}]
    desc, _ = dp._cohort_removal_block(hits, PR_SHA, BASE_SHA)
    assert desc == f"BLOCKED: removes cohort {COHORT} (+1 more), 2 live envs still use them — keep the files"


# --- end to end through process_pr ---------------------------------------------------

def test_process_pr_blocks_a_removed_cohort_before_any_diff(rename_pr, monkeypatch):
    sinks, store, mains, files, path_map = rename_pr
    del files[ENV_COHORT]
    files[ENV] = ORCH
    monkeypatch.setattr(dp, "get_pr_changed_files", lambda pr_id, repo=None: ([ENV_COHORT], {}))
    dp.process_pr(_pr(), path_map, base_sha=BASE_SHA)
    assert sinks.statuses[-1] == ("FAILED", f"BLOCKED: removes cohort {ENV_COHORT}, 1 live env "
                                            f"still uses it — keep the file")
    assert sinks.diff_calls == [] and "read" not in store
    assert list(dp._seen.values()) == [(PR_SHA, BASE_SHA)]

    # The fix push puts the file back: a new sha, checked again, and it passes.
    files[ENV_COHORT] = COHORT_BODY
    dp.process_pr(_pr("0011223344ff"), path_map, base_sha=BASE_SHA)
    assert sinks.statuses[-1][0] == "SUCCESSFUL"
    assert store["ids"][1:] == [123], "the same comment is edited in place"


def test_moving_a_whole_cohort_folder_is_not_a_move_without_cohort(monkeypatch):
    # COPS-2552 checks moved envs. A moved cohort file under hardcoded/ has no
    # config.yaml above it, and that used to block the move the fix text asks for.
    old, new = "gcp/prod/private-cloud/na1-b/hardcoded/monthly", "gcp/prod/private-cloud/na1-b/hardcoded/monthly-b"
    monkeypatch.setattr(dp, "_bb_fetch_cached", lambda path, sha, repo=None: (
        ("x", dp.BB_OK) if path == f"{new}/config.yaml" else (None, dp.BB_NOT_FOUND)))
    renames = {f"{old}/config.yaml": f"{new}/config.yaml",
               f"{old}/pv-x-a/customer.yaml": f"{new}/pv-x-a/customer.yaml"}
    assert dp._moves_missing_cohort(renames, "sha1") == []
    # A moved env whose new cohort is missing is still blocked.
    assert [b["env"] for b in dp._moves_missing_cohort(
        {f"{old}/pv-x-a/customer.yaml": "gcp/prod/private-cloud/na1-b/none/pv-x-a/customer.yaml"},
        "sha1")] == ["pv-x-a"]


def test_process_pr_a_whole_cohort_decommission_is_not_blocked(rename_pr, monkeypatch):
    sinks, store, mains, files, path_map = rename_pr
    del files[ENV_COHORT]
    del files[ENV]
    monkeypatch.setattr(dp, "get_pr_changed_files", lambda pr_id, repo=None: ([ENV_COHORT, ENV], {}))
    dp.process_pr(_pr(), path_map, base_sha=BASE_SHA)
    assert not sinks.statuses[-1][1].startswith("BLOCKED: removes cohort")


# --- history, against the local config repos ------------------------------------------

@pytest.mark.local_components
@pytest.mark.parametrize("sha,expected", [
    ("21b78a5209", {"gcp/prod/private-cloud/eu1-b/weekly/config.yaml": 11}),   # #768
    ("f78ecdc035", {"gcp/prod/private-cloud/na1-b/matching-public-cloud/config.yaml": 1,
                    "gcp/prod/private-cloud/na2-a/matching-public-cloud/config.yaml": 1}),  # #1354
    ("f43bf0d9db", {}),   # 31 envs removed with their cohort
])
def test_past_prod_cohort_removals(monkeypatch, sha, expected):
    root = os.path.expanduser("~/gitprojects/acme-config-prod")
    if not os.path.isdir(os.path.join(root, ".git")):
        pytest.skip("acme-config-prod not checked out")

    def git(*args):
        return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True)

    def fetch(path, at, repo=None):
        r = git("show", f"{at}:{path}")
        return (r.stdout, dp.BB_OK) if r.returncode == 0 else (None, dp.BB_NOT_FOUND)
    monkeypatch.setattr(dp, "_bb_fetch_cached", fetch)
    changed, renames = [], {}
    for line in git("diff", "--name-status", "-M", f"{sha}^1", sha).stdout.splitlines():
        f = line.split("\t")
        if f[0].startswith("R"):
            renames[f[1]] = f[2]
        changed += f[1:]
    live = {p: [] for p in git("ls-tree", "-r", "--name-only", f"{sha}^1").stdout.splitlines()
            if dp._is_pv_env_file(p)}
    hits = dp._detect_orphaning_cohort_removals(changed, renames, live, sha)
    assert {h["cohort"]: len(h["envs"]) for h in hits} == expected
