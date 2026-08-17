"""COPS-2683: leftover detection/count gaps after COPS-2675..2682.

Surgical regressions for env-vs-app wording, partial HPA targeting,
VM values scan completeness, parent KCC fail-closed, env-level shutdown
aggregation, workload kinds, merged-chain strip/arming, and replica_stats
fallback. Does not change COMMENT identical-diff collapse or abandon
semantics beyond count/list wording.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_preview as m  # noqa: E402
from comment_render import _build_merge_summary  # noqa: E402
from vm_analysis import (  # noqa: E402
    _WORKLOAD_KINDS,
    _count_hpas_targeting_zeroed,
    _detect_workload_shutdown,
    _vm_deletion_armed_flat,
)


ERR = ('execution error at (acme/templates/configmaps/'
       'micro-versions-info.yaml:16:20): Missing Image Tag on => platform')


def _indet(reason=None, error=ERR):
    return m.DiffResult(
        "", [], 0, False, error, m.OUT_INDETERMINATE,
        reason or m.REASON_MISSING_REQUIRED)


def _comment(results, **kw):
    return m.format_comment("a" * 40, results, base_sha="b" * 40, **kw)


def _deploy_yaml(name, replicas):
    body = (
        f"apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {name}\n"
        f"  namespace: ns\nspec:\n")
    if replicas is not None:
        body += f"  replicas: {replicas}\n"
    body += (
        "  selector:\n    matchLabels:\n      app: x\n"
        "  template:\n    metadata:\n      labels:\n        app: x\n"
        "    spec:\n      containers:\n      - name: c\n        image: x\n")
    return body + "\n"


def _hpa_yaml(name, target):
    return (
        f"apiVersion: autoscaling/v2\nkind: HorizontalPodAutoscaler\n"
        f"metadata:\n  name: {name}\n  namespace: ns\n"
        f"spec:\n  scaleTargetRef:\n    apiVersion: apps/v1\n"
        f"    kind: Deployment\n    name: {target}\n"
        f"  minReplicas: 1\n  maxReplicas: 4\n")


def _scaled_down():
    return (
        "--- \n+++ \n@@ -20,7 +20,7 @@\n"
        " spec:\n-  replicas: 2\n+  replicas: 0\n template:\n")


def _result(sections, stats=None, deleted=None):
    return m.DiffResult(
        text="x", sections=sections, n_res=len(sections), has_diff=True,
        error=None, outcome=m.OUT_DIFF, reason=None,
        version_change=None, deleted_resources=deleted or [],
        replicas_zeroed=[h for h, _ in sections],
        fingerprint="fp", renamed_resources=[], vm_changes=[],
        version_fold=None, shutdown_stats=stats)


def _summary(results):
    return "\n".join(_build_merge_summary(results, {}, [], [], [], [], None))


# ── P0.1 RENDER BLOCKED counts environments, not apps ─────────────────────

def test_render_blocked_panel_counts_envs_not_apps(monkeypatch):
    """pv-b-ms + pv-b-ss + pv-a-ms → 2 environments in EVERY cannot-render line."""
    monkeypatch.setattr(m, "generate_ai_summary", lambda *a, **k: None)
    results = {
        "pv-a-ms": _indet(),
        "pv-b-ms": _indet(),
        "pv-b-ss": _indet(),
    }
    out = _comment(results)
    lines = [l for l in out.split("\n") if "cannot render" in l]
    assert lines, out
    for line in lines:
        assert "2 environment" in line, line
        assert "3 environment" not in line, line


# ── P0.3 deletion headline: env count matches env list ────────────────────

def test_deletion_headline_counts_environments_not_apps():
    results = {
        "pv-x-ms": _result([], deleted=["/apps/Deployment a"]),
        "pv-x-ss": _result([], deleted=["/apps/Deployment b"]),
    }
    out = _summary(results)
    line = next(l for l in out.split("\n") if "resource(s) deleted" in l)
    assert "1 environment" in line, line
    assert "2 app(s)" not in line, line
    assert "pv-x" in line


# ── P0.2 partial scale: only HPAs targeting zeroed workloads ──────────────

def test_partial_scale_names_hpas_targeting_zeroed_only():
    sections = [
        ("/apps/Deployment reservationiot", _scaled_down()),
        ("/apps/Deployment reservationiot-background", _scaled_down()),
    ]
    pr = {("apps/Deployment", "ns", f"svc-{i}"): _deploy_yaml(f"svc-{i}", 2)
          for i in range(40)}
    pr[("apps/Deployment", "ns", "reservationiot")] = _deploy_yaml(
        "reservationiot", 0)
    pr[("apps/Deployment", "ns", "reservationiot-background")] = _deploy_yaml(
        "reservationiot-background", 0)
    for i in range(20):
        pr[("autoscaling/HorizontalPodAutoscaler", "ns", f"hpa-{i}")] = (
            _hpa_yaml(f"hpa-{i}", f"svc-{i}"))
    pr[("autoscaling/HorizontalPodAutoscaler", "ns", "reservationiot")] = (
        _hpa_yaml("reservationiot", "reservationiot"))
    pr[("autoscaling/HorizontalPodAutoscaler", "ns",
        "reservationiot-background")] = _hpa_yaml(
            "reservationiot-background", "reservationiot-background")

    assert _count_hpas_targeting_zeroed(pr) == 2
    stats = _detect_workload_shutdown(sections, pr_resources=pr)
    assert stats["zeroed"] < stats["workloads"]
    assert stats["hpas_targeting_zeroed"] == 2
    # Fleet leftover count stays available for full shutdown wording.
    assert stats["hpas_remaining"] == 22

    out = _summary({"pv-x-ms": _result(sections, stats=stats)})
    assert "Replicas scaled to zero" in out, out
    assert "Environment shutting down" not in out, out
    assert "2 HorizontalPodAutoscaler" in out, out
    assert "22 HorizontalPodAutoscaler" not in out, out


# ── P1.4 VM values scan is not capped at 12 files ─────────────────────────

def test_vm_values_scan_sees_file_past_twelfth(monkeypatch):
    paths = [f"gcp/prod/x/env-{i:02d}/customer.yaml" for i in range(15)]
    arm_path = paths[13]
    table = {}
    for p in paths:
        old = "appspace:\n  customerName: x\n"
        new = old
        if p == arm_path:
            new = ("appspace:\n  customerName: x\n  infra:\n"
                   "    deployLinuxServicesK8s:\n"
                   "      defaults:\n        allowDeletion: true\n")
        table[(p, "pr")] = (new, m.BB_OK)
        table[(p, "base")] = (old, m.BB_OK)
    monkeypatch.setattr(
        m, "_bb_fetch_cached",
        lambda f, sha, repo=None: table.get((f, sha), (None, m.BB_NOT_FOUND)))
    path_map = {p: [f"env-{i:02d}-ms"] for i, p in enumerate(paths)}
    body = "\n".join(m._summarize_vm_changes(
        paths, "pr", "base", path_map, []))
    assert "allowDeletion" in body, body
    assert "env-13" in body, body


# ── P1.5 parent KCC fail-closed on unreadable ancestor ────────────────────

_ID = "gcp/prod/private-cloud/eu1-b/weekly/pv-cohort-a/customer.yaml"
_PARENT = "gcp/prod/private-cloud/eu1-b/weekly/config.yaml"


def test_unreadable_parent_is_not_fully_phased(monkeypatch):
    m._yaml_cache.clear()
    identity = ("appspace:\n  customerName: pv-cohort\n"
                "  decommission: true\n")
    table = {
        (_ID, "mainsha"): (identity, m.BB_OK),
        (_PARENT, "mainsha"): (None, m.BB_ERROR),
    }
    monkeypatch.setattr(
        m, "_bb_fetch_cached",
        lambda f, sha, repo=None: table.get((f, sha), (None, m.BB_NOT_FOUND)))
    assert m._decommission_fully_phased(_ID, "mainsha") is False


def test_unparseable_parent_is_not_fully_phased(monkeypatch):
    m._yaml_cache.clear()
    identity = ("appspace:\n  customerName: pv-cohort\n"
                "  decommission: true\n")
    table = {
        (_ID, "mainsha"): (identity, m.BB_OK),
        (_PARENT, "mainsha"): (": not: yaml: [", m.BB_OK),
    }
    monkeypatch.setattr(
        m, "_bb_fetch_cached",
        lambda f, sha, repo=None: table.get((f, sha), (None, m.BB_NOT_FOUND)))
    assert m._decommission_fully_phased(_ID, "mainsha") is False


# ── P2.6 shutdown judged across sibling apps of one env ───────────────────

def test_ms_shutdown_ss_still_running_is_partial_not_env_shutdown():
    ms = _result(
        [("/apps/Deployment a", _scaled_down()),
         ("/apps/Deployment b", _scaled_down())],
        stats={"zeroed": 2, "workloads": 2, "hpas_remaining": 0,
               "hpas_targeting_zeroed": 0})
    ss = _result(
        [("/apps/Deployment c", _scaled_down())],
        stats={"zeroed": 1, "workloads": 5, "hpas_remaining": 0,
               "hpas_targeting_zeroed": 1})
    out = _summary({"pv-x-ms": ms, "pv-x-ss": ss})
    assert "Replicas scaled to zero" in out, out
    assert "Environment shutting down" not in out, out


# ── P2.7 workload kinds for shutdown ──────────────────────────────────────

def test_shutdown_workload_kinds_are_deployment_and_statefulset_only():
    """Charts hibernate via zeroPods on Deployments/StatefulSets.
    ReplicaSet is controller-owned; Job/CronJob/DaemonSet are not
    hibernation targets for this detector (documented COPS-2683)."""
    assert _WORKLOAD_KINDS == ("Deployment", "StatefulSet")


# ── P2.8 role-level allowDeletion arms Phase 1 ────────────────────────────

def test_role_level_allow_deletion_is_armed():
    flat = {
        "appspace.infra.deployLinuxServicesK8s.svc.allowDeletion": "true",
    }
    assert _vm_deletion_armed_flat(flat) is True
    assert _vm_deletion_armed_flat({}) is False


def test_strip_on_parent_with_arm_on_identity_is_broken(monkeypatch):
    """Arm on customer.yaml while parent disables the live role → strip."""
    m._yaml_cache.clear()
    ident = "gcp/prod/private-cloud/eu1-b/weekly/pv-x/customer.yaml"
    parent = "gcp/prod/private-cloud/eu1-b/weekly/config.yaml"
    old_parent = ("appspace:\n  infra:\n    deployLinuxServicesK8s:\n"
                  "      svc:\n        enabled: true\n")
    new_parent = ("appspace:\n  infra:\n    deployLinuxServicesK8s:\n"
                  "      svc:\n        enabled: false\n")
    old_id = "appspace:\n  customerName: x\n"
    new_id = ("appspace:\n  customerName: x\n  decommission: true\n"
              "  infra:\n    deployLinuxServicesK8s:\n"
              "      defaults:\n        allowDeletion: true\n")
    table = {
        (ident, "pr"): (new_id, m.BB_OK),
        (ident, "base"): (old_id, m.BB_OK),
        (parent, "pr"): (new_parent, m.BB_OK),
        (parent, "base"): (old_parent, m.BB_OK),
    }
    monkeypatch.setattr(
        m, "_bb_fetch_cached",
        lambda f, sha, repo=None: table.get((f, sha), (None, m.BB_NOT_FOUND)))
    lines = m._summarize_appspace_state_changes(
        [ident, parent], "pr", "base", {ident: ["pv-x-ms"]})
    body = "\n".join(lines)
    assert ("strips the VM" in body
            or "VM CONFIG STRIPPED" in body
            or "orphaned" in body.lower()), body


# ── P2.9 replica_stats (0,0) falls back to sections ───────────────────────

def test_replica_stats_zero_falls_back_to_sections():
    sections = [
        ("/apps/Deployment a", _scaled_down()),
        ("/apps/Deployment b", _scaled_down()),
    ]
    stats = _detect_workload_shutdown(
        sections, replica_stats=(0, 0))
    assert stats == {
        "zeroed": 2, "workloads": 2,
        "hpas_remaining": 0, "hpas_targeting_zeroed": 0}
