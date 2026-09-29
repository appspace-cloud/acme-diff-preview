"""COPS-2766 block 7: versions and images, warnings only.

A chart downgrade was already a REVIEW finding and a banner, but the green
build status did not name it (#4549). A downgrade next to environments that
go up is how a stale branch looks: #3315 reverted the ap1-b weekly cohort
bump that #3314 merged about one hour before, while 15 other files went up.
A new -dev chart next to release charts (#4382) is a mutable tag in a
release PR. All of them stay warnings: the build stays green.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import comment_render as cr  # noqa: E402
import diff_preview as m  # noqa: E402

from test_coverage_orchestration import (  # noqa: E402,F401
    world, _mk_pr, PATH_MAP, BASE_SHA)


@pytest.fixture(autouse=True)
def no_vertex(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)


def _r(vc):
    return m.DiffResult("d", [("apps/Deployment x", "+a")], 1, True, "",
                        m.OUT_DIFF, "", vc)


def _bullets(results):
    lines = cr._build_merge_summary(results, {}, None, None, None, None, False)
    return [l[2:] for l in lines if l.startswith("- ")]


# #3315: the ap1-b weekly cohort config.yaml goes 2602.0.5 -> 2601.4.10
# (it reverts #3314), and other environments go 2601.4.9 -> 2601.4.10.
DOWN = ("2602.0.5", "2601.4.10")
UP = ("2601.4.9", "2601.4.10")


def _pr3315():
    """Built at call time: some tests reload m, and format_comment checks
    the DiffResult class."""
    return {
        **{f"pv-wk{i}-b-{c}": _r(DOWN) for i in (1, 2) for c in ("ms", "ss")},
        **{f"pv-up{i}-a-{c}": _r(UP) for i in (1, 2, 3) for c in ("ms", "ss")},
    }


NOTE = ("3 other environment(s) in this PR move up: check that the branch "
        "is not out of date.")


# ── the mixed-direction note ─────────────────────────────────────────────

def test_a_cohort_revert_next_to_bumps_gets_the_note():
    assert cr._downgrade_mix_note(_pr3315()) == NOTE


def test_the_note_counts_environments_not_apps():
    results = {"pv-dn-a-ms": _r(DOWN), "pv-up-a-ms": _r(UP),
               "pv-up-a-ss": _r(UP), "pv-up-b-ms": _r(UP)}
    assert cr._downgrade_mix_note(results).startswith("2 other environment(s)")


@pytest.mark.parametrize("results", [
    {"pv-dn-a-ms": _r(DOWN), "pv-up-a-ms": _r(UP)},          # only 2 envs
    {f"pv-dn{i}-a-ms": _r(DOWN) for i in range(3)},          # #4501: all down
    {f"pv-up{i}-a-ms": _r(UP) for i in range(3)},            # all up
    {"pv-dn-a-ms": _r(DOWN), "pv-x-a-ms": _r(None), "pv-y-a-ms": _r(None)},
])
def test_no_note_unless_three_envs_go_both_ways(results):
    assert cr._downgrade_mix_note(results) == ""


def test_an_env_that_goes_down_is_never_counted_up():
    """Its -ss goes up, its -ms goes down: the env is a downgrade."""
    results = {"pv-dn-a-ms": _r(DOWN), "pv-dn-a-ss": _r(UP),
               "pv-up-a-ms": _r(UP), "pv-up-b-ms": _r(UP)}
    assert cr._downgrade_mix_note(results).startswith("2 other environment(s)")


def test_the_summary_finding_ends_with_the_note():
    b = _bullets(_pr3315())
    assert b[0].startswith("⬇️ **Chart version downgrade** "
                           "`2602.0.5` → `2601.4.10` in pv-wk1-b, pv-wk2-b"), b
    assert b[0].endswith(". " + NOTE), b[0]


def test_a_plain_downgrade_keeps_its_finding():
    b = _bullets({"pv-dn-a-ms": _r(DOWN)})
    assert b[0] == ("⬇️ **Chart version downgrade** `2602.0.5` "
                    "→ `2601.4.10` in pv-dn-a")


def test_the_banner_has_the_note_only_when_mixed():
    body = m.format_comment("c" * 12, _pr3315())
    lines = body.splitlines()
    i = lines.index(NOTE)
    assert "LOWER version" in lines[i - 2] and lines[i + 1] == ""
    assert lines[i + 2].startswith("### \U0001f53b `pv-wk1-b-ms`")
    assert NOTE not in m.format_comment("c" * 12, {"pv-dn-a-ms": _r(DOWN)})


# ── a new -dev chart next to release charts (#4382) ──────────────────────

DEV = ("\U0001f9ea **-dev chart next to release charts** "
       "`2603.0.20-rev1-copr-32089-dev` in pv-aexp-a. A -dev tag can be "
       "pushed again at any time: check that it belongs in this PR.")


def test_a_new_dev_chart_next_to_a_release_is_a_review_item():
    results = {"pv-aexp-a-ms": _r(("2603.1.27", "2603.0.20-rev1-copr-32089-dev")),
               "cl-prod-b-ms": _r(("2603.1.27", "2603.2.2"))}
    lines = cr._build_merge_summary(results, {}, None, None, None, None, False)
    assert "- " + DEV in lines
    assert lines[2].startswith("⚠️ **Review before merging**")


@pytest.mark.parametrize("results", [
    # a lone -dev pin, the other 15 prod shapes
    {"pv-aexp-a-ms": _r(("2603.1.27", "2603.1.28-copr-1-dev"))},
    {"pv-aexp-a-ms": _r(("2603.1.27", "2603.1.28-copr-1-dev")),
     "pv-other-a-ms": _r(None)},
    # -dev to -dev next to a release
    {"pv-aexp-a-ms": _r(("2603.1.27-copr-1-dev", "2603.1.28-copr-2-dev")),
     "cl-prod-b-ms": _r(("2603.1.27", "2603.2.2"))},
    # two new -dev charts, no release
    {"pv-a-a-ms": _r(("2603.1.27", "2603.1.28-copr-1-dev")),
     "pv-b-a-ms": _r(("2603.1.27", "2603.1.28-copr-1-dev"))},
])
def test_no_dev_line_without_a_release_next_to_it(results):
    assert not any("-dev chart next to" in b for b in _bullets(results))


# ── the build status names the downgrade (#4549) ─────────────────────────

SUFFIX = " | CHART DOWNGRADE in 1 environment(s)"


def _downgrade_plan(plan):
    plan["pv-orch-a-ms"] = m.DiffResult(
        "--- main\n+++ pr", [("Application/pv-orch-a-ms", "-2603.0.1\n+2600.0.0")],
        1, True, "", m.OUT_DIFF, "",
        version_change=("2603.0.1-dev", "2600.0.0-dev"))


def test_the_green_status_names_the_downgrade(world):
    sinks, plan = world
    _downgrade_plan(plan)
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    body = sinks.upserts[-1]
    assert m._extract_status_token(body) == "clean", "not the transient path"
    assert sinks.statuses[-1] == (
        "SUCCESSFUL",
        "⚠️ Chart version downgrade 2603.0.1-dev → 2600.0.0-dev "
        "in pv-orch-a | 1 resource(s) will change" + SUFFIX + " - review comment")


def test_both_apps_of_one_env_count_once(world):
    sinks, plan = world
    _downgrade_plan(plan)
    plan["pv-orch-a-ss"] = plan["pv-orch-a-ms"]
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    assert m._extract_status_token(sinks.upserts[-1]) == "clean"
    state, desc = sinks.statuses[-1]
    assert state == "SUCCESSFUL" and desc.endswith(SUFFIX + " - review comment")


def test_a_downgrade_with_no_manifest_change_is_named(world):
    sinks, plan = world
    plan["pv-orch-a-ms"] = m.DiffResult("", [], 0, False, None, m.OUT_NO_DIFF,
                                        "clean", ("2603.0.1", "2603.0.0"))
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    assert m._extract_status_token(sinks.upserts[-1]) == "clean"
    state, desc = sinks.statuses[-1]
    assert state == "SUCCESSFUL" and desc.endswith(SUFFIX), desc


def test_a_plain_bump_has_no_suffix(world):
    sinks, plan = world
    plan["pv-orch-a-ms"] = m.DiffResult(
        "--- main\n+++ pr", [("Deployment/webx", "-image: a:1\n+image: a:2")],
        1, True, "", m.OUT_DIFF, "", version_change=("2603.0.1", "2603.0.2"))
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    assert m._extract_status_token(sinks.upserts[-1]) == "clean"
    state, desc = sinks.statuses[-1]
    assert state == "SUCCESSFUL" and "DOWNGRADE" not in desc, desc


def test_the_red_unavailable_status_keeps_it(world):
    sinks, plan = world
    _downgrade_plan(plan)
    plan["pv-orch-a-ss"] = m.DiffResult("", [], 0, False, "", m.OUT_INDETERMINATE,
                                        m.REASON_TIMEOUT)
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    assert sinks.statuses[-1] == (
        "FAILED", "Diff unavailable for 1 app(s) | 1 resource(s) confirmed "
        "changed" + SUFFIX + " - review comment (will retry automatically "
        "if transient)")


def test_other_failed_descriptions_do_not_change(world):
    sinks, plan = world
    _downgrade_plan(plan)
    plan["pv-orch-a-ss"] = m.DiffResult("", [], 0, False, "helm exploded\nstack",
                                        m.OUT_ERROR, "")
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    assert sinks.statuses[-1] == ("FAILED",
                                  "Diff failed: helm exploded - check PR comment")


# ── -revN on release tags (open decision row 20) ─────────────────────────

@pytest.mark.parametrize("current,new,down", [
    ("2603.1.38-rev2", "2603.1.38-rev1", True),
    ("2603.1.38-rev1", "2603.1.38", True),
    ("2602.1.14-rev1", "2602.1.14", True),         # the prod revert 5e59f9f9f
    ("2603.1.38-rev10", "2603.1.38-rev9", True),   # a number, not text
    ("2603.1.39", "2603.1.38-rev5", True),         # the version still leads
    ("2603.1.38", "2603.1.38-rev1", False),
    ("2603.1.38-rev1", "2603.1.39", False),
    ("2603.1.38-rev1", "2603.1.38-rev1", False),
    # feature and -dev tags keep the old compare: no rev there
    ("2603.0.20-rev1-copr-32089-dev", "2603.0.20-rev1", False),
    ("2602.4.3-rev2-ap-68093-dev", "2602.4.3-rev1-dev", False),
    ("8.12.0-ac.1.4-rev6-dev", "8.12.0-ac.4.8-dev", False),
])
def test_a_rev_counts_only_on_release_tags(current, new, down):
    assert cr._is_version_downgrade(current, new) is down


def test_a_rev_revert_is_a_downgrade_not_a_bump():
    b = _bullets({"pv-x-a-ms": _r(("2602.1.14-rev1", "2602.1.14"))})
    assert b == ["⬇️ **Chart version downgrade** `2602.1.14-rev1` "
                 "→ `2602.1.14` in pv-x-a"], b


def test_a_new_rev_counts_up_for_the_note():
    results = {"pv-dn-a-ms": _r(DOWN),
               "pv-up-a-ms": _r(("2603.1.38", "2603.1.38-rev1")),
               "pv-up-b-ms": _r(("2603.1.38-rev1", "2603.1.38-rev2"))}
    assert cr._downgrade_mix_note(results).startswith("2 other environment(s)")
