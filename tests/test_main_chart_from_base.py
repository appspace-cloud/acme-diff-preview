"""The main side renders the chart version main has, not the one Argo CD has.

The Argo CD Application's targetRevision lags behind main: the
ApplicationSet has to refresh and the app list is cached for PATH_MAP_TTL.
A PR rendered in that window used the old version for the main side, so it
showed a version bump that was already merged (or, the other way round,
hid a real one). The main chart version now comes from the value files at
the base sha, with the same resolver the PR side uses, and the Argo CD
targetRevision is only a fallback when the files set no version.
"""
import pytest

import diff_preview as m

APP = "pv-example-a-ss"
ENV_DIR = "gcp/qa/private-cloud/ap1/monthly/pv-example-a"
COHORT = "gcp/qa/private-cloud/ap1/monthly/config.yaml"
CUSTOMER = f"{ENV_DIR}/customer.yaml"
VALUE_FILES = [f"$config/{COHORT}", f"$config/{CUSTOMER}"]
PR_SHA, BASE_SHA = "prsha0000001", "basesha00001"
OLD, NEW = "2604.0.6", "2604.0.6-rev2"


def _version(v):
    return f"appspace:\n  customerName: example\n  version: {v}\n"


def _world(monkeypatch, argo_rev, files_by_sha):
    """files_by_sha: {sha: {clean_path: content}}."""
    monkeypatch.setitem(m._app_chart_map, APP, "appspace-supporting-services")
    monkeypatch.setitem(m._app_chart_revision_map, APP, argo_rev)
    monkeypatch.setitem(m._app_chart_registry_map, APP, "registry.example.com")
    monkeypatch.setitem(m._app_value_files_map, APP, list(VALUE_FILES))
    monkeypatch.setitem(m._app_namespace_map, APP, "pv-example-a")
    def fake_fetch(clean, sha, repo=None):
        content = files_by_sha.get(sha, {}).get(clean.replace("$config/", "").lstrip("/"))
        if content is None:
            return None, m.BB_NOT_FOUND
        return content, m.BB_OK

    monkeypatch.setattr(m, "_bb_fetch_status", fake_fetch)


# ── _main_chart_revision ──────────────────────────────────────────────────

@pytest.mark.parametrize("argo_rev,base_rev", [(OLD, NEW), (NEW, OLD)])
def test_main_version_comes_from_the_base_files(monkeypatch, argo_rev, base_rev):
    _world(monkeypatch, argo_rev, {BASE_SHA: {CUSTOMER: _version(base_rev)}})
    assert m._main_chart_revision(APP, BASE_SHA) == base_rev


def test_cohort_version_counts_when_customer_sets_none(monkeypatch):
    _world(monkeypatch, OLD, {BASE_SHA: {
        COHORT: _version(NEW),
        CUSTOMER: "appspace:\n  customerName: example\n",
    }})
    assert m._main_chart_revision(APP, BASE_SHA) == NEW


def test_argo_revision_is_the_fallback_when_files_set_no_version(monkeypatch):
    _world(monkeypatch, OLD, {BASE_SHA: {CUSTOMER: "appspace:\n  customerName: example\n"}})
    assert m._main_chart_revision(APP, BASE_SHA) == OLD


def test_argo_revision_is_the_fallback_when_customer_yaml_is_unread(monkeypatch):
    _world(monkeypatch, OLD, {BASE_SHA: {COHORT: _version(NEW)}})
    assert m._main_chart_revision(APP, BASE_SHA) == OLD


def test_no_base_sha_keeps_the_argo_revision(monkeypatch):
    _world(monkeypatch, OLD, {BASE_SHA: {CUSTOMER: _version(NEW)}})
    assert m._main_chart_revision(APP, None) == OLD


def test_app_not_discovered_yet_stays_unknown(monkeypatch):
    _world(monkeypatch, OLD, {BASE_SHA: {CUSTOMER: _version(NEW)}})
    m._app_chart_revision_map.pop(APP)
    assert m._main_chart_revision(APP, BASE_SHA) is None


# ── bump detection ────────────────────────────────────────────────────────

def test_version_already_on_main_is_not_a_bump(monkeypatch):
    # Main pinned NEW minutes ago, Argo CD still reports OLD, and the PR
    # edits customer.yaml for something else.
    pr_customer = _version(NEW) + "  zeroPods: false\n"
    _world(monkeypatch, OLD, {
        BASE_SHA: {CUSTOMER: _version(NEW)},
        PR_SHA: {CUSTOMER: pr_customer},
    })
    new_rev, invalid = m._pr_chart_revision_checked(
        APP, [CUSTOMER], PR_SHA, main_sha=BASE_SHA)
    assert (new_rev, invalid) == (None, False)


def test_pr_setting_the_version_argo_still_runs_is_a_real_change(monkeypatch):
    # Main moved back to OLD, Argo CD still runs NEW, and the PR sets NEW
    # again. Against main that is a change, not "no change".
    _world(monkeypatch, NEW, {
        BASE_SHA: {CUSTOMER: _version(OLD)},
        PR_SHA: {CUSTOMER: _version(NEW)},
    })
    new_rev, invalid = m._pr_chart_revision_checked(
        APP, [CUSTOMER], PR_SHA, main_sha=BASE_SHA)
    assert (new_rev, invalid) == (NEW, False)


# ── main render ───────────────────────────────────────────────────────────

def test_main_side_renders_the_base_version(monkeypatch):
    _world(monkeypatch, OLD, {})
    pulled = []

    def fake_chart(reg, chart, ver):
        pulled.append(ver)
        return f"/fake/{ver}"

    monkeypatch.setattr(m, "_ensure_chart", fake_chart)
    monkeypatch.setattr(m, "_fetch_value_files", lambda vfs, sha: {
        vf: _version(NEW) for vf in vfs})
    monkeypatch.setattr(m, "_main_render_content_key", lambda *a: "k-base")
    monkeypatch.setattr(m, "_main_render_cache_get", lambda k: (None, None, "miss"))
    monkeypatch.setattr(m, "_main_render_cache_put", lambda *a: None)
    monkeypatch.setattr(m, "_helm_template", lambda chart, release, ns, vals: (
        f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\ndata:\n  chart: {chart}\n",
        None))

    step = m._run_one_diff(APP, PR_SHA, BASE_SHA)
    assert step[1] is None, step[:3]
    assert pulled == [NEW, NEW]
    assert step[0] == ""          # both sides on NEW: nothing changes
    assert step[3] is None        # no version change reported


# ── comment ───────────────────────────────────────────────────────────────

def test_comment_says_argo_has_not_applied_main_yet():
    result = m.DiffResult("", [], 0, False, None, m.OUT_NO_DIFF, None)
    body = m.format_comment(PR_SHA, {APP: result}, base_sha=BASE_SHA,
                            argo_lag={APP: (OLD, NEW)})
    line = next(ln for ln in body.splitlines() if "has not applied main yet" in ln)
    assert "pv-example-a" in line and OLD in line and NEW in line


def test_comment_has_no_lag_line_when_argo_is_current():
    result = m.DiffResult("", [], 0, False, None, m.OUT_NO_DIFF, None)
    body = m.format_comment(PR_SHA, {APP: result}, base_sha=BASE_SHA)
    assert "has not applied main yet" not in body
