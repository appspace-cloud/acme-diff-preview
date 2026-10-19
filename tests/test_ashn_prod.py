"""In prod and AEC, an ashn another customer uses, or an ashn that is not in
the ASHN format, blocks the merge.

The PowerDNS name servers load a map of every Private Cloud ashn, built from
acme-config-prod by a job that refuses one ashn on two instances and a
malformed ashn. So the same two cases block here, before the merge, for every
prod and AEC environment whose own `appspace.ashn` is new or changed, not only
for the AEC clones. A clone keeps its own gate (`ashn_copy`). Another
environment of the same customer is no hit: it has the same instance host, so
it is one entry in the map. Dev, QA and stage throwaway environments use
placeholder ashns, so they are never read.
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
NEW = f"{SPOKE}/pv-newco-a/customer.yaml"
LIVE = f"{SPOKE}/pv-oldco-a/customer.yaml"
AZURE = "azure/prod/private-cloud/na1-b/monthly/pv-newco-a/customer.yaml"
CLONE = "gcp/aec/private-cloud/na2-a/monthly/pv-newco--aec1-a/customer.yaml"
COHORT = f"{SPOKE}/config.yaml"
ASHN = "3a02x8649"
TAKEN = cr.GATES["ashn_taken"][0]
FORMAT = cr.GATES["ashn_format"][0]
SHEET = "https://appspace.atlassian.net/wiki/spaces/cops/pages/66094009"
TAKEN_FIX = ("The name servers answer one ashn with one instance, and the ashn names the "
             f"environment in Customers. Take a new ashn from the ASHN sheet ({SHEET}).")
FORMAT_FIX = ("An ashn is 4 hex digits, `x` and 4 hex digits, like `3a02x8649`. Copy it "
              f"from column A of the ASHN sheet ({SHEET}).")


def _doc(name, ashn=ASHN):
    return ("appspace:\n" + (f"  ashn: {ashn}\n" if ashn else "")
            + f"  customerName: {name}\n  version: 2604.1.1\n")


@pytest.fixture(autouse=True)
def no_vertex(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)


def _serve(monkeypatch, head, base=None):
    base = base or {}

    def fetch(path, sha, repo=None):
        side = head if sha == H else base if sha == B else {}
        return (side[path], m.BB_OK) if path in side else (None, m.BB_NOT_FOUND)
    monkeypatch.setattr(m, "_bb_fetch_status", fetch)


def _fleet(monkeypatch, paths):
    monkeypatch.setattr(m, "_fleet_identity_files", lambda repo=None: sorted(paths))


def _detect(changed, renames=None):
    return m._detect_ashn_gates(changed, renames or {}, H, B)


def _env(path):
    return path.split("/")[-2]


def _taken(path=NEW, others=(LIVE,), ashn=ASHN):
    return {"kind": "ashn_taken", "env": _env(path),
            "why": "the ashn of " + ", ".join(_env(o) for o in others),
            "ashn": ashn, "path": path, "others": list(others)}


def _format(path=NEW, ashn="98c3z6d18"):
    return {"kind": "ashn_format", "env": _env(path), "why": ashn, "ashn": ashn, "path": path}


# ── the rule ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [NEW, AZURE])
def test_a_new_prod_env_with_the_ashn_of_another_customer_is_a_gate(monkeypatch, path):
    _serve(monkeypatch, {path: _doc("newco")}, {LIVE: _doc("oldco")})
    _fleet(monkeypatch, [LIVE])
    assert _detect([path]) == ([_taken(path)], [])


def test_a_live_prod_env_that_takes_an_ashn_in_use_is_a_gate(monkeypatch):
    _serve(monkeypatch, {NEW: _doc("newco")},
           {NEW: _doc("newco", "1111x2222"), LIVE: _doc("oldco")})
    _fleet(monkeypatch, [NEW, LIVE])
    assert _detect([NEW]) == ([_taken()], [])


def test_another_env_of_the_same_customer_is_no_gate(monkeypatch):
    _serve(monkeypatch, {NEW: _doc("oldco")}, {LIVE: _doc("oldco")})
    _fleet(monkeypatch, [LIVE])
    assert _detect([NEW]) == ([], [])


def test_a_new_prod_env_with_its_own_ashn_is_clean(monkeypatch):
    _serve(monkeypatch, {NEW: _doc("newco", "1111x2222")}, {LIVE: _doc("oldco")})
    _fleet(monkeypatch, [LIVE])
    assert _detect([NEW]) == ([], [])


@pytest.mark.parametrize("bad", ["98c3z6d18", "3A02X8649", "3a02x864", "3a02-8649",
                                 "2026-02-28", "12345678"])
def test_a_malformed_prod_ashn_is_a_gate(monkeypatch, bad):
    _serve(monkeypatch, {NEW: _doc("newco", bad)}, {LIVE: _doc("oldco")})
    _fleet(monkeypatch, [LIVE])
    assert _detect([NEW]) == ([_format(ashn=bad)], [])


def test_a_malformed_ashn_on_a_clone_is_a_gate(monkeypatch):
    _serve(monkeypatch, {CLONE: _doc("newco--aec1", "98c3z6d18")}, {LIVE: _doc("oldco")})
    _fleet(monkeypatch, [LIVE])
    assert _detect([CLONE]) == ([_format(CLONE)], [])


def test_a_clone_keeps_the_clone_gate(monkeypatch):
    _serve(monkeypatch, {CLONE: _doc("newco--aec1")}, {LIVE: _doc("oldco")})
    _fleet(monkeypatch, [LIVE])
    gates, notes = _detect([CLONE])
    assert [g["kind"] for g in gates] == ["ashn_copy"] and notes == []


@pytest.mark.parametrize("path", [
    "gcp/qa/private-cloud/ap1/custom/pv-qa98-a/customer.yaml",
    "gcp/dev/private-cloud/ap1/custom/pv-dev-spiralscout3-a/customer.yaml",
    "gcp/stage/private-cloud/na1-a/pv-stage-x-a/customer.yaml",
    f"{SPOKE}/pv-newco-a/cicd-versions.yaml",
    COHORT,
])
def test_dev_qa_stage_and_other_files_are_never_read(monkeypatch, path):
    monkeypatch.setattr(m, "_bb_fetch_status", lambda *a, **k: pytest.fail("read"))
    assert _detect([path]) == ([], [])


def test_an_unchanged_prod_ashn_reads_no_fleet(monkeypatch):
    _serve(monkeypatch, {NEW: _doc("newco").replace("2604.1.1", "2604.1.2")},
           {NEW: _doc("newco")})
    monkeypatch.setattr(m, "_fleet_own_identities",
                        lambda *a, **k: pytest.fail("the fleet was read"))
    assert _detect([NEW]) == ([], [])


def test_a_malformed_ashn_stands_without_the_app_list(monkeypatch):
    _serve(monkeypatch, {NEW: _doc("newco", "98c3z6d18")})
    _fleet(monkeypatch, [])
    gates, notes = _detect([NEW])
    assert gates == [_format()] and len(notes) == 1


# ── the gates and the panel ──────────────────────────────────────────────

def test_nothing_lifts_them():
    for kind in ("ashn_taken", "ashn_format"):
        assert cr.GATES[kind][1] is None and cr.GATES[kind][2] == "blocked"


def test_the_status_names_the_fix():
    assert cr.gate_status_description([_taken()]) == (
        f"Blocked - {TAKEN} (the ashn of pv-oldco-a) in pv-newco-a. Take a new ashn "
        "from the ASHN sheet (see PR comment)")
    assert cr.gate_status_description([_format()]) == (
        f"Blocked - {FORMAT} (98c3z6d18) in pv-newco-a. Copy the ashn from column A of "
        "the ASHN sheet (see PR comment)")


def test_the_panels_name_each_env_and_the_fix():
    gates = m._merge_gates((), extra=[_taken(), _format(AZURE), {"kind": "shrink", "env": ""}])
    assert m._ashn_lines(gates) == [
        "## ⛔ ASHN ALREADY IN USE", "",
        f"- `pv-newco-a` has `ashn: {ASHN}`, the ashn of `pv-oldco-a` (`{LIVE}`).", "",
        TAKEN_FIX, "",
        "## ⛔ ASHN NOT IN THE ASHN FORMAT", "",
        "- `pv-newco-a` has `ashn: 98c3z6d18`.", "",
        FORMAT_FIX, ""]


# ── process_pr ───────────────────────────────────────────────────────────

COHORT_DOC = "appspace:\n  version: 2604.1.1\n"
RENDERED = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: platform-config\n"


@pytest.fixture()
def new_env(world, monkeypatch):
    """A PR that adds pv-newco-a and touches no live app. pv-oldco-a is live."""
    sinks, _plan = world
    monkeypatch.setattr(m, "get_pr_changed_files", lambda pr_id, repo=None: ([NEW], {}))
    monkeypatch.setattr(m, "_render_new_env_diff",
                        lambda env_info, pr_sha: (RENDERED, None, 1, "2604.1.1"))
    monkeypatch.setattr(m, "_pr_commit_messages",
                        lambda *a: pytest.fail("commits read with no trailer to find"))
    _fleet(monkeypatch, [LIVE])

    def run(own):
        live = {LIVE: _doc("oldco"), COHORT: COHORT_DOC}
        _serve(monkeypatch, dict(live, **{NEW: own}), live)
        m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
        return sinks.upserts[-1], sinks.statuses[-1]
    return run


def test_a_taken_ashn_blocks_a_new_prod_env(new_env):
    body, (state, desc) = new_env(_doc("newco"))
    assert m._extract_status_token(body) == "blocked" and state == "FAILED"
    assert desc == (f"Blocked - {TAKEN} (the ashn of pv-oldco-a) in pv-newco-a. Take a new "
                    "ashn from the ASHN sheet (see PR comment)")
    assert "## ⛔ ASHN ALREADY IN USE" in body and TAKEN_FIX in body


def test_a_malformed_ashn_blocks_a_new_prod_env(new_env):
    body, (state, desc) = new_env(_doc("newco", "98c3z6d18"))
    assert m._extract_status_token(body) == "blocked" and state == "FAILED"
    assert desc.startswith(f"Blocked - {FORMAT} (98c3z6d18) in pv-newco-a.")
    assert "## ⛔ ASHN NOT IN THE ASHN FORMAT" in body


def test_a_new_prod_env_with_its_own_ashn_stays_green(new_env):
    body, (state, _desc) = new_env(_doc("newco", "1111x2222"))
    assert m._extract_status_token(body) == "clean" and state == "SUCCESSFUL"
    assert "ASHN ALREADY IN USE" not in body and "ASHN FORMAT" not in body


# ── docs ─────────────────────────────────────────────────────────────────

from test_cops2766_block3_docs import _cells, _guard_rows, _read, _slug  # noqa: E402

ANCHOR = "why-a-prod-ashn-must-be-unique-and-well-formed"


@pytest.mark.parametrize("guard", ["ASHN already in use", "ASHN not in the ASHN format"])
def test_each_new_gate_has_a_guards_row_with_no_override(guard):
    rows = [r for r in _guard_rows() if _cells(r)[0] == f"**{guard}**"]
    assert len(rows) == 1 and _cells(rows[0])[3] == "none", rows
    assert f"internals.md#{ANCHOR}" in rows[0]


def test_the_internals_section_exists_and_is_listed():
    internals = _read("docs/internals.md")
    assert ANCHOR in {_slug(l.lstrip("#")) for l in internals.splitlines() if l.startswith("#")}
    assert f"](#{ANCHOR})" in internals
