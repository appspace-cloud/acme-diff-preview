"""COPS-2766 block 3, item 2 (row 7, C03): a new env with the name of another env.

The private-cloud ApplicationSet names the apps and the namespace of an env
`pv-<customerName>-<suffix>`. Application names are global on the hub, so a
new env that computes the name of a live env (a copy that keeps the original)
would take its apps and share its namespace. Two new envs in one PR with one
name collide the same way. This is the `dup_identity` gate, and nothing lifts
it: the fix is `git mv`, or another `customerName` or `suffix`. When the
ArgoCD app list is not loaded, a visible line says the check did not run.
"""
import os
import posixpath
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import comment_render as cr  # noqa: E402
import diff_preview as m  # noqa: E402

from test_coverage_orchestration import (  # noqa: E402,F401
    world, _mk_pr, PATH_MAP, ANCILLARY, IDENTITY, BASE_SHA, PR_SHA)

SPOKE = "gcp/qa/private-cloud/ap1"
COHORT = f"{SPOKE}/custom/config.yaml"
ORIG = f"{SPOKE}/custom/pv-qa88-a/customer.yaml"
COPY = f"{SPOKE}/custom/pv-qa88-copy/customer.yaml"
AZ = "azure/qa/private-cloud/na1-a/custom/pv-qa88-az/customer.yaml"
AZ_COHORT = "azure/qa/private-cloud/na1-a/custom/config.yaml"
NS = "pv-qa88-a"
APPS = ["pv-qa88-a-glb", "pv-qa88-a-ms", "pv-qa88-a-ss"]
DOC = "appspace:\n  customerName: qa88\n  version: 2604.0.1\n"
COHORT_DOC = "appspace:\n  version: 2604.0.1\n"
TEXT = cr.GATES["dup_identity"][0]
H, B = "head0001", "base0001"
NOTE = ("ℹ️ Name check not run: the ArgoCD app list is not loaded, so the "
        "new environment names are not compared with the live apps.")


@pytest.fixture(autouse=True)
def no_vertex(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)


@pytest.fixture()
def hub(monkeypatch):
    """pv-qa88-a is live. `other` shares its namespace with a name the
    ApplicationSet never makes, so it is not a name the new env takes."""
    ns = dict({a: NS for a in APPS}, other=NS, **{"pv-qa88-b-ms": "pv-qa88-b"})
    monkeypatch.setattr(m, "_app_namespace_map", ns)
    monkeypatch.setattr(m, "_app_value_files_map", {
        a: [f"$config/{SPOKE}/config.yaml", f"$config/{ORIG}"] for a in APPS})


def _serve(monkeypatch, head, base=None, error=()):
    base = base or {}

    def fetch(path, sha, repo=None):
        if path in error:
            return None, m.BB_ERROR
        side = head if sha == H else base if sha == B else {}
        return (side[path], m.BB_OK) if path in side else (None, m.BB_NOT_FOUND)
    monkeypatch.setattr(m, "_bb_fetch_status", fetch)


def _cands(paths):
    return [{"name": p.split("/")[-2], "config_file": p, "env_dir": posixpath.dirname(p),
             "all_yaml_files": [p]} for p in paths]


def _detect(new, changed=None, renames=None):
    return m._detect_duplicate_identities(
        list(new) if changed is None else changed, renames or {}, _cands(new), H)


def _gate(path=COPY, apps=APPS, files=(ORIG,), also=(), ns=NS):
    why = (f"live apps {', '.join(apps)}" + (f" from {', '.join(files)}" if files else "")
           if apps else f"same name as {', '.join(also)} in this PR")
    return {"kind": "dup_identity", "env": path.split("/")[-2], "why": why, "path": path,
            "ns": ns, "apps": list(apps), "files": list(files), "also": list(also)}


# ── the detector ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("changed", [[COPY], [COPY, ORIG]])
def test_a_copy_that_keeps_the_original_is_a_gate(monkeypatch, hub, changed):
    _serve(monkeypatch, {COPY: DOC, ORIG: DOC.replace("2604.0.1", "2604.0.2"),
                         COHORT: COHORT_DOC})
    assert _detect([COPY], changed) == ([_gate()], False)


def test_a_copy_plus_the_delete_of_the_original_is_a_move(monkeypatch, hub):
    """The augmenter pairs them by declared identity, so no new env is left."""
    _serve(monkeypatch, {COPY: DOC, COHORT: COHORT_DOC}, {ORIG: DOC, COHORT: COHORT_DOC})
    path_map = {ORIG: APPS}
    renames = m._augment_renames_with_identity_moves([ORIG, COPY], {}, path_map, B, H)
    assert renames == {ORIG: COPY}
    cands = m._detect_new_env_candidates([ORIG, COPY], path_map, renames, pr_sha=H)
    cands += _cands([COPY])        # even a stale candidate is a rename new side
    assert m._detect_duplicate_identities([ORIG, COPY], renames, cands, H) == ([], False)


def test_a_delete_the_augmenter_cannot_pair_is_a_rebuild(monkeypatch, hub):
    """A customerName from the cohort declares no identity, so the augmenter
    leaves the delete alone. The old apps go away on merge: no gate."""
    inherited = "appspace:\n  version: 2604.0.1\n"
    cohort = "appspace:\n  customerName: qa88\n"
    _serve(monkeypatch, {COPY: inherited, COHORT: cohort}, {ORIG: inherited, COHORT: cohort})
    assert m._augment_renames_with_identity_moves([ORIG, COPY], {}, {ORIG: APPS}, B, H) == {}
    assert _detect([COPY], [ORIG, COPY]) == ([], False)


def test_a_cohort_inherited_customer_name_is_a_gate(monkeypatch, hub):
    _serve(monkeypatch, {COPY: "appspace:\n  version: 2604.0.1\n",
                         COHORT: "appspace:\n  customerName: qa88\n"})
    assert _detect([COPY]) == ([_gate()], False)


@pytest.mark.parametrize("doc", [
    DOC + "  suffix: c\n",
    DOC.replace("qa88", "qa89"),
])
def test_another_suffix_or_name_is_no_gate(monkeypatch, hub, doc):
    _serve(monkeypatch, {COPY: doc, COHORT: COHORT_DOC})
    assert _detect([COPY]) == ([], False)


def test_two_new_files_with_one_identity_are_a_gate_each(monkeypatch, hub):
    """Another cloud too: Application names are global on the hub."""
    doc = DOC.replace("qa88", "qa89")
    _serve(monkeypatch, {COPY: doc, AZ: doc, COHORT: COHORT_DOC, AZ_COHORT: COHORT_DOC})
    assert _detect([COPY, AZ]) == ([
        _gate(AZ, (), (), ["pv-qa88-copy"], "pv-qa89-a"),
        _gate(COPY, (), (), ["pv-qa88-az"], "pv-qa89-a")], False)


def test_two_new_copies_of_a_live_env_name_the_live_apps(monkeypatch, hub):
    monkeypatch.setattr(m, "_app_value_files_map", {})     # no customer.yaml known
    _serve(monkeypatch, {COPY: DOC, AZ: DOC, COHORT: COHORT_DOC, AZ_COHORT: COHORT_DOC})
    assert _detect([COPY, AZ]) == ([_gate(AZ, files=(), also=["pv-qa88-copy"]),
                                    _gate(files=(), also=["pv-qa88-az"])], False)


def test_an_empty_app_list_gives_the_note_and_no_gate(monkeypatch):
    monkeypatch.setattr(m, "_app_namespace_map", {})
    _serve(monkeypatch, {COPY: DOC, AZ: DOC.replace("qa88", "qa89"), COHORT: COHORT_DOC,
                         AZ_COHORT: COHORT_DOC})
    assert _detect([COPY, AZ]) == ([], True)
    assert _detect([]) == ([], False)


@pytest.mark.parametrize("head,new", [
    ({COPY: "appspace: [\n", COHORT: COHORT_DOC}, [COPY]),          # cannot be parsed
    ({COPY: COHORT_DOC, COHORT: COHORT_DOC}, [COPY]),               # no customerName
    ({COPY: DOC}, [COPY]),                                          # no cohort: no apps
    ({}, ["gcp/qa/public-cloud/ap1/cl-qa88-a/api/customer.yaml"]),     # public cloud
])
def test_files_that_make_no_apps_are_skipped(monkeypatch, hub, head, new):
    _serve(monkeypatch, head)
    assert _detect(new) == ([], False)


def test_a_failed_read_retries(monkeypatch, hub):
    _serve(monkeypatch, {COPY: DOC}, error=(COHORT,))
    with pytest.raises(m.ValueFileUnreadable):
        _detect([COPY])


def test_the_live_identity_guard_uses_the_same_taken_rule(hub):
    assert m._live_apps_in_namespace(NS) == APPS
    assert m._live_apps_in_namespace(NS, ["pv-qa88-a-ms"]) == ["pv-qa88-a-glb", "pv-qa88-a-ss"]
    assert m._live_apps_in_namespace("pv-qa88") == []


# ── the gate and the panel ───────────────────────────────────────────────

def test_nothing_lifts_the_gate(monkeypatch):
    monkeypatch.setattr(m, "_pr_commit_messages", lambda *a: [
        "Confirm-Rename: pv-qa88-a -> pv-qa88-copy\nConfirm-Teardown: pv-qa88-copy",
        "Confirm-IP-Release: pv-qa88-copy"])
    gates = m._merge_gates((), extra=[_gate(), {"kind": "ip", "env": "pv-qa88-copy"}])
    assert cr.gate_trailer(gates[0]) == ""
    assert [g["lifted"] for g in m._lift_gates(gates, "r", 7, B, H)] == [False, True]
    assert cr.gate_token(gates) == "blocked"


def test_the_panel_names_the_apps_and_the_ways_out():
    gates = m._merge_gates((), extra=[
        _gate(), _gate(AZ, (), (), ["pv-qa88-b2"], "pv-qa89-a"),
        {"kind": "shrink", "env": ""}])
    assert m._dup_identity_lines(gates, True) == [
        "## ⛔ NEW ENVIRONMENT NAME ALREADY IN USE", "",
        f"- `{COPY}` names its apps `{NS}-*`, but live apps already use these names: "
        f"`pv-qa88-a-glb`, `pv-qa88-a-ms`, `pv-qa88-a-ss` (from `{ORIG}`).",
        f"- `{AZ}` names its apps `pv-qa89-a-*`, like `pv-qa88-b2` in this PR.", "",
        "Two environments cannot share one namespace. To move an environment, use "
        "`git mv`, so the old folder goes in the same PR. Otherwise choose another "
        "`customerName` or `suffix`.", "",
        "If the old environment is being removed, wait until its apps are gone in "
        "ArgoCD, then push again (an empty commit is enough).", "",
        NOTE, ""]


def test_the_panel_of_new_envs_only_does_not_wait_for_apps():
    lines = m._dup_identity_lines([_gate(files=(), also=["pv-qa88-az"])], False)
    assert lines[2] == (f"- `{COPY}` names its apps `{NS}-*`, but live apps already use "
                        "these names: `pv-qa88-a-glb`, `pv-qa88-a-ms`, `pv-qa88-a-ss`.")
    lines = m._dup_identity_lines([_gate(apps=(), files=(), also=["pv-qa88-az"])], False)
    assert not any("wait until" in line for line in lines) and NOTE not in lines
    assert m._dup_identity_lines([{"kind": "shrink", "env": ""}], False) == []
    assert m._dup_identity_lines(None, True) == [NOTE, ""]


# ── process_pr ───────────────────────────────────────────────────────────

ENV_DIR = posixpath.dirname(IDENTITY)
CUSTOM = posixpath.dirname(ENV_DIR)
DUP = f"{CUSTOM}/pv-orch-copy/customer.yaml"
ORCH = "appspace:\n  customerName: orch\n  version: 2603.0.1-dev\n"
ORCH_APPS = PATH_MAP[IDENTITY]
RENDERED = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: platform-config\n"
DUP_WHY = f"live apps {', '.join(ORCH_APPS)} from {IDENTITY}"
DUP_FIX = ". Use git mv, or choose another customerName or suffix"
DUP_DESC = f"Blocked - {TEXT} ({DUP_WHY}) in pv-orch-copy{DUP_FIX} (see PR comment)"
SK = (m.BB_REPO, 991)


@pytest.fixture()
def orch(world, monkeypatch):
    """pv-orch-a is live. `run(changed, extra)` adds files at the PR head."""
    sinks, _plan = world
    monkeypatch.setattr(m, "_app_namespace_map", {a: "pv-orch-a" for a in ORCH_APPS})
    monkeypatch.setattr(m, "_app_value_files_map", {
        a: [f"$config/{CUSTOM}/config.yaml", f"$config/{IDENTITY}"] for a in ORCH_APPS})
    monkeypatch.setattr(m, "_render_new_env_diff",
                        lambda env_info, pr_sha: (RENDERED, None, 1, "2603.0.1-dev"))
    monkeypatch.setattr(m, "_pr_commit_messages",
                        lambda *a: pytest.fail("commits read with no trailer to find"))

    def run(changed, extra=None, messages=None):
        files = {IDENTITY: ORCH, f"{CUSTOM}/config.yaml": COHORT_DOC, **(extra or {})}
        monkeypatch.setattr(m, "_bb_fetch_status", lambda path, sha, repo=None:
                            (files[path], m.BB_OK) if path in files and (
                                sha == PR_SHA or path not in (extra or {}))
                            else (None, m.BB_NOT_FOUND))
        monkeypatch.setattr(m, "get_pr_changed_files", lambda pr_id, repo=None: (changed, {}))
        if messages is not None:
            monkeypatch.setattr(m, "_pr_commit_messages", lambda *a: list(messages))
        m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
        return sinks.upserts[-1], sinks.statuses[-1]
    return run


def test_a_copy_blocks_the_new_env_path(orch):
    body, (state, desc) = orch([DUP], {DUP: ORCH})
    assert m._extract_status_token(body) == "blocked"
    assert state == "FAILED" and desc == DUP_DESC
    assert f"- ⛔ **{TEXT} ({DUP_WHY})** in `pv-orch-copy`" in body
    assert "to merge anyway" not in body
    assert body.index(cr.MERGE_SUMMARY_HDR) < body.index("NEW ENVIRONMENT NAME ALREADY IN USE") \
        < body.index("New Environment(s) Detected")
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA)


def test_a_copy_blocks_the_diff_path(orch):
    body, (state, desc) = orch([ANCILLARY, DUP], {DUP: ORCH})
    assert m._extract_status_token(body) == "blocked"
    assert state == "FAILED" and desc == DUP_DESC
    assert body.index(cr.MERGE_SUMMARY_HDR) < body.index("NEW ENVIRONMENT NAME ALREADY IN USE")


def test_a_confirm_line_does_not_lift_it(orch):
    """A new clone that copies a live clone: its trailer lifts the clone wake
    gate, and no Confirm-* line lifts the name gate."""
    live = "gcp/aec/private-cloud/na1-b/monthly/pv-orch--aec1-a/customer.yaml"
    new = "gcp/aec/private-cloud/na1-b/monthly/pv-orch--aec1-copy/customer.yaml"
    doc = "appspace:\n  customerName: orch--aec1\n  version: 2603.0.1-dev\n"
    apps = ["pv-orch--aec1-a-ms", "pv-orch--aec1-a-ss"]
    m._app_namespace_map.update({a: "pv-orch--aec1-a" for a in apps})
    m._app_value_files_map.update({a: [f"$config/{live}"] for a in apps})
    body, (state, desc) = orch([new], {new: doc, live: doc,
                                       "gcp/aec/private-cloud/na1-b/monthly/config.yaml": COHORT_DOC},
                               ["Confirm-Clone-Sanitized: pv-orch--aec1-copy",
                                "Confirm-Rename: pv-orch--aec1-a -> pv-orch--aec1-copy"])
    assert m._extract_status_token(body) == "blocked" and state == "FAILED"
    assert desc == (f"Blocked - {TEXT} (live apps {', '.join(apps)} from {live}) in "
                    f"pv-orch--aec1-copy{DUP_FIX} (see PR comment)")
    assert "☑️ **Confirmed in a commit:** `Confirm-Clone-Sanitized: pv-orch--aec1-copy`" in body


def test_a_new_env_with_its_own_name_is_clean(orch):
    new = f"{CUSTOM}/pv-orch-b/customer.yaml"
    body, (state, _desc) = orch([new], {new: ORCH + "  suffix: b\n"})
    assert m._extract_status_token(body) == "clean" and state == "SUCCESSFUL"
    assert "NEW ENVIRONMENT NAME" not in body and "Name check not run" not in body


def test_an_empty_app_list_shows_the_note_and_stays_green(orch, monkeypatch):
    monkeypatch.setattr(m, "_app_namespace_map", {})
    body, (state, _desc) = orch([DUP], {DUP: ORCH})
    assert m._extract_status_token(body) == "clean" and state == "SUCCESSFUL"
    assert NOTE in body and "NEW ENVIRONMENT NAME" not in body
