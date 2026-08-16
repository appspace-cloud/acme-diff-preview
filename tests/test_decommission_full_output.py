"""Audit appendix for properly phased decommissions (v2.26.0).

acme-config-prod PR #3862 (Phase 3 delete of pv-ukhsa-a, 533 resources)
showed the gap: the decommission panel lists counts and names, but the
actual manifests being deleted — already rendered from main inside
_evaluate_env_decommissions — were discarded, so there was no audit
record of WHAT the cascade removed. These tests pin the fix:

  * a "Full rendered output — everything that will be DELETED" appendix,
    returned separately (opt-in) so the panel's 2-tuple contract stays;
  * gated on the delete-phases contract from acme-components
    documentation/delete.md: the cascade must be armed at base (phase 2
    done first) and, when the identity file declares Linux VMs, the VM
    deletion must be armed too (phase 1); anything unphased gets NO
    appendix — orphaning deletes nothing, so there is nothing to audit;
  * retained resources (CRDs, keep-policy) stay OUT of the appendix: it
    lists exactly what the cascade removes, values redacted.
"""
import os, sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m


_ID = "gcp/prod/private-cloud/gb1/custom/pv-audit-a/customer.yaml"

ARMED = "appspace:\n  customerName: pv-audit\n  decommission: true\n"
ARMED_PURGE = ARMED + "  decommissionPurgeData: true\n"
NOT_ARMED = "appspace:\n  customerName: pv-audit\n"
ARMED_VMS_OK = (ARMED +
    "  infra:\n    deployLinuxServicesK8s:\n      defaults:\n"
    "        allowDeletion: true\n"
    "      svc:\n        enabled: true\n")
ARMED_VMS_NOT_OK = (ARMED +
    "  infra:\n    deployLinuxServicesK8s:\n      defaults:\n"
    "        size: e2-small\n"
    "      svc:\n        enabled: true\n")

DEPLOY_DOC = ("kind: Deployment\napiVersion: apps/v1\nmetadata:\n"
              "  name: web\nspec:\n  replicas: 2\n")
SECRET_DOC = ("kind: Secret\napiVersion: v1\nmetadata:\n  name: creds\n"
              "type: Opaque\nstringData:\n  dbPass: topsecret-audit-value\n")
CRD_DOC = ("kind: CustomResourceDefinition\napiVersion: apiextensions.k8s.io/v1\n"
           "metadata:\n  name: widgets.example.io\n")

RESOURCES = {
    ("apps/Deployment", "pv-audit-a", "web"): DEPLOY_DOC,
    ("v1/Secret", "pv-audit-a", "creds"): SECRET_DOC,
    ("apiextensions.k8s.io/CustomResourceDefinition", "", "widgets.example.io"): CRD_DOC,
}


def _candidate():
    return {"env_name": "pv-audit-a", "identity_file": _ID,
            "apps": ["pv-audit-a-ms"]}


def _fetch_table(monkeypatch, base_content):
    table = {(_ID, "prsha"): (None, m.BB_NOT_FOUND),
             (_ID, "mainsha"): (base_content, m.BB_OK)}
    def fetch(f, sha, repo=None):
        # COPS-2677: Phase 1 walks ancestor config.yaml; missing parents
        # are ignoreMissingValueFiles-shaped (not found), not KeyError.
        return table.get((f, sha), (None, m.BB_NOT_FOUND))
    monkeypatch.setattr(m, "_bb_fetch_cached", fetch)


def _env(monkeypatch, base_content, resources=RESOURCES):
    _fetch_table(monkeypatch, base_content)
    monkeypatch.setattr(m, "_app_chart_revision_map", {"pv-audit-a-ms": "2603.0.14"})
    monkeypatch.setattr(m, "_render_main_side_resources",
                        lambda app, main_sha: dict(resources))


# ── the phase gate ───────────────────────────────────────────────────────────

def test_phase_gate_false_when_cascade_not_armed(monkeypatch):
    _fetch_table(monkeypatch, NOT_ARMED)
    assert m._decommission_fully_phased(_ID, "mainsha") is False

def test_phase_gate_true_when_armed_and_no_vms(monkeypatch):
    _fetch_table(monkeypatch, ARMED)
    assert m._decommission_fully_phased(_ID, "mainsha") is True

def test_phase_gate_true_when_armed_and_vm_deletion_armed(monkeypatch):
    _fetch_table(monkeypatch, ARMED_VMS_OK)
    assert m._decommission_fully_phased(_ID, "mainsha") is True

def test_phase_gate_false_when_vms_declared_but_not_armed(monkeypatch):
    _fetch_table(monkeypatch, ARMED_VMS_NOT_OK)
    assert m._decommission_fully_phased(_ID, "mainsha") is False

def test_phase_gate_fails_closed_on_unreadable_identity(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_cached",
                        lambda f, sha, repo=None: (None, m.BB_ERROR))
    assert m._decommission_fully_phased(_ID, "mainsha") is False


# ── _evaluate_env_decommissions: contract and appendix ──────────────────────

def test_evaluate_decommissions_default_contract_is_two_tuple(monkeypatch):
    _env(monkeypatch, ARMED)
    result = m._evaluate_env_decommissions([_candidate()], "prsha", "mainsha")
    assert len(result) == 2

def test_phased_decommission_gets_the_deleted_manifest_appendix(monkeypatch):
    _env(monkeypatch, ARMED_PURGE)
    lines, envs, full = m._evaluate_env_decommissions(
        [_candidate()], "prsha", "mainsha", with_full_output=True)
    panel = "\n".join(lines)
    appendix = "\n".join(full)
    assert envs == ["pv-audit-a"]
    # the panel keeps its shape: no manifest wall in it
    assert "kind: Deployment" not in panel
    # the appendix carries exactly what the cascade removes
    assert "everything that will be DELETED" in appendix
    assert "```yaml" in appendix
    assert "kind: Deployment" in appendix
    assert "pv-audit-a-ms" in appendix
    # retained resources are NOT in the appendix
    assert "CustomResourceDefinition" not in appendix
    # secret values are redacted, identity survives
    assert "topsecret-audit-value" not in appendix
    assert "name: creds" in appendix

def test_unarmed_deletion_gets_no_appendix(monkeypatch):
    _env(monkeypatch, NOT_ARMED)
    lines, envs, full = m._evaluate_env_decommissions(
        [_candidate()], "prsha", "mainsha", with_full_output=True)
    assert envs == ["pv-audit-a"]      # the warning itself still fires
    assert full == []

def test_armed_but_vm_unarmed_gets_no_appendix(monkeypatch):
    _env(monkeypatch, ARMED_VMS_NOT_OK)
    lines, envs, full = m._evaluate_env_decommissions(
        [_candidate()], "prsha", "mainsha", with_full_output=True)
    assert envs == ["pv-audit-a"]
    assert full == []

def test_render_failure_still_warns_but_no_appendix(monkeypatch):
    _fetch_table(monkeypatch, ARMED)
    monkeypatch.setattr(m, "_app_chart_revision_map", {"pv-audit-a-ms": "2603.0.14"})
    def boom(app, main_sha):
        raise RuntimeError("chart pull failed")
    monkeypatch.setattr(m, "_render_main_side_resources", boom)
    lines, envs, full = m._evaluate_env_decommissions(
        [_candidate()], "prsha", "mainsha", with_full_output=True)
    assert envs == ["pv-audit-a"]
    assert full == []


# ── orchestrator: the appendix reaches the comment and the artifact ─────────

_PR_SHA, _BASE = "aabbccddeeff0011", "112233445566mm77"

def _mk_pr(pr_id):
    return {"id": pr_id, "title": "synthetic decommission",
            "source": {"commit": {"hash": _PR_SHA}, "branch": {"name": "b"}},
            "destination": {"branch": {"name": "main"}}}

def test_process_pr_threads_the_audit_appendix_through(monkeypatch):
    m._seen.clear(); m._force_recompute.clear()
    sinks = {"upserts": [], "artifacts": []}
    m._app_chart_map.update({"pv-audit-a-ms": "appspace-ms"})
    m._app_chart_revision_map.update({"pv-audit-a-ms": "2603.0.14"})
    pm = {_ID: ["pv-audit-a-ms"]}
    monkeypatch.setattr(m, "get_pr_changed_files", lambda pr_id, repo=None: ([_ID], {}))
    monkeypatch.setattr(m, "find_existing_comment", lambda pr_id, repo=None: (None, "", ""))
    monkeypatch.setattr(m, "fix_stuck_inprogress", lambda *a, **k: None)
    monkeypatch.setattr(m, "_touch_progress", lambda: None)
    monkeypatch.setattr(m, "upsert_comment",
                        lambda pr_id, body, existing_id=None, repo=None,
                        artifact_url="":
                        sinks["upserts"].append(body) or 1)
    monkeypatch.setattr(m, "post_build_status", lambda *a, **k: None)
    monkeypatch.setattr(m, "_save_diff_ui_artifact",
                        lambda repo, pr_id, pr_sha, body, **kw:
                        sinks["artifacts"].append(body))
    monkeypatch.setattr(m, "_detect_new_env_candidates", lambda *a, **k: [])
    monkeypatch.setattr(m, "_summarize_input_changes", lambda *a, **k: [])
    monkeypatch.setattr(m, "_summarize_appspace_state_changes", lambda *a, **k: [])
    monkeypatch.setattr(m, "_changed_files_with_bad_names", lambda *a, **k: {})
    monkeypatch.setattr(m, "_detect_env_decommission_candidates",
                        lambda *a, **k: [_candidate()])
    monkeypatch.setattr(m, "_render_main_side_resources",
                        lambda app, main_sha: dict(RESOURCES))
    table = {(_ID, _PR_SHA): (None, m.BB_NOT_FOUND),
             (_ID, _BASE): (ARMED, m.BB_OK)}
    monkeypatch.setattr(m, "_bb_fetch_cached",
                        lambda f, sha, repo=None: table.get((f, sha), (None, m.BB_NOT_FOUND)))
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda fp, sha, **kw: table.get((fp, sha), (None, m.BB_NOT_FOUND)))
    monkeypatch.setattr(m, "argocd_diff",
                        lambda app, pr_sha, main_sha, chart_revision=None,
                               changed_paths=None, renames=None:
                        m.DiffResult("", [], 0, False, "", m.OUT_NO_DIFF, ""))
    try:
        m.process_pr(_mk_pr(9100), pm, base_sha=_BASE)
    finally:
        m._seen.clear(); m._force_recompute.clear()
    body = sinks["artifacts"][0]
    assert "ENVIRONMENT DECOMMISSION" in body
    assert "everything that will be DELETED" in body
    assert "kind: Deployment" in body
    assert "topsecret-audit-value" not in body
    # Superseded contract (was: artifact == comment). The artifact is now
    # the COMPLETE record and the comment is the scannable summary that
    # points at it: inlining hundreds of lines of rendered manifest is what
    # made decommission comments unreadable (acme-config-prod PR #3894).
    # The appendix must therefore be in the artifact and NOT in the comment.
    comment = sinks["upserts"][0]
    assert "kind: Deployment" not in comment, \
        "the raw manifest dump must not be inlined in the comment"
    assert "Full rendered output" in comment, "the comment must point at it"
    assert "ENVIRONMENT DECOMMISSION" in comment, "the warning still shouts"
    assert len(body) > len(comment)


def test_phase_gate_fails_closed_when_refetch_degrades(monkeypatch):
    # The gate re-reads the identity file after the cascade check; if the
    # cache degrades between the two reads (eviction under pressure), the
    # answer must be the conservative one, never a stale True.
    calls = {"n": 0}
    def flaky(f, sha, repo=None):
        calls["n"] += 1
        return (ARMED, m.BB_OK) if calls["n"] == 1 else (None, m.BB_ERROR)
    monkeypatch.setattr(m, "_bb_fetch_cached", flaky)
    assert m._decommission_fully_phased(_ID, "mainsha") is False

def test_phase_gate_fails_closed_when_reparse_breaks(monkeypatch):
    calls = {"n": 0}
    def flaky(f, sha, repo=None):
        calls["n"] += 1
        return (ARMED, m.BB_OK) if calls["n"] == 1 else ("::\nnot: [valid", m.BB_OK)
    monkeypatch.setattr(m, "_bb_fetch_cached", flaky)
    assert m._decommission_fully_phased(_ID, "mainsha") is False
