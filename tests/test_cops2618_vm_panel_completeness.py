"""The VM panel must never say "no changes" when a VM object changed (COPS-2618).

acme-config-prod #3923 rendered:

    ### VM infrastructure - no changes
    No changes to VM infrastructure (KCC linux-services) in this PR.

while three ComputeInstance / ComputeDisk / ComputeAddress objects across two
environments genuinely gained five labels each, visible in the per-app diff
of the very same comment.

Root cause: _VM_TRACKED_FIELDS is a closed list, and _detect_vm_changes drops
every +/- line whose key is not in it (`if key not in tracked: continue`).
Anything outside the list is invisible, so the panel reports nothing and the
caller falls into the clean branch.

Note on the shape of a label diff: the rendered lines are
`+    business-area: appspace-platform`, so the *key* is `business-area`, not
`labels`. Tracking a literal "labels" key would match nothing. The fix is
therefore generic -- a section that changed in ways the panel does not track
individually still reports that it changed -- which also closes the same hole
for any field nobody has thought of yet, not just labels.

deviceName is tracked explicitly on top of that, because it is not merely
untracked noise: a mismatch detaches and reattaches a live disk under a
mounted filesystem (COPS-2592, pv-stagemig-a).
"""
import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m

INST = "/compute.cnrm.cloud.google.com/ComputeInstance pv-euroclear-a/pv-euroclear-svc-a"
DISK = "/compute.cnrm.cloud.google.com/ComputeDisk pv-euroclear-a/pv-euroclear-svc-a-data"

# The real #3923 diff: five labels added, nothing else.
LABELS_ADDED = """     hosting-id: "hst-00000466"
+    business-area: appspace-platform
+    component: legacy-compute
+    deployment: private-cloud
+    lifecycle: production
+    sub-component: rabbit-mongo
     owner: "ops"
"""

# The pre-fix pv-stagemig-a shape: the rendered device name differs from the
# live one, so KCC renames the attachment by detach + attach.
DEVICE_RENAME = """   attachedDisk:
     - mode: READ_WRITE
-      deviceName: persistent-disk-1
+      deviceName: pv-stagemig-svc-a-data
       sourceDiskRef:
         name: pv-stagemig-svc-a-data
"""

# The COPS-2592 fix shape: the pin goes away entirely on adoption.
DEVICE_DROPPED = """   attachedDisk:
     - mode: READ_WRITE
-      deviceName: pv-bos-svc-a-data
       sourceDiskRef:
         name: pv-bos-svc-a-data
"""

MACHINE_RESIZE = """     zone: europe-west1-d
-    machineType: n2d-standard-4
+    machineType: n2d-standard-8
     desiredStatus: RUNNING
"""


def _facts(header, body):
    return m._detect_vm_changes([(header, body)])


# --- 1. the bug: a labels-only change must not read as "no changes" ------

def test_labels_only_change_is_reported(monkeypatch):
    facts = _facts(INST, LABELS_ADDED)
    assert facts, "the section must produce a fact at all"
    f = facts[0]
    assert not f["deleted"] and not f["created"]
    assert f["fields"] or f["notes"], \
        "a real object change must leave something for the panel to render"


def test_labels_only_change_names_the_keys(monkeypatch):
    """Naming the keys is what makes the line actionable: a reviewer can tell
    a taxonomy-label rollout from something they should look at."""
    f = _facts(INST, LABELS_ADDED)[0]
    blob = " ".join(f["notes"]) + " ".join(str(x) for x in f["fields"])
    assert "business-area" in blob or "sub-component" in blob


def test_labels_only_change_is_not_dangerous(monkeypatch):
    """Label churn must be visible, not alarming. Crying wolf here would
    undo COPS-2608."""
    f = _facts(INST, LABELS_ADDED)[0]
    assert not f["dangerous"], "labels alone are not a dangerous change"


def test_labels_change_on_a_disk_is_reported_too(monkeypatch):
    f = _facts(DISK, LABELS_ADDED)[0]
    assert f["fields"] or f["notes"]
    assert not f["dangerous"]


# --- 2. deviceName: the field that took a disk offline in production ----

def test_device_name_rename_is_dangerous(monkeypatch):
    """Both sides present and different: KCC renames the attachment by
    detaching and reattaching, on a RUNNING VM, under a mounted filesystem.
    This is the pv-stagemig-a incident."""
    f = _facts(INST, DEVICE_RENAME)[0]
    assert f["dangerous"], "a deviceName rename must be flagged dangerous"
    joined = " ".join(f["dangerous"]).lower()
    assert "detach" in joined or "reattach" in joined, \
        "the reason must say what actually happens to the disk"


def test_device_name_dropped_is_routine_not_dangerous(monkeypatch):
    """The COPS-2592 fix's own shape: the chart stops rendering the pin when
    adopting. Visible, but not a wolf -- flagging this would block every
    adoption PR, which is exactly what COPS-2608 just stopped doing."""
    f = _facts(INST, DEVICE_DROPPED)[0]
    assert not f["dangerous"], \
        "dropping the pin does not touch the live attachment"
    assert f["fields"] or f["notes"], "but it must still be visible"


# --- 3. nothing already working may regress ----------------------------

def test_machine_type_resize_still_dangerous(monkeypatch):
    f = _facts(INST, MACHINE_RESIZE)[0]
    assert f["dangerous"]
    assert any("machineType" in d or "parked" in d for d in f["dangerous"])


def test_a_section_with_no_plus_or_minus_lines_produces_nothing(monkeypatch):
    """Context-only sections must stay silent, or the panel becomes noise
    on every unrelated PR."""
    facts = _facts(INST, "     machineType: n2d-standard-4\n"
                         "     zone: europe-west1-d\n")
    assert not facts or not (facts[0]["fields"] or facts[0]["notes"]
                             or facts[0]["dangerous"])


def test_non_vm_kinds_are_still_ignored(monkeypatch):
    facts = _facts("/apps/Deployment pv-foo-a/web",
                   "-  replicas: 3\n+  replicas: 0\n")
    assert facts == [], "the VM panel must not comment on Deployments"


def test_created_resource_still_reads_as_created(monkeypatch):
    facts = _facts(INST, "+    machineType: n2d-standard-4\n"
                         "+    zone: europe-west1-d\n")
    assert facts[0]["created"] is True
    assert not facts[0]["dangerous"]


def test_deleted_resource_defaults_to_abandon_orphan(monkeypatch):
    # COPS-2682: chart default is abandon when allowDeletion is unset.
    # A CR leaving the render without deletion-policy: delete is unmanage.
    facts = _facts(INST, "-    machineType: n2d-standard-4\n"
                         "-    zone: europe-west1-d\n")
    assert facts[0]["deleted"] is True
    assert facts[0].get("orphaned")
    assert not facts[0]["dangerous"]


def test_deleted_resource_with_delete_policy_still_dangerous(monkeypatch):
    facts = _facts(
        INST,
        "-  annotations:\n"
        "-    cnrm.cloud.google.com/deletion-policy: delete\n"
        "-    machineType: n2d-standard-4\n")
    assert facts[0]["deleted"] is True
    assert facts[0]["dangerous"]
    assert not facts[0].get("orphaned")
