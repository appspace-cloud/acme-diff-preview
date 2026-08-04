"""DECOMMISSION ARMED panel must match the current delete procedure (COPS-2586).

The acme-components runbook was renamed and renumbered:

  * old: environment-decommission-runbook.md — Phase 1 = arm decommission
  * new: delete.md — Phase 1 = allowDeletion, Phase 2 = decommission,
    Phase 3 = remove folder; optional snapshot sits before the phases

PR comments must not pin a specific markdown filename (those keep renaming).
Always link the shared `documentation/` folder.
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

STALE_DOC_PATHS = (
    "environment-decommission-runbook",
    "pausing-auto-sync",
    "environment-delete",
    "pause-autosync.md",
    "delete.md",
)


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


def test_armed_uses_phase2_for_decommission_flag(monkeypatch):
    out = _armed_output(monkeypatch)
    assert "Phase 2" in out
    assert "Phase 3" in out
    assert "Phase 1" in out  # allowDeletion is Phase 1 now
    assert "This is Phase 2 of the delete procedure" in out


def test_armed_mentions_allow_deletion_as_phase1(monkeypatch):
    out = _armed_output(monkeypatch)
    assert "allowDeletion" in out
    assert "deployLinuxServicesK8s" in out


def test_armed_links_documentation_folder_not_a_filename(monkeypatch):
    out = _armed_output(monkeypatch)
    assert m.ACME_COMPONENTS_DOCS_URL in out
    assert "browse/documentation" in out
    for stale in STALE_DOC_PATHS:
        assert stale not in out, f"stale doc path still present: {stale}"


def test_paused_links_documentation_folder(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_status", _mk_fetch({
        (IDENT, "mainsha"): "appspace:\n  customerName: ukhsa\n",
        (IDENT, "prsha"):   "appspace:\n  autosync: false\n  customerName: ukhsa\n",
    }))
    out = "\n".join(m._summarize_appspace_state_changes(
        [IDENT], "prsha", "mainsha", PATH_MAP))
    assert "PAUSED" in out.upper()
    assert m.ACME_COMPONENTS_DOCS_URL in out
    for stale in STALE_DOC_PATHS:
        assert stale not in out


def test_source_has_no_stale_doc_filenames():
    """Guard the whole module, not only the panels exercised above."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "src",
                            "diff_preview.py"), encoding="utf-8").read()
    for stale in (
        "environment-decommission-runbook",
        "pausing-auto-sync",
        "environment-delete.md",
    ):
        assert stale not in src, f"stale filename in source: {stale}"
    assert "browse/documentation" in src
    assert "ACME_COMPONENTS_DOCS_URL" in src
