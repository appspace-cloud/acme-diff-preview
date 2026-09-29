"""COPS-2766 block 5 S2: the same change over sibling files, and tenant reach.

acme-config-prod #4565 set `appspace.infra.noCore: true` in 11 weekly cohort
`config.yaml` files. Each file alone reaches about 5 environments on one
spoke, under the blast-radius thresholds (30 environments or 4 spokes), so
the comment was silent. On merge all 57 environments got it at the same time.
Now the same change (key, old value, new value) is summed over the files
that got no note of their own, with the same thresholds. It is a warning:
the build stays green.

The second part is information only. A prod `cl-*-ms` or `cl-*-ss` app is
shared by every customer of its constellation, so a change there reaches
every tenant, while the blast radius counts one environment. The comment
says so as a routine line, and the green status carries it in its tail.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import blast_radius as br  # noqa: E402
import comment_render as cr  # noqa: E402
import diff_preview as m  # noqa: E402

from test_coverage_orchestration import world, _mk_pr, BASE_SHA, PR_SHA  # noqa: E402,F401

K = "appspace.infra.noCore"
# The 11 cohorts of #4565, sorted. Azure and GCP na1-b share a spoke name.
COHORTS = [f"{c}/prod/private-cloud/{s}/weekly/config.yaml" for c, s in (
    ("azure", "na1-a"), ("azure", "na1-b"), ("gcp", "au1-b"), ("gcp", "ca1-a"),
    ("gcp", "eu1-b"), ("gcp", "gb1-b"), ("gcp", "na1-b"), ("gcp", "na2-a"),
    ("gcp", "na3-a"), ("gcp", "na4-a"), ("gcp", "sa1-a"))]
DASHES = ("\u2014", "\u2013")


def _envs(cohort, n, start=0):
    d = cohort.rsplit("/", 1)[0]
    return [f"{d}/pv-e{i}-a/customer.yaml" for i in range(start, start + n)]


def _file(path, old, new, envs):
    return (path, old, new, envs)


def _4565():
    return [_file(c, {"appspace.version": "2603.2.15"},
                  {"appspace.version": "2603.2.15", K: True}, _envs(c, 5))
            for c in COHORTS]


# ── (a) sibling_findings ─────────────────────────────────────────────────

def test_the_4565_shape_is_one_finding():
    out = br.sibling_findings(_4565(), 30, 4)
    assert out == [{"paths": COHORTS, "envs": 55, "spokes": 10, "keys": [K],
                    "changes": ["`appspace.infra.noCore` `unset` → `true`"]}]


def test_different_new_values_do_not_group():
    files = _4565()[:6]
    files[0] = _file(COHORTS[0], {}, {K: False}, _envs(COHORTS[0], 5))
    out = br.sibling_findings(files, 30, 4)
    assert [f["paths"] for f in out] == [COHORTS[1:6]], "the odd one is left out"
    assert br.sibling_findings(files[:2], 30, 4) == []


def test_false_and_unset_are_different_changes():
    """noCore false and unset read the same in Helm, but other keys do not."""
    files = [_file(COHORTS[0], {K: False}, {K: True}, _envs(COHORTS[0], 20)),
             _file(COHORTS[1], {}, {K: True}, _envs(COHORTS[1], 20))]
    assert br.sibling_findings(files, 30, 4) == []


def test_a_version_key_is_ignored():
    files = [_file(c, {"appspace.version": "1"}, {"appspace.version": "2",
                                                  "appspace.x.version": "3"},
                   _envs(c, 5)) for c in COHORTS]
    assert br.sibling_findings(files, 30, 4) == []


def test_two_keys_over_the_same_files_are_one_finding():
    files = [_file(c, {"a.b": 1}, {"a.b": 2, K: True}, _envs(c, 5)) for c in COHORTS[:4]]
    out = br.sibling_findings(files, 30, 4)
    assert len(out) == 1
    assert out[0]["keys"] == ["a.b", K]
    assert out[0]["changes"] == ["`a.b` `1` → `2`", "`appspace.infra.noCore` `unset` → `true`"]
    assert (out[0]["envs"], out[0]["spokes"]) == (20, 4)


def test_one_file_is_never_a_group():
    assert br.sibling_findings(_4565()[:1], 1, 1) == []


def test_the_union_counts_an_env_once():
    envs = _envs(COHORTS[5], 20)
    files = [_file(COHORTS[5], {}, {K: True}, envs),
             _file("gcp/prod/private-cloud/gb1-b/config.yaml", {}, {K: True}, envs + _envs(
                 COHORTS[5], 9, start=20))]
    assert br.sibling_findings(files, 30, 4) == []
    files[1] = files[1][:3] + (envs + _envs(COHORTS[5], 10, start=20),)
    assert [f["envs"] for f in br.sibling_findings(files, 30, 4)] == [30]


def test_a_hidden_key_shows_no_value():
    files = [_file(c, {"a.password": "old"}, {"a.password": "new"}, _envs(c, 5))
             for c in COHORTS[:4]]
    out = br.sibling_findings(files, 30, 4, hide=lambda k: "password" in k)
    assert out[0]["changes"] == ["`a.password` *** → ***"]
    added = [_file(c, {}, {"a.password": "x"}, _envs(c, 5)) for c in COHORTS[:4]]
    assert br.sibling_findings(added, 30, 4, hide=m._SENSITIVE_KEY_RE.search)[0][
        "changes"] == ["`a.password` `unset` → ***"]


def test_values_read_like_yaml():
    assert br._shown(br._ABSENT, False) == "`unset`"
    assert [br._shown(v, False) for v in (True, False, None, 3, "x")] == \
        ["`true`", "`false`", "`null`", "`3`", "`x`"]
    assert br._shown("a`b", False) == "`a'b`"
    assert br._shown("x" * 60, False) == "`" + "x" * 48 + "...`"


# ── (b) the note and the verdict ─────────────────────────────────────────

SIBLING_LINE = (
    "⚠️ **Blast radius.** The same change in 11 sibling `config.yaml` files reaches "
    "**55 environments across 10 spoke(s)**: `appspace.infra.noCore` `unset` → `true`. "
    "Each file alone is under the thresholds, but on merge they all land at the same "
    "time: `azure/prod/private-cloud/na1-a/weekly/config.yaml`, "
    "`azure/prod/private-cloud/na1-b/weekly/config.yaml`, "
    "`gcp/prod/private-cloud/au1-b/weekly/config.yaml`, "
    "`gcp/prod/private-cloud/ca1-a/weekly/config.yaml` (+7 more files).")
SHARED = "gcp/prod/private-cloud/config.yaml"
SINGLE_LINE = (
    "⚠️ **Blast radius.** `gcp/prod/private-cloud/config.yaml` changes non-version values "
    "reaching **40 environments across 5 spoke(s)**: `appspace.replicas`. Changes to this "
    "shared file bypass cohort staging \u2014 they land on everything under it simultaneously "
    "on merge (auto-sync, prune and selfHeal are armed fleet-wide).")


def _lines(findings):
    return br.render_lines(findings, cr._BLAST_RADIUS_HDR, 30, 4)


def test_the_sibling_note_text():
    lines = _lines(br.sibling_findings(_4565(), 30, 4))
    assert lines[0] == SIBLING_LINE
    assert lines[1] == "" and lines[2].startswith("*Thresholds: ≥30 environments")
    assert not any(d in SIBLING_LINE for d in DASHES)


def test_more_than_six_changes_are_counted():
    new = {f"a.k{i}": True for i in range(8)}
    lines = _lines(br.sibling_findings([_file(c, {}, new, _envs(c, 5)) for c in COHORTS[:2]],
                                       10, 4))
    assert "`a.k5` `unset` → `true` (+2 more). Each file" in lines[0]
    assert lines[0].endswith("`azure/prod/private-cloud/na1-b/weekly/config.yaml`.")


def test_the_single_file_note_is_unchanged_and_sorts_first():
    single = br.assess(SHARED, {"appspace.replicas"}, [
        f"gcp/prod/private-cloud/sp{i % 5}-a/pv-e{i}-a/customer.yaml" for i in range(40)],
        30, 4)
    sib = br.sibling_findings(_4565(), 30, 4)
    lines = _lines(sib + [single])
    assert lines[0] == SIBLING_LINE, "55 environments sort before 40"
    assert lines[2] == SINGLE_LINE


def test_the_verdict_names_the_summed_reach():
    lines = _lines(br.sibling_findings(_4565(), 30, 4))
    body = m.format_comment(PR_SHA, {}, base_sha=BASE_SHA, appspace_state_lines=lines)
    assert ("- \U0001f4a5 **Wide-reach config change** \u2014 reaches 55 environments "
            "across 10 spoke(s)") in body
    assert "DO NOT MERGE" not in body


# ── (c) _blast_radius_lines ──────────────────────────────────────────────

A = "gcp/prod/private-cloud/na2-a/weekly/config.yaml"
B = "gcp/prod/private-cloud/na3-a/weekly/config.yaml"
C = "gcp/prod/private-cloud/na4-a/monthly/config.yaml"


def _wire(monkeypatch, sizes, old, new):
    """sizes: {cohort: number of envs}. Each env has one app with its customer.yaml."""
    path_map, vf_map = {}, {}
    for cohort, n in sizes.items():
        path_map[cohort] = []
        for env in _envs(cohort, n):
            app = env.split("/")[-2] + "-" + cohort.split("/")[3] + "-ss"
            path_map[cohort].append(app)
            vf_map[app] = [f"$config/{cohort}", f"$config/{env}"]
    monkeypatch.setattr(m, "_app_value_files_map", vf_map)
    monkeypatch.setattr(m, "_bb_fetch_cached", lambda p, sha, repo=None: (
        new if sha == PR_SHA else old, m.BB_OK))
    return path_map


def test_two_cohorts_under_the_threshold_add_up(monkeypatch):
    pm = _wire(monkeypatch, {A: 18, B: 18}, "appspace: {}\n",
               "appspace:\n  infra:\n    noCore: true\n")
    lines = m._blast_radius_lines([A, B, "gcp/prod/x/customer.yaml"], PR_SHA, BASE_SHA, pm)
    assert lines[0].startswith("⚠️ **Blast radius.** The same change in 2 sibling "
                               "`config.yaml` files reaches **36 environments across "
                               "2 spoke(s)**"), lines
    assert len([l for l in lines if cr._BLAST_RADIUS_HDR in l]) == 1


def test_a_file_with_its_own_note_is_not_counted_again(monkeypatch):
    pm = _wire(monkeypatch, {A: 12, B: 12, C: 40}, "appspace: {}\n",
               "appspace:\n  infra:\n    noCore: true\n")
    lines = m._blast_radius_lines([A, B, C], PR_SHA, BASE_SHA, pm)
    notes = [l for l in lines if cr._BLAST_RADIUS_HDR in l]
    assert len(notes) == 1 and C in notes[0] and "sibling" not in notes[0], notes
    pm = _wire(monkeypatch, {A: 18, B: 18, C: 40}, "appspace: {}\n",
               "appspace:\n  infra:\n    noCore: true\n")
    notes = [l for l in m._blast_radius_lines([A, B, C], PR_SHA, BASE_SHA, pm)
             if cr._BLAST_RADIUS_HDR in l]
    assert [("sibling" in l, "40 environments" in l) for l in notes] == [
        (False, True), (True, False)]
    assert "2 sibling `config.yaml` files reaches **36 environments" in notes[1]


def test_bad_yaml_in_one_cohort_leaves_it_out(monkeypatch):
    pm = _wire(monkeypatch, {A: 18, B: 18}, "appspace: {}\n",
               "appspace:\n  infra:\n    noCore: true\n")
    good = m._bb_fetch_cached
    monkeypatch.setattr(m, "_bb_fetch_cached", lambda p, sha, repo=None: (
        ("appspace: [\n", m.BB_OK) if p == B else good(p, sha)))
    assert m._blast_radius_lines([A, B], PR_SHA, BASE_SHA, pm) == []


# ── (d) _tenant_wide ─────────────────────────────────────────────────────

PROD_VF = ["$config/gcp/prod/public-cloud/na1-a/cl-prod-b/config.yaml",
           "$config/gcp/prod/public-cloud/na1-a/cl-prod-b/constellation/customer.yaml"]
STAGE_VF = ["$config/gcp/stage/public-cloud/na1/cl-stage-b/config.yaml",
            "$config/gcp/stage/public-cloud/na1/cl-stage-b/constellation/customer.yaml"]


def _res(outcome=m.OUT_DIFF):
    return m.DiffResult("d", [], 1, outcome == m.OUT_DIFF, "", outcome, "")


def test_tenant_wide(monkeypatch):
    monkeypatch.setattr(m, "_app_value_files_map", {
        "cl-prod-b-ms": PROD_VF, "cl-prod-b-ss": PROD_VF, "cl-stage-b-ms": STAGE_VF,
        "cl-prod-b-app1-glb": PROD_VF[:1], "argocd-x/cl-prod-c-ss": PROD_VF,
        "cl-prod-d-ms": PROD_VF})
    assert m._tenant_wide({"cl-prod-b-ms": _res()}) == ["cl-prod-b"]
    assert m._tenant_wide({"cl-stage-b-ms": _res()}) == []
    assert m._tenant_wide({"cl-prod-b-app1-glb": _res()}) == []
    assert m._tenant_wide({"cl-prod-b-ms": _res(m.OUT_NO_DIFF)}) == []
    assert m._tenant_wide({"cl-prod-b-ms": _res(), "cl-prod-b-ss": _res()}) == ["cl-prod-b"]
    assert m._tenant_wide({"argocd-x/cl-prod-c-ss": _res(), "cl-prod-b-ss": _res()}) == [
        "cl-prod-b", "cl-prod-c"], "matched on the bare app name"
    assert m._tenant_wide({"cl-prod-e-ms": _res()}) == [], "no value files, no flag"
    assert m._tenant_wide({"cl-prod-d-ms": ("d", True, None)}) == ["cl-prod-d"]
    assert m._tenant_wide({}) == []


def test_tenant_wide_lines():
    assert m._tenant_wide_lines([]) == []
    line = m._tenant_wide_lines(["cl-prod-b"])
    assert line == [
        "\U0001f310 **Reaches every public-cloud tenant.** This PR changes the shared ms "
        "or ss app of `cl-prod-b`. It serves every customer of its constellation, so the "
        "change reaches every tenant there, not one environment.", ""]
    assert "`cl-a`, `cl-b`" in m._tenant_wide_lines(["cl-a", "cl-b"])[0]
    assert not any(d in line[0] for d in DASHES)


# ── (e) the merge summary ────────────────────────────────────────────────

def _summary(state):
    return cr._build_merge_summary({}, {}, None, None, state, None, False)


def test_the_tenant_line_is_routine():
    lines = _summary(m._tenant_wide_lines(["cl-prod-b"]))
    assert lines[2] == cr._VERDICTS[cr._SEV_ROUTINE]
    assert [l for l in lines if l.startswith("- ")] == [
        "- \U0001f310 **Reaches every public-cloud tenant** of `cl-prod-b`"]
    assert cr.status_lead("\n".join(lines)) == ""


def test_the_tenant_line_names_every_constellation_and_sorts_last():
    lines = _summary(m._tenant_wide_lines(["cl-a", "cl-b"])
                     + _lines(br.sibling_findings(_4565(), 30, 4)))
    bullets = [l for l in lines if l.startswith("- ")]
    assert bullets[0].startswith("- \U0001f4a5 **Wide-reach config change**")
    assert bullets[-1] == "- \U0001f310 **Reaches every public-cloud tenant** of `cl-a`, `cl-b`"
    assert cr.status_lead("\n".join(lines)).startswith("⚠️ Wide-reach config change")


def test_the_header_in_another_line_is_not_the_tenant_line():
    assert _summary(["- a value: " + cr._TENANT_WIDE_HDR])[2] == cr._VERDICTS[cr._SEV_ROUTINE]
    assert not any("public-cloud tenant" in l for l in _summary(
        ["- a value: " + cr._TENANT_WIDE_HDR]))


# ── (f) process_pr ───────────────────────────────────────────────────────

CL = "gcp/prod/public-cloud/na1-a/cl-prod-b/constellation"
CL_ID, CL_ANC = f"{CL}/customer.yaml", f"{CL}/cicd-versions.yaml"
CL_APPS = ["cl-prod-b-ms", "cl-prod-b-ss"]
CL_YAML = "appspace:\n  customerName: prod-b\n  version: 2603.2.15\n"


def _cl_world(world, monkeypatch, vf=PROD_VF):
    sinks, plan = world
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)
    for app in CL_APPS:
        monkeypatch.setitem(m._app_chart_map, app, "appspace-" + app[-2:])
        monkeypatch.setitem(m._app_chart_revision_map, app, "2603.2.15")
    monkeypatch.setattr(m, "_app_value_files_map", {a: vf for a in CL_APPS})
    monkeypatch.setattr(m, "get_pr_changed_files", lambda pr_id, repo=None: ([CL_ANC], {}))
    monkeypatch.setattr(m, "_bb_fetch_status", lambda path, sha, repo=None: (
        (CL_YAML, m.BB_OK) if path == CL_ID else (None, m.BB_NOT_FOUND)))
    plan["cl-prod-b-ms"] = m.DiffResult(
        "--- main\n+++ pr", [("Deployment/webx", "-replicas: 2\n+replicas: 3")],
        1, True, "", m.OUT_DIFF, "")
    m.process_pr(_mk_pr(), {CL_ID: CL_APPS, CL_ANC: CL_APPS}, base_sha=BASE_SHA)
    return sinks.upserts[-1], sinks.statuses[-1]


def test_a_prod_constellation_change_says_so_in_the_status(world, monkeypatch):
    body, (state, desc) = _cl_world(world, monkeypatch)
    assert m._extract_status_token(body) == "clean"
    assert (state, desc) == ("SUCCESSFUL", "1 resource(s) will change | \U0001f310 every "
                                           "tenant of cl-prod-b - review comment")
    assert "- \U0001f310 **Reaches every public-cloud tenant** of `cl-prod-b`" in body
    assert "\U0001f310 **Reaches every public-cloud tenant.** This PR changes" in body
    assert "✅ **Routine**" in body


def test_a_stage_constellation_change_is_quiet(world, monkeypatch):
    body, (state, desc) = _cl_world(world, monkeypatch, vf=STAGE_VF)
    assert m._extract_status_token(body) == "clean"
    assert (state, desc) == ("SUCCESSFUL", "1 resource(s) will change - review comment")
    assert "public-cloud tenant" not in body


def test_a_bug_in_the_tenant_check_keeps_the_comment(world, monkeypatch):
    monkeypatch.setattr(m, "_tenant_wide", lambda app_results: 1 / 0)
    body, (state, desc) = _cl_world(world, monkeypatch)
    assert m._extract_status_token(body) == "clean"
    assert (state, desc) == ("SUCCESSFUL", "1 resource(s) will change - review comment")
