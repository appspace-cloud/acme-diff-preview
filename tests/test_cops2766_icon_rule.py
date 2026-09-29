"""COPS-2766 block 2, items 7 and 10: the icons follow the build colour.

acme-config-dev #7372 (2.121.0): a green build under a comment that said
'⛔ DO NOT MERGE', with 🚨 VM bullets, and a green status that began with 🚨.
The reviewer could not know which one to believe. Now the build decides:

  * a green build (the token [clean], the only one process_pr posts
    SUCCESSFUL for) shows ⚠️ in the merge summary, in the VM, decommission
    and state panels and in the status lead. Its verdict is at most
    'Review before merging';
  * ⛔, 🚨 and ❌ are only for a FAILED build. A Confirm-* line that lifts a
    gate makes the build green, so its findings show ⚠️ too.

Item 7: a correctly armed cascade (with or without purge), 'Data purge
ARMED' and 'Decommission ARMED' are review items, not DO NOT MERGE. The
build was always green for them, so now the comment and the status agree.
"""
import glob
import os
import re
import sys

import pytest
from hypothesis import given, settings, strategies as st

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import comment_render as cr  # noqa: E402
import diff_preview as m  # noqa: E402

from test_coverage_orchestration import (  # noqa: E402,F401
    world, _mk_pr, PATH_MAP, BASE_SHA, IDENTITY, IDENTITY_YAML)
from test_cops2766_shrink_and_typo import values_pr, QA88  # noqa: E402,F401
from test_cops2766_teardown_gates import teardown  # noqa: E402,F401
from test_cops2766_ip_release import ip_pr, _gone, CA  # noqa: E402,F401

settings.register_profile("suite", deadline=None)
settings.load_profile("suite")

GOLDEN = os.path.join(os.path.dirname(__file__), "golden")
RED = re.compile("⛔|\U0001f6a8|❌")
WARN = "⚠️"
BLOCK = cr._VERDICTS[cr._SEV_BLOCK]
REVIEW = cr._VERDICTS[cr._SEV_REVIEW]
ROUTINE = cr._VERDICTS[cr._SEV_ROUTINE]


@pytest.fixture(autouse=True)
def no_vertex(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)


def _outside_fences(body):
    out, fence = [], False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
        elif not fence:
            out.append(line)
    return out


def _verdict(body):
    lines = body.split("\n---\n", 1)[0].splitlines()
    return next(l for l in lines[lines.index(cr.MERGE_SUMMARY_HDR) + 1:] if l.strip())


def _assert_green(body, desc=""):
    assert m._extract_status_token(body) == "clean"
    red = [l for l in _outside_fences(body) if RED.search(l)]
    assert not red, red
    assert not _verdict(body).startswith(BLOCK), _verdict(body)
    lead = cr.status_lead(body)
    assert lead == "" or lead.startswith(WARN + " "), lead
    assert not RED.search(desc), desc


def _gate(kind, env="pv-x-a", lifted=False):
    return {"kind": kind, "env": env, "arg": env, "lifted": lifted}


# ── (a) every green golden ───────────────────────────────────────────────

def _golden(name):
    with open(os.path.join(GOLDEN, name), encoding="utf-8") as fh:
        return fh.read()


def _goldens(*tokens):
    names = [os.path.basename(p) for p in sorted(glob.glob(os.path.join(GOLDEN, "*.md")))
             if not p.endswith("README.md")]
    return [n for n in names if m._extract_status_token(_golden(n)) in tokens]


def test_most_goldens_are_green():
    assert len(_goldens("clean")) >= 15


@pytest.mark.parametrize("name", _goldens("clean"))
def test_a_green_golden_shows_no_red_mark(name):
    _assert_green(_golden(name))


# ── (b) a red build keeps its stop sign ──────────────────────────────────

@pytest.mark.parametrize("name", [
    "env_decommission_orphan.md", "vm_strip_while_arming.md",
    "teardown_flag_misspelled.md", "schema_failure_readable.md",
    "cops2676_fleet_missing_image_quiet.md"])
def test_a_red_golden_keeps_its_stop_sign(name):
    body = _golden(name)
    assert name in _goldens("blocked", "permanent")
    assert _verdict(body).startswith(BLOCK)


# ── real scenarios, end to end ───────────────────────────────────────────

def test_7372_with_only_machine_type_is_green_and_shows_warnings(values_pr):
    """The VM is RUNNING, so a machineType change is a danger the build does
    not stop. 2.121.0 showed it with ⛔ and 🚨."""
    run, _plan = values_pr
    body, (state, desc) = run(QA88, QA88.replace("n2d-standard-2", "n2d-standard-4"))
    assert state == "SUCCESSFUL"
    _assert_green(body, desc)
    assert "runbook requires stopping the VM first" in body, "still the danger"
    assert _verdict(body).startswith(REVIEW)
    assert desc.startswith(f"{WARN} VM infrastructure change flagged dangerous"), desc


def test_7372_with_a_disk_shrink_stays_red(values_pr):
    run, _plan = values_pr
    body, (state, _desc) = run(QA88, QA88.replace("dataDiskSizeGb: 64", "dataDiskSizeGb: 32"))
    assert state == "FAILED" and _verdict(body).startswith(BLOCK)
    assert "- \U0001f6a8 " in body, "the VM bullets keep their red mark"


def test_a_confirmed_teardown_is_green_and_shows_warnings(teardown):
    commits, _calls = teardown
    body, (state, desc) = commits(["Remove pv-orch-a\n\nConfirm-Teardown: pv-orch-a\n"])
    assert state == "SUCCESSFUL"
    _assert_green(body, desc)
    assert "☑️ **Confirmed in a commit:** `Confirm-Teardown: pv-orch-a`" in body
    assert desc.startswith(f"{WARN} Environment decommission"), desc


def test_the_same_teardown_without_the_line_stays_red(teardown):
    commits, _calls = teardown
    body, (state, _desc) = commits(["Remove pv-orch-a"])
    assert state == "FAILED" and _verdict(body).startswith(BLOCK)
    assert f"- ⛔ **{cr.GATES['orphan'][0]}**" in body


def test_a_confirmed_ip_release_is_green_and_shows_warnings(ip_pr):
    body, (state, desc) = ip_pr(CA, _gone("ComputeAddress", "delete"),
                                ["New IP\n\nConfirm-IP-Release: pv-orch-a\n"])
    assert state == "SUCCESSFUL"
    _assert_green(body, desc)
    assert "Confirmed in a commit" in body


def test_arming_the_flag_on_cl_is_green_and_shows_warnings(world, monkeypatch):
    """No gate: the flag arms nothing on cl-*. The panel keeps its words."""
    sinks, _plan = world
    ident = "gcp/stage/public-cloud/na1/cl-adapter-a/customer.yaml"
    live = "appspace:\n  customerName: adapter\n"
    monkeypatch.setattr(m, "_bb_fetch_status", lambda p, s, repo=None: (
        (live + "  decommission: true\n" if s == "prsha" else live), m.BB_OK)
        if p == ident else (None, m.BB_NOT_FOUND))
    monkeypatch.setattr(m, "_merged_kcc_flat_for_env", lambda *a, **k: {})
    panel = m._summarize_appspace_state_changes(
        [ident], "prsha", "mainsha", {ident: ["cl-adapter-a-ms"]})
    assert any(RED.search(l) for l in panel), "precondition: the raw panel is red"
    monkeypatch.setattr(m, "_summarize_appspace_state_changes", lambda *a, **k: panel)
    monkeypatch.setattr(m, "_bb_fetch_status", lambda path, sha, repo=None:
                        (IDENTITY_YAML, m.BB_OK) if path.endswith(IDENTITY)
                        else (None, m.BB_NOT_FOUND))
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    body, (state, desc) = sinks.upserts[-1], sinks.statuses[-1]
    assert state == "SUCCESSFUL"
    _assert_green(body, desc)
    assert cr._DECOM_PUBLIC_CLOUD_NOOP_HDR in body
    assert f"{WARN} " + cr._DECOM_PUBLIC_CLOUD_NOOP_HDR in body


# ── format_comment: the three panels follow the token ────────────────────

PANELS = dict(
    appspace_state_lines=["## \U0001f6a8 PURGE ARMED for `pv-x-a` \U0001f6a8", ""],
    decommission_lines=["\U0001f6a8 " + cr._DECOM_SHARED_UC_HDR + " `pv-x-a` goes.", ""],
    vm_change_lines=[cr._VM_PANEL_DANGER_HDR, "", "- \U0001f6a8 `pv-x-a` machineType", ""])


@pytest.mark.parametrize("lifted,token", [(True, "clean"), (False, "blocked")])
def test_the_panels_follow_the_token(lifted, token):
    decom = {"pv-x-a-ms": m.DiffResult("", [], 0, False, None, m.OUT_DECOMMISSIONED, "")}
    body = m.format_comment("a" * 40, decom, base_sha="b" * 40,
                            gates=[_gate("shared_uc", lifted=lifted)], **PANELS)
    assert m._extract_status_token(body) == token
    shown = body.splitlines()
    for line in (l for panel in PANELS.values() for l in panel if l):
        assert (cr.build_marks([line], True)[0] if lifted else line) in shown, line
    assert cr._DECOM_SHARED_UC_HDR in body, "the words of a constant never change"
    assert _verdict(body).startswith(REVIEW if lifted else BLOCK)


def test_build_marks():
    assert cr.build_marks(["⛔️ a \U0001f6a8 b ❌", "\U0001f5a5⛔ c"], True) == [
        f"{WARN} a {WARN} b {WARN}", f"\U0001f5a5{WARN} c"]
    assert cr.build_marks(["⛔ a"], False) == ["⛔ a"]
    assert cr.build_marks(None, True) is None


# ── (c) fuzz: a green summary has no red mark ────────────────────────────

_RESULTS = {
    "deleted": lambda: m.DiffResult("d", [], 1, True, "", m.OUT_DIFF, "", None,
                                    ["/v1/Secret pv-x-a/s"]),
    "downgrade": lambda: m.DiffResult("d", [], 1, True, "", m.OUT_DIFF, "",
                                      ("2603.1.0", "2603.0.0")),
    "cannot_render": lambda: m.DiffResult("", [], 0, False, "bad", m.OUT_INDETERMINATE,
                                          m.REASON_INVALID_YAML),
    "timeout": lambda: m.DiffResult("", [], 0, False, "", m.OUT_INDETERMINATE,
                                    m.REASON_TIMEOUT),
    "error": lambda: m.DiffResult("", [], 0, False, "boom", m.OUT_ERROR, ""),
    "kcc_nil": lambda: m.DiffResult(
        "d", [], 1, True, "", m.OUT_DIFF, "",
        template_artifacts=["/compute.cnrm.cloud.google.com/ComputeInstance vm-a"]),
}
_STATE = [cr._DECOM_VM_STRIP_HDR, "\U0001f6a8 " + cr._DECOM_FLAG_TYPO_HDR,
          "\U0001f6a8 " + cr._DECOM_PUBLIC_CLOUD_NOOP_HDR,
          "## \U0001f6a8 PURGE ARMED for `pv-x-a` \U0001f6a8",
          "## \U0001f512 DECOMMISSION ARMED for `pv-x-a`", cr._AUTOSYNC_PAUSED_HDR,
          cr._BLAST_RADIUS_HDR, cr._IDENTITY_MIGRATION_HDR, cr._VALUES_REDUNDANCY_HDR]
_DECOM_LINES = ["\U0001f6a8 " + cr._DECOM_PURGE_HDR, cr._DECOM_ORPHAN_HDR,
                cr._DECOM_PUBLIC_CLOUD_HDR, "\U0001f6a8 " + cr._DECOM_SHARED_UC_HDR,
                "\U0001f6a8 " + cr._DECOM_FLAG_TYPO_HDR, "# ENVIRONMENT DECOMMISSION"]
_VM = [None, [cr._VM_PANEL_ROUTINE_HDR],
       [cr._VM_PANEL_DANGER_HDR, "- \U0001f6a8 **2 environments provision a new linux VM**"],
       [cr._VM_PANEL_DANGER_HDR, "- \U0001f6a8 `pv-x-a` machineType"]]


@given(st.dictionaries(st.sampled_from(["pv-x-a-ms", "pv-y-b-ss", "pv-z-c-glb"]),
                       st.sampled_from(sorted(_RESULTS)), max_size=3),
       st.sampled_from(_VM), st.lists(st.sampled_from(_DECOM_LINES), max_size=3),
       st.lists(st.sampled_from(_STATE), max_size=4),
       st.lists(st.builds(_gate, st.sampled_from(sorted(cr.GATES)),
                          lifted=st.booleans()), max_size=3),
       st.booleans())
def test_fuzz_a_green_summary_has_no_red_mark(kinds, vm, decom, state, gates, new_env):
    results = {app: _RESULTS[k]() for app, k in kinds.items()}
    args = (results, {}, vm, decom or None, state or None,
            ["new"] if new_env else None, new_env)
    red = cr._build_merge_summary(*args, gates=gates)
    green = cr._build_merge_summary(*args, gates=gates, green=True)
    assert not [l for l in green if RED.search(l)], green
    assert green[2].startswith((REVIEW, ROUTINE)), green[2]
    # Only the marks and the verdict change: the findings, their order and
    # their count stay.
    assert green[3:] == cr.build_marks(red[3:], True)
    assert red[2].rpartition(" (")[2] == green[2].rpartition(" (")[2]


# ── (d) the status lead ──────────────────────────────────────────────────

def _summary(verdict, bullet):
    return f"{cr.MERGE_SUMMARY_HDR}\n\n{verdict} (1 item(s))\n\n- {bullet}\n\n---\n"


def test_a_legacy_green_comment_with_a_stop_sign_leads_with_a_warning():
    """#7372 as 2.121.0 posted it: fix_stuck_inprogress still reads it."""
    lead = cr.status_lead(_summary(BLOCK, "\U0001f5a5️ **VM infrastructure change "
                                          "flagged dangerous** — see the VM section"))
    assert lead == f"{WARN} VM infrastructure change flagged dangerous — see the VM section"


@pytest.mark.parametrize("bullet,lead", [
    ("\U0001f6a8 **Data purge ARMED** — x", "Data purge ARMED — x"),
    ("\U0001f5a5⛔ **Decommission arming is BROKEN** — x",
     "Decommission arming is BROKEN — x"),
    ("\U0001f5d1️ **2 deleted** ❌ then \U0001f6a8 and ⛔️",
     f"2 deleted {WARN} then {WARN} and {WARN}"),
])
def test_a_green_lead_never_carries_a_red_mark(bullet, lead):
    assert cr.status_lead(_summary(BLOCK, bullet)) == f"{WARN} {lead}"


@given(st.sampled_from([BLOCK, REVIEW, ROUTINE]),
       st.text(alphabet=st.sampled_from("ab ⛔️\U0001f6a8❌⚠*`")))
def test_fuzz_a_lead_has_no_red_mark(verdict, bullet):
    lead = cr.status_lead(_summary(verdict, bullet.replace("\n", " ")))
    assert not RED.search(lead), lead
    assert lead == "" or lead.startswith(WARN + " "), lead


# ── item 7: an armed cascade is a review ─────────────────────────────────

def _bullets(out):
    return [l[2:] for l in out if l.startswith("- ")]


@pytest.mark.parametrize("decom,what", [
    (["\U0001f6a8 " + cr._DECOM_PURGE_HDR],
     "data purge is ARMED: buckets/datasets are destroyed, not abandoned"),
    (["# ENVIRONMENT DECOMMISSION"],
     "resources are deleted; data is abandoned, not purged"),
])
def test_a_correctly_armed_cascade_is_a_review(decom, what):
    out = cr._build_merge_summary({}, {}, None, decom, None, None, False)
    assert out[2] == f"{REVIEW} (1 item(s))", out
    assert _bullets(out) == [f"\U0001f5d1️ **Environment decommission** — {what}"]


@pytest.mark.parametrize("decom", [[cr._DECOM_ORPHAN_HDR], [cr._DECOM_PUBLIC_CLOUD_HDR],
                                   ["\U0001f6a8 " + cr._DECOM_SHARED_UC_HDR]])
def test_a_teardown_that_is_a_gate_stays_block(decom):
    out = cr._build_merge_summary({}, {}, None, decom, None, None, False)
    assert out[2].startswith(BLOCK), out


@pytest.mark.parametrize("state,bullet", [
    ("## \U0001f6a8 PURGE ARMED for `pv-x-a` \U0001f6a8", "\U0001f512 **Data purge ARMED**"),
    ("## \U0001f512 DECOMMISSION ARMED for `pv-x-a`", "\U0001f512 **Decommission ARMED**"),
])
def test_arming_is_a_review(state, bullet):
    out = cr._build_merge_summary({}, {}, None, None, [state], None, False)
    assert out[2] == f"{REVIEW} (1 item(s))", out
    assert _bullets(out)[0].startswith(bullet), out


def test_the_cl_noop_is_still_a_block_finding():
    """The build stays green for it (no gate), so the icon rule shows it as
    a review. On a red build it keeps its stop sign."""
    out = cr._build_merge_summary({}, {}, None, None,
                                  ["\U0001f6a8 " + cr._DECOM_PUBLIC_CLOUD_NOOP_HDR],
                                  None, False)
    assert out[2].startswith(BLOCK)


def test_arming_follows_a_downgrade_and_leads_the_deletions():
    results = {"pv-x-a-ms": m.DiffResult("d", [], 1, True, "", m.OUT_DIFF, "",
                                         ("2603.1.0", "2603.0.0"), ["/v1/Secret pv-x-a/s"])}
    out = cr._build_merge_summary(results, {}, None, None,
                                  ["## \U0001f512 DECOMMISSION ARMED for `pv-x-a`"], None, False)
    assert [b[:1] for b in _bullets(out)] == ["⬇", "\U0001f512", "\U0001f5d1"], out
