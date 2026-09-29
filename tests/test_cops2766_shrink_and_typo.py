"""COPS-2766 block 2, item 3: two errors that no Confirm-* line can lift.

A misspelled teardown flag on the folder-removal panel. The panel explained
it, but only the appspace-state panel failed the build. A removal PR with
`decomission: true` in its base customer.yaml was [blocked] by the orphan
gate, so `Confirm-Teardown` made it green with the typo still there. Now it
is [permanent], like the typo on the arming PR, and it keeps the full
comment: the folder removal is not a STOP-only PR.

A disk shrink. GCP cannot shrink a disk in place, so it is never valid. The
comment said DO NOT MERGE and the build was green (acme-config-dev #7372).
Now every producer writes the same reason, _VM_SHRINK_REASON, and the
`shrink` gate fails the build with [blocked]. That gate has no trailer.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import comment_render as cr  # noqa: E402
import diff_preview as m  # noqa: E402
import vm_analysis as vma  # noqa: E402

from test_coverage_orchestration import (  # noqa: E402,F401
    world, _mk_pr, PATH_MAP, BASE_SHA, IDENTITY, IDENTITY_YAML)

SHRINK = vma._VM_SHRINK_REASON
EVERY_TRAILER = ("Confirm-Teardown: pv-orch-a\nConfirm-Decommission: pv-orch-a\n"
                 "Confirm-IP-Release: pv-orch-a\nConfirm-Rename: a -> b\n")


@pytest.fixture(autouse=True)
def no_vertex(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)


def _run(sinks):
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    return sinks.upserts[-1], sinks.statuses[-1]


# ── (a) the flag typo on the folder-removal panel ────────────────────────

TYPO_BASE = IDENTITY_YAML + "  decomission: true\n"
# The file is gone in this PR, so the fix goes to main first.
TYPO_DESC = ("Teardown flag misspelled on main: appspace.decomission arms nothing - "
             "fix it to appspace.decommission on main in a separate PR, let it "
             "sync, then rebase this removal")


@pytest.fixture()
def typo_removal(world, monkeypatch):
    """The PR removes pv-orch-a/customer.yaml; main has `decomission: true`."""
    sinks, _plan = world
    monkeypatch.setattr(m, "get_pr_changed_files",
                        lambda pr_id, repo=None: ([IDENTITY], {}))
    monkeypatch.setattr(m, "_bb_fetch_status", lambda path, sha, repo=None:
                        (TYPO_BASE, m.BB_OK) if path == IDENTITY and sha == BASE_SHA
                        else (None, m.BB_NOT_FOUND))
    monkeypatch.setattr(m, "_render_main_side_resources", lambda app, sha: {})
    monkeypatch.setattr(m, "_shared_user_content_lines", lambda *a, **k: [])

    def run(messages):
        monkeypatch.setattr(m, "_pr_commit_messages", lambda *a: messages)
        return _run(sinks)
    return run


@pytest.mark.parametrize("messages", [[], ["Remove pv-orch-a\n\n" + EVERY_TRAILER]])
def test_a_removal_with_a_misspelled_flag_fails_and_no_trailer_lifts_it(
        typo_removal, messages):
    body, (state, desc) = typo_removal(messages)
    assert m._extract_status_token(body) == "permanent"
    assert state == "FAILED" and desc == TYPO_DESC
    assert "| \u26d4 TEARDOWN FLAG MISSPELLED" in body
    # The full comment, not the STOP-only one of the arming PR.
    assert "ENVIRONMENT DECOMMISSION" in body and cr._DECOM_FLAG_TYPO_HDR in body
    assert "## \u26d4 STOP" not in body and "Everything else in this PR" not in body


def test_the_typo_status_says_where_the_fix_goes():
    rows = ["| `appspace.decomission` | `appspace.decommission` |"]
    assert m._flag_typo_status_description(rows, removal=True) == TYPO_DESC
    assert m._flag_typo_status_description(rows).endswith("and push")
    assert m._flag_typo_status_description([], removal=True).startswith(
        "Teardown flag misspelled or misplaced")


def test_format_comment_reads_the_typo_from_the_decommission_panel():
    decom = m.DiffResult("", [], 0, False, None, m.OUT_DECOMMISSIONED, "")
    body = m.format_comment("a" * 40, {"pv-x-a-ms": decom}, base_sha="b" * 40,
                            decommission_lines=["\U0001f6a8 " + cr._DECOM_FLAG_TYPO_HDR, ""])
    assert m._extract_status_token(body) == "permanent"
    assert "TEARDOWN FLAG MISSPELLED" in body and "## \u26d4 STOP" not in body


# ── (b) disk shrink: one reason, every producer ──────────────────────────

DISK = ("/compute.cnrm.cloud.google.com/ComputeDisk "
        "pv-orch-a/pv-orch-mongo1-a-data")

VM = ("appspace:\n  customerName: orch\n  version: 2603.0.1-dev\n  infra:\n"
      "    deployLinuxServicesK8s:\n      enabled: true\n      mongo:\n"
      "        enabled: true\n        machineType: n2d-standard-2\n"
      "        dataDiskSizeGb: 256\n        desiredStatus: TERMINATED\n")
LEGACY = ("appspace:\n  customerName: orch\n  version: 2603.0.1-dev\n  infra:\n"
          "    deployLinuxServices:\n      deployVM: false\n"
          "      machineType: n2d-highmem-2\n      dataDiskSizeGb: 256\n")
ADOPTED = ("appspace:\n  customerName: orch\n  version: 2603.0.1-dev\n  infra:\n"
           "    deployLinuxServicesK8s:\n      enabled: true\n      svc:\n"
           "        enabled: true\n        machineType: n2d-highmem-2\n"
           "        dataDiskSizeGb: 128\n        createNewBootDisk: false\n"
           "        manageMetadata: false\n")


def test_every_producer_writes_the_same_reason():
    assert SHRINK == "GCP cannot shrink a disk in place"
    fact = vma._detect_vm_changes([(DISK, "-    size: 64\n+    size: 32\n")])[0]
    assert any(SHRINK in d for d in fact["dangerous"])
    assert SHRINK in vma._kcc_move_disk_shrink(
        {vma._LEGACY_PREFIX + "dataDiskSizeGb": 256},
        {vma._KCC_PREFIX + "svc.dataDiskSizeGb": 128}, ["svc"])


@pytest.fixture()
def values_pr(world, monkeypatch):
    """The PR edits pv-orch-a/customer.yaml from `old` to `new`."""
    sinks, plan = world
    monkeypatch.setattr(m, "get_pr_changed_files",
                        lambda pr_id, repo=None: ([IDENTITY], {}))
    monkeypatch.setattr(m, "_pr_commit_messages", lambda *a: [EVERY_TRAILER])

    def run(old, new):
        monkeypatch.setattr(m, "_bb_fetch_status", lambda path, sha, repo=None:
                            ((old if sha == BASE_SHA else new), m.BB_OK)
                            if path == IDENTITY else (None, m.BB_NOT_FOUND))
        return _run(sinks)
    return run, plan


SHRINK_DESC = ("Blocked - Disk shrink in pv-orch-a. GCP cannot shrink a disk in "
               "place, so keep the old size or grow it (see PR comment)")


def _assert_blocked_shrink(body, state, desc):
    assert m._extract_status_token(body) == "blocked"
    assert state == "FAILED" and desc == SHRINK_DESC
    assert ("- \u26d4 **Disk shrink** in `pv-orch-a`. GCP cannot shrink a disk in "
            "place, so keep the old size or grow it") in body
    assert "Confirmed in a commit" not in body


def test_a_values_level_shrink_is_blocked(values_pr):
    run, _plan = values_pr
    body, (state, desc) = run(VM, VM.replace("dataDiskSizeGb: 256", "dataDiskSizeGb: 128"))
    assert f"disk size DECREASES \u2014 {SHRINK}" in body
    _assert_blocked_shrink(body, state, desc)


def test_a_shrink_across_the_kcc_move_is_blocked(values_pr):
    run, _plan = values_pr
    body, (state, desc) = run(LEGACY, ADOPTED)
    assert "DECREASES across the Terraform \u2192 KCC move" in body
    _assert_blocked_shrink(body, state, desc)


def _rendered(plan, old, new):
    body = f"-    size: {old}\n+    size: {new}\n"
    plan["pv-orch-a-ss"] = m.DiffResult(
        "--- main\n+++ pr", [(DISK, body)], 1, True, "", m.OUT_DIFF, "",
        vm_changes=vma._detect_vm_changes([(DISK, body)]))


def test_a_rendered_disk_shrink_is_blocked(values_pr):
    run, plan = values_pr
    _rendered(plan, 64, 32)
    body, (state, desc) = run(IDENTITY_YAML, IDENTITY_YAML)
    assert f"disk size DECREASES \u2014 {SHRINK}" in body
    _assert_blocked_shrink(body, state, desc)


def test_no_trailer_lifts_a_shrink(values_pr, monkeypatch):
    """A lifted gate in the same PR proves the commits were read."""
    run, _plan = values_pr
    real = m._merge_gates
    monkeypatch.setattr(m, "_merge_gates", lambda *a: real(*a) + [
        {"kind": "orphan", "env": "pv-orch-a", "arg": "pv-orch-a", "lifted": False}])
    body, (state, desc) = run(VM, VM.replace("dataDiskSizeGb: 256", "dataDiskSizeGb: 128"))
    assert "\u2611\ufe0f **Confirmed in a commit:** `Confirm-Teardown: pv-orch-a`" in body
    _assert_blocked_shrink(body.replace("Confirmed in a commit", ""), state, desc)


# ── (c) growth is fine ───────────────────────────────────────────────────

def test_a_values_level_growth_stays_clean(values_pr, monkeypatch):
    monkeypatch.setattr(m, "_pr_commit_messages", lambda *a: pytest.fail("no gate"))
    run, _plan = values_pr
    body, (state, _desc) = run(VM, VM.replace("dataDiskSizeGb: 256", "dataDiskSizeGb: 512"))
    assert "dataDiskSizeGb" in body and SHRINK not in body
    assert m._extract_status_token(body) == "clean" and state == "SUCCESSFUL"


def test_a_rendered_growth_stays_clean(values_pr):
    run, plan = values_pr
    _rendered(plan, 32, 64)
    body, (state, _desc) = run(IDENTITY_YAML, IDENTITY_YAML)
    assert m._extract_status_token(body) == "clean" and state == "SUCCESSFUL"


# ── (d) acme-config-dev #7372, reconstructed ─────────────────────────────
# pv-qa88-a/customer.yaml on 2.121.0: the mongo data disk 64 -> 32, and the
# bot posted SUCCESSFUL under a DO NOT MERGE comment. The mongo VM runs here,
# so a machineType change is a danger too, but one the build does not stop.

QA88 = ("appspace:\n  customerName: orch\n  version: 2603.0.1-dev\n  infra:\n"
        "    noCore: true\n    deployLinuxServicesK8s:\n      enabled: true\n"
        "      svc:\n        enabled: false\n      mongo:\n        enabled: true\n"
        "        instances:\n          - pv-qa88-mongo1-a\n"
        "        machineType: n2d-standard-2\n        dataDiskSizeGb: 64\n"
        "        createNewBootDisk: false\n        manageMetadata: false\n"
        "        desiredStatus: RUNNING\n")


def test_7372_disk_shrink_is_now_red(values_pr):
    run, _plan = values_pr
    body, (state, desc) = run(QA88, QA88.replace("dataDiskSizeGb: 64", "dataDiskSizeGb: 32"))
    _assert_blocked_shrink(body, state, desc)


def test_7372_with_only_machine_type_stays_green(values_pr):
    run, _plan = values_pr
    body, (state, _desc) = run(QA88, QA88.replace("n2d-standard-2", "n2d-standard-4"))
    assert "runbook requires stopping the VM first" in body, "still a VM danger"
    assert SHRINK not in body
    assert m._extract_status_token(body) == "clean" and state == "SUCCESSFUL"


# ── _merge_gates ─────────────────────────────────────────────────────────

def test_merge_gates_adds_one_shrink_gate_per_env():
    """The env is the first name in backticks of the panel line: the env,
    or the ancestor file."""
    lines = [cr._VM_PANEL_DANGER_HDR,
             f"- \U0001f6a8 `pv-a` \u00b7 **linux VM (KCC) \u00b7 mongo**: x \u2014 {SHRINK}",
             f"- \U0001f6a8 `pv-a` \u00b7 `ComputeDisk d`: `size` \u2014 {SHRINK}",
             f"- \U0001f6a8 ancestor `gcp/x/config.yaml` (inherited): y \u2014 {SHRINK}",
             f"- b \u2014 {SHRINK}"]
    assert m._merge_gates(None, None, None, lines) == [
        {"kind": "shrink", "env": e, "arg": e, "lifted": False}
        for e in ("pv-a", "gcp/x/config.yaml", "")]
    assert m._merge_gates(None, None, None, [cr._VM_PANEL_DANGER_HDR, "- grow"]) == []
