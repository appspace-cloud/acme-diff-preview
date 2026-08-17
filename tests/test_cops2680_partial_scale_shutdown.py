"""COPS-2680: scaling two services to 0 is not an environment shutdown.

Live case, acme-config-prod PR #4321: only reservationiot +
reservationiot-background went to replicas: 0 on pv-blackrock--aec1-a-ms
(~109 Deployments). The changeset overview correctly said "2 resources",
but the merge summary shouted "Environment shutting down — every workload
scaled to 0 … and 23 HorizontalPodAutoscaler(s) remain".

Cause: `_detect_workload_shutdown` counted workloads only from unified-diff
sections. Unchanged Deployments never appear there, so 2/2 looked like a
full hibernation. HPA counting already used the full PR-side render
(COPS-2677); workload totals must too.
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
    _count_workload_replicas,
    _detect_workload_shutdown,
    _manifest_replicas,
)


def _scaled_down(old=2):
    return (
        "--- \n+++ \n@@ -20,7 +20,7 @@\n"
        " spec:\n"
        f"-  replicas: {old}\n"
        "+  replicas: 0\n"
        " template:\n"
    )


def _deploy_yaml(name, replicas):
    """Minimal PR-side Deployment body (not a diff hunk)."""
    body = (
        f"apiVersion: apps/v1\n"
        f"kind: Deployment\n"
        f"metadata:\n"
        f"  name: {name}\n"
        f"  namespace: ns\n"
        f"spec:\n"
    )
    if replicas is not None:
        body += f"  replicas: {replicas}\n"
    body += (
        "  selector:\n"
        "    matchLabels:\n"
        f"      app: {name}\n"
        "  template:\n"
        "    metadata:\n"
        "      labels:\n"
        f"        app: {name}\n"
        "    spec:\n"
        "      containers:\n"
        "      - name: c\n"
        "        image: x\n"
    )
    return body + "\n"


def _result(sections, zeroed=None, stats=None):
    return m.DiffResult(
        text="x", sections=sections, n_res=len(sections), has_diff=True,
        error=None, outcome=m.OUT_DIFF, reason=None,
        version_change=None, deleted_resources=[],
        replicas_zeroed=zeroed or [h for h, _ in sections],
        fingerprint="fp", renamed_resources=[], vm_changes=[],
        version_fold=None, shutdown_stats=stats)


def _summary(results):
    return "\n".join(_build_merge_summary(results, {}, [], [], [], [], None))


def _pr4321_resources():
    """2 services to zero + many unchanged Deployments + leftover HPAs."""
    pr = {("apps/Deployment", "ns", f"svc-{i}"): _deploy_yaml(f"svc-{i}", 2)
          for i in range(50)}
    pr[("apps/Deployment", "ns", "reservationiot")] = _deploy_yaml(
        "reservationiot", 0)
    pr[("apps/Deployment", "ns", "reservationiot-background")] = _deploy_yaml(
        "reservationiot-background", 0)
    pr[("apps/Deployment", "ns", "oauthintegration")] = _deploy_yaml(
        "oauthintegration", 0)  # already off; unchanged in the diff
    for i in range(23):
        pr[("autoscaling/HorizontalPodAutoscaler", "ns", f"hpa-{i}")] = "x\n"
    return pr


def test_manifest_replicas_reads_spec_field():
    assert _manifest_replicas(_deploy_yaml("a", 0)) == 0
    assert _manifest_replicas(_deploy_yaml("a", 2)) == 2
    assert _manifest_replicas(_deploy_yaml("a", None)) is None


def test_count_workload_replicas_from_pr_resources():
    pr = {
        ("apps/Deployment", "ns", "a"): _deploy_yaml("a", 0),
        ("apps/Deployment", "ns", "b"): _deploy_yaml("b", 0),
        ("apps/Deployment", "ns", "c"): _deploy_yaml("c", 2),
        ("apps/Deployment", "ns", "d"): _deploy_yaml("d", None),  # HPA-managed
        ("autoscaling/HorizontalPodAutoscaler", "ns", "c"): "x\n",
    }
    assert _count_workload_replicas(pr) == (4, 2)


def test_partial_scale_with_full_render_is_not_a_shutdown():
    """PR #4321 shape: 2 diff sections to zero, ~50 Deployments in desired."""
    sections = [
        ("/apps/Deployment reservationiot", _scaled_down()),
        ("/apps/Deployment reservationiot-background", _scaled_down()),
    ]
    pr = _pr4321_resources()
    stats = _detect_workload_shutdown(sections, pr_resources=pr)
    assert stats["workloads"] == 53
    assert stats["zeroed"] == 3
    assert stats["hpas_remaining"] == 23
    assert stats["zeroed"] != stats["workloads"]

    out = _summary({"pv-blackrock--aec1-a-ms": _result(sections, stats=stats)})
    assert "Replicas scaled to zero" in out, out
    assert "Environment shutting down" not in out, out
    assert "HorizontalPodAutoscaler" not in out, out


def test_full_render_all_zero_still_is_a_shutdown():
    """zeroPods: every Deployment in desired ends at 0, even if HPAs remain."""
    sections = [
        ("/apps/Deployment a", _scaled_down()),
        ("/apps/Deployment b", _scaled_down()),
    ]
    pr = {
        ("apps/Deployment", "ns", "a"): _deploy_yaml("a", 0),
        ("apps/Deployment", "ns", "b"): _deploy_yaml("b", 0),
        ("autoscaling/HorizontalPodAutoscaler", "ns", "a"): "x\n",
    }
    stats = _detect_workload_shutdown(sections, pr_resources=pr)
    assert stats == {"zeroed": 2, "workloads": 2, "hpas_remaining": 1}
    out = _summary({"pv-x-ms": _result(sections, stats=stats)})
    assert "Environment shutting down" in out, out
    assert "HorizontalPodAutoscaler" in out, out


def test_sections_only_fallback_unchanged_without_pr_resources():
    """Unit tests that omit pr_resources keep the pre-COPS-2680 behaviour."""
    sections = [
        ("/apps/Deployment a", _scaled_down()),
        ("/apps/Deployment b", _scaled_down()),
    ]
    assert _detect_workload_shutdown(sections) == {
        "zeroed": 2, "workloads": 2, "hpas_remaining": 0}
