"""Phase 1 must locate itself, and a misspelled teardown flag must not pass (COPS-2707).

Both gaps came off the same live teardown, `pv-gsk--aec1-b` in
acme-config-prod:

  #4376 added `appspace.decomission: true` -- one `m`. Neither the chart nor
  the ApplicationSet templatePatch reads that key, so nothing was armed, the
  render was byte-identical, and the verdict read "Routine -- nothing
  dangerous detected". It merged.

  #4378 armed `allowDeletion` correctly, and its comment carried only the VM
  danger bullets. The first PR of a three-PR sequence was the one with no
  phase table -- the same gap COPS-2616 closed for phases 2 and 3.

  #4377 then tried to remove the folder. It was correctly blocked for having
  no cascade armed, but it could not say WHY: the file it was judging carried
  a flag that looked armed to any reader.

Phase numbering is acme-components documentation/decommission-environment.md:
Phase 1 arms `allowDeletion`, Phase 2 arms the cascade (the data purge is a
qualifier on it), Phase 3 removes the folder. Phases 1 and 2 may share a PR;
Phase 3 must not.
"""
import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m  # noqa: E402
from decommission import (  # noqa: E402
    _flag_edit_distance,
    _reads_as_flag,
    _teardown_flag_typos,
)

IDENT = "gcp/aec/private-cloud/na4-a/pv-gsk--aec1-b/customer.yaml"
CL_IDENT = "gcp/prod/public-cloud/na1-a/cl-prod-b/customer.yaml"
APPS = ["pv-gsk--aec1-b-ms", "pv-gsk--aec1-b-ss", "pv-gsk--aec1-b-glb"]
PATH_MAP = {IDENT: APPS}

# The VM block exactly as #4378 found it, and as it left it. Only
# `defaults.allowDeletion` is added: the runbook is explicit that the rest of
# the block has to stay, or helm stops rendering the CRs the arming acts on.
VM_BLOCK = ("  infra:\n"
            "    deployLinuxServicesK8s:\n"
            "      enabled: true\n"
            "      svc:\n"
            "        enabled: true\n")
VM_BLOCK_ARMED = ("  infra:\n"
                  "    deployLinuxServicesK8s:\n"
                  "      defaults:\n"
                  "        allowDeletion: true\n"
                  "      enabled: true\n"
                  "      svc:\n"
                  "        enabled: true\n")

BASE = "appspace:\n  customerName: gsk--aec1\n" + VM_BLOCK
ARMS_VM = "appspace:\n  customerName: gsk--aec1\n" + VM_BLOCK_ARMED


def _mk_fetch(files_by_sha):
    def fake(path, sha, repo=None):
        v = files_by_sha.get((path, sha))
        return (v, m.BB_OK) if v is not None else (None, m.BB_NOT_FOUND)
    return fake


def _panel(monkeypatch, base, head, sha_tag, ident=IDENT, path_map=None):
    """Render the appspace-state panels for one identity file.

    Fresh shas per call: the fetch layer memoises on (path, sha), so reusing
    a tag would serve the previous fixture's content back. Same trap
    documented in test_cops2587_armed_phase_context.py.
    """
    b, h = "base" + sha_tag, "pr" + sha_tag
    monkeypatch.setattr(m, "_bb_fetch_status", _mk_fetch({
        (ident, b): base,
        (ident, h): head,
    }))
    return "\n".join(m._summarize_appspace_state_changes(
        [ident], h, b, path_map if path_map is not None else {ident: APPS}))


def _phase_rows(panel):
    return [l for l in panel.splitlines()
            if l.startswith("|") and "Phase" in l]


def _phase_state(panel, phase):
    rows = [l for l in _phase_rows(panel) if f"**Phase {phase}" in l]
    assert rows, f"no Phase {phase} row in:\n{panel}"
    cells = [c.strip() for c in rows[0].strip().strip("|").split("|")]
    assert len(cells) == 3, "unexpected phase-table row shape: " + rows[0]
    return cells[1]


# ── 1. Phase 1 alone renders the table ───────────────────────────────────

def test_arming_allow_deletion_alone_renders_the_phase_table(monkeypatch):
    """The #4378 shape. Before COPS-2707 this produced no phase table at all,
    because no cascade transition fires and the VM panel does not carry one."""
    out = _panel(monkeypatch, BASE, ARMS_VM, "p1only")
    assert _phase_rows(out), (
        "a PR that arms allowDeletion must locate itself in the "
        "sequence:\n" + out)
    assert "Phase 1" in out and "Phase 2" in out and "Phase 3" in out


def test_phase1_row_marks_this_pr_and_the_others_are_not_claimed(monkeypatch):
    out = _panel(monkeypatch, BASE, ARMS_VM, "p1mark")
    assert _phase_state(out, 1) == m._PH_THIS_PR
    assert _phase_state(out, 2) == m._PH_PENDING, \
        "the cascade is untouched by this PR, it cannot read as done"
    assert _phase_state(out, 3) == m._PH_PENDING


def test_phase1_panel_says_this_pr_deletes_nothing(monkeypatch):
    """The reviewer's first question on a red VM panel is whether merging
    destroys something today. It does not, and the panel has to say so
    without softening what the flag enables."""
    out = _panel(monkeypatch, BASE, ARMS_VM, "p1safe").lower()
    assert "deletes nothing by itself" in out
    assert "abandon" in out and "delete" in out


def test_phase1_panel_names_the_next_step(monkeypatch):
    out = _panel(monkeypatch, BASE, ARMS_VM, "p1next")
    assert "appspace.decommission: true" in out, \
        "the reviewer must be told what Phase 2 actually is"
    assert "Phase 3 must not" in out, \
        "the one-PR-per-phase-3 rule is the part operators get wrong"


def test_phase1_next_step_skips_phase2_when_the_cascade_is_already_armed(
        monkeypatch):
    """Arming order is not fixed by the runbook. When Phase 2 landed first,
    telling the reviewer to go and arm it is wrong."""
    base = "appspace:\n  customerName: g\n  decommission: true\n" + VM_BLOCK
    head = ("appspace:\n  customerName: g\n  decommission: true\n"
            + VM_BLOCK_ARMED)
    out = _panel(monkeypatch, base, head, "p1after2")
    assert _phase_state(out, 1) == m._PH_THIS_PR
    assert _phase_state(out, 2) == m._PH_DONE
    assert "arm the cascade" not in out, \
        "Phase 2 is already done, do not send the reviewer to redo it"


# ── 2. phases 1 and 2 in one PR ──────────────────────────────────────────

def test_phases_1_and_2_in_one_pr_mark_both_rows_this_pr(monkeypatch):
    """decommission-environment.md: "Phases 1 and 2 can be the same PR."
    Reporting Phase 1 as a bare "done" there is not wrong, it is just
    useless -- it reads as work an earlier PR did."""
    head = ("appspace:\n  customerName: g\n  decommission: true\n"
            + VM_BLOCK_ARMED)
    out = _panel(monkeypatch, BASE, head, "p12")
    assert "DECOMMISSION ARMED" in out, \
        "the Phase 2 heading must survive (the merge summary matches it)"
    assert _phase_state(out, 1) == m._PH_THIS_PR
    assert _phase_state(out, 2) == m._PH_THIS_PR
    assert "DECOMMISSION PHASE 1" not in out, \
        "one panel per PR: the armed panel already carries the table"


def test_arming_the_cascade_alone_still_reports_phase1_as_earlier_work(
        monkeypatch):
    """Scope guard. When allowDeletion was armed by an earlier PR, Phase 1 is
    done, not "this PR" -- the distinction is the whole point of the change."""
    base = "appspace:\n  customerName: g\n" + VM_BLOCK_ARMED
    head = ("appspace:\n  customerName: g\n  decommission: true\n"
            + VM_BLOCK_ARMED)
    out = _panel(monkeypatch, base, head, "p2only")
    assert _phase_state(out, 1) == m._PH_DONE
    assert _phase_state(out, 2) == m._PH_THIS_PR


# ── 3. scope guards on the Phase 1 panel ─────────────────────────────────

def test_no_phase1_panel_when_allow_deletion_was_already_armed(monkeypatch):
    """A PR that merely touches a file where the flag has been on for weeks
    is not a Phase 1 PR, and must not be announced as one."""
    head = ("appspace:\n  customerName: g\n  version: 2603.1.0\n"
            + VM_BLOCK_ARMED)
    base = "appspace:\n  customerName: g\n" + VM_BLOCK_ARMED
    out = _panel(monkeypatch, base, head, "p1noise")
    assert "DECOMMISSION PHASE 1" not in out, out


def test_no_phase1_panel_on_public_cloud(monkeypatch):
    """COPS-2700/2701: the private Phase 1/2/3 model does not exist on cl-*.
    No cascade is ever templated there, so promising one is false guidance."""
    out = _panel(monkeypatch, BASE, ARMS_VM, "p1cl",
                 ident=CL_IDENT, path_map={CL_IDENT: ["cl-prod-b-ms"]})
    assert "DECOMMISSION PHASE 1" not in out, out
    assert "Phase 2" not in out, out


def test_phase1_survives_an_unreadable_ancestor_chain(monkeypatch):
    """COPS-2683 made the arming state read the merged ancestor chain, and
    that merge returns None when any parent `config.yaml` is unreadable
    (BB_ERROR or broken YAML). Phase 1 then falls back to the identity file
    alone, which is where `allowDeletion` is written in practice -- so a
    transient Bitbucket failure on a region config must not silently cost
    the reviewer the phase table on a teardown PR.
    """
    base, head = "baseanc", "pranc"
    contents = {(IDENT, base): BASE, (IDENT, head): ARMS_VM}

    def fake(path, sha, repo=None):
        if path.endswith("config.yaml"):
            return (None, m.BB_ERROR)
        v = contents.get((path, sha))
        return (v, m.BB_OK) if v is not None else (None, m.BB_NOT_FOUND)

    monkeypatch.setattr(m, "_bb_fetch_status", fake)
    out = "\n".join(m._summarize_appspace_state_changes(
        [IDENT], head, base, {IDENT: APPS}))
    assert "DECOMMISSION PHASE 1" in out, out
    assert _phase_state(out, 1) == m._PH_THIS_PR


def test_a_stripping_pr_keeps_the_broken_panel_not_the_phase1_one(monkeypatch):
    """COPS-2660 owns this shape. Arming while removing the block the arming
    acts through is not Phase 1 done, it is Phase 1 broken."""
    head = ("appspace:\n  customerName: g\n  infra:\n"
            "    deployLinuxServicesK8s:\n      defaults:\n"
            "        allowDeletion: true\n")
    out = _panel(monkeypatch, BASE, head, "p1strip")
    assert "DECOMMISSION PHASE 1" not in out
    assert _phase_state(out, 1) == m._PH_BROKEN


# ── 4. the misspelled-flag detector ──────────────────────────────────────

def test_flag_edit_distance_is_a_plain_levenshtein():
    """The threshold that decides what counts as a near miss is only as
    trustworthy as the distance behind it, so the distance is pinned
    directly rather than inferred from the detector's verdicts."""
    assert _flag_edit_distance("decommission", "decommission") == 0
    assert _flag_edit_distance("decomission", "decommission") == 1, \
        "one dropped m, the PR #4376 shape"
    assert _flag_edit_distance("decomision", "decommission") == 2
    assert _flag_edit_distance("", "abc") == 3
    assert _flag_edit_distance("decommission", "decommissionPurgeData") == 9, \
        "the two real flags must stay far apart or each would flag the other"


def test_reads_as_flag_catches_the_shapes_operators_type():
    assert _reads_as_flag("decomission", "decommission"), "one m, PR #4376"
    assert _reads_as_flag("decommisson", "decommission")
    assert _reads_as_flag("Decommission", "decommission"), "casing"
    assert _reads_as_flag("decommission_purge_data", "decommissionPurgeData")
    assert _reads_as_flag("decommissionpurgedata", "decommissionPurgeData")
    assert _reads_as_flag("allowdeletion", "allowDeletion")


def test_reads_as_flag_rejects_the_correct_spelling_and_the_siblings():
    assert not _reads_as_flag("decommission", "decommission")
    assert not _reads_as_flag("decommissionPurgeData", "decommission"), \
        "a real, different flag is not a typo of this one"
    assert not _reads_as_flag("decommission", "decommissionPurgeData")
    assert not _reads_as_flag("customerName", "decommission")
    assert not _reads_as_flag("zeroPods", "decommission")


def test_typo_detector_reports_the_key_and_its_working_form():
    found = _teardown_flag_typos({"appspace.decomission": "true"})
    assert found == [{"found": "appspace.decomission",
                      "canonical": "appspace.decommission"}]


def test_typo_detector_ignores_a_correctly_spelled_flag():
    assert _teardown_flag_typos({"appspace.decommission": "true"}) == []


def test_typo_detector_only_speaks_when_the_value_is_true():
    """`decomission: false` misleads nobody into thinking a cascade is armed,
    and a panel that fires on it trains reviewers to skip the panel."""
    assert _teardown_flag_typos({"appspace.decomission": "false"}) == []


def test_typo_next_to_a_correctly_armed_flag_is_not_reported():
    """Both keys present: the cascade IS armed, so nothing the operator
    intended failed to happen and there is nothing to warn about."""
    assert _teardown_flag_typos({
        "appspace.decommission": "true",
        "appspace.decomission": "true",
    }) == []


def test_typo_detector_matches_depth_not_just_name():
    """`appspace.<leaf>` is the flag. The same word nested inside another
    map belongs to a schema this detector knows nothing about."""
    assert _teardown_flag_typos({
        "appspace.someTool.decomission": "true"}) == []


def test_typo_detector_ignores_keys_outside_the_appspace_tree():
    """Value files carry top-level trees this service does not own. A
    `decomission` under one of them is not ours to interpret."""
    assert _teardown_flag_typos({
        "decomission": "true",
        "someChart.decomission": "true",
    }) == []


def test_typo_detector_covers_the_vm_arming_flag():
    found = _teardown_flag_typos({
        "appspace.infra.deployLinuxServicesK8s.defaults.allowdeletion": "true"})
    assert len(found) == 1
    assert found[0]["canonical"].endswith("defaults.allowDeletion")


def test_typo_detector_transition_mode_ignores_pre_existing_keys():
    """PR-review mode. The typo was merged weeks ago; an unrelated version
    bump touching the same file must not be blocked by it."""
    flat = {"appspace.decomission": "true"}
    assert _teardown_flag_typos(flat, previous=flat) == []
    assert _teardown_flag_typos(flat, previous={}) != []


# ── 5. the misspelled-flag panel and verdict ─────────────────────────────

def test_a_pr_adding_a_misspelled_flag_says_so(monkeypatch):
    """The #4376 shape end to end. It merged with a green comment."""
    head = "appspace:\n  decomission: true\n  customerName: g\n" + VM_BLOCK
    out = _panel(monkeypatch, BASE, head, "typo1")
    assert "TEARDOWN FLAG MISSPELLED" in out, out
    assert m._DECOM_FLAG_TYPO_HDR in out
    assert "`appspace.decomission`" in out, "name what was written"
    assert "`appspace.decommission`" in out, "and what works"
    assert "Nothing is armed" in out


def test_the_misspelled_flag_panel_blocks_the_merge(monkeypatch):
    """The verdict is the only part of the comment a rushed reviewer reads.
    #4376 said "Routine -- nothing dangerous detected" and was merged."""
    head = "appspace:\n  decomission: true\n  customerName: g\n" + VM_BLOCK
    out = _panel(monkeypatch, BASE, head, "typo2")
    joined = "\n".join(m._build_merge_summary(
        {}, {}, [], [], out.splitlines(), [], False))
    assert "DO NOT MERGE" in joined, joined
    assert "misspelled" in joined.lower()
    assert "Routine" not in joined


def test_a_correctly_armed_pr_does_not_get_the_typo_panel(monkeypatch):
    head = "appspace:\n  decommission: true\n  customerName: g\n" + VM_BLOCK
    out = _panel(monkeypatch, BASE, head, "typo3")
    assert "TEARDOWN FLAG MISSPELLED" not in out
    assert "DECOMMISSION ARMED" in out


# ── 6. the folder-removal panel explains the pending cascade ─────────────

PLAIN_DEPLOY = "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n"


def _removal_panel(monkeypatch, base_content, sha_tag):
    base, head = "rmbase" + sha_tag, "rmhead" + sha_tag

    def fake(path, sha, repo=None):
        if sha == head:
            return (None, m.BB_NOT_FOUND)
        return (base_content, m.BB_OK)

    monkeypatch.setattr(m, "_bb_fetch_status", fake)
    monkeypatch.setattr(
        m, "_render_main_side_resources",
        lambda app, sha: {("apps/Deployment", "ns", "web"): PLAIN_DEPLOY})
    lines, _envs = m._evaluate_env_decommissions(
        [{"env_name": "pv-gsk--aec1-b", "identity_file": IDENT,
          "apps": ["pv-gsk--aec1-b-ms"],
          "env_dir": os.path.dirname(IDENT)}],
        head, base)
    return "\n".join(lines)


def test_folder_removal_explains_why_phase2_is_pending(monkeypatch):
    """#4377 said "no cascade armed" over a customer.yaml that any reader
    would call armed. The reason has to sit next to the row that raised the
    question, or the reviewer concludes the tool is wrong."""
    out = _removal_panel(
        monkeypatch,
        "appspace:\n  decomission: true\n  customerName: g\n" + VM_BLOCK,
        "typo")
    assert _phase_state(out, 2) == m._PH_PENDING
    assert m._DECOM_FLAG_TYPO_HDR in out, out
    assert "`appspace.decomission`" in out
    assert "`appspace.decommission`" in out


def test_folder_removal_stays_quiet_when_the_flag_is_spelled_right(
        monkeypatch):
    out = _removal_panel(
        monkeypatch,
        "appspace:\n  decommission: true\n  customerName: g\n" + VM_BLOCK,
        "ok")
    assert _phase_state(out, 2) == m._PH_DONE
    assert m._DECOM_FLAG_TYPO_HDR not in out


def test_folder_removal_verdict_names_the_misspelling(monkeypatch):
    """"No cascade armed" alone sends the operator back to a file that looks
    armed. The verdict has to carry the reason, not just the symptom."""
    out = _removal_panel(
        monkeypatch,
        "appspace:\n  decomission: true\n  customerName: g\n" + VM_BLOCK,
        "verdict")
    joined = "\n".join(m._build_merge_summary(
        {}, {}, [], out.splitlines(), [], [], False))
    assert "DO NOT MERGE" in joined
    assert "misspelled" in joined.lower(), joined
    assert "no cascade armed" in joined.lower(), \
        "the orphaning finding must survive alongside it"


# ── 7. the phase table is context, never a verdict ───────────────────────

def test_the_phase1_panel_does_not_invent_a_verdict(monkeypatch):
    """COPS-2616 contract, extended to the new panel: the table is positional
    context. The danger of arming `allowDeletion` is the VM panel's finding
    to raise, and one event must not be reported twice."""
    out = _panel(monkeypatch, BASE, ARMS_VM, "p1verdict")
    joined = "\n".join(m._build_merge_summary(
        {}, {}, [], [], out.splitlines(), [], False))
    assert "Decommission ARMED" not in joined, \
        "arming allowDeletion is not arming the cascade"
    assert "misspelled" not in joined.lower()
    assert "Routine" in joined, joined
