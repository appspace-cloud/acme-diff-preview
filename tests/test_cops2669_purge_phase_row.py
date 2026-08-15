"""A purge-arming PR must locate itself without misreporting a phase (COPS-2669).

Left open by COPS-2668 as a product call, and it was the right call to stop:
the two obvious options were both wrong, and the third only became visible
after looking at what the other panels do.

The PURGE ARMED branch fires only when `appspace.decommission` was ALREADY
true at base — that is its own precondition — and the PR under review adds
`decommissionPurgeData`. It passed `cascade_state=_PH_THIS_PR`, telling the
reviewer that this PR arms the cascade. An earlier, separately-reviewed PR
did.

The two obvious fixes:

  * leave it — accurate about nothing, since the statement is simply false;
  * switch to `_PH_DONE` — accurate, but this branch also passes
    `removal_state=None`, so NO row would be marked "this PR" and the table
    would locate the reader nowhere. The table exists to locate the reader.

What the other three panels do settles it. The folder-removal panel marks
`removal_state`, the arming panel marks `cascade_state`, and the standalone
VM-strip panel already reports `cascade_state=_PH_DONE` for a cascade armed
earlier. Every one of them marks the phase it actually performs. The purge
panel was the only one marking a phase it did not.

The reason it looked like a dilemma is that arming the purge is NOT one of
the three phases. It is a qualifier on Phase 2 — which is exactly where the
table already renders it, in that row's note. So the row can carry both
facts: the cascade is done, and this PR is the change adding the purge to it.
No phase is misreported and the reviewer still sees where they are.
"""
import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import decommission
import diff_preview as m
from test_cops2616_phase_table_all_phases import _mk_fetch, IDENT, PATH_MAP

ARMED = "appspace:\n  customerName: foo\n  decommission: true\n"
PURGE = ARMED + "  decommissionPurgeData: true\n"


def _panel(monkeypatch, base, head, tag):
    b, h = "main" + tag, "pr" + tag
    monkeypatch.setattr(m, "_bb_fetch_status",
                        _mk_fetch({(IDENT, b): base, (IDENT, h): head}))
    return "\n".join(m._summarize_appspace_state_changes([IDENT], h, b, PATH_MAP))


def _phase2(panel_text):
    for line in panel_text.splitlines():
        if line.startswith("| **Phase 2"):
            return line
    raise AssertionError("no Phase 2 row in:\n" + panel_text)


# ── the correction ───────────────────────────────────────────────────────

def test_purge_panel_does_not_claim_this_pr_armed_the_cascade():
    """The branch's own precondition is that the cascade was armed at base."""
    rows = decommission._decommission_phase_table(
        vm_state=None, cascade_state=decommission._PH_DONE,
        removal_state=None, declares_vms=False,
        purge=True, purge_this_pr=True)
    phase2 = [r for r in rows if r.startswith("| **Phase 2")][0]
    assert "done" in phase2, (
        "the cascade was armed by an earlier PR; the state must say so")


def test_purge_panel_still_marks_this_pr_somewhere():
    """The table's job is to locate the reader in the sequence. Reporting the
    phase honestly must not cost that."""
    rows = decommission._decommission_phase_table(
        vm_state=None, cascade_state=decommission._PH_DONE,
        removal_state=None, declares_vms=False,
        purge=True, purge_this_pr=True)
    joined = "\n".join(rows)
    assert "this PR" in joined, (
        "no row marks the change under review; the reviewer cannot tell what "
        "they are approving:\n" + joined)


def test_the_marker_lands_on_the_phase_the_purge_qualifies():
    """Arming the purge is not a fourth phase — it is a qualifier on Phase 2,
    which is where the table already renders it."""
    rows = decommission._decommission_phase_table(
        vm_state=None, cascade_state=decommission._PH_DONE,
        removal_state=None, declares_vms=False,
        purge=True, purge_this_pr=True)
    phase2 = [r for r in rows if r.startswith("| **Phase 2")][0]
    assert "this PR" in phase2
    assert "decommissionPurgeData" in phase2


# ── everything else must keep its current wording ────────────────────────

def test_purge_armed_earlier_is_not_marked_as_this_pr():
    """The decommission panel also renders purge=True, for an environment
    whose purge was armed in some earlier PR. It must NOT claim this one."""
    rows = decommission._decommission_phase_table(
        vm_state=None, cascade_state=decommission._PH_DONE,
        removal_state=decommission._PH_THIS_PR, declares_vms=False,
        purge=True)                       # purge_this_pr defaults False
    phase2 = [r for r in rows if r.startswith("| **Phase 2")][0]
    assert "this PR" not in phase2, (
        "only the PR that actually arms the purge may claim it:\n" + phase2)


def test_no_purge_wording_is_untouched():
    rows = decommission._decommission_phase_table(
        vm_state=None, cascade_state=None, removal_state=None,
        declares_vms=False, purge=False)
    phase2 = [r for r in rows if r.startswith("| **Phase 2")][0]
    assert "not armed" in phase2 and "recoverable" in phase2


# ── driven end to end, not just the helper ───────────────────────────────

def test_real_purge_panel_reports_done_and_marks_this_pr(monkeypatch):
    panel = _panel(monkeypatch, ARMED, PURGE, "cops2669a")
    assert "PURGE ARMED" in panel
    row = _phase2(panel)
    assert "done" in row, "the cascade was armed at base:\n" + row
    assert "this PR" in row, "the purge qualifier is this PR's change:\n" + row


def test_real_arming_panel_still_marks_the_cascade_as_this_pr(monkeypatch):
    """The panel that genuinely arms the cascade must be unaffected."""
    panel = _panel(monkeypatch, "appspace:\n  customerName: foo\n",
                   ARMED, "cops2669b")
    row = _phase2(panel)
    assert "this PR" in row, "this PR really does arm the cascade here:\n" + row
