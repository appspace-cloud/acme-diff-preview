"""The DECOMMISSION ARMED panel must teach the phase model (COPS-2587).

COPS-2584 made the arm visible; this makes it understandable. A reviewer of
a Phase 1 PR (e.g. acme-config-prod #3860, the pv-ukhsa-a arm) should not
need the runbook from memory to answer: what happens right now (nothing --
the environment stays fully live and billing), what happens next (Phase 3,
a later folder-removal PR, is what actually deletes), what must be checked
before Phase 3 (nothing manual: the phase model guarantees the finalizer,
and Phase 2's state is read from the environment's own config and stated
outright), and what survives even a full cascade (the content backup
bucket, per the runbook).

2026-08-06: the panel is now a phase TABLE. The prose version rendered as
an unreadable wall in Bitbucket -- the phase list had no blank line before
it, so markdown inlined the bullets -- and it told the reviewer to run a
kubectl finalizer check by hand. Same facts, scannable shape.

Everything here is additive text inside the existing ARMED branch. Every
COPS-2584 assertion must keep passing untouched.
"""
import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m

IDENT = "gcp/prod/private-cloud/gb1-b/weekly/pv-ukhsa-a/customer.yaml"
APPS = ["pv-ukhsa-a-ss", "pv-ukhsa-a-ms", "pv-ukhsa-a-glb"]
PATH_MAP = {IDENT: APPS}


def _mk_fetch(files_by_sha):
    def fake(path, sha, repo=None):
        v = files_by_sha.get((path, sha))
        return (v, m.BB_OK) if v is not None else (None, m.BB_NOT_FOUND)
    return fake


def _armed_output(monkeypatch, purge=False):
    new = "appspace:\n  decommission: true\n  customerName: ukhsa\n"
    if purge:
        new = ("appspace:\n  decommission: true\n  decommissionPurgeData: true\n"
               "  customerName: ukhsa\n")
    monkeypatch.setattr(m, "_bb_fetch_status", _mk_fetch({
        (IDENT, "mainsha"): "appspace:\n  customerName: ukhsa\n",
        (IDENT, "prsha"):   new,
    }))
    return "\n".join(m._summarize_appspace_state_changes(
        [IDENT], "prsha", "mainsha", PATH_MAP))


def test_armed_names_the_phase_model(monkeypatch):
    out = _armed_output(monkeypatch)
    assert "Phase 1" in out
    assert "Phase 3" in out


def test_armed_says_the_environment_stays_fully_live_right_now(monkeypatch):
    out = _armed_output(monkeypatch)
    low = out.lower()
    assert "keeps running" in low or "stays fully live" in low or "keep running" in low
    assert "billing" in low or "costing" in low or "costs" in low
    assert "managed by argocd" in low or "still managed" in low


def test_armed_states_phase2_from_config_instead_of_asking_for_a_check(monkeypatch):
    """Superseded contract (was: the panel must print a kubectl finalizer
    verification). The reviewer should not be sent to run commands: the
    phase model guarantees the finalizer, and Phase 2's state is already
    known from the environment's own configuration, so the panel states it
    outright. Asked for directly by the operators reading these comments,
    after seeing the rendered panel on acme-config-prod PR #3893."""
    out = _armed_output(monkeypatch)
    assert "kubectl" not in out, "do not send the reviewer to run commands"
    assert "finalizers" not in out
    assert "not armed" in out, "Phase 2 state must be stated, not checked"
    # The purge-armed wording is pinned by
    # test_armed_with_purge_still_carries_the_data_destruction_note; calling
    # the helper twice here would hit the (path, sha) fetch cache and read
    # back the first fixture.


def test_armed_says_what_survives_a_full_cascade(monkeypatch):
    out = _armed_output(monkeypatch)
    low = out.lower()
    assert "content backup bucket" in low
    assert "documentation/" in low or "acme-components" in low


def test_armed_points_at_the_phase3_inventory_panel(monkeypatch):
    """The rendered what-gets-deleted inventory lives on the Phase 3 PR.
    Phase 1 must say so instead of duplicating it."""
    out = _armed_output(monkeypatch)
    low = out.lower()
    assert "inventory" in low or "its own" in low


def test_armed_with_purge_still_carries_the_data_destruction_note(monkeypatch):
    out = _armed_output(monkeypatch, purge=True)
    assert "permanently destroy" in out.lower()
    assert "Phase 1" in out


def test_cops2584_wording_contract_still_holds(monkeypatch):
    """The exact phrases the COPS-2584 tests assert on must survive."""
    out = _armed_output(monkeypatch)
    assert "ARMED" in out.upper()
    assert "appspace.decommission" in out
    low = out.lower()
    assert ("deletes nothing" in low or "nothing is deleted" in low
            or "nothing by itself" in low)
    for app in APPS:
        assert app in out


def test_disarmed_panel_now_carries_the_table_too(monkeypatch):
    """Superseded scope guard. COPS-2587 kept the phase table out of the
    DISARMED branch to hold its own change small, and COPS-2710 reversed
    that on purpose: every PR in the sequence renders the same three rows
    with only the marks moving, and a rollback is exactly when someone is
    recovering from a mistake and most needs to see where they now are.

    What this test still guards is the part that was never about scope: the
    reviewer is not sent to run commands.
    """
    monkeypatch.setattr(m, "_bb_fetch_status", _mk_fetch({
        (IDENT, "mainsha"): "appspace:\n  decommission: true\n  customerName: ukhsa\n",
        (IDENT, "prsha"):   "appspace:\n  customerName: ukhsa\n",
    }))
    out = "\n".join(m._summarize_appspace_state_changes(
        [IDENT], "prsha", "mainsha", PATH_MAP))
    assert "DISARMED" in out.upper()
    assert "kubectl" not in out
    phase2 = next(l for l in out.splitlines() if "**Phase 2" in l)
    assert m._PH_UNDONE in phase2, phase2


def test_paused_panel_is_unchanged(monkeypatch):
    """Scope guard: the autosync panels must not pick up phase chatter."""
    monkeypatch.setattr(m, "_bb_fetch_status", _mk_fetch({
        (IDENT, "mainsha"): "appspace:\n  customerName: ukhsa\n",
        (IDENT, "prsha"):   "appspace:\n  autosync: false\n  customerName: ukhsa\n",
    }))
    out = "\n".join(m._summarize_appspace_state_changes(
        [IDENT], "prsha", "mainsha", PATH_MAP))
    assert "PAUSED" in out.upper()
    assert "Phase 1" not in out
