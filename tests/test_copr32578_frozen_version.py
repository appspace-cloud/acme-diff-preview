"""COPR-32578: block a PR that leaves a live env with no chart version.

The ApplicationSets set targetRevision to appspace.version from the generator
file (customer.yaml over the config.yaml one folder up; for a public-cloud
constellation, cl-*/config.yaml alone), else to `watch-only`. The apps then go
Sync Unknown and stop: no sync, no self-heal (acme-config-prod #4042, 15 apps).
"""
import os
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
    repo, rename_pr, _pr, ERR, AEC, COHORT, COHORT_BODY, NBC, ENV, ENV_COHORT, HEAD)

OWN = "appspace:\n  customerName: nbc--aec1\n  version: 2603.1.38\n"
BARE = "appspace:\n  customerName: nbc--aec1\n"
CL = "gcp/prod/public-cloud/na1-a/cl-prod-b"
CL_ENV = f"{CL}/config.yaml"
CL_APP = f"{CL}/app1/customer.yaml"
CL_CONST = f"{CL}/constellation/customer.yaml"
CL_MAP = {CL_ENV: ["cl-prod-b-ms", "cl-prod-b-ss", "cl-prod-b-app1-glb"],
          CL_APP: ["cl-prod-b-app1-glb"], CL_CONST: ["cl-prod-b-ms", "cl-prod-b-ss"]}


def _frozen(changed, renames=None, path_map=None):
    return [(h["path"], h["why"]) for h in
            dp._detect_frozen_versions(changed, renames or {}, path_map or {}, HEAD)]


# --- private cloud --------------------------------------------------------------------

def test_a_cohort_that_loses_its_version_freezes_the_envs_without_their_own(repo):
    base, head, _ = repo
    head[COHORT] = "appspace:\n  maintenanceWindow: sun\n"
    pinned = f"{AEC}/pv-pin-a/customer.yaml"
    head[NBC], head[pinned] = BARE, OWN
    assert _frozen([COHORT], path_map={NBC: [], pinned: []}) == [(NBC, "missing")]


def test_a_cohort_that_keeps_a_version_reads_no_env(repo):
    _, head, calls = repo
    head[COHORT] = COHORT_BODY.replace("2603.2.19", "2603.3.1")
    assert _frozen([COHORT], path_map={NBC: []}) == [] and calls == [COHORT]


@pytest.mark.parametrize("line,why", [
    ('  version: ""\n', "empty"), ("  version:\n", "empty"), ("  version: ~\n", "empty"),
    ("  version: false\n", "empty"), ("  version: 0\n", "empty"),
    ("  version: watch-only\n", "watch-only"),
])
def test_an_empty_version_hides_the_cohort_value(repo, line, why):
    # mergo WithOverride: a key present in customer.yaml wins even when Go-falsy.
    _, head, _ = repo
    head[NBC] = BARE + line
    assert _frozen([NBC]) == [(NBC, why)]


def test_dropping_the_own_version_keeps_the_cohort_one(repo):
    _, head, _ = repo
    head[NBC] = BARE
    assert _frozen([NBC]) == []


@pytest.mark.parametrize("body", ["", "appspace:\n", "appspace: [a]\n", "appspace: x\n", "- a\n"])
def test_a_file_the_generator_cannot_use_stops_the_whole_set(repo, body):
    _, head, _ = repo
    head[NBC] = body
    assert _frozen([NBC]) == [(NBC, "shape")]


def test_a_new_env_uses_only_the_generator_files(repo):
    # #4042 shape: the version sat in a file the ApplicationSet does not read.
    _, head, _ = repo
    head[COHORT] = "appspace: {}\n"
    head[NBC] = BARE
    head[f"{AEC}/pv-nbc--aec1-a/config.yaml"] = "appspace:\n  version: 2603.1.38\n"
    assert _frozen([NBC, f"{AEC}/pv-nbc--aec1-a/config.yaml"]) == [(NBC, "missing")]


def test_a_move_into_a_cohort_without_version(repo):
    _, head, _ = repo
    new = "gcp/aec/private-cloud/na2-a/hardcoded/pv-nbc--aec1-a/customer.yaml"
    head["gcp/aec/private-cloud/na2-a/hardcoded/config.yaml"] = "# no values\n"
    head[new] = BARE
    assert _frozen([NBC, new], {NBC: new}, {NBC: []}) == [(new, "missing")]


def test_a_removed_env_is_a_decommission_not_a_freeze(repo):
    _, head, _ = repo
    head.pop(NBC, None)
    assert _frozen([NBC], path_map={NBC: []}) == []


def test_a_bitbucket_error_is_retried_never_passed(repo):
    _, head, _ = repo
    head[NBC] = ERR
    with pytest.raises(dp.ValueFileUnreadable):
        _frozen([NBC])


# --- public cloud ---------------------------------------------------------------------

def test_a_cl_config_without_version_freezes_the_constellation_and_its_apps(repo):
    _, head, _ = repo
    head[CL_ENV] = "appspace:\n  customerName: prod-b\n"
    head[CL_APP] = "appspace:\n  replicas: 2\n"
    assert sorted(_frozen([CL_ENV], path_map=CL_MAP)) == [(CL_APP, "missing"), (CL_ENV, "missing")]


def test_a_pinned_cl_app_does_not_save_the_constellation(repo):
    # The constellation set reads only cl-*/config.yaml.
    _, head, _ = repo
    head[CL_ENV] = "appspace:\n  customerName: prod-b\n"
    head[CL_APP] = "appspace:\n  version: 2603.1.38\n"
    assert _frozen([CL_ENV], path_map=CL_MAP) == [(CL_ENV, "missing")]


def test_only_real_generator_files_count_in_public_cloud(repo):
    _, head, calls = repo
    head[CL_CONST] = 'appspace:\n  version: ""\n'          # Helm values only
    head[f"{CL}/app9/customer.yaml"] = 'appspace:\n  version: ""\n'  # no live app9 glb
    assert _frozen([CL_CONST, f"{CL}/app9/customer.yaml"], path_map=CL_MAP) == [] and calls == []


def test_an_empty_version_in_a_live_cl_app(repo):
    _, head, _ = repo
    head[CL_ENV] = "appspace:\n  version: 2603.2.19\n"
    head[CL_APP] = 'appspace:\n  version: ""\n'
    assert _frozen([CL_APP], path_map=CL_MAP) == [(CL_APP, "empty")]


# --- the message and process_pr -------------------------------------------------------

def test_the_blocked_comment_says_why_and_how_to_fix():
    hits = [{"path": NBC, "cohort": COHORT, "why": "missing"}, {"path": CL_APP, "cohort": CL_ENV, "why": "empty"}]
    desc, body = dp._frozen_version_block(hits, PR_SHA, BASE_SHA)
    assert len(desc) <= 255 and desc.startswith("BLOCKED: 2 env files would get no appspace.version")
    assert f"no `version` here or in `{COHORT}`" in body and "hides the cohort value" in body
    assert "`targetRevision: watch-only`" in body and "delete the line" in body
    assert _extract_status_token(body) == "blocked"
    one, _ = dp._frozen_version_block(hits[:1], PR_SHA, BASE_SHA)
    assert one.startswith("BLOCKED: pv-nbc--aec1-a would get no appspace.version")


def test_process_pr_blocks_before_the_render_that_would_look_normal(rename_pr, monkeypatch):
    sinks, store, mains, files, path_map = rename_pr
    files[ENV_COHORT] = "appspace:\n  maintenanceWindow: sun\n"
    files[ENV] = "appspace:\n  customerName: orch\n"
    mains[BASE_SHA][ENV] = "appspace:\n  customerName: orch\n"
    monkeypatch.setattr(dp, "get_pr_changed_files", lambda pr_id, repo=None: ([ENV_COHORT], {}))
    dp.process_pr(_pr(), path_map, base_sha=BASE_SHA)
    state, desc = sinks.statuses[-1]
    assert state == "FAILED" and desc.startswith("BLOCKED: pv-orch-a would get no appspace.version")
    assert sinks.diff_calls == []
    # The fix push brings the version back: checked again, and it passes.
    files[ENV_COHORT] = COHORT_BODY
    dp.process_pr(_pr("0011223344ff"), path_map, base_sha=BASE_SHA)
    assert sinks.statuses[-1][0] == "SUCCESSFUL"
