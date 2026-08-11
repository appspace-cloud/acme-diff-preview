"""KCC adoption is an ownership transfer, not a machineType resize (COPS-2608).

Moving an environment from the legacy Terraform key
`appspace.infra.deployLinuxServices` to `appspace.infra.deployLinuxServicesK8s`
moves the same machine type from one key to the other. The values-level VM
panel flattened both trees, saw `removed legacy.machineType` and
`added kcc.svc.machineType`, and scored each independently as a resize --
producing a DO NOT MERGE on a PR that resizes nothing.

COPS-2592 has 239 production environments left to migrate. A guard that
cries wolf 239 times is a guard nobody reads the 240th time, which is
exactly when it will be right.

The guard rails matter more than the happy path here: this ticket makes the
panel *quieter*, so every test that proves it still shouts when it should is
worth more than the one that proves it stopped shouting.
"""
import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m

IDENT = "gcp/prod/private-cloud/ap1-b/monthly/pv-bos-b/customer.yaml"
PATH_MAP = {IDENT: ["pv-bos-b-ss"]}

LEGACY = """appspace:
  customerName: bos
  infra:
    deployLinuxServices:
      deployVM: false
      machineType: n2d-highmem-2
"""

ADOPTED = """appspace:
  customerName: bos
  infra:
    deployLinuxServicesK8s:
      enabled: true
      svc:
        enabled: true
        machineType: n2d-highmem-2
        dataDiskSizeGb: 128
        createNewBootDisk: false
        manageMetadata: false
"""


def _panel(monkeypatch, old, new, ident=IDENT, path_map=None, sfx="a",
           app_results=None):
    """Render the values-level VM panel for one changed identity file.

    Distinct shas per call: the fetch layer memoises on (path, sha).
    """
    b, h = "main" + sfx, "pr" + sfx
    store = {(ident, b): old, (ident, h): new}

    def fake(path, sha, repo=None):
        v = store.get((path, sha))
        return (v, m.BB_OK) if v is not None else (None, m.BB_NOT_FOUND)

    monkeypatch.setattr(m, "_bb_fetch_status", fake)
    return "\n".join(m._summarize_vm_changes(
        [ident], h, b, path_map if path_map is not None else PATH_MAP,
        app_results or []))


def _is_danger(out):
    # COPS-2636 dropped the sirens, making the danger header a
    # substring of the routine one. Match the way production does:
    # the panel's FIRST line, exactly.
    return out.startswith(m._VM_PANEL_DANGER_HDR)


def _machinetype_danger(out):
    return "runbook requires" in out


# --- 1. the bug itself ---------------------------------------------------

def test_pure_adoption_is_review_not_block(monkeypatch):
    out = _panel(monkeypatch, LEGACY, ADOPTED, sfx="pure")
    assert not _is_danger(out), \
        "an ownership transfer must not render the danger panel"
    assert not _machinetype_danger(out), \
        "the machine type does not change, it moves key"


def test_pure_adoption_renders_the_adoption_card(monkeypatch):
    out = _panel(monkeypatch, LEGACY, ADOPTED, sfx="card")
    assert "ADOPTION" in out.upper()
    assert "createNewBootDisk" in out, \
        "the card must state why this is an adoption and not a creation"


# --- guard rails: adoption must not silence anything else ---------------

def test_guard_allow_deletion_still_blocks(monkeypatch):
    new = ADOPTED.replace("        manageMetadata: false\n",
                          "        manageMetadata: false\n"
                          "        allowDeletion: true\n")
    out = _panel(monkeypatch, LEGACY, new, sfx="gdel")
    assert _is_danger(out), \
        "arming deletion in the same PR must still block"
    assert "allowDeletion" in out


def test_guard_disk_shrink_still_blocks(monkeypatch):
    old = LEGACY.replace("      machineType: n2d-highmem-2\n",
                         "      machineType: n2d-highmem-2\n"
                         "      dataDiskSizeGb: 256\n")
    out = _panel(monkeypatch, old, ADOPTED, sfx="gshrink")
    assert _is_danger(out), "a disk shrink must still block"


def test_guard_real_machinetype_change_still_blocks(monkeypatch):
    new = ADOPTED.replace("machineType: n2d-highmem-2",
                          "machineType: n2d-standard-8")
    out = _panel(monkeypatch, LEGACY, new, sfx="greal")
    assert _is_danger(out), \
        "the values genuinely differ, this is a resize wearing adoption's coat"
    assert _machinetype_danger(out)


def test_guard_greenfield_boot_disk_is_not_adoption(monkeypatch):
    new = ADOPTED.replace("createNewBootDisk: false",
                          "createNewBootDisk: true")
    out = _panel(monkeypatch, LEGACY, new, sfx="ggreen")
    assert "ADOPTION" not in out.upper(), \
        "createNewBootDisk: true creates a VM, it does not adopt one"


def test_guard_zone_change_still_blocks(monkeypatch):
    new = ADOPTED.replace("        manageMetadata: false\n",
                          "        manageMetadata: false\n"
                          "        zone: us-east1-b\n")
    out = _panel(monkeypatch, LEGACY, new, sfx="gzone")
    assert _is_danger(out), "zone is immutable, that must still block"


# --- edge cases the ticket asked to decide deliberately -----------------

def test_legacy_machinetype_absent_is_still_adoption(monkeypatch):
    """The old Terraform module defaulted machineType, so many customer.yaml
    files never set it. There is no old value, so nothing can be changing;
    the rendered level is the authority and this stays Review."""
    old = "appspace:\n  customerName: bos\n  infra:\n" \
          "    deployLinuxServices:\n      deployVM: false\n"
    out = _panel(monkeypatch, old, ADOPTED, sfx="noold")
    assert not _is_danger(out)
    assert not _machinetype_danger(out)


def test_terminated_desired_status_shape_is_review(monkeypatch):
    """The pv-chaostest-a shape: adopting a stopped VM."""
    new = ADOPTED.replace("        manageMetadata: false\n",
                          "        manageMetadata: false\n"
                          "        desiredStatus: TERMINATED\n")
    out = _panel(monkeypatch, LEGACY, new, sfx="term")
    assert not _is_danger(out)


def test_pinned_instance_name_is_named_in_the_card(monkeypatch):
    """The pv-bos-b shape: folder suffix b, real instance pv-bos-svc-a."""
    new = ADOPTED.replace("        machineType: n2d-highmem-2\n",
                          "        instanceName: pv-bos-svc-a\n"
                          "        machineType: n2d-highmem-2\n")
    out = _panel(monkeypatch, LEGACY, new, sfx="pin")
    assert not _is_danger(out)
    assert "pv-bos-svc-a" in out, \
        "the card must name the instance actually being adopted"


def test_legacy_keys_removed_with_kcc_already_live_is_routine(monkeypatch):
    """Cleanup, not adoption: KCC was already enabled at base, so this PR
    only drops the dead legacy block. No card, no danger."""
    old = LEGACY.rstrip("\n") + "\n" + ADOPTED.split("  infra:\n", 1)[1]
    new = ADOPTED
    out = _panel(monkeypatch, old, new, sfx="cleanup")
    assert not _is_danger(out)
    assert "ADOPTION" not in out.upper(), \
        "nothing is being adopted here, the VM was already KCC-managed"


def test_windows_and_azure_domains_are_untouched(monkeypatch):
    """Adoption detection is scoped to the linux keys. A Windows change in
    the same file must keep behaving exactly as before."""
    old = "appspace:\n  customerName: bos\n  infra:\n" \
          "    deployWindows:\n      machineType: n2d-standard-4\n"
    new = "appspace:\n  customerName: bos\n  infra:\n" \
          "    deployWindows:\n      machineType: n2d-standard-8\n"
    out = _panel(monkeypatch, old, new, sfx="win")
    assert _is_danger(out), \
        "a real Windows resize must still block; adoption logic is linux-only"


def test_two_environments_in_one_pr_each_get_a_card(monkeypatch):
    ident2 = "gcp/prod/private-cloud/ap1-b/monthly/pv-bat-a/customer.yaml"
    b, h = "mainmulti", "prmulti"
    store = {(IDENT, b): LEGACY, (IDENT, h): ADOPTED,
             (ident2, b): LEGACY, (ident2, h): ADOPTED}

    def fake(path, sha, repo=None):
        v = store.get((path, sha))
        return (v, m.BB_OK) if v is not None else (None, m.BB_NOT_FOUND)

    monkeypatch.setattr(m, "_bb_fetch_status", fake)
    out = "\n".join(m._summarize_vm_changes(
        [IDENT, ident2], h, b,
        {IDENT: ["pv-bos-b-ss"], ident2: ["pv-bat-a-ss"]}, []))
    assert not _is_danger(out)
    assert out.upper().count("ADOPTION") >= 2, \
        "one card per environment"
