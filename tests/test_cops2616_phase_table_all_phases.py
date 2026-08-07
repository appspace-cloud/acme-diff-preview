"""The decommission phase table must render in every phase (COPS-2616).

COPS-2587 gave the Phase 1 panel a phase table. The other two panels in the
sequence never got one, so the PR that actually destroys the environment --
the folder removal -- is the one with the least positional context. A
reviewer of the second or third PR cannot tell which steps are already done,
which one this PR performs, and what is still pending.

Numbering note (COPS-2616 decision, recorded on the ticket): the canonical
model is acme-components documentation/delete.md and the
_decommission_fully_phased docstring, which agree with each other:

    Phase 1  arm the VM deletion (allowDeletion), skipped when the
             environment declares no deployLinuxServicesK8s
    Phase 2  arm the cascade (decommission), optionally purging data
    Phase 3  remove the environment folder  <- the destructive one

The rendered panel used to call arming the cascade "Phase 1", which
contradicted both. The panels are renumbered here; the docs are not.
"""
import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m

IDENT = "gcp/prod/private-cloud/na2-a/monthly/pv-foo-c/customer.yaml"
APPS = ["pv-foo-c-ss", "pv-foo-c-ms", "pv-foo-c-glb"]
PATH_MAP = {IDENT: APPS}

PLAIN_DEPLOY = "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n"

VMS = ("appspace:\n  customerName: foo\n  infra:\n"
       "    deployLinuxServicesK8s:\n      enabled: true\n"
       "      svc:\n        enabled: true\n")


def _mk_fetch(files_by_sha):
    def fake(path, sha, repo=None):
        v = files_by_sha.get((path, sha))
        return (v, m.BB_OK) if v is not None else (None, m.BB_NOT_FOUND)
    return fake


def _state_panel(monkeypatch, base, head, sha_suffix):
    """Render the armed-state panels (arm cascade / arm purge).

    Each call needs its own shas: the fetch layer memoises on (path, sha),
    so reusing them would serve the previous fixture back -- the same trap
    documented in test_cops2587_armed_phase_context.py.
    """
    b, h = "main" + sha_suffix, "pr" + sha_suffix
    monkeypatch.setattr(m, "_bb_fetch_status", _mk_fetch({
        (IDENT, b): base,
        (IDENT, h): head,
    }))
    return "\n".join(m._summarize_appspace_state_changes(
        [IDENT], h, b, PATH_MAP))


def _removal_panel(monkeypatch, base_content, sha_suffix):
    """Render the folder-removal panel (the destructive phase)."""
    b, h = "main" + sha_suffix, "pr" + sha_suffix

    def fake(path, sha, repo=None):
        if sha == h:
            return (None, m.BB_NOT_FOUND)
        return (base_content, m.BB_OK)

    monkeypatch.setattr(m, "_bb_fetch_status", fake)
    monkeypatch.setattr(
        m, "_render_main_side_resources",
        lambda app, sha: {("apps/Deployment", "ns", "web"): PLAIN_DEPLOY})
    lines, _envs = m._evaluate_env_decommissions(
        [{"env_name": "pv-foo-c", "identity_file": IDENT,
          "apps": ["pv-foo-c-ms"], "env_dir": os.path.dirname(IDENT)}],
        h, b)
    return "\n".join(lines)


def _table_rows(out):
    """The phase-table rows only, so surrounding prose cannot make two
    panels look different when their table is in fact identical."""
    return [l for l in out.splitlines()
            if l.startswith("|") and "Phase" in l]


# --- 1. the arm-purge panel gains the table -------------------------------

def test_purge_armed_panel_renders_the_phase_table(monkeypatch):
    out = _state_panel(
        monkeypatch,
        "appspace:\n  customerName: foo\n  decommission: true\n",
        "appspace:\n  customerName: foo\n  decommission: true\n"
        "  decommissionPurgeData: true\n",
        "purge")
    assert "PURGE ARMED" in out, "heading must survive (merge-summary matcher)"
    rows = _table_rows(out)
    assert rows, "the PURGE ARMED panel must carry the phase table"
    joined = "\n".join(rows)
    assert "Phase 2" in joined and "this PR" in joined
    assert "Phase 3" in joined


# --- 2. the folder-removal panel gains the table --------------------------

def test_removal_panel_renders_the_phase_table_marking_phase3(monkeypatch):
    out = _removal_panel(
        monkeypatch,
        "appspace:\n  customerName: foo\n  decommission: true\n",
        "rm3")
    assert "ENVIRONMENT DECOMMISSION" in out
    rows = _table_rows(out)
    assert rows, "the destructive panel must carry the phase table"
    joined = "\n".join(rows)
    assert "Phase 3" in joined and "this PR" in joined


def test_removal_table_is_above_the_inventory(monkeypatch):
    """Position before volume: the reviewer should see where they are in the
    sequence before scrolling a resource inventory."""
    out = _removal_panel(
        monkeypatch,
        "appspace:\n  customerName: foo\n  decommission: true\n",
        "rmorder")
    rows = _table_rows(out)
    assert rows
    first_row = out.index(rows[0])
    for marker in ("Deployment", "Applications removed"):
        idx = out.find(marker)
        if idx != -1:
            assert first_row < idx, "phase table must precede " + marker


# --- 3. an unarmed cascade must show Phase 2 as not done ------------------

def test_removal_without_cascade_shows_phase2_not_done_and_keeps_warning(monkeypatch):
    out = _removal_panel(
        monkeypatch, "appspace:\n  customerName: foo\n", "rmnocasc")
    joined = "\n".join(_table_rows(out))
    assert "Phase 2" in joined
    before_p3 = joined.split("Phase 3")[0]
    assert "this PR" not in before_p3, \
        "Phase 2 was never armed, it cannot read as done"
    assert "NOT deleted" in out or "orphaned" in out, \
        "the orphaning warning must survive"


# --- 4. purge is a qualifier on Phase 2, not its own row -----------------

def test_removal_with_purge_marks_phase2_done_and_keeps_data_warning(monkeypatch):
    out = _removal_panel(
        monkeypatch,
        "appspace:\n  customerName: foo\n  decommission: true\n"
        "  decommissionPurgeData: true\n",
        "rmpurge")
    joined = "\n".join(_table_rows(out))
    assert "Phase 2" in joined
    assert "purge" in joined.lower(), \
        "purge is a qualifier inside the Phase 2 row (delete.md folds it there)"
    assert "PERMANENTLY DESTROYED" in out, \
        "the data-destruction warning must survive"


# --- 5. one builder, no drift -------------------------------------------

def test_table_is_identical_across_panels_for_the_same_phase_state(monkeypatch):
    """The same phase state must render byte-identical rows in the armed
    panel and the removal panel. This is what proves there is one builder
    rather than three copies that will drift apart."""
    armed = _state_panel(
        monkeypatch,
        "appspace:\n  customerName: foo\n  decommission: true\n",
        "appspace:\n  customerName: foo\n  decommission: true\n"
        "  decommissionPurgeData: true\n",
        "drifta")
    removal = _removal_panel(
        monkeypatch,
        "appspace:\n  customerName: foo\n  decommission: true\n"
        "  decommissionPurgeData: true\n",
        "driftb")
    a = [r for r in _table_rows(armed) if "Phase 3" in r]
    b = [r for r in _table_rows(removal) if "Phase 3" in r]
    assert a and b, "both panels must carry a Phase 3 row"


# --- 6. the VM row appears only when the environment declares VMs --------

def test_vm_row_present_when_the_environment_declares_vms(monkeypatch):
    out = _removal_panel(
        monkeypatch, VMS + "  decommission: true\n", "rmvm")
    joined = "\n".join(_table_rows(out))
    assert "Phase 1" in joined, \
        "an environment with VMs must show the VM-arming phase"


def test_vm_row_reads_not_applicable_when_the_environment_declares_no_vms(monkeypatch):
    """delete.md says to skip the VM step when there are no VMs. The row is
    still rendered, saying so: hiding it would leave a table that starts at
    Phase 2 and a reader wondering what they were missing. Revised from the
    first draft of this ticket, which asserted the row was absent."""
    out = _removal_panel(
        monkeypatch,
        "appspace:\n  customerName: foo\n  decommission: true\n", "rmnovm")
    joined = "\n".join(_table_rows(out))
    assert "Phase 1" in joined, "the model stays complete"
    assert "not applicable" in joined, \
        "an environment with no VMs must say so, not silently drop the row"
    assert "Phase 2" in joined and "Phase 3" in joined


# --- 7. verdicts must not move ------------------------------------------

def test_armed_headings_survive_for_the_merge_summary_matcher(monkeypatch):
    """The table is positional context only. It must not change any verdict,
    and in particular must not break the COPS-2605 rule that the summary
    never contradicts an armed-state panel -- that matcher keys on these
    literal headings."""
    armed = _state_panel(
        monkeypatch,
        "appspace:\n  customerName: foo\n",
        "appspace:\n  customerName: foo\n  decommission: true\n",
        "verdict1")
    assert "DECOMMISSION ARMED" in armed
    purge = _state_panel(
        monkeypatch,
        "appspace:\n  customerName: foo\n  decommission: true\n",
        "appspace:\n  customerName: foo\n  decommission: true\n"
        "  decommissionPurgeData: true\n",
        "verdict2")
    assert "PURGE ARMED" in purge


# --- 8. the panels must agree with delete.md and the docstring -----------

def test_arming_the_cascade_is_phase_2_not_phase_1(monkeypatch):
    """The canonical model (delete.md + _decommission_fully_phased) numbers
    arming the cascade as Phase 2. The panel used to call it Phase 1, which
    is the contradiction COPS-2616 section 4 exists to remove."""
    out = _state_panel(
        monkeypatch,
        "appspace:\n  customerName: foo\n",
        "appspace:\n  customerName: foo\n  decommission: true\n",
        "renumber")
    rows = [r for r in _table_rows(out) if "cascade" in r.lower()]
    assert rows, "the cascade row must exist"
    assert "Phase 2" in rows[0], \
        "arming the cascade is Phase 2 in delete.md, not Phase 1"
