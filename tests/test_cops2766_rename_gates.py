"""COPS-2766 block 2, item 8: a rename that is not a git mv, and a cl-* move.

A live env removed and another one added in the same PR, with no git mv, is
the orphan teardown of item 2, so the build is already [blocked]. The panel
now says what it looks like, so the author sees the two ways out.

A live cl-*/config.yaml renamed or moved renames every Application of the
constellation. It is not a pv identity hit, so the comment still shows the
diff, but the build is [blocked] until a commit message has
`Confirm-Rename: <old dir> -> <new dir>`.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import comment_render as cr  # noqa: E402
import diff_preview as m  # noqa: E402
import identity  # noqa: E402

from test_coverage_orchestration import (  # noqa: E402,F401
    world, _mk_pr, PATH_MAP, BASE_SHA, PR_SHA, IDENTITY, IDENTITY_YAML)

NEW_IDENT = "gcp/dev/private-cloud/ap1/custom/pv-new-a/customer.yaml"
NEW_YAML = "appspace:\n  customerName: new\n  version: 2603.0.1-dev\n"
HINT = ("\U0001f4a1 Looks like a rebuild or rename of `pv-orch-a`: arm decommission "
        "on the old env, or use `git mv` with `Confirm-Rename`.")

CL_DIR = "gcp/prod/public-cloud/na1-a/cl-prod-b"
CL_OLD = f"{CL_DIR}/config.yaml"
CL_NEW_DIR = "gcp/prod/public-cloud/na2-a/cl-prod-b"
CL_NEW = f"{CL_NEW_DIR}/config.yaml"
CL_YAML = "appspace:\n  customerName: prodb\n  version: 2603.0.1\n"
CL_MAP = {CL_OLD: ["cl-prod-b-ms"]}
CL_TRAILER = f"Confirm-Rename: {CL_DIR} -> {CL_NEW_DIR}"


@pytest.fixture(autouse=True)
def no_vertex(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)


# ── (a), (b) the delete + add rename ─────────────────────────────────────

@pytest.fixture()
def rebuild(world, monkeypatch):
    """pv-orch-a/customer.yaml is removed; the PR may add pv-new-a."""
    sinks, _plan = world
    files = {(IDENTITY, BASE_SHA): IDENTITY_YAML, (NEW_IDENT, PR_SHA): NEW_YAML}
    monkeypatch.setattr(m, "_bb_fetch_status", lambda path, sha, repo=None:
                        (files[(path, sha)], m.BB_OK) if (path, sha) in files
                        else (None, m.BB_NOT_FOUND))
    monkeypatch.setattr(m, "_render_main_side_resources", lambda app, sha: {})
    monkeypatch.setattr(m, "_shared_user_content_lines", lambda *a, **k: [])
    monkeypatch.setattr(m, "_cascade_finalizer_live", lambda apps: True)
    monkeypatch.setattr(m, "_teardown_hold_met", lambda *a, **k: True)
    monkeypatch.setattr(m, "_evaluate_new_envs", lambda *a, **k: (
        ["### \U0001f195 New Environment(s) Detected", ""], [], 5, []))

    def run(changed, messages=(), base=IDENTITY_YAML):
        files[(IDENTITY, BASE_SHA)] = base
        monkeypatch.setattr(m, "get_pr_changed_files", lambda pr_id, repo=None: (changed, {}))
        monkeypatch.setattr(m, "_pr_commit_messages", lambda *a: list(messages))
        m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
        return sinks.upserts[-1], sinks.statuses[-1][0]
    return run


def test_delete_plus_add_is_blocked_with_the_hint(rebuild):
    body, state = rebuild([IDENTITY, NEW_IDENT])
    assert "New Environment(s) Detected" in body, "pv-new-a is a new env"
    assert HINT in body
    assert body.count("Looks like a rebuild") == 1
    assert m._extract_status_token(body) == "blocked" and state == "FAILED"


def test_confirm_teardown_lifts_it(rebuild):
    body, state = rebuild([IDENTITY, NEW_IDENT], ["Confirm-Teardown: pv-orch-a"])
    assert m._extract_status_token(body) == "clean" and state == "SUCCESSFUL"
    assert HINT in body


def test_no_hint_without_a_new_env(rebuild):
    body, state = rebuild([IDENTITY])
    assert "Looks like a rebuild" not in body
    assert m._extract_status_token(body) == "blocked" and state == "FAILED"


def test_no_hint_when_the_old_env_has_its_cascade_armed(rebuild):
    body, state = rebuild([IDENTITY, NEW_IDENT],
                          base=IDENTITY_YAML + "  decommission: true\n")
    assert "Looks like a rebuild" not in body
    assert m._extract_status_token(body) == "clean" and state == "SUCCESSFUL"


def test_rebuild_hint_is_one_line_per_env():
    cands = [{"env_name": "cl-prod-b", "gates": [{"kind": "public", "env": "cl-prod-b"}]},
             {"env_name": "cl-prod-b", "gates": [{"kind": "public", "env": "cl-prod-b"}]},
             {"env_name": "pv-x-a", "gates": [{"kind": "orphan", "env": "pv-x-a"}]},
             {"env_name": "pv-y-a", "gates": [{"kind": "shared_uc", "env": "pv-y-a"}]},
             {"env_name": "pv-z-a", "gates": []},
             {"env_name": "not-confirmed"}]
    out = m._rebuild_hint_lines(cands)
    # The flag arms nothing on cl-*, so there the hint only names git mv.
    assert out == ["\U0001f4a1 Looks like a rebuild or rename of `cl-prod-b`: use "
                   "`git mv` with `Confirm-Rename`.", "",
                   HINT.replace("pv-orch-a", "pv-x-a"), ""]
    assert m._rebuild_hint_lines([]) == []


# ── (c) a live cl-*/config.yaml renamed or moved ─────────────────────────

def test_merge_gates_names_a_moved_live_constellation():
    assert m._merge_gates([], {CL_OLD: CL_NEW}, CL_MAP) == [
        {"kind": "cl_rename", "env": "cl-prod-b", "arg": f"{CL_DIR} -> {CL_NEW_DIR}",
         "lifted": False}]
    renamed = "gcp/prod/public-cloud/na1-a/cl-prod-c/config.yaml"
    assert m._merge_gates([], {CL_OLD: renamed}, CL_MAP)[0]["arg"] == \
        f"{CL_DIR} -> gcp/prod/public-cloud/na1-a/cl-prod-c"


def test_merge_gates_skips_a_cl_config_that_is_not_live():
    assert m._merge_gates([], {CL_OLD: CL_NEW}, {}) == []
    assert m._merge_gates([], {CL_OLD: CL_NEW}, None) == []


def test_merge_gates_skips_other_renames():
    block = f"{CL_DIR}/api/customer.yaml"
    region = "gcp/prod/public-cloud/na1-a/config.yaml"
    renames = {block: f"{CL_NEW_DIR}/api/customer.yaml", IDENTITY: NEW_IDENT,
               region: "gcp/prod/public-cloud/na2-a/config.yaml"}
    live = {block: ["cl-prod-b-api-glb"], IDENTITY: ["pv-orch-a-ms"],
            region: ["cl-prod-b-ms"]}
    assert m._merge_gates([], renames, live) == []


def test_the_cl_trailer_fits_the_status_and_reads_with_an_arrow():
    gates = m._merge_gates([], {CL_OLD: CL_NEW}, CL_MAP)
    desc = cr.gate_status_description(gates)
    assert f"add '{CL_TRAILER}' to a commit message" in desc and len(desc) <= 255
    arrow = f"Confirm-Rename: `{CL_DIR}` → `{CL_NEW_DIR}`."
    assert cr.gate_trailer(gates[0]).lower() in identity._confirmations([arrow])


@pytest.fixture()
def cl_move(world, monkeypatch):
    """cl-prod-b moves from na1-a to na2-a; the region has its config.yaml."""
    sinks, plan = world
    files = {(CL_OLD, BASE_SHA): CL_YAML, (CL_NEW, PR_SHA): CL_YAML,
             ("gcp/prod/public-cloud/na2-a/config.yaml", PR_SHA): "appspace: {}\n"}
    monkeypatch.setattr(m, "_bb_fetch_status", lambda path, sha, repo=None:
                        (files[(path, sha)], m.BB_OK) if (path, sha) in files
                        else (None, m.BB_NOT_FOUND))
    monkeypatch.setattr(m, "get_pr_changed_files",
                        lambda pr_id, repo=None: ([CL_OLD, CL_NEW], {CL_OLD: CL_NEW}))
    plan["cl-prod-b-ms"] = m.DiffResult(
        "--- main\n+++ pr", [("Deployment/webx", "-replicas: 2\n+replicas: 3")],
        1, True, "", m.OUT_DIFF, "")

    def run(messages):
        monkeypatch.setattr(m, "_pr_commit_messages", lambda *a: list(messages))
        m.process_pr(_mk_pr(), CL_MAP, base_sha=BASE_SHA)
        return sinks.upserts[-1], sinks.statuses[-1]
    return run


def test_a_cl_move_is_blocked_and_still_shows_the_diff(cl_move):
    body, (state, desc) = cl_move(["Move cl-prod-b to na2-a"])
    assert m._extract_status_token(body) == "blocked" and state == "FAILED"
    assert f"`{CL_TRAILER}` to a commit message" in body
    assert f"add '{CL_TRAILER}'" in desc
    assert "Deployment/webx" in body, "the diff is still there"


def test_confirm_rename_lifts_a_cl_move(cl_move):
    body, (state, desc) = cl_move([f"Move cl-prod-b\n\n{CL_TRAILER}\n"])
    assert m._extract_status_token(body) == "clean"
    assert state == "SUCCESSFUL", desc
    assert f"☑️ **Confirmed in a commit:** `{CL_TRAILER}`" in body
