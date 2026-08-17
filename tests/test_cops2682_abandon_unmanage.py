"""COPS-2682: abandon unmanage of KCC VMs is not a GCP delete.

acme-config-prod #4326 disables deployLinuxServicesK8s for a TERMINATED
svc VM whose live CRs already carry deletion-policy: abandon. Argo prune
drops the KCC CR only; the GCP VM/disk/IP stay. Preview 2.90.0 shouted
DO NOT MERGE + 9 RESOURCE(S) DELETED + machineType resize runbook on
sibling key removals. That verdict is wrong for abandon unmanage.
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import comment_render as cr
import diff_preview as m
import vm_analysis as vma


CI_HDR = "/compute.cnrm.cloud.google.com/ComputeInstance pv-dpdhl-svc-a"
CD_HDR = "/compute.cnrm.cloud.google.com/ComputeDisk pv-dpdhl-svc-a-data"
CA_HDR = "/compute.cnrm.cloud.google.com/ComputeAddress pv-dpdhl-svc-a-iip"
ATT_HDR = ("/compute.cnrm.cloud.google.com/ComputeDiskResourcePolicyAttachment "
           "pv-dpdhl-svc-a-eu-daily")

# Whole CR leaving the render (all minus, no context), with abandon on the
# live object — the chart default when allowDeletion is unset.
VM_ABANDON_DELETED = (
    "--- \n+++ \n@@ -1,10 +0,0 @@\n"
    "-apiVersion: compute.cnrm.cloud.google.com/v1beta1\n"
    "-kind: ComputeInstance\n"
    "-metadata:\n"
    "-  name: pv-dpdhl-svc-a\n"
    "-  annotations:\n"
    "-    cnrm.cloud.google.com/deletion-policy: abandon\n"
    "-spec:\n"
    "-  machineType: \"n2d-highmem-2\"\n"
    "-  desiredStatus: \"TERMINATED\"\n"
)

DISK_ABANDON_DELETED = (
    "--- \n+++ \n@@ -1,8 +0,0 @@\n"
    "-apiVersion: compute.cnrm.cloud.google.com/v1beta1\n"
    "-kind: ComputeDisk\n"
    "-metadata:\n"
    "-  name: pv-dpdhl-svc-a-data\n"
    "-  annotations:\n"
    "-    cnrm.cloud.google.com/deletion-policy: abandon\n"
    "-spec:\n"
    "-  size: 128\n"
)

# Armed for real destroy: deletion-policy delete on the way out.
VM_DELETE_POLICY_DELETED = (
    "--- \n+++ \n@@ -1,8 +0,0 @@\n"
    "-apiVersion: compute.cnrm.cloud.google.com/v1beta1\n"
    "-kind: ComputeInstance\n"
    "-metadata:\n"
    "-  name: pv-acme-svc-a\n"
    "-  annotations:\n"
    "-    cnrm.cloud.google.com/deletion-policy: delete\n"
    "-spec:\n"
    "-  machineType: \"n2d-standard-4\"\n"
)

ATT_DELETED = (
    "--- \n+++ \n@@ -1,6 +0,0 @@\n"
    "-apiVersion: compute.cnrm.cloud.google.com/v1alpha1\n"
    "-kind: ComputeDiskResourcePolicyAttachment\n"
    "-metadata:\n"
    "-  name: pv-dpdhl-svc-a-eu-daily\n"
    "-spec:\n"
    "-  zone: europe-west1-d\n"
)

OLD_DISABLE = (
    "appspace:\n"
    "  infra:\n"
    "    deployLinuxServicesK8s:\n"
    "      enabled: true\n"
    "      svc:\n"
    "        enabled: true\n"
    "        instanceName: pv-dpdhl-svc-a\n"
    "        machineType: n2d-highmem-2\n"
    "        desiredStatus: TERMINATED\n"
)

NEW_DISABLE = (
    "appspace:\n"
    "  infra:\n"
    "    deployLinuxServicesK8s:\n"
    "      enabled: false\n"
)

PR_SHA, BASE_SHA = "prsha", "basesha"
PATH = "gcp/prod/private-cloud/eu1-b/monthly-friday/pv-dpdhl-c/customer.yaml"


def _fetch_stub(table):
    def fake(path, sha, repo=None):
        try:
            return table[(path, sha)], m.BB_OK
        except KeyError:
            return "", None
    return fake


def _result(secs, vm_changes=None, deleted=None):
    secs = secs or []
    return m.DiffResult(
        "", secs, len(secs), True, None, m.OUT_DIFF, "changes",
        None, deleted or [h for h, _ in secs], None, None, None,
        vm_changes if vm_changes is not None else vma._detect_vm_changes(secs),
    )


# ── rendered-level facts ───────────────────────────────────────────────

def test_abandon_instance_leaving_render_is_orphan_not_dangerous():
    facts = vma._detect_vm_changes([(CI_HDR, VM_ABANDON_DELETED)])
    assert len(facts) == 1
    f = facts[0]
    assert f["deleted"] and f.get("orphaned")
    assert not f["dangerous"], "abandon prune must not read as GCP destroy"
    assert any("abandon" in n.lower() for n in f["notes"])


def test_abandon_disk_leaving_render_is_orphan_not_dangerous():
    facts = vma._detect_vm_changes([(CD_HDR, DISK_ABANDON_DELETED)])
    assert facts[0].get("orphaned") and not facts[0]["dangerous"]


def test_delete_policy_instance_leaving_render_stays_dangerous():
    facts = vma._detect_vm_changes([(CI_HDR, VM_DELETE_POLICY_DELETED)])
    assert facts[0]["deleted"] and not facts[0].get("orphaned")
    assert facts[0]["dangerous"]


def test_snapshot_attachment_leaving_render_is_schedule_note_not_vm_destroy():
    facts = vma._detect_vm_changes([(ATT_HDR, ATT_DELETED)])
    assert facts[0]["deleted"]
    assert not facts[0]["dangerous"], (
        "attachment prune is not a VM/disk destroy; schedule note only")
    assert any("snapshot" in n.lower() or "schedule" in n.lower()
               for n in facts[0]["notes"])


# ── values-level: enabled false without allowDeletion ──────────────────

def test_enabled_false_without_allowDeletion_is_not_dangerous(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_cached", _fetch_stub({
        (PATH, PR_SHA): NEW_DISABLE, (PATH, BASE_SHA): OLD_DISABLE}))
    lines = m._summarize_vm_changes(
        [PATH], PR_SHA, BASE_SHA, {PATH: ["argocd/pv-dpdhl-c-ss"]}, {})
    body = "\n".join(lines)
    assert "enabled" in body and "False" in body
    assert "\U0001f6a8" not in body, "unmanage without arming must not 🚨"
    assert "abandon" in body.lower() or "unmanage" in body.lower() or "kept" in body.lower()
    assert lines[0] == cr._VM_PANEL_ROUTINE_HDR


def test_machineType_removal_under_disable_is_not_resize_runbook(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_cached", _fetch_stub({
        (PATH, PR_SHA): NEW_DISABLE, (PATH, BASE_SHA): OLD_DISABLE}))
    lines = m._summarize_vm_changes(
        [PATH], PR_SHA, BASE_SHA, {PATH: ["argocd/pv-dpdhl-c-ss"]}, {})
    body = "\n".join(lines)
    assert "stopping the VM first" not in body
    assert "machineType changes while desiredStatus" not in body


# ── merge summary ──────────────────────────────────────────────────────

def test_merge_summary_abandon_orphans_are_review_not_block_delete():
    secs = [(CI_HDR, VM_ABANDON_DELETED), (CD_HDR, DISK_ABANDON_DELETED),
            (ATT_HDR, ATT_DELETED)]
    facts = vma._detect_vm_changes(secs)
    results = {"pv-dpdhl-c-ss": _result(secs, vm_changes=facts)}
    vm_lines = m._summarize_vm_changes([], PR_SHA, BASE_SHA, {}, results)
    body = m.format_comment(PR_SHA, results, base_sha=BASE_SHA,
                            vm_change_lines=vm_lines)
    # Destroy deleted finding must not fire for abandon orphans (+ schedule
    # attachments that are notes, not GCP VM destroys).
    assert "resource(s) deleted**" not in body
    assert "DO NOT MERGE" not in body
    assert ("unmanage" in body.lower() or "abandon" in body.lower()
            or "orphaned" in body.lower() or "kept" in body.lower())
