"""COPS-2766 block 3, item 4 (C03): a new env that shares GCP objects.

The BigQuery dataset is `{prefix}_{customerName}_analytics_{bigQuery.suffix}`
and the user content bucket and DNS record are built from customerName too.
So a new env with the customerName of a live env manages the same GCP objects:
two KCC resources on one object fail or fight (acme-config-prod #4333). It is
a warning, never a gate: the user decided never to block a shared
customerName, and a planned migration shares them on purpose.

The siblings are the live envs whose own customer.yaml has that customerName,
or whose folder is `<prefix>-<customerName>-<suffix>`. The folder is not
enough: #4671 was pv-nbc--aec1-a with customerName nbc, and #3286 was an aec
pv-usbank-c next to the prod pv-usbank-c. The check lines count in the merge
summary, so the build stays green with the verdict Review.
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import comment_render as cr  # noqa: E402
import diff_preview as m  # noqa: E402
import user_content  # noqa: E402

from test_coverage_orchestration import (  # noqa: E402,F401
    world, _mk_pr, PATH_MAP, BASE_SHA, PR_SHA, IDENTITY, IDENTITY_YAML, ANCILLARY)

pytestmark = pytest.mark.fleet_reads

H, B = PR_SHA, BASE_SHA
AEC = "gcp/aec/private-cloud"
PV = "gcp/prod/private-cloud"
GSK_C = f"{AEC}/na2-a/pv-gsk--aec1-c/customer.yaml"     # #4333, the new clone
GSK_B = f"{AEC}/na4-a/pv-gsk--aec1-b/customer.yaml"     # live
NBC_A = f"{AEC}/na2-a/monthly/pv-nbc--aec1-a/customer.yaml"   # #4671
NBC_B = f"{PV}/na2-a/monthly/pv-nbc-b/customer.yaml"
USB_AEC = f"{AEC}/na1-b/pv-usbank-c/customer.yaml"      # #3286
USB_PV = f"{PV}/na1-b/monthly/pv-usbank-c/customer.yaml"
OTHER = f"{PV}/na1-b/monthly/pv-other-a/customer.yaml"
RED = ("⛔", "\U0001f6a8", "❌")
TAIL = ("Two KCC resources on one object fail or fight (acme-config-prod #4333). If this "
        "is not a planned migration, change `customerName`, or set "
        "`appspace.bigQuery.suffix` to a new value.")


def _region(key="na1", suffix="a", domain="appspacestorage.com",
            project="appspace-cloud-private-bq"):
    """A spoke config.yaml: the user content bucket, its zone and BigQuery."""
    return ("appspace:\n  buckets:\n    userContent:\n"
            f"      suffix: \"{suffix}\"\n" + (f"      domain: {domain}\n" if domain else "")
            + f"      regionMapping:\n        {key}:\n          region: us-central1\n"
            "      managedZone:\n        domain: appspacestorage.com\n"
            + (f"  bigQuery:\n    project: {project}\n" if project else ""))


def _cust(name, suffix, extra=""):
    return (f"appspace:\n  customerName: {name}\n  suffix: {suffix}\n  zeroPods: true\n"
            f"  version: 2603.3.7\n{extra}")


TREE = {   # the ancestors of every path above
    f"{AEC}/config.yaml": "appspace:\n  prefix: pv\n",
    f"{PV}/config.yaml": "appspace:\n  prefix: pv\n",
    f"{AEC}/na2-a/config.yaml": _region(),
    f"{AEC}/na4-a/config.yaml": _region(),
    f"{AEC}/na1-b/config.yaml": _region(),
    f"{PV}/na2-a/config.yaml": _region(),
    f"{PV}/na1-b/config.yaml": _region(),
    f"{PV}/na1-b/monthly/config.yaml": "appspace:\n  tier: monthly\n",
}


def _serve(monkeypatch, head, base=None, error=()):
    """head/base: {path: text} on top of TREE. A path in `error` fails."""
    head, base = dict(TREE, **head), dict(TREE, **(base or {}))

    def fetch(path, sha, repo=None):
        if path in error:
            return None, m.BB_ERROR
        side = head if sha == H else base if sha == B else {}
        return (side[path], m.BB_OK) if path in side else (None, m.BB_NOT_FOUND)
    monkeypatch.setattr(m, "_bb_fetch_status", fetch)


def _fleet(monkeypatch, paths):
    monkeypatch.setattr(m, "_fleet_identity_files", lambda repo=None: sorted(paths))


def _cand(path):
    return {"name": path.split("/")[-2], "config_file": path,
            "env_dir": path.rsplit("/", 1)[0], "all_yaml_files": [path]}


def _lines(path, changed=(), renames=None, base_sha=B):
    return m._new_env_shared_lines(_cand(path), H, base_sha, None, changed or [path], renames)


def _objs(stem, name, dataset=True):
    bq = [f"BigQuery dataset `{stem.replace('-', '_')}_analytics_a`"] if dataset else []
    host = f"{stem}-content-{name}.appspacestorage.com"
    return ", ".join(bq + [f"bucket `{host}`", f"DNS `{host}.`"])


def _line(path, live, objs):
    return (f"- ⚠️ **Check:** `{path.split('/')[-2]}` uses the same customerName as live "
            f"`{live.split('/')[-2]}`, so both manage the same GCP objects: {objs}. The live "
            f"environment is `{live}`. {TAIL}")


def _unread(path, live):
    return (f"- ⚠️ **Check:** could not check live `{live.split('/')[-2]}` (`{live}`): its "
            "values cannot be read or are incomplete, so "
            f"`{path.split('/')[-2]}` may share GCP objects with it.")


GSK_LINE = _line(GSK_C, GSK_B, _objs("pv-gsk--aec1", "na1-a"))


# ── user_content.bq_dataset ──────────────────────────────────────────────

@pytest.mark.parametrize("flat,want", [
    ({"appspace.prefix": "pv", "appspace.customerName": "gsk--aec1"},
     "pv_gsk__aec1_analytics_a"),                                  # the chart default suffix
    ({"appspace.prefix": "pv", "appspace.customerName": "Big-Co",
      "appspace.bigQuery.suffix": "c"}, "pv_big_co_analytics_c"),  # dashes, lower case
    ({"appspace.prefix": "pv", "appspace.customerName": "x",
      "appspace.bigQuery.enabled": True}, "pv_x_analytics_a"),
    ({"appspace.prefix": "pv", "appspace.customerName": "x",
      "appspace.bigQuery.enabled": False}, None),
    ({"appspace.prefix": "pv", "appspace.customerName": "x",
      "appspace.bigQuery.enabled": "false"}, None),
    ({"appspace.customerName": "x"}, None),                        # no prefix
    ({"appspace.prefix": "pv"}, None),                             # no customerName
    ({}, None),
    (None, None),
])
def test_bq_dataset(flat, want):
    assert user_content.bq_dataset(flat) == want


# ── the rule, on the real shapes ─────────────────────────────────────────

def test_pr_4333_a_clone_next_to_the_live_clone_b(monkeypatch):
    """pv-gsk--aec1-c and the live pv-gsk--aec1-b: one dataset, one bucket, one record."""
    _serve(monkeypatch, {GSK_C: _cust("gsk--aec1", "c")}, {GSK_B: _cust("gsk--aec1", "b")})
    _fleet(monkeypatch, [GSK_B, OTHER])
    assert _lines(GSK_C) == [GSK_LINE]


def test_pr_4671_the_folder_does_not_name_the_customer(monkeypatch):
    """pv-nbc--aec1-a has customerName nbc: its folder token is pv-nbc--aec1."""
    _serve(monkeypatch, {NBC_A: _cust("nbc", "a")}, {NBC_B: _cust("nbc", "b")})
    _fleet(monkeypatch, [NBC_B])
    assert _lines(NBC_A) == [_line(NBC_A, NBC_B, _objs("pv-nbc", "na1-a"))]


def test_pr_3286_the_same_folder_name_in_another_tree(monkeypatch):
    """Only a unit test: today the token guard stops #3286 first (below)."""
    _serve(monkeypatch, {USB_AEC: _cust("usbank", "a")}, {USB_PV: _cust("usbank", "c")})
    _fleet(monkeypatch, [USB_PV])
    assert _lines(USB_AEC) == [_line(USB_AEC, USB_PV, _objs("pv-usbank", "na1-a"))]


def test_a_folder_match_whose_customer_name_is_in_its_cohort(monkeypatch):
    """The own file of pv-gsk--aec1-b does not set customerName."""
    cohort = f"{AEC}/na4-a/config.yaml"
    _serve(monkeypatch, {GSK_C: _cust("gsk--aec1", "c")},
           {GSK_B: "appspace:\n  suffix: b\n",
            cohort: _region() + "  customerName: gsk--aec1\n"})
    _fleet(monkeypatch, [GSK_B])
    assert _lines(GSK_C) == [GSK_LINE]


@pytest.mark.parametrize("new_extra,region,objs", [
    ("  bigQuery:\n    suffix: c\n", "a", _objs("pv-gsk--aec1", "na1-a", False)),
    ("  bigQuery:\n    enabled: false\n", "a", _objs("pv-gsk--aec1", "na1-a", False)),
    ("", "b", "BigQuery dataset `pv_gsk__aec1_analytics_a`"),
    ("  bigQuery:\n    suffix: c\n", "b", None),
])
def test_only_the_shared_objects_are_named(monkeypatch, new_extra, region, objs):
    """The fix: its own bigQuery.suffix, or another user content suffix."""
    _serve(monkeypatch, {GSK_C: _cust("gsk--aec1", "c", new_extra),
                         f"{AEC}/na2-a/config.yaml": _region(suffix=region)},
           {GSK_B: _cust("gsk--aec1", "b")})
    _fleet(monkeypatch, [GSK_B])
    assert _lines(GSK_C) == ([_line(GSK_C, GSK_B, objs)] if objs else [])


@pytest.mark.parametrize("project,warns", [
    ("appspace-cloud-private-bq", True),   # the project of the live env
    ("appspace-cloud-stage-bq", False),    # the same name in another project
    (None, True),                          # unknown: read as the same project
])
def test_a_dataset_is_one_name_in_one_project(monkeypatch, project, warns):
    """COPS-2772: stage envs use appspace-cloud-stage-bq. Another user content
    suffix, so only the dataset can be shared."""
    _serve(monkeypatch, {GSK_C: _cust("gsk--aec1", "c"),
                         f"{AEC}/na2-a/config.yaml": _region(suffix="b", project=project)},
           {GSK_B: _cust("gsk--aec1", "b")})
    _fleet(monkeypatch, [GSK_B])
    objs = "BigQuery dataset `pv_gsk__aec1_analytics_a`"
    assert _lines(GSK_C) == ([_line(GSK_C, GSK_B, objs)] if warns else [])


def test_one_line_per_live_sibling(monkeypatch):
    gsk_a = f"{AEC}/na4-a/pv-gsk--aec1-a/customer.yaml"
    _serve(monkeypatch, {GSK_C: _cust("gsk--aec1", "c")},
           {GSK_B: _cust("gsk--aec1", "b"), gsk_a: _cust("gsk--aec1", "a")})
    _fleet(monkeypatch, [GSK_B, gsk_a])
    objs = _objs("pv-gsk--aec1", "na1-a")
    assert _lines(GSK_C) == [_line(GSK_C, gsk_a, objs), _line(GSK_C, GSK_B, objs)]


def test_another_customer_name_is_never_read(monkeypatch):
    _serve(monkeypatch, {GSK_C: _cust("gsk--aec1", "c")}, {OTHER: _cust("other", "a")},
           error=(f"{PV}/na1-b/config.yaml",))
    _fleet(monkeypatch, [OTHER])
    assert _lines(GSK_C) == []


# ── what is left out ─────────────────────────────────────────────────────

def test_a_live_env_this_pr_deletes_is_left_out(monkeypatch):
    """A rebuild: the old env goes in the same PR."""
    _serve(monkeypatch, {GSK_C: _cust("gsk--aec1", "c")}, {GSK_B: _cust("gsk--aec1", "b")})
    _fleet(monkeypatch, [GSK_B])
    assert _lines(GSK_C, [GSK_B, GSK_C]) == []


def test_a_live_env_this_pr_changes_or_moves_still_counts(monkeypatch):
    """It stays live, so it is compared as it is at base."""
    moved = GSK_B.replace("na4-a", "na2-a")
    _serve(monkeypatch, {GSK_C: _cust("gsk--aec1", "c"), moved: _cust("gsk--aec1", "b")},
           {GSK_B: _cust("gsk--aec1", "b")})
    _fleet(monkeypatch, [GSK_B])
    assert _lines(GSK_C, [GSK_B, moved, GSK_C], {GSK_B: moved}) == [GSK_LINE]
    m._vf_cache.clear()
    _serve(monkeypatch, {GSK_C: _cust("gsk--aec1", "c"), GSK_B: _cust("gsk--aec1", "b")},
           {GSK_B: _cust("gsk--aec1", "b")})
    assert _lines(GSK_C, [GSK_B, GSK_C]) == [GSK_LINE]


@pytest.mark.parametrize("head,base_sha", [
    ({GSK_C: _cust("gsk--aec1", "c")}, ""),                         # no base
    ({GSK_C: "appspace:\n  suffix: c\n"}, B),                       # no customerName
    ({GSK_C: "appspace: [\n"}, B),                                  # the render reports it
])
def test_nothing_to_compare_reads_no_fleet(monkeypatch, head, base_sha):
    _serve(monkeypatch, head)
    monkeypatch.setattr(m, "_fleet_own_identities",
                        lambda *a, **k: pytest.fail("the fleet was read"))
    assert _lines(GSK_C, base_sha=base_sha) == []


def test_an_empty_fleet_says_nothing(monkeypatch):
    _serve(monkeypatch, {GSK_C: _cust("gsk--aec1", "c")})
    _fleet(monkeypatch, [])
    assert _lines(GSK_C) == []


# ── a sibling that cannot be read ────────────────────────────────────────

@pytest.mark.parametrize("base,error", [
    ({}, (GSK_B,)),                                                 # its own file fails
    ({GSK_B: "appspace: [\n"}, ()),                                 # it cannot be parsed
    ({GSK_B: _cust("gsk--aec1", "b")}, (f"{AEC}/na4-a/config.yaml",)),  # its chain fails
    ({GSK_B: _cust("gsk--aec1", "b"), f"{AEC}/na4-a/config.yaml": _region(domain="")},
     ()),                                                           # no domain: unproven
])
def test_an_unreadable_sibling_is_a_check_too(monkeypatch, base, error):
    _serve(monkeypatch, {GSK_C: _cust("gsk--aec1", "c")}, base, error)
    _fleet(monkeypatch, [GSK_B])
    assert _lines(GSK_C) == [_unread(GSK_C, GSK_B)]


def test_an_unreadable_file_of_another_folder_is_not_a_sibling(monkeypatch):
    _serve(monkeypatch, {GSK_C: _cust("gsk--aec1", "c")}, error=(OTHER,))
    _fleet(monkeypatch, [OTHER])
    assert _lines(GSK_C) == []


# ── the merge summary ────────────────────────────────────────────────────

def _summary(new_env_lines, structural=False):
    return cr._build_merge_summary({}, {}, None, None, None, new_env_lines, structural)


FIRST = ("`pv-gsk--aec1-c` uses the same customerName as live `pv-gsk--aec1-b`, so both "
         "manage the same GCP objects: " + _objs("pv-gsk--aec1", "na1-a"))
FINDING = f"- \U0001f195 **New environment: 2 check(s) to review** - {FIRST}"


def test_the_checks_are_one_review_finding():
    lines = ["### x", "", GSK_LINE, _unread(GSK_C, GSK_B), "- ⚠️ other text", ""]
    assert _summary(lines) == [cr.MERGE_SUMMARY_HDR, "",
                               cr._VERDICTS[cr._SEV_REVIEW] + " (1 item(s))", "", FINDING, ""]


def test_a_could_not_check_line_leads_the_same_way():
    assert _summary([_unread(GSK_C, GSK_B)])[4] == (
        "- \U0001f195 **New environment: 1 check(s) to review** - could not check live "
        f"`pv-gsk--aec1-b` (`{GSK_B}`): its values cannot be read or are incomplete, so "
        "`pv-gsk--aec1-c` may share GCP objects with it")


def test_no_check_keeps_the_routine_line():
    assert _summary(["### x", ""])[4] == "- \U0001f195 **New environment** in this PR"


def test_a_structural_env_keeps_its_line_next_to_the_checks():
    out = _summary([GSK_LINE, GSK_LINE], structural=True)
    assert FINDING in out
    assert ("- \U0001f195 **New environment** in this PR \u2014 its configuration did "
            "not validate") in out


# ── process_pr ───────────────────────────────────────────────────────────

RENDERED = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: platform-config\n"
CLEAN_DESC = "1 new environment(s), ~1 resource(s) to create"
LEAD = ("⚠️ New environment: 1 check(s) to review - pv-gsk--aec1-c uses the same "
        "customerName as live pv-gsk--aec1-b, so both manage the same GCP objects: "
        "BigQuery dataset pv_gsk__aec1_analytics_a")


def _outside_fences(body):
    return re.sub(r"```.*?```", "", body, flags=re.S)


@pytest.fixture()
def new_clone(world, monkeypatch):
    """#4333: a PR that adds pv-gsk--aec1-c asleep and touches no live app."""
    sinks, _plan = world
    monkeypatch.setattr(m, "get_pr_changed_files", lambda pr_id, repo=None: ([GSK_C], {}))
    monkeypatch.setattr(m, "_render_new_env_diff",
                        lambda env_info, pr_sha: (RENDERED, None, 1, "2603.3.7"))
    monkeypatch.setattr(m, "_pr_commit_messages",
                        lambda *a: pytest.fail("commits read with no trailer to find"))
    _fleet(monkeypatch, [GSK_B])

    def run(error=()):
        live = {GSK_B: _cust("gsk--aec1", "b")}
        _serve(monkeypatch, dict(live, **{GSK_C: _cust("gsk--aec1", "c")}), live, error)
        m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
        return sinks.upserts[-1], sinks.statuses[-1]
    return run


def test_pr_4333_is_green_with_the_verdict_review(new_clone):
    body, (state, desc) = new_clone()
    assert m._extract_status_token(body) == "clean"
    assert state == "SUCCESSFUL"
    assert desc.startswith(LEAD) and desc.endswith(f" | {CLEAN_DESC}")
    assert cr._VERDICTS[cr._SEV_REVIEW] in body
    assert f"- \U0001f195 **New environment: 1 check(s) to review** - {FIRST}" in body
    assert GSK_LINE in body
    assert body.index("New Environment(s) Detected") < body.index(GSK_LINE)
    assert "**New environment** in this PR" not in body
    assert not any(r in _outside_fences(body) for r in RED)
    assert m._seen.get((m.BB_REPO, 991)) == (PR_SHA, BASE_SHA)


def test_an_unreadable_sibling_stays_green(new_clone):
    body, (state, desc) = new_clone(error=(GSK_B,))
    assert m._extract_status_token(body) == "clean" and state == "SUCCESSFUL"
    assert _unread(GSK_C, GSK_B) in body
    assert desc.startswith("⚠️ New environment: 1 check(s) to review - could not check")


def test_a_migration_clone_on_the_diff_path(world, monkeypatch):
    """pv-usbank-d next to the live pv-usbank-c, with a change to a live app:
    the same line and verdict on the diff path."""
    sinks, _plan = world
    usb_d = USB_PV.replace("pv-usbank-c", "pv-usbank-d")
    monkeypatch.setattr(m, "get_pr_changed_files",
                        lambda pr_id, repo=None: ([ANCILLARY, usb_d], {}))
    monkeypatch.setattr(m, "_render_new_env_diff",
                        lambda env_info, pr_sha: (RENDERED, None, 1, "2603.3.7"))
    _fleet(monkeypatch, [USB_PV])
    live = {USB_PV: _cust("usbank", "c"), IDENTITY: IDENTITY_YAML}
    _serve(monkeypatch, dict(live, **{usb_d: _cust("usbank", "d")}), live)
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    body, (state, desc) = sinks.upserts[-1], sinks.statuses[-1]
    line = _line(usb_d, USB_PV, _objs("pv-usbank", "na1-a"))
    assert m._extract_status_token(body) == "clean" and state == "SUCCESSFUL"
    assert desc.startswith("⚠️ New environment: 1 check(s) to review - pv-usbank-d uses")
    assert cr._VERDICTS[cr._SEV_REVIEW] in body and line in body
    assert not any(r in _outside_fences(body) for r in RED)


def test_pr_3286_the_token_guard_speaks_first(world, monkeypatch):
    """Under gcp/aec the folder and customerName need --aec1, so today #3286
    gets the token block and this check never runs (the critic counted it
    as reaching process_pr; only #4333 does)."""
    sinks, _plan = world
    monkeypatch.setattr(m, "get_pr_changed_files", lambda pr_id, repo=None: ([USB_AEC], {}))
    monkeypatch.setattr(m, "_new_env_shared_lines", lambda *a: pytest.fail("checked"))
    _serve(monkeypatch, {USB_AEC: _cust("usbank", "a")}, {USB_PV: _cust("usbank", "c")})
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    body, (state, desc) = sinks.upserts[-1], sinks.statuses[-1]
    assert m._extract_status_token(body) == "blocked" and state == "FAILED"
    assert desc.startswith("BLOCKED: pv-usbank-c needs --aec1 in folder")
