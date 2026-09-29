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


# ── an image that goes down is never folded as a bump (#4679) ───────────
# #4679 pinned device, device-background and devicegateway to
# 1.116.10-rc.20260923001 (and signschannel to 1.90.15-rc) on 15 AEC clones.
# On pv-gsk--aec1-c they ran 1.117.4, so 5 Deployments went down, and the
# chart did not move. The fold called it version noise and the rollup said
# "jumping", Routine.

import version_fold as vf  # noqa: E402

GSK_DOWN = (("device", "appspace-device", "1.117.4", "1.116.10-rc.20260923001"),
            ("device-background", "appspace-device", "1.117.4",
             "1.116.10-rc.20260923001"),
            ("devicegateway", "appspace-devicegateway", "1.117.4",
             "1.116.10-rc.20260923001"),
            ("signschannel", "appspace-signschannel", "1.91.2",
             "1.90.15-rc.20260923031"),
            ("signschannelgateway", "appspace-signschannelgateway", "1.91.2",
             "1.90.15-rc.20260923031"))


def _img(env, name, repo, old, new, dash=""):
    return (f"/apps/Deployment {env}/{name}",
            "--- \n+++ \n@@ -12,7 +12,7 @@\n"
            "       containers:\n"
            f"-      {dash}image: gcr.example/acme/{repo}:{old}\n"
            f"+      {dash}image: gcr.example/acme/{repo}:{new}\n")


def _ups(env="pv-x-a", n=3, old="2603.2.19", new="2603.3.10"):
    return [_img(env, f"svc-{i}", f"appspace-svc-{i}", old, new) for i in range(n)]


def _diff_text(sections):
    return "\n".join(f"===== {h} ======\n{b}" for h, b in sections)


def _via_argocd_diff(monkeypatch, sections, version_change=None):
    """The REAL argocd_diff on a scripted render."""
    monkeypatch.setattr(m, "_run_one_diff", lambda *a, **k: (
        _diff_text(sections), None, "", version_change, 0, None))
    return m.argocd_diff("pv-x-a-ms", "aaaa1111", "bbbb2222")


@pytest.mark.parametrize("old,new,down", [
    ("1.117.4", "1.116.10-rc.20260923001", True),
    ("1.116.10-rc.20260923001", "1.117.4", False),
    ("1.2.3-rev2", "1.2.3-rev1", True),
    ("1.2.3", "1.2.3", False),
    ("9f8e7d", "1a2b3c", False),          # a git sha is not a version
    ("latest", "1.0.0", False),
    ("1.0.0", "latest", False),
    ("v1.2", "v1.1", False),              # dotted tags only
    ("20260923", "20260922", False),
])
def test_only_a_dotted_tag_can_go_down(old, new, down):
    assert vf._image_tag_downgrade(old, new) is down


def test_a_downgraded_section_stays_out_of_the_fold():
    down = _img("pv-x-a", "device", "appspace-device", "1.117.4", "1.116.10")
    fold = vf._classify_version_fold(_ups() + [down])
    assert fold["n_foldable"] == 3 and fold["n_total"] == 4
    assert down[0] not in fold["headers"]
    assert fold["label"] == "2603.2.19 → 2603.3.10"


def test_three_downgraded_sections_do_not_fold():
    assert vf._classify_version_fold(_ups(old="2603.3.10", new="2603.2.19")) is None


def test_a_chart_that_goes_up_does_not_hide_an_image_that_goes_down():
    down = _img("pv-x-a", "device", "appspace-device", "1.117.4", "1.116.10")
    fold = vf._classify_version_fold(
        _ups() + [down], version_change=("2603.2.19", "2603.3.10"))
    assert fold["n_foldable"] == 3 and down[0] not in fold["headers"]


@pytest.mark.parametrize("vc", [
    ("2603.3.10-rev3", "2603.2.19-rev3"),       # a chart downgrade (#4501)
    ("2603.1.33-rev1-dev", "2603.1.33"),        # same version, other tag
    ("2603.1.33", "2603.1.33-rev1-dev"),
    ("main", "HEAD"),                           # no version to compare
])
def test_a_chart_that_does_not_go_up_folds_its_images_as_today(vc):
    """Every image goes down with the chart: the chart finding names it,
    and the fold must keep working on these PRs (critic blocker)."""
    secs = _ups(old="2603.3.10", new="2603.2.19")
    fold = vf._classify_version_fold(secs, version_change=vc)
    assert fold is not None and fold["n_foldable"] == 3
    assert vf._detect_image_downgrades(secs, vc) == ()


def test_the_detector_names_only_the_image_that_goes_down():
    body = ("--- \n+++ \n@@ -12,9 +12,9 @@\n"
            "       containers:\n"
            "-      - image: gcr.example/acme/appspace-device:1.117.4\n"
            "+      - image: gcr.example/acme/appspace-device:1.116.10\n"
            "-      - image: gcr.example/acme/logshipper:2.0.1\n"
            "+      - image: gcr.example/acme/logshipper:2.1.0\n"
            "-        initImage: gcr.example/acme/migrate:abc123\n"
            "+        initImage: gcr.example/acme/migrate:def456\n")
    hdr = "/apps/Deployment pv-x-a/device"
    assert vf._detect_image_downgrades([(hdr, body)]) == (
        (hdr, "appspace-device", "1.117.4", "1.116.10"),)


def test_the_detector_keeps_the_last_tag_of_a_repo_on_each_side():
    body = ("-  image: r/app:1.0.0\n-  image: r/app:3.0.0\n"
            "+  image: r/app:2.0.0\n+  image: r/app:2.5.0\n")
    assert vf._detect_image_downgrades([("h", body)]) == (("h", "app", "3.0.0", "2.5.0"),)


@pytest.mark.parametrize("body", [
    "-  replicas: 2\n+  replicas: 3\n",                   # no image line
    "+  image: r/app:1.0.0\n",                            # created only
    "-  image: r/app:2.0.0\n+  image: r/other:1.0.0\n",   # another repo
    "-  image: r:5000/app\n+  image: r:5000/app\n",       # no tag
    "--- \n+++ \n-  image: r/app:2.0.0\n+  image: r/app:2.0.1\n",
])
def test_the_detector_finds_nothing_without_a_lower_tag(body):
    assert vf._detect_image_downgrades([("h", body)]) == ()


def test_argocd_diff_carries_the_image_downgrades(monkeypatch):
    down = _img("pv-x-a", "device", "appspace-device", "1.117.4", "1.116.10")
    r = _via_argocd_diff(monkeypatch, _ups() + [down])
    assert r.outcome == m.OUT_DIFF
    assert r.image_downgrades == ((down[0], "appspace-device", "1.117.4", "1.116.10"),)
    assert r.version_fold["n_foldable"] == 3
    assert m._is_risky_result(r) and m._routine_bump_signature(r) is None


def test_argocd_diff_has_no_image_downgrades_on_a_plain_bump(monkeypatch):
    r = _via_argocd_diff(monkeypatch, _ups())
    assert r.image_downgrades is None
    assert not m._is_risky_result(r) and m._routine_bump_signature(r) is not None


def test_a_chart_downgrade_pr_folds_and_has_no_image_line(monkeypatch):
    """#4501 shape: 2603.3.10 -> 2603.2.19, every image goes down."""
    vc = ("2603.3.10", "2603.2.19")
    r = _via_argocd_diff(monkeypatch, _ups(old="2603.3.10", new="2603.2.19"), vc)
    assert r.image_downgrades is None and r.version_fold["n_foldable"] == 3
    b = _bullets({"pv-x-a-ms": r})
    assert b[0].startswith("⬇️ **Chart version downgrade**"), b
    assert not any("Image downgrade" in x for x in b), b


def _pr4679(monkeypatch):
    """pv-gsk--aec1-c and two more clones take the same pins, the same way."""
    results = {}
    for env in ("pv-gsk--aec1-c", "pv-hsbc--aec1-a", "pv-ford--aec1-b"):
        secs = [_img(env, *d) for d in GSK_DOWN]
        results[f"{env}-ms"] = _via_argocd_diff(monkeypatch, secs)
    return results


def test_pr4679_is_a_review_item_not_a_routine_jump(monkeypatch):
    body = m.format_comment("c" * 12, _pr4679(monkeypatch))
    summary = body.split("\n---\n", 1)[0]
    assert "Review before merging" in summary
    first = next(l for l in summary.splitlines() if l.startswith("- "))
    assert first == (
        "- ⬇️ **Image downgrade** in pv-ford--aec1-b, pv-gsk--aec1-c, "
        "pv-hsbc--aec1-a: `device` `1.117.4` → `1.116.10-rc.20260923001`, "
        "`device-background` `1.117.4` → `1.116.10-rc.20260923001`, "
        "`devicegateway` `1.117.4` → `1.116.10-rc.20260923001` (+2 more). "
        "Check that an old pin or an old branch is not moving it back."), first
    assert "jumping" not in body


def test_the_image_line_has_no_more_suffix_up_to_three():
    r = m.DiffResult("d", [("apps/Deployment x", "+a")], 1, True, "", m.OUT_DIFF,
                     "", image_downgrades=(("/apps/Deployment pv-x-a/web", "web",
                                            "2.0.0", "1.9.9"),))
    assert _bullets({"pv-x-a-ms": r})[0] == (
        "⬇️ **Image downgrade** in pv-x-a: `web` `2.0.0` → `1.9.9`. Check "
        "that an old pin or an old branch is not moving it back.")


IMG_SUFFIX = " | IMAGE DOWNGRADE in 1 environment(s)"


def test_the_green_status_names_the_image_downgrade(world):
    sinks, plan = world
    plan["pv-orch-a-ms"] = m.DiffResult(
        "--- main\n+++ pr", [("/apps/Deployment pv-orch-a/device",
                              "-image: r/d:1.117.4\n+image: r/d:1.116.10")],
        1, True, "", m.OUT_DIFF, "",
        image_downgrades=(("/apps/Deployment pv-orch-a/device", "d",
                           "1.117.4", "1.116.10"),))
    plan["pv-orch-a-ss"] = plan["pv-orch-a-ms"]
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    assert m._extract_status_token(sinks.upserts[-1]) == "clean", "not the transient path"
    assert sinks.statuses[-1] == (
        "SUCCESSFUL",
        "⚠️ Image downgrade in pv-orch-a: device 1.117.4 → 1.116.10. Check "
        "that an old pin or an old branch is not moving it back. | 2 resource(s) "
        "will change" + IMG_SUFFIX + " - review comment")
