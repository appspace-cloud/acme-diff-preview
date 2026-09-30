"""COPS-2766 block 3, item 3 (row 7, C03): a clone with the ashn of another env.

The ashn names an environment in Customers and PDNS. acme-config-prod #4042
made four AEC clones with the ashn of the production env they copy, so each
clone and its original look like one environment there. A changed clone
customer.yaml whose own `appspace.ashn` is new or changed is compared with the
own customer.yaml of every live env at base, and with the other clones of the
PR. An env with another customerName is a hit: the `ashn_copy` gate, and
nothing lifts it. A fleet file that cannot be read is not checked, and a note
says so.
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
SPOKE = "gcp/aec/private-cloud/na1-b"
COHORT = f"{SPOKE}/monthly/config.yaml"
X = f"{SPOKE}/monthly/pv-x--aec1-a/customer.yaml"
Y = f"{SPOKE}/monthly/pv-y--aec1-a/customer.yaml"
OLD_X = f"{SPOKE}/weekly/pv-x--aec1-a/customer.yaml"
PROD = "gcp/prod/private-cloud/na1-b/monthly/pv-x-a/customer.yaml"
OTHER = "gcp/prod/private-cloud/na1-b/monthly/pv-o-a/customer.yaml"
ENV = "pv-x--aec1-a"
ASHN = "754axaae6"
TEXT = cr.GATES["ashn_copy"][0]
SK = (m.BB_REPO, 991)
FIX = ("The ashn names the environment in Customers and PDNS, so the clone and the "
       "other environment look like one environment there. Give the clone its own ashn "
       "(runbook https://appspace.atlassian.net/wiki/spaces/cops/pages/66093626 step 2: "
       "change ashn, suffix, customerName and instanceName).")
UNAVAILABLE = ("⚠️ ashn check unavailable: the ArgoCD app list is not loaded, so the "
               "clone ashn is not compared with the live environments.")


def _skipped(n):
    return (f"⚠️ ashn check skipped {n} environment(s) that could not be read, so a "
            "copied ashn there is not ruled out.")


def _doc(name="x--aec1", ashn=ASHN, zero="true"):
    return ("appspace:\n" + (f"  ashn: {ashn}\n" if ashn else "")
            + f"  customerName: {name}\n" + (f"  zeroPods: {zero}\n" if zero else "")
            + "  version: 2603.2.19\n")


@pytest.fixture(autouse=True)
def no_vertex(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)


def _serve(monkeypatch, head, base=None, error=()):
    """head/base: {path: text}. A path in `error` is a Bitbucket failure."""
    base = base or {}

    def fetch(path, sha, repo=None):
        if path in error:
            return None, m.BB_ERROR
        side = head if sha == H else base if sha == B else {}
        return (side[path], m.BB_OK) if path in side else (None, m.BB_NOT_FOUND)
    monkeypatch.setattr(m, "_bb_fetch_status", fetch)


def _fleet(monkeypatch, paths):
    monkeypatch.setattr(m, "_fleet_identity_files", lambda repo=None: sorted(paths))


def _no_fleet(monkeypatch):
    monkeypatch.setattr(m, "_fleet_own_identities",
                        lambda *a, **k: pytest.fail("the fleet was read"))


def _detect(changed, renames=None, base_sha=B):
    return m._detect_copied_clone_ashn(changed, renames or {}, H, base_sha)


def _env(path):
    return path.split("/")[-2]


def _gate(path=X, others=(PROD,), ashn=ASHN):
    return {"kind": "ashn_copy", "env": _env(path),
            "why": "the ashn of " + ", ".join(_env(o) for o in others),
            "ashn": ashn, "path": path, "others": list(others)}


# ── acme-config-prod #4042 (5fad0057f) ───────────────────────────────────

AEC = "gcp/aec/private-cloud/na2-a"
PV = "gcp/prod/private-cloud"
PR_4042 = [   # clone, its customerName, its ashn, the live env with that ashn, its name
    (f"{AEC}/monthly/pv-heb--aec1-b", "heb--aec1", "14efxa88e",
     f"{PV}/na3-a/monthly/pv-heb-a", "heb"),
    (f"{AEC}/monthly/pv-universal--aec1-b", "universal--aec1", "892cxbadb",
     f"{PV}/na1-b/monthly/pv-universalhollywood-c", "universalhollywood"),
    (f"{AEC}/weekly/pv-blackrock--aec1-a", "blackrock--aec1", "788bx265d",
     f"{PV}/na1-b/weekly/pv-blackrock-c", "blackrock"),
    (f"{AEC}/weekly/pv-pfizer--aec1-b", "pfizer--aec1", "5180x7f8e",
     f"{PV}/na2-a/weekly/pv-pfizer-b", "pfizer"),
]
FORD = f"{AEC}/monthly/pv-ford--aec1-b/customer.yaml"     # its own ashn


def test_pr_4042_gives_one_gate_per_copied_clone(monkeypatch):
    head = {f"{c}/customer.yaml": _doc(n, a) for c, n, a, _o, _on in PR_4042}
    head[FORD] = _doc("ford--aec1", "8ac7x4c59")
    base = {f"{o}/customer.yaml": _doc(on, a, None) for _c, _n, a, o, on in PR_4042}
    base[OTHER] = _doc("o", "1111x2222", None)
    _serve(monkeypatch, head, base)
    _fleet(monkeypatch, list(base))
    assert _detect(sorted(head)) == ([
        _gate(f"{c}/customer.yaml", [f"{o}/customer.yaml"], a)
        for c, _n, a, o, _on in PR_4042], [])


# ── the rule ─────────────────────────────────────────────────────────────

def test_the_same_customer_name_is_no_gate(monkeypatch):
    """Clones b and c of one customer are one customer in Customers."""
    live_b = f"{SPOKE}/monthly/pv-x--aec1-b/customer.yaml"
    _serve(monkeypatch, {X: _doc()}, {live_b: _doc(), PROD: _doc("x", "1234x5678", None)})
    _fleet(monkeypatch, [live_b, PROD])
    assert _detect([X]) == ([], [])


def test_a_live_clone_that_takes_a_copied_ashn_is_a_gate(monkeypatch):
    _serve(monkeypatch, {X: _doc()}, {X: _doc(ashn="9999x0000"), PROD: _doc("x", zero=None)})
    _fleet(monkeypatch, [X, PROD])
    assert _detect([X]) == ([_gate()], [])


@pytest.mark.parametrize("head,base,changed,renames", [
    ({X: _doc().replace("19", "20")}, {X: _doc()}, [X], {}),        # a version bump
    ({X: _doc(ashn=None)}, {X: _doc()}, [X], {}),                   # the ashn is removed
    ({X: _doc(ashn=None)}, {}, [X], {}),                            # no own ashn
    ({}, {X: _doc()}, [X], {}),                                     # the clone is removed
    ({X: "appspace: [\n"}, {}, [X], {}),                            # the render reports it
    ({X: _doc("x--aec2")}, {OLD_X: _doc()}, [OLD_X, X], {OLD_X: X}),   # a move, a new name
])
def test_an_unchanged_or_missing_ashn_reads_no_fleet(monkeypatch, head, base, changed, renames):
    _serve(monkeypatch, head, base)
    _no_fleet(monkeypatch)
    assert _detect(changed, renames) == ([], [])


def test_a_move_the_augmenter_did_not_pair_is_not_a_copy(monkeypatch):
    """The critic: the old path is still live at base, with the same ashn and
    the old customerName. It is gone at the head, so it is not a copy."""
    _serve(monkeypatch, {X: _doc("x--aec2")}, {OLD_X: _doc()})
    _fleet(monkeypatch, [OLD_X])
    assert _detect([OLD_X, X]) == ([], [])


@pytest.mark.parametrize("prod_head,gates", [
    (_doc("x", zero=None).replace("19", "20"), [_gate()]),       # a bump keeps the ashn
    (_doc("x", "1234x5678", None), []),                          # the PR gives it another
])
def test_a_live_env_this_pr_changes_counts_as_it_is_at_the_head(monkeypatch, prod_head, gates):
    _serve(monkeypatch, {X: _doc(), PROD: prod_head}, {PROD: _doc("x", zero=None)})
    _fleet(monkeypatch, [PROD])
    assert _detect([PROD, X]) == (gates, [])


def test_a_live_env_this_pr_moves_counts_at_its_new_path(monkeypatch):
    new_prod = PROD.replace("/monthly/", "/weekly/")
    _serve(monkeypatch, {X: _doc(), new_prod: _doc("x", zero=None)},
           {PROD: _doc("x", zero=None)})
    _fleet(monkeypatch, [PROD])
    assert _detect([PROD, new_prod, X], {PROD: new_prod}) == ([_gate(others=[new_prod])], [])


@pytest.mark.parametrize("y_name,gates", [
    ("y--aec1", [_gate(others=[Y]), _gate(Y, [X])]),
    ("x--aec1", []),
])
def test_two_clones_in_the_pr_with_one_ashn(monkeypatch, y_name, gates):
    _serve(monkeypatch, {X: _doc(), Y: _doc(y_name)}, {OTHER: _doc("o", "1111x2222", None)})
    _fleet(monkeypatch, [OTHER])
    assert _detect([X, Y]) == (gates, [])


def test_unreadable_fleet_files_give_the_note_and_no_gate(monkeypatch):
    """BB_ERROR and YAML that cannot be parsed: that file is not checked. A
    file absent at base is not a live env any more."""
    gone = "gcp/prod/private-cloud/na1-b/monthly/pv-gone-a/customer.yaml"
    _serve(monkeypatch, {X: _doc()}, {OTHER: "appspace: [\n"}, error=(PROD,))
    _fleet(monkeypatch, [PROD, OTHER, gone])
    assert _detect([X]) == ([], [_skipped(2)])


def test_an_empty_fleet_gives_the_unavailable_note(monkeypatch):
    """The clones of the PR are still compared with each other."""
    _serve(monkeypatch, {X: _doc(), Y: _doc("y--aec1")})
    _fleet(monkeypatch, [])
    assert _detect([X]) == ([], [UNAVAILABLE])
    assert _detect([X, Y]) == ([_gate(others=[Y]), _gate(Y, [X])], [UNAVAILABLE])


@pytest.mark.parametrize("error", [X, OLD_X])
def test_a_failed_read_of_a_pr_file_retries(monkeypatch, error):
    """The clone at the head, or its old path at base."""
    _serve(monkeypatch, {X: _doc()}, {OLD_X: _doc(ashn="9999x0000")}, error=(error,))
    _no_fleet(monkeypatch)
    with pytest.raises(m.ValueFileUnreadable):
        _detect([X], {OLD_X: X})


def test_a_failed_read_of_a_changed_live_env_retries(monkeypatch):
    """Not read at base, so it is not in the fleet, but the PR changes it."""
    _serve(monkeypatch, {X: _doc()}, error=(PROD,))
    _fleet(monkeypatch, [PROD])
    with pytest.raises(m.ValueFileUnreadable):
        _detect([PROD, X])


@pytest.mark.parametrize("path", [
    PROD,                                                            # not a clone
    "gcp/dev/private-cloud/ap1/sandbox/pv-x--sbx1-a/customer.yaml",  # a sandbox
    f"{SPOKE}/monthly/pv-x--aec1-a/cicd-versions.yaml",              # not the identity
    COHORT,
])
def test_other_files_are_never_read(monkeypatch, path):
    monkeypatch.setattr(m, "_bb_fetch_status", lambda *a, **k: pytest.fail("read"))
    assert _detect([path]) == ([], [])


def test_with_no_base_nothing_is_checked(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_status", lambda *a, **k: pytest.fail("read"))
    assert _detect([X], base_sha="") == ([], [])


# ── the fleet read ───────────────────────────────────────────────────────

def test_the_fleet_is_read_once_for_the_last_base_of_each_repo(monkeypatch):
    reads = []

    def fetch(path, sha, repo=None):
        reads.append((path, sha, repo))
        return _doc("x", zero=None), m.BB_OK
    monkeypatch.setattr(m, "_bb_fetch_status", fetch)
    _fleet(monkeypatch, [PROD])
    want = ({PROD: ("x", "", ASHN)}, [])
    assert m._fleet_own_identities("r1", "b1") == want
    m._vf_cache.clear()
    assert m._fleet_own_identities("r1", "b1") == want and len(reads) == 1
    assert m._fleet_own_identities("r2", "b1") == want and len(reads) == 2
    assert m._fleet_own_identities("r1", "b2") == want and len(reads) == 3
    assert m._fleet_own_cache == {"r1": ("b2", want), "r2": ("b1", want)}


@pytest.mark.parametrize("paths,error", [([], ()), ([PROD], (PROD,))])
def test_a_fleet_read_that_is_not_complete_is_not_kept(monkeypatch, paths, error):
    _serve(monkeypatch, {}, {PROD: _doc("x", zero=None)}, error=error)
    _fleet(monkeypatch, paths)
    m._fleet_own_identities(None, B)
    assert m._fleet_own_cache == {}


def test_the_fleet_keeps_only_what_each_file_writes_itself(monkeypatch):
    """A date or an unknown tag is text, like ArgoCD's Go YAML reads it."""
    _serve(monkeypatch, {}, {PROD: "appspace:\n  ashn: 2026-02-30\n  suffix: b\n",
                             OTHER: "- a list\n"})
    _fleet(monkeypatch, [PROD, OTHER])
    assert m._fleet_own_identities(None, B) == (
        {PROD: ("", "b", "2026-02-30"), OTHER: ("", "", "")}, [])


# ── the gate and the panel ───────────────────────────────────────────────

def test_nothing_lifts_the_gate(monkeypatch):
    monkeypatch.setattr(m, "_pr_commit_messages", lambda *a: [
        f"Confirm-Clone-Sanitized: {ENV}\nConfirm-Rename: pv-x-a -> {ENV}",
        f"Confirm-Teardown: {ENV}\nConfirm-IP-Release: {ENV}"])
    gates = m._merge_gates((), extra=[_gate(), {"kind": "ip", "env": ENV}])
    assert cr.gate_trailer(gates[0]) == ""
    assert [g["lifted"] for g in m._lift_gates(gates, "r", 7, B, H)] == [False, True]
    assert cr.gate_token(gates) == "blocked"
    assert cr.gate_status_description(gates) == (
        f"Blocked - {TEXT} (the ashn of pv-x-a) in {ENV}. Give the clone its own ashn "
        "(see PR comment)")


def test_the_panel_names_each_clone_and_the_fix():
    gates = m._merge_gates((), extra=[
        _gate(), _gate(Y, [X, PROD], "1234x5678"), {"kind": "shrink", "env": ""}])
    assert m._ashn_copy_lines(gates, [_skipped(3)]) == [
        "## ⛔ CLONE ASHN ALREADY IN USE", "",
        f"- `{ENV}` has `ashn: {ASHN}`, the ashn of `pv-x-a` (`{PROD}`).",
        f"- `pv-y--aec1-a` has `ashn: 1234x5678`, the ashn of `{ENV}` (`{X}`), "
        f"`pv-x-a` (`{PROD}`).", "",
        FIX, "", _skipped(3), ""]


def test_the_panel_of_notes_only():
    assert m._ashn_copy_lines([{"kind": "shrink", "env": ""}], [UNAVAILABLE]) == [
        UNAVAILABLE, ""]
    assert m._ashn_copy_lines(None, []) == []


# ── process_pr ───────────────────────────────────────────────────────────

COHORT_DOC = "appspace:\n  version: 2603.2.19\n"
RENDERED = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: platform-config\n"
DESC = (f"Blocked - {TEXT} (the ashn of pv-x-a) in {ENV}. Give the clone its own ashn "
        "(see PR comment)")
LINE = f"- ⛔ **{TEXT} (the ashn of pv-x-a)** in `{ENV}`"
HDR = "CLONE ASHN ALREADY IN USE"


@pytest.fixture()
def new_clone(world, monkeypatch):
    """A PR that adds pv-x--aec1-a and touches no live app. pv-x-a is live."""
    sinks, _plan = world
    monkeypatch.setattr(m, "get_pr_changed_files", lambda pr_id, repo=None: ([X], {}))
    monkeypatch.setattr(m, "_render_new_env_diff",
                        lambda env_info, pr_sha: (RENDERED, None, 1, "2603.2.19"))
    monkeypatch.setattr(m, "_pr_commit_messages",
                        lambda *a: pytest.fail("commits read with no trailer to find"))
    _fleet(monkeypatch, [PROD])

    def run(own=_doc(), messages=None, error=()):
        live = {PROD: _doc("x", zero=None), COHORT: COHORT_DOC}
        _serve(monkeypatch, dict(live, **{X: own}), live, error)
        if messages is not None:
            monkeypatch.setattr(m, "_pr_commit_messages", lambda *a: list(messages))
        m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
        return sinks.upserts[-1], sinks.statuses[-1]
    return run


def test_a_copied_ashn_blocks_the_new_env_path(new_clone):
    body, (state, desc) = new_clone()
    assert m._extract_status_token(body) == "blocked"
    assert state == "FAILED" and desc == DESC
    assert LINE in body and "to merge anyway" not in body
    assert f"- `{ENV}` has `ashn: {ASHN}`, the ashn of `pv-x-a` (`{PROD}`)." in body
    assert body.index(cr.MERGE_SUMMARY_HDR) < body.index(HDR) \
        < body.index("New Environment(s) Detected")
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA)


def test_a_confirm_line_does_not_lift_it(new_clone):
    """The trailer lifts the clone wake gate of an awake clone, not this one."""
    body, (state, desc) = new_clone(_doc(zero="false"), [
        f"Confirm-Clone-Sanitized: {ENV}", f"Confirm-Rename: pv-x-a -> {ENV}"])
    assert m._extract_status_token(body) == "blocked"
    assert state == "FAILED" and desc == DESC
    assert f"☑️ **Confirmed in a commit:** `Confirm-Clone-Sanitized: {ENV}`" in body
    assert LINE in body


def test_a_clone_with_its_own_ashn_is_clean(new_clone):
    body, (state, _desc) = new_clone(_doc(ashn="1234x5678"))
    assert m._extract_status_token(body) == "clean" and state == "SUCCESSFUL"
    assert HDR not in body and "ashn check" not in body


def test_an_unreadable_fleet_file_shows_the_note_and_stays_green(new_clone):
    body, (state, _desc) = new_clone(error=(PROD,))
    assert m._extract_status_token(body) == "clean" and state == "SUCCESSFUL"
    assert _skipped(1) in body and HDR not in body


def test_the_token_guard_speaks_first(new_clone, monkeypatch):
    """A clone missing --aec1 in its customerName: the token block, no fleet read."""
    _no_fleet(monkeypatch)
    body, (state, desc) = new_clone(_doc("x"))
    assert m._extract_status_token(body) == "blocked" and state == "FAILED"
    assert desc.startswith(f"BLOCKED: {ENV} needs --aec1 in customerName")
    assert HDR not in body and cr.MERGE_SUMMARY_HDR not in body


def test_a_copied_ashn_blocks_the_diff_path(world, monkeypatch):
    """A live clone that takes the ashn of pv-x-a."""
    sinks, _plan = world
    app = f"{ENV}-ms"
    monkeypatch.setitem(m._app_chart_map, app, "appspace-ms")
    monkeypatch.setitem(m._app_chart_revision_map, app, "2603.2.17")
    monkeypatch.setattr(m, "get_pr_changed_files", lambda pr_id, repo=None: ([X], {}))
    _fleet(monkeypatch, [X, PROD])
    live = {PROD: _doc("x", zero=None), COHORT: COHORT_DOC}
    _serve(monkeypatch, dict(live, **{X: _doc()}), dict(live, **{X: _doc(ashn="9999x0000")}))
    m.process_pr(_mk_pr(), {X: [app], COHORT: [app]}, base_sha=BASE_SHA)
    body, (state, desc) = sinks.upserts[-1], sinks.statuses[-1]
    assert m._extract_status_token(body) == "blocked"
    assert state == "FAILED" and desc == DESC
    assert LINE in body and body.index(cr.MERGE_SUMMARY_HDR) < body.index(HDR)
