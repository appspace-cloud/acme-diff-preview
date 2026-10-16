"""COPS-2865 (B1): a VM migration that a PR plans with the vmMigration block.

acme-components COPS-2835 added `<role>.vmMigration` to supporting-services.
With it set, the chart stops rendering that VM's ComputeInstance and data
ComputeDisk and renders a Job, vmm-<vm>-<attempt>, that copies the disks to the
target names and types and swaps them onto the same VM, or only verifies a VM
already on target. The target values in the same PR (an n4 machine type,
hyperdisk-balanced disks) are therefore not an in-place change, and the vm_disk
gate and the disk type danger must not block it: acme-config-dev #7511 (the
verify-only alignment of pv-dev-05-a) was blocked by both.

The rules only step aside when the chart honours the block (its Job is in the
render), the type is one the chart knows, and the role's only VM is the one
selected. Anything else keeps every rule.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import diff_preview as m  # noqa: E402
import vm_analysis as va  # noqa: E402

from test_coverage_orchestration import (  # noqa: E402,F401
    PATH_MAP, BASE_SHA, PR_SHA, IDENTITY, IDENTITY_YAML)

K = "appspace.infra.deployLinuxServicesK8s."
REASON = va._VM_DISK_FAMILY_REASON
GCP = "gcp/config.yaml"
KCC_ON = "appspace:\n  infra:\n    deployLinuxServicesK8s:\n      enabled: true\n"
COHORT = IDENTITY.rsplit("/", 2)[0] + "/config.yaml"
COHORT_DOC = "appspace:\n  version: 2603.4.8-rev2-dev\n"
VM = "pv-orch-svc-a"
SS = "pv-orch-a-ss"


def _svc(*lines):
    return IDENTITY_YAML + ("  infra:\n    deployLinuxServicesK8s:\n      svc:\n"
                            "        enabled: true\n        instanceName: pv-orch-svc-a\n"
                            + "".join(f"        {line}\n" for line in lines))


BASE = _svc("machineType: n2d-standard-2", "createNewBootDisk: true")       # pv-dev-05-a
TARGET = ("machineType: n4-standard-2", "bootDiskType: hyperdisk-balanced",
          "dataDiskType: hyperdisk-balanced", "createNewBootDisk: false")
BLOCK = ("vmMigration:", "  type: disk-replace", f"  instance: {VM}", "  attempt: 1")
PR1 = _svc(*TARGET, *BLOCK)                                                  # #7511
PR2 = _svc(*TARGET)


def _serve(monkeypatch, head, base):
    base = {GCP: KCC_ON, COHORT: COHORT_DOC, IDENTITY: base}
    head = dict(base, **{IDENTITY: head})

    def fetch(path, sha, repo=None):
        side = head if sha == PR_SHA else base
        return (side[path], m.BB_OK) if path in side else (None, m.BB_NOT_FOUND)
    monkeypatch.setattr(m, "_bb_fetch_status", fetch)


def _result(*jobs, outcome=None, facts=None, body="+apiVersion: batch/v1\n+kind: Job\n"):
    """The env's two apps: -ss with the given Job sections, -ms without a diff."""
    secs = [(f"/batch/Job pv-orch-a/{j}", body) for j in jobs]
    return {SS: m.DiffResult(text="", sections=secs, n_res=len(secs), has_diff=bool(secs),
                             error=None, outcome=outcome or m.OUT_DIFF, reason=None,
                             vm_changes=facts),
            "pv-orch-a-ms": m.DiffResult(text="", sections=[], n_res=0, has_diff=False,
                                         error=None, outcome=m.OUT_NO_DIFF, reason=None)}


def _lines(monkeypatch, head, base=BASE, results=None):
    _serve(monkeypatch, head, base)
    return m._summarize_vm_changes([IDENTITY], PR_SHA, BASE_SHA, PATH_MAP, results or {})


def _gates(lines):
    return [g["kind"] for g in m._merge_gates((), vm_change_lines=lines)]


def _danger(lines):
    return [line for line in lines if "\U0001f6a8" in line]


# ── the planned migration of pv-dev-05-a (#7511) ─────────────────────────

def test_a_planned_migration_opens_no_gate_and_no_danger(monkeypatch):
    lines = _lines(monkeypatch, PR1, results=_result(f"vmm-{VM}-1"))
    assert not any(REASON in line for line in lines)
    assert _gates(lines) == []
    assert _danger(lines) == []


def test_a_planned_migration_says_what_happens(monkeypatch):
    lines = _lines(monkeypatch, PR1, results=_result(f"vmm-{VM}-1"))
    assert va._planned_vm_migration_line("pv-orch-a", "svc", "disk-replace", VM, "1") in lines
    assert any("`svc.machineType`: `n2d-standard-2` \u2192 `n4-standard-2`" in line
               and va._VMM_PLANNED_NOTE in line for line in lines)
    assert any("`svc.dataDiskType` = `hyperdisk-balanced`" in line
               and va._VMM_PLANNED_NOTE in line for line in lines)


# ── every other case keeps the rules ─────────────────────────────────────

def test_without_the_job_the_gate_stays_and_says_why(monkeypatch):
    lines = _lines(monkeypatch, PR1, results=_result())
    assert "vm_disk" in _gates(lines)
    assert "vmm_unrendered" in _gates(lines)   # COPS-2869: own gate, no trailer
    assert va._unrendered_vm_migration_line("pv-orch-a", "svc", VM, "1") in lines


def test_the_job_of_another_attempt_does_not_count(monkeypatch):
    lines = _lines(monkeypatch, PR1, results=_result(f"vmm-{VM}-2"))
    assert "vm_disk" in _gates(lines)
    assert "vmm_unrendered" in _gates(lines)


def test_a_type_the_chart_does_not_know_keeps_the_rules(monkeypatch):
    head = PR1.replace("type: disk-replace", "type: disk-copy")
    lines = _lines(monkeypatch, head, results=_result(f"vmm-{VM}-1"))
    assert _gates(lines) == ["vm_disk"]
    assert not any("planned migration" in line for line in lines)


def test_a_block_for_another_vm_keeps_the_rules(monkeypatch):
    head = PR1.replace(f"instance: {VM}", "instance: pv-other-svc-a")
    lines = _lines(monkeypatch, head, results=_result("vmm-pv-other-svc-a-1"))
    assert _gates(lines) == ["vm_disk"]


def test_a_role_with_other_vms_keeps_the_rules(monkeypatch):
    """A role-level machineType also changes the VMs the block does not select."""
    def mongo(*lines):
        return IDENTITY_YAML + ("  infra:\n    deployLinuxServicesK8s:\n      mongo:\n"
                                "        enabled: true\n        instances:\n"
                                "        - pv-orch-mongo1-a\n        - pv-orch-mongo2-a\n"
                                + "".join(f"        {line}\n" for line in lines))
    head = mongo(*TARGET, "vmMigration:", "  type: disk-replace",
                 "  instance: pv-orch-mongo1-a")
    lines = _lines(monkeypatch, head, mongo("machineType: n2d-standard-2"),
                   _result("vmm-pv-orch-mongo1-a-1"))
    assert _gates(lines) == ["vm_disk"]


# ── B2: the PR that removes the block (PR2) waits for the Job ────────────

def test_pr2_that_removes_the_block_opens_the_vmm_pr2_gate(monkeypatch):
    lines = _lines(monkeypatch, PR2, base=PR1, results=_result())
    assert _gates(lines) == ["vmm_pr2"]
    assert va._removed_vm_migration_line("pv-orch-a", "svc", VM, "1") in lines
    assert m._merge_gates((), vm_change_lines=lines)[0]["arg"] == "pv-orch-a"
    assert "Confirm-VM-Migrated: pv-orch-a" in lines[lines.index(
        va._removed_vm_migration_line("pv-orch-a", "svc", VM, "1"))]


def test_a_revert_of_pr1_always_passes(monkeypatch):
    lines = _lines(monkeypatch, BASE, base=PR1, results=_result())
    assert "vmm_pr2" not in _gates(lines)


# ── what keeps every rule (review of 2.126.0) ────────────────────────────

@pytest.mark.parametrize("where", ["svc", "defaults"])
def test_deletion_armed_on_main_keeps_the_gate(monkeypatch, where):
    """An allowDeletion: false not synced yet comes too late (runbook)."""
    if where == "svc":
        base = BASE + "        allowDeletion: true\n"
    else:
        base = BASE.replace("      svc:\n", "      defaults:\n        allowDeletion: true\n      svc:\n")
    head = PR1 + "        allowDeletion: false\n"
    lines = _lines(monkeypatch, head, base=base, results=_result(f"vmm-{VM}-1"))
    assert "vm_disk" in _gates(lines)
    assert va._armed_vm_migration_line("pv-orch-a", "svc", VM) in lines
    assert not any("planned migration" in line for line in lines)


def test_a_vm_cr_pruned_with_delete_keeps_the_gate(monkeypatch):
    fact = {"header": f"/compute.cnrm.cloud.google.com/ComputeInstance pv-orch-a/{VM}",
            "kind": "ComputeInstance", "name": VM, "fields": [], "created": False,
            "deleted": True, "orphaned": False, "notes": [], "untracked": [],
            "dangerous": ["resource DELETED from the render with `deletion-policy: delete`"]}
    lines = _lines(monkeypatch, PR1, results=_result(f"vmm-{VM}-1", facts=[fact]))
    assert "vm_disk" in _gates(lines)
    assert va._armed_vm_migration_line("pv-orch-a", "svc", VM) in lines


def test_an_incomplete_render_does_not_blame_the_chart(monkeypatch):
    lines = _lines(monkeypatch, PR1, results=_result(outcome=m.OUT_INDETERMINATE))
    assert not any("this chart version does not know the key" in line for line in lines)
    assert "vm_disk" in _gates(lines)


def test_a_modified_job_of_the_same_attempt_is_not_a_plan(monkeypatch):
    lines = _lines(monkeypatch, PR1, results=_result(
        f"vmm-{VM}-1", body="-  backoffLimit: 0\n+  backoffLimit: 1\n"))
    assert "vm_disk" in _gates(lines)
    assert "vmm_unrendered" in _gates(lines)


def test_a_block_equal_to_main_adds_no_line(monkeypatch):
    head = PR1.replace("  attempt: 1\n", "")          # the same block: attempt defaults to 1
    base = PR1.replace("instanceName: pv-orch-svc-a", "instanceName: pv-orch-svc-a\n        dataDiskSizeGb: 128")
    lines = _lines(monkeypatch, head, base=base, results=_result())
    assert not any("vmMigration` selects" in line or "planned migration" in line for line in lines)


# ── vm_analysis helpers ──────────────────────────────────────────────────

def test_the_attempt_defaults_to_1():
    flat = {K + "svc.vmMigration.type": "disk-replace", K + "svc.vmMigration.instance": VM}
    assert va._kcc_vm_migrations(flat) == {"svc": ("disk-replace", VM, "1")}
    assert va._kcc_vm_migrations(None) == {}


@pytest.mark.parametrize("header,body,want", [
    (f"/batch/Job pv-orch-a/vmm-{VM}-1", "+x\n", True),
    (f"/v1/ConfigMap pv-orch-a/vmm-{VM}-1", "+x\n", False),
    (f"/batch/Job pv-orch-a/vmm-{VM}-12", "+x\n", False),
    (f"/batch/Job pv-orch-a/vmm-{VM}-1", "-x\n+y\n", False),     # modified
    (f"/batch/Job pv-orch-a/vmm-{VM}-1", "-x\n", False),          # deleted
])
def test_only_a_created_job_counts(header, body, want):
    assert va._vmm_job_created([(header, body)], f"vmm-{VM}-1") is want
