"""COPS-2869 (B3): guards around a planned KCC VM migration.

B1/B2 (COPS-2865) let PR1 through and hold PR2. B3 blocks:

* a `diskNameSuffix` change outside the migration flow;
* an `appspace.version` change while a `vmMigration` block is still set;
* a `vmMigration` block on a chart that does not render its Job (own gate,
  including a member of a mongo/rabbit set).

A revert of PR1 always passes these gates.
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
from test_cops2865_vm_migration_b1 import (  # noqa: E402
    BASE, PR1, PR2, TARGET, BLOCK, VM, SS, _svc, _serve, _result, _lines, _gates)


def test_suffix_alone_opens_the_vmm_suffix_gate(monkeypatch):
    head = _svc("machineType: n2d-standard-2", "createNewBootDisk: true",
                "diskNameSuffix: \"-hd\"")
    lines = _lines(monkeypatch, head, results=_result())
    assert "vmm_suffix" in _gates(lines)
    assert va._suffix_outside_migration_line("pv-orch-a", "svc", "role") in lines


def test_suffix_with_a_planned_migration_passes(monkeypatch):
    head = _svc(*TARGET, "diskNameSuffix: \"-hd\"", *BLOCK)
    lines = _lines(monkeypatch, head, results=_result(f"vmm-{VM}-1"))
    assert "vmm_suffix" not in _gates(lines)
    assert _gates(lines) == []


def test_suffix_tweak_while_the_block_stays_is_blocked(monkeypatch):
    base = _svc(*TARGET, "diskNameSuffix: \"-hd\"", *BLOCK)
    head = _svc(*TARGET, "diskNameSuffix: \"-hd2\"", *BLOCK)
    lines = _lines(monkeypatch, head, base=base, results=_result())
    assert "vmm_suffix" in _gates(lines)


def test_a_revert_of_pr1_does_not_open_vmm_suffix(monkeypatch):
    """PR1 set the suffix with the block; reverting both is not this gate."""
    lines = _lines(monkeypatch, BASE, base=PR1, results=_result())
    assert "vmm_suffix" not in _gates(lines)
    assert "vmm_pr2" not in _gates(lines)
    assert "vmm_chart" not in _gates(lines)
    assert "vmm_unrendered" not in _gates(lines)


def _with_version(results, change):
    return {app: r._replace(version_change=change) for app, r in results.items()}


def test_chart_bump_while_migration_set_opens_vmm_chart(monkeypatch):
    """Main still has the block; the PR only bumps appspace.version."""
    _serve(monkeypatch, PR1, PR1)   # values unchanged; version comes from DiffResult
    lines = m._summarize_vm_changes(
        [IDENTITY], PR_SHA, BASE_SHA, PATH_MAP,
        _with_version(_result(), ("2603.4.8-rev2-dev", "2604.1.1-rev3-dev")))
    assert "vmm_chart" in _gates(lines)
    assert va._chart_bump_during_migration_line("pv-orch-a", "svc", VM, "1") in lines


def test_chart_bump_after_pr2_passes(monkeypatch):
    """Block already gone on main: a later chart bump is ordinary."""
    _serve(monkeypatch, PR2, PR2)
    lines = m._summarize_vm_changes(
        [IDENTITY], PR_SHA, BASE_SHA, PATH_MAP,
        _with_version(_result(), ("2603.4.8-rev2-dev", "2604.1.1-rev3-dev")))
    assert "vmm_chart" not in _gates(lines)


def test_chart_bump_removed_with_revert_of_pr1_passes(monkeypatch):
    """Reverting PR1 drops the block; a version change in the same PR is fine."""
    _serve(monkeypatch, BASE, PR1)
    lines = m._summarize_vm_changes(
        [IDENTITY], PR_SHA, BASE_SHA, PATH_MAP,
        _with_version(_result(), ("2603.4.8-rev2-dev", "2603.4.8-rev1-dev")))
    assert "vmm_chart" not in _gates(lines)


def test_unrendered_is_its_own_gate(monkeypatch):
    lines = _lines(monkeypatch, PR1, results=_result())
    gates = m._merge_gates((), vm_change_lines=lines)
    kinds = [g["kind"] for g in gates]
    assert "vmm_unrendered" in kinds
    assert all(m.gate_trailer(g) == "" for g in gates if g["kind"] == "vmm_unrendered")


def test_unrendered_on_a_mongo_member_opens_the_gate(monkeypatch):
    """Prod readiness gap: multi-instance roles were skipped by B1 entirely."""
    def mongo(*lines):
        return IDENTITY_YAML + ("  infra:\n    deployLinuxServicesK8s:\n      mongo:\n"
                                "        enabled: true\n        instances:\n"
                                "        - name: pv-orch-mongo1-a\n"
                                "          machineType: n4-standard-2\n"
                                "        - name: pv-orch-mongo2-a\n"
                                + "".join(f"        {line}\n" for line in lines))
    base = mongo()
    head = mongo("vmMigration:", "  type: disk-replace",
                 "  instance: pv-orch-mongo1-a")
    lines = _lines(monkeypatch, head, base=base, results=_result())
    assert "vmm_unrendered" in _gates(lines)
    assert va._unrendered_vm_migration_line(
        "pv-orch-a", "mongo", "pv-orch-mongo1-a", "1") in lines


# ── helpers ──────────────────────────────────────────────────────────────

def test_suffix_change_helpers():
    K = "appspace.infra.deployLinuxServicesK8s."
    old = {K + "svc.diskNameSuffix": None}
    new = {K + "svc.diskNameSuffix": "-hd"}
    assert va._disk_name_suffix_changes(old, new) == [("svc", "role")]
    assert va._suffix_outside_migration(old, new) == [("svc", "role")]
    planned = {"svc": ("disk-replace", VM, "1")}
    assert va._suffix_outside_migration(old, new, planned) == []


def test_chart_bump_helper():
    K = "appspace.infra.deployLinuxServicesK8s."
    block = {K + "svc.vmMigration.type": "disk-replace",
             K + "svc.vmMigration.instance": VM}
    assert va._chart_bump_during_migration(block, block, True) == {
        "svc": ("disk-replace", VM, "1")}
    assert va._chart_bump_during_migration(block, block, False) == {}
    assert va._chart_bump_during_migration(block, {}, True) == {}
