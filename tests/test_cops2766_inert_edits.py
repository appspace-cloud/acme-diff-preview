"""COPS-2766 block 4, item 3: an edit that changes nothing gets a warning.

A live env's customer.yaml changes keys, and every app of that file renders
the same. Helm ignores a key that no chart reads, with no error, so the PR
looks done: #4463 added `autosync: true`, #4554 wrote env vars under
`microservices.library`. The comment now gets a 💤 REVIEW line, the build
stays green [clean], and the green status leads with it.

Keys that the ApplicationSet or another panel owns are left out, and so are
the keys the higher-layer note already lists (#4647). The pause itself stays
with the auto-sync panel (#4639).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import comment_render as cr  # noqa: E402
import diff_preview as m  # noqa: E402
import values_redundancy as vr  # noqa: E402

from test_coverage_orchestration import (  # noqa: E402,F401
    world, _mk_pr, PATH_MAP, BASE_SHA, PR_SHA, IDENTITY, IDENTITY_YAML)

SK = ("acme-config-dev", 991)
ERR = object()

PV = "gcp/prod/private-cloud/na1-b/monthly-friday/pv-chevron-c"
ENV = f"{PV}/customer.yaml"
COHORT = "gcp/prod/private-cloud/na1-b/monthly-friday/config.yaml"
CL_APP = "gcp/prod/public-cloud/na1-a/cl-prod-b/app1/customer.yaml"
BASE = "appspace:\n  customerName: chevron\n"
APPS = ["pv-chevron-c-ms", "pv-chevron-c-ss"]
NO = m.DiffResult("", [], 0, False, "", m.OUT_NO_DIFF, "")
YES = m.DiffResult("d", [], 1, True, "", m.OUT_DIFF, "")
AUTOSYNC_HINT = ("  - Only `autosync: false` pauses auto-sync. `true` is the default, "
                 "so this line changes nothing.")
LIBRARY = ("  microservices:\n    library:\n      env:\n"
           "        appspace_platform_enablePlatformCoreCalls:\n          value: true\n"
           "        ASPNETCORE_ENVIRONMENT:\n          value: 'dev'\n"
           "        Serilog_MinimumLevel_Default:\n          value: 'Debug'\n")
LIBRARY_KEYS = [f"appspace.microservices.library.env.{k}.value" for k in (
    "ASPNETCORE_ENVIRONMENT", "Serilog_MinimumLevel_Default",
    "appspace_platform_enablePlatformCoreCalls")]


def _serve(monkeypatch, files):
    """_bb_fetch_status from `files[sha][path]`, ERR a failed read; returns the reads."""
    calls = []

    def fetch(path, sha, repo=None):
        calls.append((path, sha))
        v = files.get(sha, {}).get(path)
        if v is ERR:
            return None, m.BB_ERROR
        return (None, m.BB_NOT_FOUND) if v is None else (v, m.BB_OK)
    monkeypatch.setattr(m, "_bb_fetch_status", fetch)
    m._vf_cache.clear()     # a test may serve new bodies at the same sha
    return calls


def _lines(monkeypatch, head, base=BASE, path=ENV, results=None, redundant=(),
           cohort=(None, None), path_map=None):
    files = {"base1": {path: base, COHORT: cohort[0]}, "head1": {path: head, COHORT: cohort[1]}}
    _serve(monkeypatch, files)
    pm = {path: APPS} if path_map is None else path_map
    res = {a: NO for a in APPS} if results is None else results
    return m._inert_edit_lines([path], "head1", "base1", pm, res, list(redundant),
                               repo="acme-config-prod")


def _rows(lines):
    return [x for x in lines if x.startswith("- ")]


# ── which edit is inert ──────────────────────────────────────────────────

def test_4463_autosync_true_changes_nothing(monkeypatch):
    lines = _lines(monkeypatch, BASE + "  autosync: true\n")
    assert lines[:4] == [
        "### \U0001f4a4 Edits with no effect", "",
        f"{cr._INERT_EDIT_HDR} 1 key changed, but no rendered manifest changed:", ""]
    assert _rows(lines) == ["- `pv-chevron-c`: `appspace.autosync`"]
    assert AUTOSYNC_HINT in lines
    assert lines[-2:] == ["*Check the key path and the service name. Helm ignores a key "
                          "that no chart reads, with no error.*", ""]


def test_4554_keys_under_a_service_no_chart_reads(monkeypatch):
    lines = _lines(monkeypatch, BASE + LIBRARY)
    assert f"{cr._INERT_EDIT_HDR} 3 keys changed, but no rendered manifest changed:" in lines
    assert _rows(lines) == ["- `pv-chevron-c`: " + ", ".join(f"`{k}`" for k in LIBRARY_KEYS)]
    assert not [x for x in lines if x.startswith("  - ")], "no hint for these keys"


def test_a_long_list_shows_five_keys_and_a_count(monkeypatch):
    head = BASE + "".join(f"  k{i}: {i}\n" for i in range(7))
    row = _rows(_lines(monkeypatch, head))[0]
    assert row == "- `pv-chevron-c`: " + ", ".join(
        f"`appspace.k{i}`" for i in range(5)) + " (+2 more)"


def test_many_envs_show_ten_rows_and_a_count(monkeypatch):
    paths = [f"gcp/prod/private-cloud/na1-b/weekly/pv-e{i:02}-a/customer.yaml" for i in range(12)]
    _serve(monkeypatch, {"base1": {p: BASE for p in paths},
                         "head1": {p: BASE + "  web: 1\n" for p in paths}})
    lines = m._inert_edit_lines(paths, "head1", "base1", {p: [p] for p in paths},
                                {p: NO for p in paths}, [], repo="acme-config-prod")
    assert f"{cr._INERT_EDIT_HDR} 12 keys changed, but no rendered manifest changed:" in lines
    assert _rows(lines)[0] == "- `pv-e00-a`: `appspace.web`"
    assert _rows(lines)[10:] == ["- ... and 2 more environment(s)"]
    _l, b = _bullets(lines)
    assert b == ["\U0001f4a4 **An edit changes nothing rendered**: `appspace.web` in "
                 "`pv-e00-a` (+11 more)"]


def test_4647_keys_the_higher_layer_note_lists_stay_out(monkeypatch):
    head = BASE + "  microservices:\n    platform:\n      replicas: 0\n    web:\n      x: 1\n"
    listed = {"path": ENV, "redundant": [
        {"key": "appspace.microservices.platform.replicas", "source": COHORT}]}
    assert _rows(_lines(monkeypatch, head, redundant=[listed])) == [
        "- `pv-chevron-c`: `appspace.microservices.web.x`"]
    listed["redundant"].append({"key": "appspace.microservices.web.x", "source": COHORT})
    assert _lines(monkeypatch, head, redundant=[listed]) == []


@pytest.mark.parametrize("head,base", [
    (BASE + "  autosync: false\n", BASE),                         # #4639: a pause
    (BASE, BASE + "  autosync: false\n"),                         # a resume
    (BASE + "  autosync: true\n", BASE + "  autosync: false\n"),  # a resume
])
def test_a_pause_or_resume_is_the_auto_sync_panels(monkeypatch, head, base):
    assert _lines(monkeypatch, head, base) == []


def test_autosync_true_under_a_paused_cohort_resumes_the_env(monkeypatch):
    """The ApplicationSet merges customer.yaml over the cohort config.yaml, and
    a key in customer.yaml wins. So `true` there undoes a cohort `false`."""
    paused = "appspace:\n  autosync: false\n"
    assert _lines(monkeypatch, BASE + "  autosync: true\n", cohort=(paused, paused)) == []
    lines = _lines(monkeypatch, BASE + "  autosync: true\n",
                   cohort=("appspace: {}\n", "appspace: {}\n"))
    assert _rows(lines) == ["- `pv-chevron-c`: `appspace.autosync`"]


def test_an_autosync_false_that_keeps_the_pause_gets_no_true_hint(monkeypatch):
    lines = _lines(monkeypatch, BASE + "  autosync: False\n", BASE + "  autosync: 'false'\n")
    assert _rows(lines) == ["- `pv-chevron-c`: `appspace.autosync`"]
    assert AUTOSYNC_HINT not in lines


def test_the_cohort_is_read_only_for_autosync(monkeypatch):
    lines = _lines(monkeypatch, BASE + "  web: 1\n", cohort=(ERR, ERR))
    assert _rows(lines) == ["- `pv-chevron-c`: `appspace.web`"]


@pytest.mark.parametrize("results", [
    {"pv-chevron-c-ms": YES, "pv-chevron-c-ss": NO},              # one app has a diff
    {"pv-chevron-c-ms": NO},                                      # one over the cap
    {"pv-chevron-c-ms": NO, "pv-chevron-c-ss": m.DiffResult(
        "", [], 0, False, "x", m.OUT_INDETERMINATE, m.REASON_TIMEOUT)},
    {"pv-chevron-c-ms": NO, "pv-chevron-c-ss": m.DiffResult(
        "", [], 0, False, "x", m.OUT_ERROR, "boom")},
    {"pv-chevron-c-ms": NO, "pv-chevron-c-ss": m.DiffResult(
        "", [], 0, False, None, m.OUT_DECOMMISSIONED, "")},
])
def test_every_app_of_the_file_must_render_the_same(monkeypatch, results):
    assert _lines(monkeypatch, BASE + "  web: 1\n", results=results) == []


@pytest.mark.parametrize("edit", [
    "  version: 2603.3.10\n",
    "  microservices:\n    definitions:\n      web:\n        image:\n          version: 1\n",
    "  infra:\n    deployWindows: true\n",
    "  decommission: true\n",
    "  decommissionPurgeData: true\n",
    "  suffix: b\n",
])
def test_keys_that_other_checks_own_are_skipped(monkeypatch, edit):
    assert _lines(monkeypatch, BASE + edit) == []


def test_a_customer_name_change_is_skipped(monkeypatch):
    assert _lines(monkeypatch, "appspace:\n  customerName: chevron2\n") == []


@pytest.mark.parametrize("key,edit", [
    ("definitions.web.replicas", "    definitions:\n      web:\n        replicas: 3\n"),
    ("web.replicas", "    web:\n      replicas: 3\n"),
])
def test_a_replicas_key_names_the_scaler(monkeypatch, key, edit):
    lines = _lines(monkeypatch, BASE + "  microservices:\n" + edit)
    assert _rows(lines) == [f"- `pv-chevron-c`: `appspace.microservices.{key}`"]
    assert ("  - `replicas` does nothing while an HPA or the ping-scaler runs `web`. For a "
            "fixed count, use `microservices.acmePingScaler.customReplicas.web` with the "
            "ping-scaler, or the service's `hpa.minReplicas` with an HPA.") in lines


def test_a_helm_key_acts_only_in_the_legacy_pipeline(monkeypatch):
    lines = _lines(monkeypatch, BASE + "  helmForceUpgrade: true\n")
    assert ("  - `appspace.helmForceUpgrade` only acts in the legacy Helm pipeline, "
            "which ArgoCD replaced.") in lines


def test_a_removed_key_is_ignored(monkeypatch):
    assert _lines(monkeypatch, BASE, BASE + "  web: 1\n") == []


@pytest.mark.parametrize("head,base,path,path_map", [
    (BASE + "  web: 1\n", BASE, ENV, {}),                          # not a live env
    (BASE + "  web: 1\n", BASE, f"{PV}/cicd-versions.yaml", None),  # not customer.yaml
    (BASE + "  web: 1\n", None, ENV, None),                         # a new file
    ("appspace: [\n", BASE, ENV, None),                             # does not parse
])
def test_what_is_not_a_live_env_customer_yaml_is_skipped(monkeypatch, head, base, path,
                                                          path_map):
    assert _lines(monkeypatch, head, base, path=path, path_map=path_map) == []


def test_a_public_cloud_app_is_named_by_its_cl_folder(monkeypatch):
    lines = _lines(monkeypatch, BASE + "  web: 1\n", path=CL_APP)
    assert _rows(lines) == ["- `cl-prod-b/app1`: `appspace.web`"]


@pytest.mark.parametrize("which", ["file", "cohort"])
def test_a_failed_read_is_retried_never_passed(monkeypatch, which):
    kw = {"base": ERR} if which == "file" else {"cohort": (ERR, None)}
    with pytest.raises(m.ValueFileUnreadable):
        _lines(monkeypatch, BASE + "  autosync: true\n", **kw)


# ── the merge summary and the status ─────────────────────────────────────

def _bullets(state):
    lines = cr._build_merge_summary({"pv-chevron-c-ms": NO}, {}, None, None, state, None,
                                    False)
    return lines, [x[2:] for x in lines if x.startswith("- ")]


def test_the_summary_names_the_first_key_and_counts_the_rest(monkeypatch):
    _l, b = _bullets(_lines(monkeypatch, BASE + "  autosync: true\n"))
    assert b == ["\U0001f4a4 **An edit changes nothing rendered**: `appspace.autosync` "
                 "in `pv-chevron-c`"]
    _l, b = _bullets(_lines(monkeypatch, BASE + LIBRARY))
    assert b == ["\U0001f4a4 **An edit changes nothing rendered**: "
                 f"`{LIBRARY_KEYS[0]}` in `pv-chevron-c` (+2 more)"]


def test_it_sorts_after_other_reviews_and_before_the_higher_layer_note(monkeypatch):
    inert = _lines(monkeypatch, BASE + "  web: 1\n")
    _l, b = _bullets([cr._VALUES_REDUNDANCY_HDR, ""] + inert + [cr._BLAST_RADIUS_HDR])
    assert [x[:1] for x in b] == ["\U0001f4a5", "\U0001f4a4", "\U0001f4da"], b


def test_the_green_status_leads_with_it(monkeypatch):
    lines, _b = _bullets(_lines(monkeypatch, BASE + "  autosync: true\n"))
    assert "⚠️ **Review before merging** (1 item(s))" in lines
    assert cr.status_lead("\n".join(lines) + "\n---\n") == (
        "⚠️ An edit changes nothing rendered: appspace.autosync in pv-chevron-c")


def test_the_no_diff_status_names_the_inert_edit():
    inert = "No manifest changes - some config edits change nothing rendered (see PR comment)"
    assert vr.noop_status_hint(False, True, has_inert=True) == inert
    assert m._clean_status_description(False, True, has_inert=True) == inert
    assert "higher layer" in vr.noop_status_hint(True, True, has_inert=True)
    assert vr.noop_status_hint(False, True) == m._clean_status_description(False, True)


# ── process_pr ───────────────────────────────────────────────────────────

@pytest.fixture()
def inert_pr(world, monkeypatch):
    """The PR changes pv-orch-a's customer.yaml, `files[sha][path]`; no merge
    preview, so the head is read at the PR commit."""
    sinks, plan = world
    files = {BASE_SHA: {IDENTITY: IDENTITY_YAML},
             PR_SHA: {IDENTITY: IDENTITY_YAML + "  autosync: true\n"}}
    _serve(monkeypatch, files)
    monkeypatch.setattr(m, "_merge_preview", lambda repo, base, pr: (None, None))
    monkeypatch.setattr(m, "get_pr_changed_files", lambda pr_id, repo=None: ([IDENTITY], {}))
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)
    monkeypatch.setattr(m, "_app_value_files_map", {})
    monkeypatch.setattr(m, "_retry_backoff", {})

    def run():
        m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
        return sinks.upserts[-1], sinks.statuses[-1]
    return run, plan, files


def test_process_pr_4463_is_green_with_the_warning(inert_pr):
    run, _plan, _files = inert_pr
    body, (state, desc) = run()
    assert state == "SUCCESSFUL" and m._extract_status_token(body) == "clean"
    assert "### \U0001f4a4 Edits with no effect" in body and AUTOSYNC_HINT in body
    tail = "No manifest changes - some config edits change nothing rendered (see PR comment)"
    assert desc == ("⚠️ An edit changes nothing rendered: appspace.autosync in "
                    f"pv-orch-a | {tail}")
    assert f"**Status:** ✅ {tail}" in body
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA)


def test_process_pr_4639_shows_only_the_pause(inert_pr):
    run, _plan, files = inert_pr
    files[PR_SHA][IDENTITY] = IDENTITY_YAML + "  autosync: false\n"
    body, (state, _desc) = run()
    assert state == "SUCCESSFUL" and m._extract_status_token(body) == "clean"
    assert cr._AUTOSYNC_PAUSED_HDR in body and cr._INERT_EDIT_HDR not in body


def test_process_pr_an_app_with_a_diff_gets_no_line(inert_pr):
    run, plan, _files = inert_pr
    # Built here: another test may reload diff_preview, and format_comment checks the class.
    plan["pv-orch-a-ms"] = m.DiffResult("d", [], 1, True, "", m.OUT_DIFF, "")
    body, (state, _desc) = run()
    assert state == "SUCCESSFUL" and m._extract_status_token(body) == "clean"
    assert cr._INERT_EDIT_HDR not in body


def test_process_pr_4647_keys_the_cohort_already_sets(inert_pr):
    run, _plan, files = inert_pr
    pin = "  microservices:\n    platform:\n      replicas: 0\n"
    cohort = "gcp/dev/private-cloud/ap1/custom/config.yaml"
    files[PR_SHA].update({IDENTITY: IDENTITY_YAML + pin, cohort: "appspace:\n" + pin})
    files[BASE_SHA][cohort] = "appspace:\n" + pin
    body, (state, desc) = run()
    assert state == "SUCCESSFUL" and m._extract_status_token(body) == "clean"
    assert cr._VALUES_REDUNDANCY_HDR in body and cr._INERT_EDIT_HDR not in body
    assert desc == ("No manifest changes — some config edits already match a higher "
                    "layer (see PR comment)")


def test_process_pr_a_failed_read_retries(inert_pr, monkeypatch):
    run, _plan, _files = inert_pr

    def boom(*a, **k):
        raise m.ValueFileUnreadable("customer.yaml")
    monkeypatch.setattr(m, "_inert_edit_lines", boom)
    body, (state, desc) = run()
    assert state == "FAILED" and "will retry" in desc
    assert m._extract_status_token(body) == "transient"
    assert SK not in m._seen and SK in m._retry_backoff


def test_process_pr_a_bug_in_the_panel_never_breaks_the_comment(inert_pr, monkeypatch):
    run, _plan, _files = inert_pr

    def boom(*a, **k):
        raise KeyError("x")
    monkeypatch.setattr(m, "_inert_edit_lines", boom)
    body, (state, _desc) = run()
    assert state == "SUCCESSFUL" and m._extract_status_token(body) == "clean"
    assert cr._INERT_EDIT_HDR not in body
