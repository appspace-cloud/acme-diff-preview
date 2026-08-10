"""A manifest that renders `%!s(<nil>)` must block the merge.

COPS-2632 was this class of bug: `pv-stage1-a` had no `appspace.hostingID`,
so the chart rendered `hosting-id: hst-%!s(<nil>)`. helm exited 0, the diff
looked ordinary, the merge summary said "Routine - nothing dangerous
detected", and KCC rejected every ComputeInstance / ComputeDisk /
ComputeAddress once it reached the cluster.

The chart-side guard does not close this. The validation helper reads

    {{- if .Values.appspace.hostingID }} ...regexMatch... {{- fail ... }}

so it only fires when the value EXISTS and is malformed. An absent value
skips the check entirely and renders the artifact instead. A chart author
who writes `required` gets the failure classified already
(REASON_MISSING_REQUIRED, permanent, blocking); one who writes an `if` guard
gets silence.

A Go template artifact in rendered YAML is never correct, for any field of
any chart, so detecting it in the service covers the fields nobody guarded,
and the ones added later. It blocks for the same reason PERMANENT_REASONS
blocks: the API server will reject this exactly as reliably here as in the
cluster.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_preview as m  # noqa: E402


def _added(line):
    return ("--- \n+++ \n@@ -8,6 +8,7 @@\n"
            " metadata:\n"
            "   labels:\n"
            f"+{line}\n"
            " spec:\n")


def _removed(line):
    return ("--- \n+++ \n@@ -8,7 +8,6 @@\n"
            " metadata:\n"
            "   labels:\n"
            f"-{line}\n"
            " spec:\n")


# ── the COPS-2632 shape and its siblings ───────────────────────────────────

def test_nil_printf_artifact_is_detected():
    """The exact live shape: hosting-id: hst-%!s(<nil>)."""
    secs = [("/compute.cnrm.cloud.google.com/ComputeInstance vm-a",
             _added("    hosting-id: hst-%!s(<nil>)"))]
    assert m._detect_template_artifacts(secs) == [
        "/compute.cnrm.cloud.google.com/ComputeInstance vm-a"]


def test_no_value_artifact_is_detected():
    secs = [("/v1/ConfigMap app", _added("    tenant: <no value>"))]
    assert m._detect_template_artifacts(secs) == ["/v1/ConfigMap app"]


def test_missing_printf_artifact_is_detected():
    secs = [("/apps/Deployment api", _added("    build: %!d(MISSING)"))]
    assert m._detect_template_artifacts(secs) == ["/apps/Deployment api"]


def test_artifact_detected_on_any_kind():
    """Not scoped to workloads: the live case was a KCC ComputeDisk."""
    secs = [("/compute.cnrm.cloud.google.com/ComputeDisk d1",
             _added("    hosting-id: hst-%!s(<nil>)")),
            ("/v1/Service svc", _added("    ok: real-value"))]
    assert m._detect_template_artifacts(secs) == [
        "/compute.cnrm.cloud.google.com/ComputeDisk d1"]


# ── the PR that FIXES an artifact must not be blocked ──────────────────────

def test_artifact_only_on_the_removed_side_is_a_fix_not_a_defect():
    """Judged on the applied side, same rule as _replicas_end_state."""
    secs = [("/apps/Deployment api", _removed("    hosting-id: hst-%!s(<nil>)"))]
    assert m._detect_template_artifacts(secs) == []


def test_artifact_replaced_by_a_real_value_is_a_fix():
    body = ("--- \n+++ \n@@ -8,7 +8,7 @@\n"
            " metadata:\n"
            "-    hosting-id: hst-%!s(<nil>)\n"
            "+    hosting-id: hst-99999999\n"
            " spec:\n")
    assert m._detect_template_artifacts([("/apps/Deployment api", body)]) == []


# ── false positives that would make the block untrustworthy ────────────────

def test_a_clean_diff_reports_nothing():
    secs = [("/apps/Deployment api", _added("    app: real"))]
    assert m._detect_template_artifacts(secs) == []


def test_a_printf_format_string_in_config_data_is_not_an_artifact():
    """A ConfigMap can legitimately carry Go/C format strings. Only the
    shapes Go emits for a nil or missing argument count."""
    secs = [("/v1/ConfigMap logging",
             _added('    format: "%s %d %v request=%q"')),
            ("/v1/ConfigMap logging2", _added('    tmpl: "{{ .Values.x }}"')),
            ("/v1/ConfigMap logging3", _added('    pct: "100%!"')),
            ("/v1/ConfigMap logging4", _added('    note: "no value set"'))]
    assert m._detect_template_artifacts(secs) == []


def test_context_lines_are_ignored():
    """An artifact that already exists on both sides is not this PR's doing;
    only a line this PR applies counts."""
    body = ("--- \n+++ \n@@ -8,6 +8,7 @@\n"
            "     hosting-id: hst-%!s(<nil>)\n"
            "+    added: fine\n"
            " spec:\n")
    assert m._detect_template_artifacts([("/apps/Deployment api", body)]) == []


# ── the merge summary must block, and say why ──────────────────────────────

def _result(sections, artifacts):
    return m.DiffResult(
        text="x", sections=sections, n_res=len(sections), has_diff=True,
        error=None, outcome=m.OUT_DIFF, reason=None,
        version_change=None, deleted_resources=[], replicas_zeroed=[],
        fingerprint="fp", renamed_resources=[], vm_changes=[],
        version_fold=None, shutdown_stats=None,
        template_artifacts=artifacts)


def _summary(results):
    return "\n".join(m._build_merge_summary(results, {}, [], [], [], [], None))


def test_artifacts_block_the_merge():
    secs = [("/compute.cnrm.cloud.google.com/ComputeInstance vm-a",
             _added("    hosting-id: hst-%!s(<nil>)"))]
    out = _summary({"pv-stage1-a-ss": _result(secs, [secs[0][0]])})
    assert "DO NOT MERGE" in out, out
    assert "nothing dangerous detected" not in out


def test_the_blocking_finding_names_the_env_and_the_count():
    secs = [(f"/apps/Deployment d{i}", _added("    x: %!s(<nil>)"))
            for i in range(3)]
    out = _summary({"pv-stage1-a-ss": _result(secs, [h for h, _ in secs])})
    assert "pv-stage1-a" in out
    assert "3" in out


def test_the_finding_explains_the_cause_not_just_the_symptom():
    """A reviewer who has never seen %!s(<nil>) must still know what to do."""
    secs = [("/apps/Deployment api", _added("    x: %!s(<nil>)"))]
    out = _summary({"pv-stage1-a-ss": _result(secs, [secs[0][0]])})
    low = out.lower()
    assert "value" in low
    assert "reject" in low or "invalid" in low


def test_no_artifacts_leaves_the_verdict_alone():
    secs = [("/apps/Deployment api", _added("    x: real"))]
    out = _summary({"pv-dev-01-a-ms": _result(secs, [])})
    assert "DO NOT MERGE" not in out


def test_missing_field_on_a_legacy_result_does_not_raise():
    secs = [("/apps/Deployment api", _added("    x: real"))]
    r = m.DiffResult(text="x", sections=secs, n_res=1, has_diff=True,
                     error=None, outcome=m.OUT_DIFF, reason=None)
    out = _summary({"pv-dev-01-a-ms": r})
    assert "DO NOT MERGE" not in out


def test_an_app_with_artifacts_is_risky_so_it_is_never_folded_away():
    secs = [("/apps/Deployment api", _added("    x: %!s(<nil>)"))]
    assert m._is_risky_result(_result(secs, [secs[0][0]])) is True


def test_the_offending_section_survives_the_display_cap():
    """Detecting the risk is half the job: the resource the block names has
    to be reachable in the comment (the COPS-2567 / PR-3845 lesson)."""
    noise = [(f"/apps/Deployment noise{i:03d}", _added("    app: real"))
             for i in range(60)]
    bad = ("/compute.cnrm.cloud.google.com/ComputeInstance vm-a",
           _added("    hosting-id: hst-%!s(<nil>)"))
    # The offender sorts last by header, exactly like the HPA in PR 3845.
    packed = m._package_sections(noise + [bad])
    clean_diff, capped = packed[0], packed[1]
    assert bad[0] in [h for h, _ in capped], "offender dropped by the cap"
    assert "hst-%!s(<nil>)" in clean_diff
