"""The DECOMMISSION ARMED panel must teach the phase model (COPS-2587).

COPS-2584 made the arm visible; this makes it understandable. A reviewer of
a Phase 1 PR (e.g. acme-config-prod #3860, the pv-ukhsa-a arm) should not
need the runbook from memory to answer: what happens right now (nothing --
the environment stays fully live and billing), what happens next (Phase 3,
a later folder-removal PR, is what actually deletes), what must be checked
before Phase 3 (the finalizer must be live on the hub -- the exact
verification whose omission nearly orphaned pv-ukhsa-a), and what survives
even a full cascade (the content backup bucket, per the runbook).

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


def test_armed_includes_the_pre_phase3_finalizer_verification(monkeypatch):
    """The pv-ukhsa-a near-miss: a folder removal merged without the
    finalizer live orphans everything. The panel must carry the check."""
    out = _armed_output(monkeypatch)
    assert "finalizers" in out
    assert "kubectl" in out
    assert "before" in out.lower() and "folder" in out.lower()


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


def test_disarmed_panel_is_unchanged(monkeypatch):
    """Scope guard: only the ARMED branch grows. DISARMED must not pick up
    phase chatter."""
    monkeypatch.setattr(m, "_bb_fetch_status", _mk_fetch({
        (IDENT, "mainsha"): "appspace:\n  decommission: true\n  customerName: ukhsa\n",
        (IDENT, "prsha"):   "appspace:\n  customerName: ukhsa\n",
    }))
    out = "\n".join(m._summarize_appspace_state_changes(
        [IDENT], "prsha", "mainsha", PATH_MAP))
    assert "DISARMED" in out.upper()
    assert "Phase 1" not in out
    assert "kubectl" not in out


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
