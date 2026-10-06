"""COPS-2790 follow-up: a PR that makes a broken main render is not blocked.

acme-config-dev #7450 merged pv-qa-perf-01-a with a KCC svc VM and no
hostingID, so main failed the supporting-services `required()`. #7462 set
`hostingID: "12345678"`: the PR side rendered, but the main-side error came
back as the PR's own and the build went FAILED "fix and push".

The PR is now diffed against an empty main and gets a review line. Only a
hostingID the charts accept gets past: a quoted string of 8 digits.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import comment_render as cr  # noqa: E402
import diff_preview as m  # noqa: E402
import manifest  # noqa: E402
import schema_errors  # noqa: E402
import vm_analysis as va  # noqa: E402

APP = "pv-qa-perf-01-a-ss"
ENV = "pv-qa-perf-01-a"
PR_SHA, MAIN_SHA = "prsha0000001", "mainsha00001"
REQUIRED = ("appspace.hostingID is required when deployLinuxServicesK8s.enabled "
            "is true")
MAIN_ERR = ("Error: execution error at (appspace-supporting-services/templates/"
            f"kcc-linux-services/compute-instance-svc.yaml:7:18): {REQUIRED}")
INVALID_ERR = ("Error: execution error at (appspace-supporting-services/templates/"
               "redis/redis.yaml:9:8): Error: Invalid hostingID '1234568'. It must "
               "contain exactly 8 numeric digits.")
TYPE_ERR = ('Error: template: appspace-supporting-services/templates/redis/'
            'redis.yaml:9:8: executing "appspace-supporting-services/templates/'
            'redis/redis.yaml" at <include "appspace.labels" .>: error calling '
            'include: template: appspace-supporting-services/templates/_helpers.tpl:'
            '119:7: executing "appspace.labels" at <.Values.appspace.hostingID>: '
            'wrong type for value; expected string; got float64')


@pytest.fixture(autouse=True)
def _no_ai(monkeypatch):
    monkeypatch.setattr(m, "AI_SUMMARY_ENABLED", False)


def _instance(hosting_id):
    """The KCC svc VM as helm renders it."""
    return (
        "apiVersion: compute.cnrm.cloud.google.com/v1beta1\n"
        "kind: ComputeInstance\n"
        "metadata:\n"
        "  name: pv-qa-perf-01-svc-a\n"
        f"  namespace: {ENV}\n"
        "  labels:\n"
        f"    hosting-id: {hosting_id}\n"
        "spec:\n"
        "  machineType: n2d-standard-2\n"
    )


GOOD = 'appspace:\n  hostingID: "12345678"\n'


def _world(monkeypatch, pr, main, pr_values=GOOD, merged=True):
    """pr and main are (doc, err) as `_helm_template` returns them. merged:
    the PR side is a real merge preview, not the branch tip."""
    monkeypatch.setitem(m._app_chart_map, APP, "appspace-supporting-services")
    monkeypatch.setitem(m._app_chart_revision_map, APP, "2604.0.0-dev")
    monkeypatch.setitem(m._app_chart_registry_map, APP, "registry.example.com")
    monkeypatch.setitem(m._app_value_files_map, APP,
                        [f"$config/gcp/qa/private-cloud/ap1/custom/{ENV}/customer.yaml"])
    monkeypatch.setitem(m._app_namespace_map, APP, ENV)
    monkeypatch.setattr(m, "_merge_preview_bases", {PR_SHA: MAIN_SHA} if merged else {})
    monkeypatch.setattr(m, "_ensure_chart", lambda reg, chart, ver: "/fake/chart")
    monkeypatch.setattr(m, "_fetch_value_files", lambda vfs, sha: {
        vf: ("# side: pr\n" + pr_values if sha == PR_SHA else "# side: main\n")
        for vf in vfs})
    monkeypatch.setattr(m, "_main_render_content_key", lambda *a: "k-2790")
    monkeypatch.setattr(m, "_main_render_cache_get", lambda k: (None, None, "miss"))
    puts = []
    monkeypatch.setattr(m, "_main_render_cache_put", lambda *a: puts.append(a))
    monkeypatch.setattr(m, "_helm_template", lambda chart, release, ns, vals: (
        pr if "# side: pr" in "".join(vals.values()) else main))
    return puts


# ── _run_one_diff ─────────────────────────────────────────────────────────

def test_a_pr_that_makes_main_render_is_diffed_against_an_empty_main(monkeypatch):
    puts = _world(monkeypatch, (_instance('"hst-12345678"'), None), (None, MAIN_ERR))
    step = m._run_one_diff(APP, PR_SHA, MAIN_SHA)
    assert step[1] is None and step[2] is None
    assert '+    hosting-id: "hst-12345678"' in step[0]
    assert step[9] == (m.REASON_MISSING_REQUIRED, MAIN_ERR)
    assert puts == [], "a main that does not render is never cached"


def test_main_that_renders_has_no_main_broken(monkeypatch):
    _world(monkeypatch, (_instance('"hst-12345678"'), None),
           (_instance('"hst-none"'), None))
    assert m._run_one_diff(APP, PR_SHA, MAIN_SHA)[9] is None


def test_both_sides_failing_is_still_the_pr_error(monkeypatch):
    _world(monkeypatch, (None, INVALID_ERR), (None, MAIN_ERR))
    diff_text, reason, detail = m._run_one_diff(APP, PR_SHA, MAIN_SHA)
    assert diff_text is None and reason == m.REASON_MISSING_REQUIRED
    assert detail == INVALID_ERR


def test_a_generic_main_failure_keeps_the_retry_path(monkeypatch):
    """Not a deterministic chart error: it could be a blip, so no empty main."""
    _world(monkeypatch, (_instance('"hst-12345678"'), None), (None, "exit status 1"))
    step = m._run_one_diff(APP, PR_SHA, MAIN_SHA)
    assert step[:3] == (None, m.REASON_RENDER, "exit status 1")


def test_a_main_error_that_is_not_hosting_id_keeps_the_block(monkeypatch):
    """A live env can have a broken main for other reasons. Against an empty
    main its deletions and the ip gate would vanish, so it stays blocked."""
    err = ("Error: execution error at (appspace-ms/templates/x.yaml:4:5): "
           "Missing Image Tag on => platform")
    _world(monkeypatch, (_instance('"hst-12345678"'), None), (None, err))
    assert m._run_one_diff(APP, PR_SHA, MAIN_SHA)[:3] == (
        None, m.REASON_MISSING_REQUIRED, err)


def test_the_branch_tip_is_not_the_merge_so_it_keeps_the_block(monkeypatch):
    """No merge preview: a branch cut before main broke renders, and still
    leaves main broken after the merge."""
    _world(monkeypatch, (_instance('"hst-12345678"'), None), (None, MAIN_ERR),
           merged=False)
    assert m._run_one_diff(APP, PR_SHA, MAIN_SHA)[:3] == (
        None, m.REASON_MISSING_REQUIRED, MAIN_ERR)


def test_a_hosting_id_the_charts_refuse_keeps_the_block(monkeypatch):
    """`required()` lets 0, {} and [] through, and the labels helper skips
    them, so the PR side renders. Only 8 digits in quotes lifts it."""
    for bad in ("00000000", "{}", "[]", '"1234568"', "12345678"):
        _world(monkeypatch, (_instance('"hst-12345678"'), None), (None, MAIN_ERR),
               pr_values=f"appspace:\n  hostingID: {bad}\n")
        assert m._run_one_diff(APP, PR_SHA, MAIN_SHA)[1] == m.REASON_MISSING_REQUIRED, bad


def test_the_last_file_that_sets_hosting_id_wins(monkeypatch):
    monkeypatch.setattr(m, "_merge_preview_bases", {PR_SHA: MAIN_SHA})
    assert m._pr_fixes_main_hosting_id(
        MAIN_ERR, ['appspace:\n  hostingID: 0\n', 'appspace:\n  hostingID: "00000000"\n'],
        PR_SHA, MAIN_SHA)
    assert not m._pr_fixes_main_hosting_id(
        MAIN_ERR, ['appspace:\n  hostingID: "00000000"\n', 'appspace:\n  hostingID: 0\n'],
        PR_SHA, MAIN_SHA)
    assert not m._pr_fixes_main_hosting_id(MAIN_ERR, [": not yaml: ["], PR_SHA, MAIN_SHA)


def test_a_merge_preview_of_another_main_does_not_count(monkeypatch):
    monkeypatch.setattr(m, "_merge_preview_bases", {PR_SHA: "othermain001"})
    assert not m._pr_fixes_main_hosting_id(MAIN_ERR, [GOOD], PR_SHA, MAIN_SHA)


# ── argocd_diff, comment and status ───────────────────────────────────────

def _diff(monkeypatch, pr, main, **kw):
    _world(monkeypatch, pr, main, **kw)
    return m.argocd_diff(APP, PR_SHA, MAIN_SHA)


def test_argocd_diff_is_a_diff_that_names_the_main_error(monkeypatch):
    r = _diff(monkeypatch, (_instance('"hst-12345678"'), None), (None, MAIN_ERR))
    assert r.outcome == m.OUT_DIFF
    assert r.main_broken == REQUIRED
    assert not m._render_covers([APP], {APP: r}), "an empty main confirms nothing"


def test_a_pr_that_renders_nothing_still_carries_main_broken(monkeypatch):
    r = _diff(monkeypatch, ("", None), (None, MAIN_ERR))
    assert r.outcome == m.OUT_NO_DIFF and r.main_broken == REQUIRED


def test_the_comment_is_green_with_a_review_line(monkeypatch):
    r = _diff(monkeypatch, (_instance('"hst-12345678"'), None), (None, MAIN_ERR))
    body = m.format_comment(PR_SHA, {APP: r}, base_sha=MAIN_SHA)
    assert m._extract_status_token(body) == "clean"
    assert "RENDER BLOCKED" not in body
    line = next(l for l in body.splitlines() if "render on main" in l)
    assert ENV in line and f"({REQUIRED})" in line
    assert "empty main" in line


def test_a_seven_digit_hosting_id_stays_blocked_and_says_how(monkeypatch):
    r = _diff(monkeypatch, (None, INVALID_ERR), (None, MAIN_ERR))
    assert r.outcome == m.OUT_INDETERMINATE and r.main_broken is None
    body = m.format_comment(PR_SHA, {APP: r}, base_sha=MAIN_SHA)
    assert m._extract_status_token(body) == "permanent"
    panel = body.split("RENDER BLOCKED", 1)[1]
    assert schema_errors._HOSTING_ID_FIX in panel
    assert "add the missing value" not in panel


def test_an_unquoted_hosting_id_is_blocked_and_says_quote_it(monkeypatch):
    r = _diff(monkeypatch, (None, TYPE_ERR), (None, MAIN_ERR))
    assert r.reason == m.REASON_TEMPLATE
    body = m.format_comment(PR_SHA, {APP: r}, base_sha=MAIN_SHA)
    assert m._extract_status_token(body) == "permanent"
    assert schema_errors._HOSTING_ID_FIX in body.split("RENDER BLOCKED", 1)[1]


def test_the_full_page_gives_the_same_hosting_id_fix(monkeypatch):
    page = m.RenderProfile("page", is_complete_record=True, inline_diffs=True)
    for err in (INVALID_ERR, TYPE_ERR):
        r = _diff(monkeypatch, (None, err), (None, MAIN_ERR))
        body = m.format_comment(PR_SHA, {APP: r}, base_sha=MAIN_SHA, profile=page)
        block = body.split("helm cannot render this environment", 1)[1]
        assert schema_errors._HOSTING_ID_FIX in block, err


def test_an_unquoted_zero_on_a_live_kcc_vm_still_fails(monkeypatch):
    """Main renders, the PR writes `hostingID: 00000000`: it loads as 0,
    `required` passes and printf renders hst-%!s(float64=0)."""
    r = _diff(monkeypatch, (_instance('"hst-%!s(float64=0)"'), None),
              (_instance('"hst-none"'), None))
    assert r.outcome == m.OUT_DIFF and r.template_artifacts
    body = m.format_comment(PR_SHA, {APP: r}, base_sha=MAIN_SHA)
    assert m._extract_status_token(body) == "permanent"


def test_the_summary_line_is_review_not_block():
    r = m.DiffResult("x", [], 1, True, None, m.OUT_DIFF, "changes",
                     main_broken=REQUIRED)
    text = "\n".join(cr._build_merge_summary({APP: r}, {}, None, None, None, None,
                                             None, green=False))
    assert "render on main" in text and "Review before merging" in text
    assert "DO NOT MERGE" not in text and "\u26d4" not in text


# ── the artifact regex ────────────────────────────────────────────────────

def test_a_go_bad_verb_with_a_typed_value_is_an_artifact():
    for bad in ("hst-%!s(float64=0)", "%!s(int=5)", "%!d(string=x)"):
        assert manifest._detect_template_artifacts(
            [("/h", f"+    hosting-id: {bad}\n")]) == ["/h"], bad


def test_a_typed_bad_verb_on_main_only_is_not_this_prs():
    assert manifest._detect_template_artifacts(
        [("/h", ("-    hosting-id: hst-%!s(float64=0)\n"
                 "+    hosting-id: hst-00000000\n"))]) == []


# ── the new-env gate takes only what the chart takes ─────────────────────

K = "appspace.infra.deployLinuxServicesK8s."


def _flat(hosting_id):
    return {K + "enabled": True, K + "svc.enabled": True,
            K + "svc.createNewBootDisk": True, "appspace.hostingID": hosting_id}


def test_the_gate_takes_a_quoted_eight_digit_string():
    for ok in ("00000000", "12345678", "88888888"):
        assert not va._kcc_missing_hosting_id(_flat(ok)), ok


def test_the_gate_refuses_what_the_chart_refuses():
    for bad in (0, 12345678, "1234568", "123456789", "0000000a", (" 00000000")):
        assert va._kcc_missing_hosting_id(_flat(bad)), bad


def test_an_invalid_hosting_id_line_names_the_value():
    line = va._new_env_prereq_findings(_flat(0), ENV)[1][0]
    assert line.startswith(f"- ⛔ `{ENV}`")
    assert "`0`, a number because it has no quotes" in line
    assert "8 digits" in line and '"00000000"' in line
    assert "is missing" not in line
    assert "`True`, not a string" in va._new_env_prereq_findings(_flat(True), ENV)[1][0]
    assert "`1234568`, and" in va._new_env_prereq_findings(_flat("1234568"), ENV)[1][0]


@pytest.mark.prereq_reads
def test_an_unquoted_zero_in_customer_yaml_fails_the_new_env_gate(monkeypatch):
    """The real YAML path: `hostingID: 00000000` loads as the number 0."""
    sys.path.insert(0, os.path.dirname(__file__))
    import test_cops2766_vm_disk_family as fam
    doc = fam._doc(["createNewBootDisk: true"], hosting_id=None).replace(
        "appspace:\n", "appspace:\n  hostingID: 00000000\n")
    fam._serve(monkeypatch, {fam.NEW: doc})
    gates, _ = m._new_env_prereqs([fam._cand(fam.NEW)], fam.PR_SHA)
    assert gates == [{"kind": "kcc_hosting_id", "env": fam.ENV,
                      "why": "appspace.hostingID 0 is not 8 quoted digits, svc enabled"}]

