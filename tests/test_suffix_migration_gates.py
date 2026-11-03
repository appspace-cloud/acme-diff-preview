"""A PR that lets two cells of one customer share the public LB record or a
KCC VM is blocked, on its own customer.yaml or through a config.yaml above it.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import comment_render as cr  # noqa: E402
import diff_preview as m  # noqa: E402

from test_coverage_orchestration import (  # noqa: E402,F401
    world, _mk_pr, PATH_MAP, BASE_SHA, PR_SHA)

pytestmark = pytest.mark.fleet_reads

H, B = PR_SHA, BASE_SHA
SPOKE = "gcp/prod/private-cloud/na1-b/monthly"
COHORT = f"{SPOKE}/config.yaml"
OLD = f"{SPOKE}/pv-example-a/customer.yaml"
NEW = f"{SPOKE}/pv-example-b/customer.yaml"
OTHER = f"{SPOKE}/pv-other-a/customer.yaml"
ON = "  loadBalancers:\n    dns:\n      managedZone:\n        enabled: true\n"
OFF = "  loadBalancers:\n    dns:\n      managedZone:\n        enabled: false\n"
COHORT_DOC = ("appspace:\n  environmentType: private-cloud\n  cloudShortName: gcp\n"
              "  deploymentType: pv-prod\n  prefix: pv\n  version: 2603.4.10\n"
              "  infra:\n    gcp:\n      project: example-project\n")
SVC = ("  infra:\n    deployLinuxServicesK8s:\n      enabled: true\n"
       "      svc:\n        enabled: true\n        instanceName: {}\n")


def _doc(suffix, extra="", name="example"):
    return f"appspace:\n  customerName: {name}\n  suffix: {suffix}\n" + extra


def _serve(monkeypatch, head, base):
    def fetch(path, sha, repo=None):
        side = head if sha == H else base if sha == B else {}
        return (side[path], m.BB_OK) if path in side else (None, m.BB_NOT_FOUND)
    monkeypatch.setattr(m, "_bb_fetch_status", fetch)


def _detect(monkeypatch, changed, head, base, fleet=(OLD, OTHER)):
    head, base = {COHORT: COHORT_DOC, **head}, {COHORT: COHORT_DOC, **base}
    _serve(monkeypatch, head, base)
    monkeypatch.setattr(m, "_fleet_identity_files", lambda repo=None: sorted(fleet))
    path_map = {p: [] for p in fleet}
    gates, notes = m._detect_suffix_migration_gates(changed, {}, path_map, H, B)
    return sorted((g["kind"], g["env"]) for g in gates), notes


def test_a_new_cell_with_the_record_on_takes_the_live_name(monkeypatch):
    live = {OLD: _doc("a"), OTHER: _doc("a", name="other")}
    assert _detect(monkeypatch, [NEW], {**live, NEW: _doc("b", ON)}, live) == (
        [("dns_new_sibling", "pv-example-b")], [])


def test_a_new_cell_with_the_record_off_is_clean(monkeypatch):
    live = {OLD: _doc("a", ON), OTHER: _doc("a", name="other")}
    assert _detect(monkeypatch, [NEW], {**live, NEW: _doc("b", OFF)}, live) == ([], [])


def test_off_and_on_in_one_pr(monkeypatch):
    base = {OLD: _doc("a", ON), NEW: _doc("b", OFF)}
    head = {OLD: _doc("a", OFF), NEW: _doc("b", ON)}
    assert _detect(monkeypatch, [OLD, NEW], head, base, fleet=(OLD, NEW)) == (
        [("dns_two_owners", "pv-example-b")], [])


def test_a_cohort_that_turns_the_record_on_for_two_cells(monkeypatch):
    cells = {OLD: _doc("a"), NEW: _doc("b")}
    head = {**cells, COHORT: COHORT_DOC + ON}
    assert _detect(monkeypatch, [COHORT], head, cells, fleet=(OLD, NEW)) == (
        [("dns_two_owners", "pv-example-a"), ("dns_two_owners", "pv-example-b")], [])


def test_a_cohort_change_to_other_keys_reads_no_cell(monkeypatch):
    head = {COHORT: COHORT_DOC + "  zeroPods: true\n"}
    _serve(monkeypatch, {**head}, {COHORT: COHORT_DOC})
    monkeypatch.setattr(m, "_fleet_identity_files",
                        lambda repo=None: pytest.fail("fleet read for a key it does not use"))
    assert m._detect_suffix_migration_gates([COHORT], {}, {OLD: []}, H, B) == ([], [])


def test_a_version_bump_reads_no_cell(monkeypatch):
    _serve(monkeypatch, {OLD: _doc("a", ON + "  version: 2603.4.11\n")}, {OLD: _doc("a", ON)})
    monkeypatch.setattr(m, "_fleet_identity_files",
                        lambda repo=None: pytest.fail("fleet read for a key it does not use"))
    assert m._detect_suffix_migration_gates([OLD], {}, {OLD: []}, H, B) == ([], [])


def test_decommission_of_the_cell_that_still_writes_the_name(monkeypatch):
    base = {OLD: _doc("a", ON), NEW: _doc("b", OFF)}
    head = {**base, OLD: _doc("a", ON + "  decommission: true\n")}
    assert _detect(monkeypatch, [OLD], head, base, fleet=(OLD, NEW)) == (
        [("dns_decommission_shared", "pv-example-a")], [])


def test_removing_the_old_folder_while_the_new_cell_turns_it_on(monkeypatch):
    base = {OLD: _doc("a", ON), NEW: _doc("b", OFF)}
    head = {NEW: _doc("b", ON)}
    assert _detect(monkeypatch, [OLD, NEW], head, base, fleet=(OLD, NEW)) == (
        [("dns_two_owners", "pv-example-b")], [])


@pytest.mark.parametrize("vm, found", [
    ("pv-example-svc-a", [("vm_shared", "pv-example-b")]), ("pv-example-svc-b", [])])
def test_a_copied_vm_name(monkeypatch, vm, found):
    live = {OLD: _doc("a", SVC.format("pv-example-svc-a"))}
    head = {**live, NEW: _doc("b", SVC.format(vm))}
    assert _detect(monkeypatch, [NEW], head, live) == (found, [])


def test_a_sibling_that_cannot_be_parsed_is_a_note(monkeypatch):
    live = {OLD: "appspace: [", OTHER: _doc("a", name="other")}
    gates, notes = _detect(monkeypatch, [NEW], {**live, NEW: _doc("b", ON)}, live)
    assert gates == [] and len(notes) == 1 and "`pv-example-a`" in notes[0]


def test_a_failed_read_retries_the_pr(monkeypatch):
    _serve(monkeypatch, {}, {})
    monkeypatch.setattr(m, "_bb_fetch_status", lambda path, sha, repo=None: (None, m.BB_ERROR))
    with pytest.raises(m.ValueFileUnreadable):
        m._detect_suffix_migration_gates([NEW], {}, {}, H, B)


def test_nothing_lifts_them():
    assert all(cr.GATES[k][1] is None for k in m._SUFFIX_KINDS)


# ── process_pr ───────────────────────────────────────────────────────────

RENDERED = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: platform-config\n"


@pytest.fixture()
def new_cell(world, monkeypatch):
    sinks, _plan = world
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)
    monkeypatch.setattr(m, "get_pr_changed_files", lambda pr_id, repo=None: ([NEW], {}))
    monkeypatch.setattr(m, "_render_new_env_diff",
                        lambda env_info, pr_sha: (RENDERED, None, 1, "2603.4.10"))
    monkeypatch.setattr(m, "_fleet_identity_files", lambda repo=None: [OLD])

    def run(own):
        live = {OLD: _doc("a"), COHORT: COHORT_DOC}
        _serve(monkeypatch, {**live, NEW: own}, live)
        m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
        return sinks.upserts[-1], sinks.statuses[-1]
    return run


def test_a_new_cell_that_takes_the_name_blocks_the_pr(new_cell):
    body, (state, desc) = new_cell(_doc("b", ON))
    assert m._extract_status_token(body) == "blocked" and state == "FAILED"
    assert desc.startswith(f"Blocked - {cr.GATES['dns_new_sibling'][0]} "
                           "(example.cloud.appspace.com) in pv-example-b.")
    assert "## ⛔ TWO CELLS OF ONE CUSTOMER SHARE A DNS NAME OR A VM" in body


def test_a_new_cell_with_the_record_off_stays_green(new_cell):
    body, (state, _desc) = new_cell(_doc("b", OFF))
    assert state == "SUCCESSFUL" and "SHARE A DNS NAME" not in body


# ── docs ─────────────────────────────────────────────────────────────────

from test_cops2766_block3_docs import _cells, _guard_rows, _read, _slug  # noqa: E402

ANCHOR = "why-two-cells-of-one-customer-must-not-share-a-dns-name-or-a-vm"


@pytest.mark.parametrize("guard", ["Two cells write one DNS record",
                                   "New cell takes a live DNS name",
                                   "Decommission deletes a shared DNS name",
                                   "Two cells manage one KCC VM"])
def test_each_gate_has_a_guards_row_with_no_override(guard):
    rows = [r for r in _guard_rows() if _cells(r)[0] == f"**{guard}**"]
    assert len(rows) == 1 and _cells(rows[0])[3] == "none", rows
    assert f"internals.md#{ANCHOR}" in rows[0]


def test_the_internals_section_exists_and_is_listed():
    internals = _read("docs/internals.md")
    assert ANCHOR in {_slug(l.lstrip("#")) for l in internals.splitlines() if l.startswith("#")}
    assert f"](#{ANCHOR})" in internals
