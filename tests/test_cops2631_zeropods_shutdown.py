"""An environment being switched off must say so in the merge summary.

Live case, acme-config-dev PR #7063: flipping `appspace.zeroPods` to true
scaled all 110 workloads of `pv-dev-01-a-ms` to zero, and the merge summary
read "Routine - nothing dangerous detected". Shutting down an environment is
the least routine thing a config PR can do.

Two separate defects behind that verdict:

1. `_detect_replicas_zeroed` required a `- replicas: N` line. The chart does
   not render `replicas` at all until zeroPods sets it, so the real diff is a
   bare `+ replicas: 0` addition and nothing matched.
2. Even once detected, "Replicas scaled to zero" reads like a partial
   scale-down. Every workload going to zero is a different event and deserves
   its own wording.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_preview as m  # noqa: E402


def _added_zero(product="core-platform"):
    """The exact shape helm renders when zeroPods adds the field (PR #7063)."""
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


def _scaled_down(old=2):
    return (
        "--- \n+++ \n@@ -20,7 +20,7 @@\n"
        " spec:\n"
        f"-  replicas: {old}\n"
        "+  replicas: 0\n"
        " template:\n"
    )


def _scaled_up():
    return (
        "--- \n+++ \n@@ -20,7 +20,7 @@\n"
        " spec:\n"
        "-  replicas: 1\n"
        "+  replicas: 3\n"
        " template:\n"
    )


# ── defect 1: the detector missed a bare `+ replicas: 0` ───────────────────

def test_replicas_added_as_zero_is_detected():
    """zeroPods adds the field; there is no minus line to pair it with."""
    sections = [("/apps/Deployment accesscontrol", _added_zero())]
    assert m._detect_replicas_zeroed(sections) == [
        "/apps/Deployment accesscontrol"]


def test_replicas_scaled_down_from_positive_still_detected():
    """The pre-existing case must keep working."""
    sections = [("/apps/Deployment api", _scaled_down(old=2))]
    assert m._detect_replicas_zeroed(sections) == ["/apps/Deployment api"]


def test_scaling_up_is_not_a_zeroing():
    sections = [("/apps/Deployment api", _scaled_up())]
    assert m._detect_replicas_zeroed(sections) == []


def test_non_workload_kind_with_zero_is_ignored():
    sections = [("/v1/ConfigMap tuning", _added_zero())]
    assert m._detect_replicas_zeroed(sections) == []


# ── defect 2: a whole-environment shutdown needs its own wording ───────────

def test_shutdown_stats_count_zeroed_against_total_workloads():
    sections = [
        ("/apps/Deployment a", _added_zero()),
        ("/apps/Deployment b", _added_zero()),
        ("/apps/StatefulSet c", _added_zero()),
    ]
    stats = m._detect_workload_shutdown(sections)
    assert stats == {"zeroed": 3, "workloads": 3, "hpas_remaining": 0}


def test_shutdown_stats_partial_when_one_workload_survives():
    sections = [
        ("/apps/Deployment a", _added_zero()),
        ("/apps/Deployment b", _scaled_up()),
    ]
    stats = m._detect_workload_shutdown(sections)
    assert stats == {"zeroed": 1, "workloads": 2, "hpas_remaining": 0}


def _result(sections, zeroed=None, stats=None):
    return m.DiffResult(
        text="x", sections=sections, n_res=len(sections), has_diff=True,
        error=None, outcome=m.OUT_DIFF, reason=None,
        version_change=None, deleted_resources=[],
        replicas_zeroed=zeroed if zeroed is not None else [
            h for h, _ in sections],
        fingerprint="fp", renamed_resources=[], vm_changes=[],
        version_fold=None, shutdown_stats=stats)


def _summary(results):
    return "\n".join(m._build_merge_summary(results, {}, [], [], [], [], None))


def test_full_shutdown_is_called_a_shutdown_not_a_scale_down():
    sections = [("/apps/Deployment %d" % i, _added_zero()) for i in range(110)]
    out = _summary({"pv-dev-01-a-ms": _result(
        sections, stats={"zeroed": 110, "workloads": 110})})
    assert "Environment shutting down" in out, out
    # _fmt_env_list names the environment, not the -ms/-ss/-glb app.
    assert "pv-dev-01-a" in out
    assert "110" in out
    # The verdict may no longer claim nothing dangerous was found.
    assert "nothing dangerous detected" not in out


def test_full_shutdown_names_the_flag_that_causes_it():
    sections = [("/apps/Deployment %d" % i, _added_zero()) for i in range(4)]
    out = _summary({"pv-dev-01-a-ms": _result(
        sections, stats={"zeroed": 4, "workloads": 4})})
    assert "zeroPods" in out, out


def test_partial_scale_down_keeps_the_milder_wording():
    sections = [("/apps/Deployment a", _scaled_down()),
                ("/apps/Deployment b", _scaled_up())]
    out = _summary({"pv-dev-01-a": _result(
        sections, zeroed=["/apps/Deployment a"],
        stats={"zeroed": 1, "workloads": 2})})
    assert "Replicas scaled to zero" in out
    assert "Environment shutting down" not in out


def test_a_single_workload_at_zero_is_not_an_environment_shutdown():
    """A one-workload app going to zero is a scale-down, not a shutdown:
    calling it a shutdown on every tiny app is how a warning gets ignored."""
    sections = [("/apps/Deployment only", _added_zero())]
    out = _summary({"pv-dev-01-a": _result(
        sections, stats={"zeroed": 1, "workloads": 1})})
    assert "Replicas scaled to zero" in out
    assert "Environment shutting down" not in out


def test_shutdown_finding_survives_a_missing_stats_field():
    """Legacy/coerced results carry no shutdown_stats; must not raise."""
    sections = [("/apps/Deployment a", _added_zero())]
    out = _summary({"pv-dev-01-a": _result(sections, stats=None)})
    assert "Replicas scaled to zero" in out


def test_shutdown_app_is_treated_as_risky():
    """Risky apps are never folded away by the comment budget."""
    sections = [("/apps/Deployment %d" % i, _added_zero()) for i in range(5)]
    r = _result(sections, stats={"zeroed": 5, "workloads": 5})
    assert m._is_risky_result(r) is True
