"""COPS-2766: the green build status leads with the top merge-summary finding.

Bitbucket shows the status description in the merge dialog, and approvers
merge from there: acme-config-prod #4684 was approved seconds after a green
"129 resource(s) will change - review comment", with the deletions only in
the comment. A SUCCESSFUL description is now

    <marker> <first finding, plain text> | <the description it had before>

  * 🚨 when the comment verdict is ⛔, ⚠️ when it is review, nothing when
    it is routine. The lead never says DO NOT MERGE: the build is green.
  * 255 UTF-8 bytes at most, which fits whatever unit Bitbucket counts.
    Only the lead is cut, never the old text.
  * The lead is read from the posted comment by one pure function, so
    process_pr and fix_stuck_inprogress write the same text.
  * FAILED descriptions do not change.
"""
import glob
import os
import sys

import pytest
from hypothesis import example, given, settings, strategies as st

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import comment_render as cr  # noqa: E402
import diff_preview as m  # noqa: E402

from test_coverage_orchestration import world, _mk_pr, PATH_MAP, BASE_SHA, PR_SHA  # noqa: E402,F401

settings.register_profile("suite", deadline=None)
settings.load_profile("suite")

# The real function: the `world` fixture replaces it with a stub.
REAL_FIX_STUCK = m.fix_stuck_inprogress

HDR = cr.MERGE_SUMMARY_HDR
REVIEW = "\u26a0\ufe0f **Review before merging** (1 item(s))"
BLOCK = ("\u26d4 **DO NOT MERGE** without checking the item(s) below "
         "(1 item(s))")
ROUTINE = "\u2705 **Routine** \u2014 nothing dangerous detected"
DELETION = ("\U0001f5d1\ufe0f **1 resource(s) deleted** in 1 environment(s) "
            "(1 Service): pv-orch-a")
LEAD = "\u26a0\ufe0f 1 resource(s) deleted in 1 environment(s) (1 Service): pv-orch-a"


def _comment(verdict, *bullets, after=""):
    return "\n".join(["## \U0001f52d ACME Diff Preview", "",
                      "**Commit** `aabbccdd` \u2192 `main`", "", HDR, "",
                      verdict, ""] + [f"- {b}" for b in bullets]
                     + ["", "---", "", after])


def _u8(s):
    return len(s.encode("utf-8"))


# Merge summaries as they were posted on acme-config-prod, with the green
# description each PR got. Old comments use the old deletion bullet (❌
# under ⛔), which is still a valid input.
REAL_SUMMARIES = (
    # #4093
    ('## ℹ️ Merge summary\n\n✅ **Routine** — nothing dangerous detected\n\n- ✅ 12 app(s) change, nothing risk-flagged\n',
     '12 resource(s) will change - review comment'),
    # #4094
    ('## ℹ️ Merge summary\n\n⛔ **DO NOT MERGE** without checking the item(s) below (1 item(s))\n\n- ❌ **1 resource(s) deleted** in 1 app(s): pv-ukhsa-b\n- 🖥️ VM infrastructure changed (routine)\n- ♻️ resources renamed/recreated (not a deletion): pv-ukhsa-b\n',
     '206 resource(s) will change - review comment'),
    # #4103
    ('## ℹ️ Merge summary\n\n⛔ **DO NOT MERGE** without checking the item(s) below (1 item(s))\n\n- 🖥️ **VM infrastructure change flagged dangerous** — see the VM section\n',
     'No manifest changes'),
    # #4298
    ('## ℹ️ Merge summary\n\n⛔ **DO NOT MERGE** without checking the item(s) below (1 item(s))\n\n- 🗑️ **Environment decommission** — data purge is ARMED: buckets/datasets are destroyed, not abandoned\n',
     'No manifest changes | 🗑️ 1 environment(s) being decommissioned'),
    # #4304
    ('## ℹ️ Merge summary\n\n⚠️ **Review before merging** (1 item(s))\n\n- 🛑 **Environment shutting down** — every workload (107) scaled to 0 in pv-heb-a. `appspace.zeroPods` hibernates the environment: nothing will be running after this merges.\n',
     '107 resource(s) will change - review comment'),
    # #4321
    ('## ℹ️ Merge summary\n\n⚠️ **Review before merging** (1 item(s))\n\n- 🧊 **Replicas scaled to zero** in pv-blackrock--aec1-a\n',
     '2 resource(s) will change - review comment'),
    # #4324
    ('## ℹ️ Merge summary\n\n⚠️ **Review before merging** (1 item(s))\n\n- ⬇️ **Chart version downgrade** `2603.0.19` → `2603.0.18` in pv-covia-a\n',
     '6 resource(s) will change - review comment'),
    # #4326
    ('## ℹ️ Merge summary\n\n⚠️ **Review before merging** (1 item(s))\n\n- 🖥️ **9 KCC resource(s) unmanaged** (abandon / schedule attachment — GCP kept)\n- 🖥️ VM infrastructure changed (routine)\n',
     '9 resource(s) will change - review comment'),
    # #4331
    ('## ℹ️ Merge summary\n\n⛔ **DO NOT MERGE** without checking the item(s) below (1 item(s))\n\n- 🖥️ **1 environment(s) provision a NEW linux VM** — see the VM section\n',
     '9 resource(s) will change - review comment'),
    # #4386
    ('## ℹ️ Merge summary\n\n⛔ **DO NOT MERGE** without checking the item(s) below (1 item(s))\n\n- 🔒 **Decommission ARMED** — this environment becomes eligible for cascade deletion when its folder is removed\n',
     'No manifest changes'),
    # #4397
    ('## ℹ️ Merge summary\n\n⚠️ **Review before merging** (1 item(s))\n\n- ⏸️ **ArgoCD auto-sync paused** for an environment — changes stop being applied\n',
     'No manifest changes'),
    # #4444
    ('## ℹ️ Merge summary\n\n⚠️ **Review before merging** (1 item(s))\n\n- 🎚️ **acme-ping-scaler activated** in pv-ford--aec1-b — it takes over replica control; 23 HPA(s) removed by design ([how it works](https://appspace.atlassian.net/wiki/spaces/cops/pages/1181089800))\n',
     '114 resource(s) will change - review comment'),
    # #4465
    ('## ℹ️ Merge summary\n\n⚠️ **Review before merging** (2 item(s))\n\n- ⏸️ **1 environment(s) are PAUSED** (`appspace.autosync: false`) — their changes below will NOT be applied on merge: pv-chevron-c\n- ⏸️ **ArgoCD auto-sync paused** for an environment — changes stop being applied\n',
     '2 resource(s) will change - review comment'),
    # #4473
    ('## ℹ️ Merge summary\n\n⚠️ **Review before merging** (1 item(s))\n\n- 💥 **Wide-reach config change** — reaches 296 environments across 11 spoke(s); changes to shared config bypass cohort staging (see the blast-radius note)\n',
     '33111 resource(s) will change - review comment'),
    # #4599
    ('## ℹ️ Merge summary\n\n⚠️ **Review before merging** (1 item(s))\n\n- 📚 **Higher-layer values already cover part of this PR** — some keys match an ancestor config.yaml, so they do not change rendered manifests (see the higher-layer note)\n',
     '80 resource(s) will change - review comment'),
    # #4595
    ('## ℹ️ Merge summary\n\n⛔ **DO NOT MERGE** without checking the item(s) below (1 item(s))\n\n- ❌ **1044 resource(s) deleted** in 174 environment(s): pv-adl-a, pv-aholddelhaize-c, pv-alcoa-a, pv-ameren-a, pv-appension-a, pv-asi-b, pv-atea-a, pv-ato-c (+166 more)\n- ⬆️ **201 environment(s) bump** `2603.1.33` → `2603.1.33-rev2`: pv-adl-a, pv-advocate-b, pv-aexp-a, pv-afs-a, pv-aholddelhaize-c, pv-alcoa-a, pv-allenisd-a, pv-allianzna-c (+193 more)\n',
     '1446 resource(s) will change - review comment'),
    # #4672
    ('## ℹ️ Merge summary\n\n⛔ **DO NOT MERGE** without checking the item(s) below (2 item(s))\n\n- ❌ **68 resource(s) deleted** in 1 environment(s): pv-nbc-a\n- 🖥️ **2 KCC resource(s) unmanaged** (abandon / schedule attachment — GCP kept)\n- 🖥️ VM infrastructure changed (routine)\n- ♻️ resources renamed/recreated (not a deletion): pv-nbc-a\n',
     '309 resource(s) will change - review comment'),
    # #4684
    ('## ℹ️ Merge summary\n\n⛔ **DO NOT MERGE** without checking the item(s) below (2 item(s))\n\n- ❌ **2 resource(s) deleted** in 1 environment(s): pv-intramax-a\n- 🧊 **Replicas scaled to zero** in pv-intramax-a\n- 🖥️ VM infrastructure changed (routine)\n- ♻️ resources renamed/recreated (not a deletion): pv-intramax-a\n- ⬆️ **1 environment(s) bump** `2603.1.38` → `2603.3.8`: pv-intramax-a\n',
     '129 resource(s) will change - review comment'),
    # #4627
    ('## ℹ️ Merge summary\n\n⚠️ **Review before merging** (1 item(s))\n\n- 🛑 **Environment shutting down** — every workload scaled to 0 in pv-gsk--aec1-c, and 24 HorizontalPodAutoscaler(s) remain in desired. Hibernation still applies `replicas: 0` (COPS-2548); leftover HPAs can fight a later scale-up. Prefer a chart that skips HPA under `appspace.zeroPods` (COPS-2677).\n',
     '97 resource(s) will change - review comment'),)

# Today's SUCCESSFUL description shapes, the longest one included.
TAILS = (
    "1 resource(s) will change - review comment",
    "No manifest changes",
    "No manifest changes \u2014 config YAML changed but render is identical "
    "(see PR comment)",
    "12 resource(s) will change | +2 new environment(s) will be created"
    " | \U0001f5d1\ufe0f 3 environment(s) being decommissioned"
    " | \U0001f9f9 4 leftover app(s) from prior decommission - review comment",
)


def _goldens():
    return [p for p in sorted(glob.glob(os.path.join(
        os.path.dirname(__file__), "golden", "*.md")))
        if not p.endswith("README.md")]


def _check(comment, tail):
    lead = cr.status_lead(comment)
    out = cr.join_status_lead(lead, tail)
    assert "do not merge" not in lead.lower(), lead
    assert _u8(out) <= 255, out
    assert out.endswith(tail), "the old description must survive byte for byte"
    assert out == tail or out.startswith(("\U0001f6a8 ", "\u26a0\ufe0f ")), out
    return lead, out


# ── the property, over every golden and every real summary ───────────────

@pytest.mark.parametrize("path", _goldens(), ids=os.path.basename)
@pytest.mark.parametrize("tail", TAILS)
def test_every_golden_gives_a_safe_green_description(path, tail):
    with open(path, encoding="utf-8") as fh:
        _check(fh.read(), tail)


@pytest.mark.parametrize("block,desc", REAL_SUMMARIES)
def test_every_real_summary_gives_a_safe_green_description(block, desc):
    _check("## \U0001f52d ACME Diff Preview\n\n" + block + "\n---\n", desc)


def test_goldens_lead_as_the_verdict_says():
    def lead(name):
        path = os.path.join(os.path.dirname(__file__), "golden", name)
        with open(path, encoding="utf-8") as fh:
            return cr.status_lead(fh.read())
    assert lead("all_clean.md") == ""
    assert lead("ordinary_version_bump.md") == ""
    assert lead("true_deletion_shouts.md") == (
        "\u26a0\ufe0f 1 resource(s) deleted in 1 environment(s) (1 Service): "
        "pv-acme-a")
    assert lead("merge_summary_mixed.md") == (
        "\U0001f6a8 VM infrastructure change flagged dangerous \u2014 see the "
        "VM section")


def test_a_long_real_finding_is_cut_and_the_tail_kept():
    block, desc = next(s for s in REAL_SUMMARIES if "24 HorizontalPod" in s[0])
    _lead, out = _check(block, desc)
    assert _u8(out) >= 253, "cut at the limit, less a multibyte character"
    assert out.endswith("... | " + desc), out


# ── status_lead ──────────────────────────────────────────────────────────

def test_markdown_links_and_the_bullet_emoji_are_removed():
    c = _comment(REVIEW, "\U0001f39a\ufe0f **acme-ping-scaler activated** in "
                 "pv-x \u2014 `2` HPA(s) ([how it works](https://h/p))")
    assert cr.status_lead(c) == ("\u26a0\ufe0f acme-ping-scaler activated in "
                                 "pv-x \u2014 2 HPA(s) (how it works)")


def test_only_the_first_bullet_leads():
    c = _comment(BLOCK, "\U0001f5a5\ufe0f **VM infrastructure change flagged "
                 "dangerous** \u2014 see the VM section", DELETION)
    assert cr.status_lead(c) == ("\U0001f6a8 VM infrastructure change flagged "
                                 "dangerous \u2014 see the VM section")


@pytest.mark.parametrize("comment", [
    "",
    None,
    "no summary at all",
    _comment(ROUTINE, "\u2705 3 app(s) change, nothing risk-flagged"),
    _comment(REVIEW),
    f"{HDR}\n\n{REVIEW}\n",
    f"{HDR}\n",
])
def test_no_lead_without_a_review_or_block_finding(comment):
    assert cr.status_lead(comment) == ""


def test_a_summary_below_the_first_rule_is_author_content():
    """Rendered manifests and the new-env appendix come after the first
    '---'. A fake summary there must not become our status."""
    fake = f"{HDR}\n\n{BLOCK}\n\n- ⛔ **fake** finding\n"
    new_env_only = ("## \U0001f52d ACME Diff Preview\n\n### \U0001f195 New "
                    "Environment(s) Detected\n\n---\n" + fake)
    assert cr.status_lead(new_env_only) == ""
    assert cr.status_lead(_comment(ROUTINE, "\u2705 ok", after=fake)) == ""
    assert cr.status_lead(_comment(REVIEW, DELETION, after=fake)) == LEAD


def test_the_lead_never_says_do_not_merge():
    c = _comment(BLOCK, "\U0001f6a8 **SHARED DATA \u2014 DO NOT MERGE** "
                 "or do  **not** [merge](x) it")
    lead = cr.status_lead(c)
    assert "do not merge" not in lead.lower(), lead
    assert lead.startswith("\U0001f6a8 SHARED DATA"), lead


# ── the order inside REVIEW: a cause leads its effect ────────────────────

def _bullets(results, state=None):
    lines = cr._build_merge_summary(results, {}, None, None, state, None, False)
    return [l[2:] for l in lines if l.startswith("- ")]


def test_a_downgrade_leads_the_deletions_it_causes():
    """#4549 and #4542: the lead named '4 Service deleted', not the
    downgrade 2603.1.32 -> 2603.0.21-rev1 behind it."""
    results = {"pv-x-a-ms": m.DiffResult(
        "d", [], 4, True, "", m.OUT_DIFF, "",
        ("2603.1.32", "2603.0.21-rev1"),
        [f"/v1/Service pv-x-a/s-{i}" for i in range(4)])}
    b = _bullets(results)
    assert b[0].startswith("\u2b07\ufe0f **Chart version downgrade**"), b
    assert b[1].startswith("\U0001f5d1\ufe0f **4 resource(s) deleted**"), b
    assert cr.status_lead("\n".join(cr._build_merge_summary(
        results, {}, None, None, None, None, False))).startswith(
        "\u26a0\ufe0f Chart version downgrade"), "the green status follows"


def test_a_shutdown_leads_the_hpas_it_deletes():
    """#4567 and #4571: '22 HorizontalPodAutoscaler...' led, not
    'Environment shutting down (109 workloads to 0)'."""
    results = {"pv-x-a-ms": m.DiffResult(
        "d", [], 131, True, "", m.OUT_DIFF, "", None,
        [f"/autoscaling/v2/HorizontalPodAutoscaler pv-x-a/h-{i}"
         for i in range(22)], True,
        shutdown_stats={"workloads": 109, "zeroed": 109})}
    b = _bullets(results)
    assert b[0].startswith("\U0001f6d1 **Environment shutting down**"), b
    assert b[1].startswith("\U0001f5d1\ufe0f **22 resource(s) deleted**"), b


def test_the_higher_layer_note_is_the_last_review_item():
    results = {
        "pv-x-a-ms": m.DiffResult("", [], 0, False, "", m.OUT_INDETERMINATE,
                                  m.REASON_TIMEOUT),
        "pv-y-a-ms": m.DiffResult("d", [], 1, True, "", m.OUT_DIFF, "", None,
                                  ["/v1/Secret pv-y-a/s"])}
    b = _bullets(results, [cr._VALUES_REDUNDANCY_HDR])
    assert [x[:1] for x in b] == ["\U0001f5d1", "\u2754", "\U0001f4da"], b


def test_block_findings_keep_their_order():
    """The rank is only inside REVIEW: a BLOCK finding still leads."""
    results = {"pv-x-a-ms": m.DiffResult(
        "d", [], 1, True, "", m.OUT_DIFF, "", ("2603.1.0", "2603.0.0"),
        ["/v1/Secret pv-x-a/s"])}
    b = _bullets(results, [cr._DECOM_FLAG_TYPO_HDR])
    assert b[0].startswith("\U0001f6a8 **Teardown flag misspelled"), b
    assert b[1].startswith("\u2b07"), b


# ── join_status_lead ─────────────────────────────────────────────────────

def test_no_lead_keeps_the_description():
    assert cr.join_status_lead("", "No manifest changes") == "No manifest changes"


def test_a_short_lead_is_joined_whole():
    assert cr.join_status_lead(LEAD, "1 resource(s) will change - review "
                               "comment") == (LEAD + " | 1 resource(s) will "
                                              "change - review comment")


def test_the_limit_counts_utf8_bytes():
    """🚨 is one code point, two UTF-16 units and four UTF-8 bytes."""
    lead = "\U0001f6a8 " + "x" * 300
    out = cr.join_status_lead(lead, "tail")
    assert _u8(out) == 255 and len(out) == 252
    assert out.endswith("x... | tail")


def test_a_multibyte_character_is_never_split():
    lead = "\u26a0\ufe0f " + "\u2014" * 100          # 3 bytes each
    out = cr.join_status_lead(lead, "tail")
    assert 253 <= _u8(out) <= 255 and out.endswith("\u2014... | tail")


def test_a_description_with_no_room_comes_back_unchanged():
    tail = "y" * 250
    assert cr.join_status_lead(LEAD, tail) == tail
    assert cr.join_status_lead(LEAD, "z" * 300) == "z" * 300


# ── fuzz ─────────────────────────────────────────────────────────────────

_VERDICT_LINES = st.sampled_from([REVIEW, BLOCK, ROUTINE, "", "- " + DELETION])


@given(st.text())
def test_fuzz_status_lead_on_any_text(text):
    lead = cr.status_lead(text)
    assert "do not merge" not in lead.lower()
    assert "\n" not in lead and "\r" not in lead


@given(_VERDICT_LINES, st.text(), st.text(), st.text())
@example(BLOCK, "DO NOT MERGE", "", "")
@example(BLOCK, "\u26d4 do\u00a0not\tmerge", "", "")
@example(BLOCK, "x [DO NOT](u) MERGE", "", "")
def test_fuzz_status_lead_on_a_summary(verdict, bullet, before, after):
    lead = cr.status_lead(_comment(verdict, bullet.replace("\n", " "),
                                   after=after).replace("## \U0001f52d",
                                                        before + "##", 1))
    assert "do not merge" not in lead.lower(), lead
    assert lead == "" or lead.startswith(("\U0001f6a8 ", "\u26a0\ufe0f ")), lead


@given(st.text(), st.text())
def test_fuzz_join_keeps_the_tail_and_the_limit(lead, tail):
    out = cr.join_status_lead(lead, tail)
    assert out.endswith(tail)
    if out != tail:
        assert _u8(out) <= 255
        assert out.endswith(" | " + tail)


# ── both paths write the same text ───────────────────────────────────────

def _deletion_plan(plan):
    plan["pv-orch-a-ms"] = m.DiffResult(
        "--- main\n+++ pr", [("/v1/Service gone", "-kind: Service")],
        1, True, "", m.OUT_DIFF, "", None, ["/v1/Service gone"])


def test_process_pr_and_fix_stuck_write_the_same_green_text(world, monkeypatch):
    sinks, plan = world
    _deletion_plan(plan)
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    body = sinks.upserts[-1]
    assert m._extract_status_token(body) == "clean", "not the transient path"
    state, desc = sinks.statuses[-1]
    assert (state, desc) == (
        "SUCCESSFUL", LEAD + " | 1 resource(s) will change - review comment")

    monkeypatch.setattr(m, "http", lambda *a, **k: {"state": "INPROGRESS"})
    assert REAL_FIX_STUCK(PR_SHA, 991, body) == "ok"
    assert sinks.statuses[-1] == (state, desc)


def test_a_routine_pr_keeps_its_green_text(world):
    sinks, plan = world
    plan["pv-orch-a-ms"] = m.DiffResult(
        "--- main\n+++ pr", [("Deployment/webx", "-replicas: 2\n+replicas: 3")],
        1, True, "", m.OUT_DIFF, "")
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    assert m._extract_status_token(sinks.upserts[-1]) == "clean"
    assert sinks.statuses[-1] == (
        "SUCCESSFUL", "1 resource(s) will change - review comment")


# ── FAILED descriptions do not change ────────────────────────────────────

@pytest.mark.parametrize("other,desc", [
    (lambda: m.DiffResult("", [], 0, False, "", m.OUT_INDETERMINATE, m.REASON_TIMEOUT),
     "Diff unavailable for 1 app(s) | 1 resource(s) confirmed changed - "
     "review comment (will retry automatically if transient)"),
    (lambda: m.DiffResult("", [], 0, False, "helm exploded\nstack", m.OUT_ERROR, ""),
     "Diff failed: helm exploded - check PR comment"),
    (lambda: m.DiffResult("", [], 0, False, "appspace-ss:2603.9.9 not found",
                  m.OUT_INDETERMINATE, m.REASON_OCI_NOT_FOUND),
     "appspace-ss:2603.9.9 not found \u2014 pv-orch-a \u2014 check the version "
     "or wait for the registry"),
    (lambda: m.DiffResult("", [], 0, False, "Error: YAML parse error",
                  m.OUT_INDETERMINATE, m.REASON_INVALID_YAML),
     "invalid YAML in values \u2014 pv-orch-a \u2014 fix and push"),
    (lambda: m.DiffResult("--- main\n+++ pr",
                  [("/compute.cnrm.cloud.google.com/ComputeInstance vm-a",
                    "+  hosting-id: hst-%!s(<nil>)")],
                          1, True, "", m.OUT_DIFF, "",
                  template_artifacts=[
                      "/compute.cnrm.cloud.google.com/ComputeInstance vm-a"]),
     "Unresolved KCC value - Compute* resources render %!s(<nil>) / "
     "<no value>; set hostingID (or the missing field) before merging "
     "(see PR comment)"),
])
def test_process_pr_failed_descriptions_are_unchanged(world, other, desc):
    """A deletion gives the comment a review finding, and the red status
    must still read exactly as before."""
    sinks, plan = world
    _deletion_plan(plan)
    plan["pv-orch-a-ss"] = other()        # built now: some tests reload m
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    assert cr.status_lead(sinks.upserts[-1])            # precondition
    assert sinks.statuses[-1] == ("FAILED", desc)


_TOKEN = "*ts \u2014 " + m.COMMENT_MARKER + " [{}]*"


@pytest.mark.parametrize("extra,desc", [
    (_TOKEN.format("permanent"), "Diff failed - check PR comment"),
    (_TOKEN.format("blocked"),
     "Blocked - merging would break the environment (see comment)"),
    (_TOKEN.format("transient"),
     "Diff unavailable - review comment (will retry automatically if transient)"),
    ("\u26d4 legacy block", "Blocked - merging would break the environment (see comment)"),
    ("Error running diff", "Diff failed - check PR comment"),
    ("chart not found in OCI registry", "Chart version not found in OCI registry"),
    ("Diff incomplete", "Diff unavailable - review comment"),
])
def test_fix_stuck_failed_descriptions_are_unchanged(monkeypatch, extra, desc):
    posted = []
    monkeypatch.setattr(m, "http", lambda *a, **k: {"state": "INPROGRESS"})
    monkeypatch.setattr(m, "post_build_status",
                        lambda sha, state, d, pr_id=None, repo=None:
                        posted.append((state, d)) or "ok")
    comment = _comment(REVIEW, DELETION, after=extra)
    assert cr.status_lead(comment) == LEAD              # precondition
    REAL_FIX_STUCK("a" * 12, 10, comment)
    assert posted == [("FAILED", desc)]


@pytest.mark.parametrize("extra,desc", [
    ("3 resource(s) will change\n" + _TOKEN.format("clean"),
     "3 resource(s) will change - review comment"),
    ("New Environment(s) Detected ~4 resource(s) to create\n"
     + _TOKEN.format("clean"), "New environment(s) - ~4 resource(s) to create"),
    ("No ArgoCD apps affected\n" + _TOKEN.format("clean"),
     "No ArgoCD apps affected by this PR"),
    (_TOKEN.format("clean"), "No manifest changes"),
    ("legacy: 7 resource(s) will change", "7 resource(s) will change - review comment"),
    ("legacy with no markers", "No manifest changes"),
])
def test_fix_stuck_leads_every_green_description(monkeypatch, extra, desc):
    posted = []
    monkeypatch.setattr(m, "http", lambda *a, **k: {"state": "INPROGRESS"})
    monkeypatch.setattr(m, "post_build_status",
                        lambda sha, state, d, pr_id=None, repo=None:
                        posted.append((state, d)) or "ok")
    REAL_FIX_STUCK("a" * 12, 10, _comment(REVIEW, DELETION, after=extra))
    assert posted == [("SUCCESSFUL", LEAD + " | " + desc)]
