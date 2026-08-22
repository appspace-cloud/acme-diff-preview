"""Undoing a decommission phase must render the table too (COPS-2710).

Reported from acme-config-prod #4385, which removes `allowDeletion` from
`pv-gsk--aec1-b` for COPS-2706. That is Phase 1 of the runbook being taken
back, and the comment showed no phase table at all: verdict Routine, one
routine VM panel, nothing about where the environment now sits.

COPS-2616 established that every PR in the sequence renders the same three
rows with only the marks moving, so a reviewer can always answer "where am
I". That held going forwards and broke going backwards, which is exactly
when someone is recovering from a mistake and most needs the map. Three
paths were missing it:

    Phase 1 disarm   nothing at all
    Phase 2 disarm   a panel, but no table
    purge disarm     one italic line, no table

A fourth defect fell out of building the first: `_vm_config_stripped`
counted the removal of `allowDeletion` as stripping the VM config, so
undoing Phase 1 on an environment whose cascade was armed rendered
"VM CONFIG STRIPPED WHILE ARMING DECOMMISSION" and told the operator to do
the opposite of what they were doing. The arming flag cannot be the config
the arming acts through: helm keeps rendering every CR, they just go back to
`deletion-policy: abandon`.

No verdict changes here. `delete` back to `abandon` is the safe direction,
the VM panel already reports it as routine, and the table is positional
context (COPS-2616).
"""
import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import comment_render  # noqa: E402
import diff_preview as m  # noqa: E402
import vm_analysis  # noqa: E402

IDENT = "gcp/aec/private-cloud/na4-a/pv-gsk--aec1-b/customer.yaml"
CL_IDENT = "gcp/prod/public-cloud/na1-a/cl-prod-b/constellation/customer.yaml"
APPS = ["pv-gsk--aec1-b-ms", "pv-gsk--aec1-b-ss"]

VM = ("  infra:\n    deployLinuxServicesK8s:\n      enabled: true\n"
      "      svc:\n        enabled: true\n")
VM_ARMED = ("  infra:\n    deployLinuxServicesK8s:\n      defaults:\n"
            "        allowDeletion: true\n      enabled: true\n"
            "      svc:\n        enabled: true\n")


def _panel(monkeypatch, base_yaml, head_yaml, tag, ident=IDENT, apps=None):
    """Fresh shas per call: the fetch layer memoises on (path, sha)."""
    b, h = "base" + tag, "pr" + tag
    files = {(ident, b): base_yaml, (ident, h): head_yaml}
    monkeypatch.setattr(
        m, "_bb_fetch_status",
        lambda p, s, repo=None: (files[(p, s)], m.BB_OK)
        if (p, s) in files else (None, m.BB_NOT_FOUND))
    return "\n".join(m._summarize_appspace_state_changes(
        [ident], h, b, {ident: apps or APPS}))


def _row(panel, phase):
    rows = [l for l in panel.splitlines()
            if l.startswith("|") and f"**Phase {phase}" in l]
    assert rows, f"no Phase {phase} row in:\n{panel}"
    cells = [c.strip() for c in rows[0].strip().strip("|").split("|")]
    return cells[1]


# ── 1. Phase 1 undone, the reported shape ────────────────────────────────

def test_removing_allow_deletion_renders_the_phase_table(monkeypatch):
    out = _panel(monkeypatch,
                 "appspace:\n  customerName: g\n" + VM_ARMED,
                 "appspace:\n  customerName: g\n" + VM, "p1undo")
    assert "PHASE 1 UNDONE" in out, out
    assert _row(out, 1) == m._PH_UNDONE
    assert _row(out, 2) == m._PH_PENDING
    assert _row(out, 3) == m._PH_PENDING


def test_the_undone_panel_says_what_changed_in_gcp(monkeypatch):
    """The reviewer's question is what this does to the live resources, and
    the answer is the deletion policy going back."""
    out = _panel(monkeypatch,
                 "appspace:\n  customerName: g\n" + VM_ARMED,
                 "appspace:\n  customerName: g\n" + VM, "p1what").lower()
    assert "abandon" in out
    assert "safe direction" in out


def test_undoing_phase_1_under_an_armed_cascade_warns_it_must_be_re_armed(
        monkeypatch):
    """The teardown is still live here. Leaving Phase 1 undone and then
    removing the folder orphans the VM instead of deleting it."""
    out = _panel(
        monkeypatch,
        "appspace:\n  customerName: g\n  decommission: true\n" + VM_ARMED,
        "appspace:\n  customerName: g\n  decommission: true\n" + VM,
        "p1armed")
    assert _row(out, 1) == m._PH_UNDONE
    assert _row(out, 2) == m._PH_DONE
    assert "armed again" in out, out


def test_no_undone_panel_on_public_cloud(monkeypatch):
    """COPS-2701: the private Phase 1/2/3 model does not exist on cl-*."""
    out = _panel(monkeypatch,
                 "appspace:\n  customerName: g\n" + VM_ARMED,
                 "appspace:\n  customerName: g\n" + VM, "p1cl",
                 ident=CL_IDENT, apps=["cl-prod-b-ms"])
    assert "PHASE 1 UNDONE" not in out, out


# ── 2. the other two undo paths ──────────────────────────────────────────

def test_disarming_the_cascade_now_renders_the_table(monkeypatch):
    out = _panel(
        monkeypatch,
        "appspace:\n  customerName: g\n  decommission: true\n" + VM_ARMED,
        "appspace:\n  customerName: g\n" + VM_ARMED, "p2undo")
    assert "DISARMED" in out.upper()
    assert _row(out, 1) == m._PH_DONE, "an earlier PR armed it, untouched here"
    assert _row(out, 2) == m._PH_UNDONE
    assert _row(out, 3) == m._PH_PENDING


def test_softening_the_purge_renders_the_table_with_the_cascade_still_done(
        monkeypatch):
    """The purge is a qualifier on Phase 2 (COPS-2669), not a phase, so
    removing it leaves Phase 2 done and drops the destruction note."""
    out = _panel(
        monkeypatch,
        "appspace:\n  customerName: g\n  decommission: true\n"
        "  decommissionPurgeData: true\n" + VM_ARMED,
        "appspace:\n  customerName: g\n  decommission: true\n" + VM_ARMED,
        "purgeundo")
    assert _row(out, 2) == m._PH_DONE
    row2 = [l for l in out.splitlines() if "**Phase 2" in l][0]
    assert "permanently destroy" not in row2.lower(), row2
    assert "not armed" in row2, row2


# ── 3. the false positive undoing Phase 1 exposed ────────────────────────

def test_removing_the_arming_flag_is_not_stripping_the_vm_config():
    """`allowDeletion` cannot be the config the arming acts through. Helm
    keeps rendering every CR; they just go back to abandon."""
    old = {
        "appspace.infra.deployLinuxServicesK8s.enabled": "true",
        "appspace.infra.deployLinuxServicesK8s.svc.enabled": "true",
        "appspace.infra.deployLinuxServicesK8s.defaults.allowDeletion": "true",
    }
    new = {k: v for k, v in old.items() if not k.endswith("allowDeletion")}
    assert vm_analysis._vm_config_stripped(old, new) == []


def test_removing_the_azure_confirmation_flag_is_not_a_strip_either():
    old = {
        "appspace.infra.deployLinuxServicesK8s.enabled": "true",
        "appspace.infra.deployLinuxServicesK8s.defaults.confirmProdDeletion":
            "true",
    }
    new = {"appspace.infra.deployLinuxServicesK8s.enabled": "true"}
    assert vm_analysis._vm_config_stripped(old, new) == []


def test_a_real_strip_is_still_caught():
    """The control. COPS-2660's whole point must survive the fix above."""
    old = {
        "appspace.infra.deployLinuxServicesK8s.enabled": "true",
        "appspace.infra.deployLinuxServicesK8s.svc.enabled": "true",
        "appspace.infra.deployLinuxServicesK8s.svc.machineType": "n2d-standard-2",
    }
    new = {"appspace.infra.deployLinuxServicesK8s.defaults.allowDeletion": "true"}
    stripped = vm_analysis._vm_config_stripped(old, new)
    assert any(k.endswith("svc.enabled") for k in stripped), stripped


def test_undoing_phase_1_does_not_render_the_broken_arming_panel(monkeypatch):
    """End to end for the same defect: before the fix this shape rendered
    VM CONFIG STRIPPED WHILE ARMING DECOMMISSION and advised the operator to
    do the opposite of what they were doing."""
    out = _panel(
        monkeypatch,
        "appspace:\n  customerName: g\n  decommission: true\n" + VM_ARMED,
        "appspace:\n  customerName: g\n  decommission: true\n" + VM,
        "p1notbroken")
    assert comment_render._DECOM_VM_STRIP_HDR not in out, out
    assert _row(out, 1) != m._PH_BROKEN


# ── 4. the table is context, never a verdict ─────────────────────────────

def test_undoing_a_phase_does_not_invent_a_verdict(monkeypatch):
    """COPS-2616 contract. Going back to `abandon` is the safe direction and
    the VM panel already reports it; a second finding would be noise."""
    out = _panel(monkeypatch,
                 "appspace:\n  customerName: g\n" + VM_ARMED,
                 "appspace:\n  customerName: g\n" + VM, "p1verdict")
    joined = "\n".join(m._build_merge_summary(
        {}, {}, [], [], out.splitlines(), [], False))
    assert "DO NOT MERGE" not in joined, joined
    assert "Routine" in joined, joined
