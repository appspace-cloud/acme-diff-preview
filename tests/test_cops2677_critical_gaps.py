"""COPS-2677: four critical detection gaps closed together.

1. zeroPods + leftover HPAs → BLOCK (HPAs invisible in unified diff)
2. Parent/cohort role-enabled VMs visible to Phase 1 (role-enabled only)
3. KCC Compute* `%!s(<nil>)` → BLOCK + build FAILED; other kinds stay REVIEW
4. Purge panels name soft-delete→0 and backup always-abandon
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_preview as m  # noqa: E402
from comment_render import _build_merge_summary  # noqa: E402
from decommission import _decommission_phase_table, _PH_DONE, _PH_THIS_PR  # noqa: E402
from manifest import _is_kcc_blocking_artifact  # noqa: E402
from vm_analysis import (  # noqa: E402
    _count_hpas_remaining,
    _detect_workload_shutdown,
)


def _added_zero(product="core-platform"):
    return (
        "--- \n+++ \n@@ -21,6 +21,7 @@\n"
        f"     app.kubernetes.io/product-area: {product}\n"
        " spec:\n"
        "   \n"
        "+  replicas: 0\n"
        "   \n"
        "   strategy:\n"
        "     rollingUpdate:\n"
    )


def _result(sections, zeroed=None, stats=None, artifacts=None):
    return m.DiffResult(
        text="x", sections=sections, n_res=len(sections), has_diff=True,
        error=None, outcome=m.OUT_DIFF, reason=None,
        version_change=None, deleted_resources=[],
        replicas_zeroed=zeroed or [h for h, _ in sections],
        fingerprint="fp", renamed_resources=[], vm_changes=[],
        version_fold=None, shutdown_stats=stats,
        template_artifacts=artifacts)


def _summary(results):
    return "\n".join(_build_merge_summary(results, {}, [], [], [], [], None))


# ── 1. zeroPods + HPA ─────────────────────────────────────────────────────

def test_hpas_remaining_counted_from_pr_resources_not_diff():
    """Unchanged HPAs never appear in the unified diff."""
    sections = [
        ("/apps/Deployment a", _added_zero()),
        ("/apps/Deployment b", _added_zero()),
    ]
    pr_resources = {
        ("apps/Deployment", "ns", "a"): "x",
        ("apps/Deployment", "ns", "b"): "x",
        ("autoscaling/HorizontalPodAutoscaler", "ns", "a"): "x",
        ("autoscaling/HorizontalPodAutoscaler", "ns", "b"): "x",
    }
    stats = _detect_workload_shutdown(sections, pr_resources=pr_resources)
    assert stats == {"zeroed": 2, "workloads": 2, "hpas_remaining": 2}
    assert _count_hpas_remaining(pr_resources) == 2


def test_clean_shutdown_without_hpas_stays_review():
    sections = [
        ("/apps/Deployment a", _added_zero()),
        ("/apps/Deployment b", _added_zero()),
    ]
    stats = {"zeroed": 2, "workloads": 2, "hpas_remaining": 0}
    out = _summary({"pv-x-ms": _result(sections, stats=stats)})
    assert "Environment shutting down" in out
    assert "HorizontalPodAutoscaler" not in out
    assert "DO NOT MERGE" not in out


def test_shutdown_with_hpas_is_review_not_block():
    """After COPS-2548, hibernation works with leftover HPAs — do not FAILED.

    REVIEW still names the leftover HPAs so reviewers know the chart gate
    (skip hpa.yaml under zeroPods) is the cleanup path.
    """
    sections = [
        ("/apps/Deployment a", _added_zero()),
        ("/apps/Deployment b", _added_zero()),
    ]
    stats = {"zeroed": 2, "workloads": 2, "hpas_remaining": 3}
    out = _summary({"pv-x-ms": _result(sections, stats=stats)})
    assert "Environment shutting down" in out
    assert "3 HorizontalPodAutoscaler" in out
    assert "DO NOT MERGE" not in out
    assert "Review before merging" in out


# ── 2. cohort / parent VMs ────────────────────────────────────────────────

_ID = "gcp/prod/private-cloud/eu1-b/weekly/pv-cohort-a/customer.yaml"
_PARENT = "gcp/prod/private-cloud/eu1-b/weekly/config.yaml"
_REGION = "gcp/prod/private-cloud/eu1-b/config.yaml"


def _clear_yaml_cache():
    m._yaml_cache.clear()


def test_parent_enabled_role_without_allowDeletion_is_not_fully_phased(
        monkeypatch):
    _clear_yaml_cache()
    identity = ("appspace:\n  customerName: pv-cohort\n"
                "  decommission: true\n")
    parent = ("appspace:\n  infra:\n    deployLinuxServicesK8s:\n"
              "      svc:\n        enabled: true\n"
              "        instanceName: pv-cohort-svc\n")
    table = {
        (_ID, "mainsha"): (identity, m.BB_OK),
        (_PARENT, "mainsha"): (parent, m.BB_OK),
    }
    monkeypatch.setattr(
        m, "_bb_fetch_cached",
        lambda f, sha, repo=None: table.get((f, sha), (None, m.BB_NOT_FOUND)))
    assert m._decommission_fully_phased(_ID, "mainsha") is False


def test_parent_sa_defaults_alone_do_not_require_phase1(monkeypatch):
    """Region SA/snapshot keys must not false-positive every env."""
    _clear_yaml_cache()
    identity = ("appspace:\n  customerName: pv-cohort\n"
                "  decommission: true\n")
    region = ("appspace:\n  infra:\n    deployLinuxServicesK8s:\n"
              "      enabled: true\n"
              "      defaults:\n"
              "        serviceAccountEmail: sa@x.iam.gserviceaccount.com\n"
              "        snapshotPolicies: [daily]\n")
    table = {
        (_ID, "mainsha"): (identity, m.BB_OK),
        (_REGION, "mainsha"): (region, m.BB_OK),
    }
    monkeypatch.setattr(
        m, "_bb_fetch_cached",
        lambda f, sha, repo=None: table.get((f, sha), (None, m.BB_NOT_FOUND)))
    assert m._decommission_fully_phased(_ID, "mainsha") is True


def test_parent_enabled_role_with_allowDeletion_on_identity_is_phased(
        monkeypatch):
    _clear_yaml_cache()
    identity = ("appspace:\n  customerName: pv-cohort\n"
                "  decommission: true\n"
                "  infra:\n    deployLinuxServicesK8s:\n"
                "      defaults:\n        allowDeletion: true\n")
    parent = ("appspace:\n  infra:\n    deployLinuxServicesK8s:\n"
              "      svc:\n        enabled: true\n")
    table = {
        (_ID, "mainsha"): (identity, m.BB_OK),
        (_PARENT, "mainsha"): (parent, m.BB_OK),
    }
    monkeypatch.setattr(
        m, "_bb_fetch_cached",
        lambda f, sha, repo=None: table.get((f, sha), (None, m.BB_NOT_FOUND)))
    assert m._decommission_fully_phased(_ID, "mainsha") is True


# ── 3. KCC nil BLOCK vs other REVIEW ──────────────────────────────────────

def _added(line):
    return ("--- \n+++ \n@@ -8,6 +8,7 @@\n"
            " metadata:\n"
            "   labels:\n"
            f"+{line}\n"
            " spec:\n")


def test_kcc_compute_nil_blocks():
    hdr = "/compute.cnrm.cloud.google.com/ComputeInstance vm-a"
    assert _is_kcc_blocking_artifact(hdr)
    out = _summary({"pv-stage1-a-ss": _result(
        [(hdr, _added("    hosting-id: hst-%!s(<nil>)"))],
        zeroed=[], stats=None, artifacts=[hdr])})
    assert "Unresolved KCC value" in out
    assert "DO NOT MERGE" in out


def test_configmap_nil_stays_review():
    hdr = "/v1/ConfigMap app"
    assert not _is_kcc_blocking_artifact(hdr)
    out = _summary({"pv-x-ms": _result(
        [(hdr, _added("    tenant: <no value>"))],
        zeroed=[], stats=None, artifacts=[hdr])})
    assert "Unresolved chart value" in out
    assert "Unresolved KCC value" not in out
    assert "DO NOT MERGE" not in out
    assert "Review before merging" in out


# ── 4. purge semantics copy ───────────────────────────────────────────────

def test_phase_table_purge_note_names_soft_delete_and_backup():
    rows = "\n".join(_decommission_phase_table(
        vm_state=None, cascade_state=_PH_DONE, removal_state=_PH_THIS_PR,
        declares_vms=False, purge=True))
    assert "soft-delete off on content" in rows
    assert "backup bucket always abandoned" in rows


def test_phase3_purge_panel_explains_soft_delete_and_backup(monkeypatch):
    _clear_yaml_cache()
    ident = "gcp/prod/private-cloud/gb1/custom/pv-purge-a/customer.yaml"
    content = ("appspace:\n  customerName: pv-purge\n"
               "  decommission: true\n"
               "  decommissionPurgeData: true\n")
    table = {
        (ident, "prsha"): (None, m.BB_NOT_FOUND),
        (ident, "mainsha"): (content, m.BB_OK),
    }
    monkeypatch.setattr(
        m, "_bb_fetch_cached",
        lambda f, sha, repo=None: table.get((f, sha), (None, m.BB_NOT_FOUND)))
    monkeypatch.setattr(m, "_app_chart_revision_map",
                        {"pv-purge-a-ms": "2603.0.14"})
    monkeypatch.setattr(m, "_render_main_side_resources",
                        lambda app, main_sha: {})
    monkeypatch.setattr(m, "_cascade_mismatch_note",
                        lambda *a, **k: [])
    cand = {"env_name": "pv-purge-a", "identity_file": ident,
            "apps": ["pv-purge-a-ms"]}
    lines, _envs = m._evaluate_env_decommissions([cand], "prsha", "mainsha")
    text = "\n".join(lines)
    assert "DATA WILL BE PERMANENTLY DESTROYED" in text
    assert "retentionDurationSeconds: 0" in text
    assert "deletion-policy: abandon" in text
    assert "backup" in text.lower()
