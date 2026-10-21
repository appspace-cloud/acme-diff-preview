"""In prod and AEC, a change the ASHN map publisher would refuse blocks the merge.

The PowerDNS name servers load a map of every Private Cloud ashn, built from
acme-config-prod by a job that refuses the whole map when one entry is wrong:
the name is not `<ashn>.api.cloud.appspace.com`, the instance host is missing
or is not lowercase letters, digits, dots and dashes, or one ashn points to
two instance hosts. One bad environment would stop every DNS update until it
is fixed. So the same cases block here, before the merge, but only when this
PR causes them: a problem already on `main` does not block an unrelated PR.

The entry is built from the merged values of the environment, the
`config.yaml` chain from the cloud folder down, then its own `customer.yaml`,
the same way as the job. So a `config.yaml` change is checked for every live
environment below it. Dev, QA, stage and public-cloud files are never read.
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
ASHN = "3a02x8649"
STAGE = "api.cloud.stage.appspace.com"
TIER = "gcp/prod/private-cloud/config.yaml"
AEC_TIER = "gcp/aec/private-cloud/config.yaml"
AZ_TIER = "azure/prod/private-cloud/config.yaml"
AZ_PROD = "azure/prod/config.yaml"
SPOKE = "gcp/prod/private-cloud/na1-b/monthly"
COHORT = f"{SPOKE}/config.yaml"
OTHER_COHORT = "gcp/prod/private-cloud/na1-b/weekly/config.yaml"
NEW = f"{SPOKE}/pv-newco-a/customer.yaml"
SECOND = f"{SPOKE}/pv-newco-b/customer.yaml"
LIVE = f"{SPOKE}/pv-oldco-a/customer.yaml"
BARE = f"{SPOKE}/pv-bare-a/customer.yaml"
AZURE = "azure/prod/private-cloud/na1-a/monthly/pv-newco-a/customer.yaml"
AEC = "gcp/aec/private-cloud/na2-a/monthly/pv-newco-a/customer.yaml"
MOVED = "gcp/prod/private-cloud/na1-b/weekly/pv-oldco-a/customer.yaml"

def _doc(name, ashn=ASHN, extra=""):
    return ("appspace:\n" + f"  customerName: {name}\n"
            + (f"  ashn: {ashn}\n" if ashn else "") + extra + "  version: 2604.1.1\n")


# A live env of another customer is always there, so the fleet is not empty.
WORLD = {
    LIVE: _doc("oldco", ashn="1111x2222"),
    "gcp/config.yaml": "appspace:\n  cloudShortName: \"gcp\"\n",
    TIER: ("appspace:\n  deploymentType: pv-prod\n  environmentType: private-cloud\n"
           "  ashnDomain: \"api.cloud.appspace.com\"\n"),
    AEC_TIER: ("appspace:\n  ashnDomain: \"api.cloud.appspace.com\"\n"
               "  deploymentType: pv-aec\n  environmentType: private-cloud\n"),
    "azure/config.yaml": "appspace:\n  cloudShortName: \"az\"\n",
    AZ_PROD: ("appspace:\n  infra:\n    az:\n      loadBalancers:\n        nginxFrontend:\n"
              "          subdomainType: \"cloud.appspace.com\"\n"),
    AZ_TIER: ("appspace:\n  ashnDomain: \"api.cloud.appspace.com\"\n"
              "  deploymentType: pv-prod\n  environmentType: private-cloud\n"),
    COHORT: "appspace:\n  version: 2604.1.1\n",
}

DOMAIN = cr.GATES["ashn_domain"][0]
NO_HOST = cr.GATES["ashn_no_host"][0]
HOST_FORMAT = cr.GATES["ashn_host_format"][0]
TWO_HOSTS = cr.GATES["ashn_two_hosts"][0]


@pytest.fixture(autouse=True)
def no_vertex(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)


@pytest.fixture(autouse=True)
def live_fleet(monkeypatch):
    _fleet(monkeypatch, [LIVE])


def _serve(monkeypatch, head, base=None):
    """Serve `head` at PR_SHA and `base` at BASE_SHA, both on top of WORLD. A
    None value removes the file on that side."""
    head = {k: v for k, v in dict(WORLD, **head).items() if v is not None}
    base = {k: v for k, v in dict(WORLD, **(base or {})).items() if v is not None}
    reads = []

    def fetch(path, sha, repo=None):
        reads.append(path)
        side = head if sha == H else base if sha == B else {}
        return (side[path], m.BB_OK) if path in side else (None, m.BB_NOT_FOUND)
    monkeypatch.setattr(m, "_bb_fetch_status", fetch)
    return reads


def _fleet(monkeypatch, paths):
    monkeypatch.setattr(m, "_fleet_identity_files", lambda repo=None: sorted(paths))


def _detect(changed, renames=None, path_map=None, skip=()):
    return m._detect_ashn_map_gates(changed, renames or {}, path_map or {}, H, B, skip=skip)


def _env(path):
    return path.split("/")[-2]


def _domain(path=NEW, why=STAGE, src=None):
    return {"kind": "ashn_domain", "env": _env(path), "why": why, "ashn": ASHN,
            "path": path, "src": src or path}


def _no_host(path=NEW, why="no subdomainType on Azure"):
    return {"kind": "ashn_no_host", "env": _env(path), "why": why, "ashn": ASHN, "path": path}


def _host_format(path=NEW, host="NewCo.cloud.appspace.com"):
    return {"kind": "ashn_host_format", "env": _env(path), "why": host, "ashn": ASHN,
            "path": path}


def _two_hosts(path=SECOND, host="newco-eu.cloud.appspace.com", other=NEW,
               other_host="newco.cloud.appspace.com"):
    return {"kind": "ashn_two_hosts", "env": _env(path),
            "why": f"{_env(other)} gives it {other_host}", "ashn": ASHN, "path": path,
            "host": host, "others": [{"path": other, "host": other_host}]}


# ── the entry the job builds ─────────────────────────────────────────────

@pytest.mark.parametrize("path,host", [
    (NEW, "newco.cloud.appspace.com"),
    (AEC, "newco.cloud.appspace.com"),
    (AZURE, "newco.cloud.appspace.com"),
])
def test_a_correct_new_env_is_clean(monkeypatch, path, host):
    _serve(monkeypatch, {path: _doc("newco")})
    assert _detect([path]) == ([], [])
    flat = m._merged_kcc_flat_for_env(path, H, strict=True)
    assert m._ashn_map_row(flat)["host"] == host
    assert m._ashn_map_row(flat)["name"] == f"{ASHN}.api.cloud.appspace.com"


def test_customer_subdomain_wins_over_customer_name(monkeypatch):
    _serve(monkeypatch, {NEW: _doc("newco", extra="  customerSubdomain: nc\n")})
    assert m._ashn_map_row(m._merged_kcc_flat_for_env(NEW, H, strict=True))["host"] == (
        "nc.cloud.appspace.com")


def test_an_azure_env_can_set_its_own_subdomain_type(monkeypatch):
    own = ("  infra:\n    az:\n      loadBalancers:\n        nginxFrontend:\n"
           "          subdomainType: \"appspace.com\"\n")
    _serve(monkeypatch, {AZURE: _doc("newco", extra=own)})
    assert m._ashn_map_row(m._merged_kcc_flat_for_env(AZURE, H, strict=True))["host"] == (
        "newco.appspace.com")


def test_no_ashn_and_no_private_cloud_have_no_entry():
    assert m._ashn_map_row({"appspace.environmentType": "private-cloud"}) is None
    assert m._ashn_map_row({"appspace.environmentType": "public-cloud",
                            "appspace.ashn": ASHN}) is None
    assert m._ashn_map_row({"appspace.environmentType": "private-cloud",
                            "appspace.ashn": False}) is None


# ── the gates ────────────────────────────────────────────────────────────

def test_a_new_prod_env_with_another_ashn_domain_is_a_gate(monkeypatch):
    _serve(monkeypatch, {NEW: _doc("newco", extra=f"  ashnDomain: {STAGE}\n")})
    assert _detect([NEW]) == ([_domain()], [])


def test_a_config_change_of_the_ashn_domain_gates_every_live_env_below(monkeypatch):
    cohort = f"appspace:\n  version: 2604.1.1\n  ashnDomain: {STAGE}\n"
    live = {LIVE: _doc("oldco"), BARE: _doc("bare", ashn="")}
    _serve(monkeypatch, dict(live, **{COHORT: cohort}), live)
    _fleet(monkeypatch, [LIVE, BARE])
    gates, notes = _detect([COHORT], path_map={LIVE: ["pv-oldco-a-ms"],
                                               BARE: ["pv-bare-a-ms"]})
    assert gates == [_domain(LIVE, src=COHORT)] and notes == []


def test_no_ashn_domain_at_all_is_a_gate(monkeypatch):
    tier = WORLD[TIER].replace("  ashnDomain: \"api.cloud.appspace.com\"\n", "")
    _serve(monkeypatch, {NEW: _doc("newco"), TIER: tier})
    gates, _notes = _detect([NEW])
    assert gates == [dict(_domain(why="no ashnDomain"), src=None)]


def test_a_new_azure_env_with_no_subdomain_type_is_a_gate(monkeypatch):
    _serve(monkeypatch, {AZURE: _doc("newco"), AZ_PROD: "appspace: {}\n"})
    assert _detect([AZURE]) == ([_no_host(AZURE)], [])


def test_an_empty_customer_subdomain_is_a_gate(monkeypatch):
    # The job reads "" as the subdomain, not as unset, so it finds no host.
    _serve(monkeypatch, {NEW: _doc("newco", extra="  customerSubdomain: \"\"\n")})
    assert _detect([NEW]) == ([_no_host(why="no customerSubdomain or customerName")], [])


def test_a_type_the_map_does_not_know_is_a_gate(monkeypatch):
    _serve(monkeypatch, {NEW: _doc("newco", extra="  deploymentType: pv-sandbox\n")})
    assert _detect([NEW]) == (
        [_no_host(why="gcp-pv-sandbox is not a type the map knows")], [])


@pytest.mark.parametrize("sub,host", [
    ("NewCo", "NewCo.cloud.appspace.com"),
    ("new_co", "new_co.cloud.appspace.com"),
    ("\"new co\"", "new co.cloud.appspace.com"),
])
def test_a_host_that_is_not_lowercase_letters_digits_dots_and_dashes_is_a_gate(
        monkeypatch, sub, host):
    _serve(monkeypatch, {NEW: _doc("newco", extra=f"  customerSubdomain: {sub}\n")})
    assert _detect([NEW]) == ([_host_format(host=host)], [])


def test_one_customer_with_one_ashn_on_two_hosts_is_a_gate(monkeypatch):
    live = {NEW: _doc("newco")}
    _serve(monkeypatch, dict(live, **{SECOND: _doc(
        "newco", extra="  customerSubdomain: newco-eu\n")}), live)
    _fleet(monkeypatch, [NEW])
    assert _detect([SECOND]) == ([_two_hosts()], [])


def test_one_customer_with_one_ashn_on_one_host_is_clean(monkeypatch):
    live = {NEW: _doc("newco")}
    _serve(monkeypatch, dict(live, **{SECOND: _doc("newco")}), live)
    _fleet(monkeypatch, [NEW])
    assert _detect([SECOND]) == ([], [])


def test_a_live_env_that_changes_its_host_gives_its_ashn_two_hosts(monkeypatch):
    live = {NEW: _doc("newco"), SECOND: _doc("newco")}
    _serve(monkeypatch, dict(live, **{SECOND: _doc(
        "newco", extra="  customerSubdomain: newco-eu\n")}), live)
    _fleet(monkeypatch, [NEW, SECOND])
    assert _detect([SECOND], path_map={NEW: ["pv-newco-a-ms"], SECOND: ["pv-newco-b-ms"]}) == (
        [_two_hosts()], [])


def test_an_env_another_gate_already_names_gets_no_two_hosts_gate(monkeypatch):
    live = {NEW: _doc("newco")}
    _serve(monkeypatch, dict(live, **{SECOND: _doc(
        "newco", extra="  customerSubdomain: newco-eu\n")}), live)
    _fleet(monkeypatch, [NEW])
    assert _detect([SECOND], skip={SECOND}) == ([], [])


def test_two_hosts_without_the_app_list_is_a_note(monkeypatch):
    _serve(monkeypatch, {SECOND: _doc("newco")})
    _fleet(monkeypatch, [])
    gates, notes = _detect([SECOND])
    assert gates == [] and len(notes) == 1 and "app list" in notes[0]


def test_a_move_into_a_cohort_with_another_ashn_domain_is_a_gate(monkeypatch):
    other = f"appspace:\n  version: 2604.1.1\n  ashnDomain: {STAGE}\n"
    _serve(monkeypatch, {LIVE: None, MOVED: _doc("oldco"), OTHER_COHORT: other},
           {LIVE: _doc("oldco"), OTHER_COHORT: other})
    _fleet(monkeypatch, [LIVE])
    gates, _notes = _detect([LIVE, MOVED], renames={LIVE: MOVED},
                            path_map={LIVE: ["pv-oldco-a-ms"]})
    assert gates == [_domain(MOVED, src=OTHER_COHORT)]


# ── only what this PR causes ─────────────────────────────────────────────

def test_a_problem_already_on_main_does_not_block_a_pr(monkeypatch):
    bad = _doc("oldco", extra=f"  ashnDomain: {STAGE}\n")
    _serve(monkeypatch, {LIVE: bad.replace("oldco", "oldco2")}, {LIVE: bad})
    _fleet(monkeypatch, [LIVE])
    assert _detect([LIVE], path_map={LIVE: ["pv-oldco-a-ms"]}) == ([], [])


def test_a_change_that_does_not_touch_the_map_keys_reads_nothing_more(monkeypatch):
    live = {LIVE: _doc("oldco")}
    reads = _serve(monkeypatch, {LIVE: _doc("oldco").replace("2604.1.1", "2604.1.2"),
                                 COHORT: "appspace:\n  version: 2604.1.2\n"}, live)
    monkeypatch.setattr(m, "_merged_kcc_flat_for_env",
                        lambda *a, **k: pytest.fail("merged values were read"))
    monkeypatch.setattr(m, "_fleet_own_identities",
                        lambda *a, **k: pytest.fail("the fleet was read"))
    assert _detect([LIVE, COHORT], path_map={LIVE: ["pv-oldco-a-ms"]}) == ([], [])
    assert set(reads) == {LIVE, COHORT}


def test_a_teardown_is_skipped(monkeypatch):
    _serve(monkeypatch, {LIVE: None}, {LIVE: _doc("oldco", extra=f"  ashnDomain: {STAGE}\n")})
    _fleet(monkeypatch, [LIVE])
    assert _detect([LIVE], path_map={LIVE: ["pv-oldco-a-ms"]}) == ([], [])


@pytest.mark.parametrize("path", [
    "gcp/qa/private-cloud/ap1/custom/pv-qa98-a/customer.yaml",
    "gcp/dev/private-cloud/ap1/custom/pv-dev-x-a/customer.yaml",
    "gcp/stage/private-cloud/na1-a/pv-stage-x-a/customer.yaml",
    "gcp/prod/public-cloud/cl-prod-b/config.yaml",
    "gcp/dev/config.yaml",
    f"{SPOKE}/pv-newco-a/cicd-versions.yaml",
])
def test_dev_qa_stage_public_and_other_files_are_never_read(monkeypatch, path):
    monkeypatch.setattr(m, "_bb_fetch_status", lambda *a, **k: pytest.fail("read"))
    assert _detect([path]) == ([], [])


def test_a_private_cloud_without_an_ashn_is_skipped(monkeypatch):
    _serve(monkeypatch, {BARE: _doc("bare", ashn="", extra=f"  ashnDomain: {STAGE}\n")})
    assert _detect([BARE]) == ([], [])


# ── the gates and the panel ──────────────────────────────────────────────

def test_nothing_lifts_them():
    for kind in ("ashn_domain", "ashn_no_host", "ashn_host_format", "ashn_two_hosts"):
        assert cr.GATES[kind][1] is None and cr.GATES[kind][2] == "blocked"


def test_the_status_names_the_fix():
    assert cr.gate_status_description([_domain()]) == (
        f"Blocked - {DOMAIN} ({STAGE}) in pv-newco-a. Set ashnDomain to "
        "api.cloud.appspace.com (see PR comment)")
    assert cr.gate_status_description([_no_host()]) == (
        f"Blocked - {NO_HOST} (no subdomainType on Azure) in pv-newco-a. Set customerName, "
        "and subdomainType on Azure (see PR comment)")
    assert cr.gate_status_description([_host_format()]) == (
        f"Blocked - {HOST_FORMAT} (NewCo.cloud.appspace.com) in pv-newco-a. Use lowercase "
        "in customerSubdomain or customerName (see PR comment)")
    assert cr.gate_status_description([_two_hosts()]) == (
        f"Blocked - {TWO_HOSTS} (pv-newco-a gives it newco.cloud.appspace.com) in "
        "pv-newco-b. Keep one instance host per ashn (see PR comment)")


def test_the_panels_name_each_env_and_the_fix():
    gates = m._merge_gates((), extra=[_domain(src=COHORT), _no_host(AZURE), _host_format(),
                                      _two_hosts()])
    lines = m._ashn_lines(gates)
    assert lines[:3] == ["## ⛔ ASHN NAME NOT ON api.cloud.appspace.com", "",
                         f"- `pv-newco-a` has `ashn: {ASHN}` on `{STAGE}`, set in "
                         f"`{COHORT}`."]
    assert "## ⛔ NO INSTANCE HOST FOR THE ASHN MAP" in lines
    assert (f"- `pv-newco-a` (`ashn: {ASHN}`): no subdomainType on Azure.") in lines
    assert "## ⛔ INSTANCE HOST NOT IN LOWERCASE" in lines
    assert f"- `pv-newco-a` (`ashn: {ASHN}`) gives `NewCo.cloud.appspace.com`." in lines
    assert "## ⛔ ONE ASHN, TWO INSTANCE HOSTS" in lines
    assert (f"- `pv-newco-b` gives `ashn: {ASHN}` the host `newco-eu.cloud.appspace.com`, "
            f"and `pv-newco-a` (`{NEW}`) gives it `newco.cloud.appspace.com`.") in lines


def test_the_existing_panels_come_first_and_do_not_change():
    taken = {"kind": "ashn_taken", "env": "pv-newco-a", "why": "the ashn of pv-oldco-a",
             "ashn": ASHN, "path": NEW, "others": [LIVE]}
    lines = m._ashn_lines(m._merge_gates((), extra=[_domain(), taken]))
    assert lines[0] == "## ⛔ ASHN ALREADY IN USE"
    assert lines.index("## ⛔ ASHN NAME NOT ON api.cloud.appspace.com") > 0


# ── process_pr ───────────────────────────────────────────────────────────

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
        live = {LIVE: _doc("oldco", ashn="1111x2222")}
        _serve(monkeypatch, dict(live, **{NEW: own}), live)
        m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
        return sinks.upserts[-1], sinks.statuses[-1]
    return run


def test_another_ashn_domain_blocks_a_new_prod_env(new_env):
    body, (state, desc) = new_env(_doc("newco", extra=f"  ashnDomain: {STAGE}\n"))
    assert m._extract_status_token(body) == "blocked" and state == "FAILED"
    assert desc.startswith(f"Blocked - {DOMAIN} ({STAGE}) in pv-newco-a.")
    assert "## ⛔ ASHN NAME NOT ON api.cloud.appspace.com" in body


def test_an_uppercase_host_blocks_a_new_prod_env(new_env):
    body, (state, desc) = new_env(_doc("newco", extra="  customerSubdomain: NewCo\n"))
    assert m._extract_status_token(body) == "blocked" and state == "FAILED"
    assert "## ⛔ INSTANCE HOST NOT IN LOWERCASE" in body


def test_a_correct_new_prod_env_stays_green(new_env):
    body, (state, _desc) = new_env(_doc("newco"))
    assert m._extract_status_token(body) == "clean" and state == "SUCCESSFUL"
    assert "ASHN NAME" not in body and "INSTANCE HOST" not in body


# ── docs ─────────────────────────────────────────────────────────────────

from test_cops2766_block3_docs import _cells, _guard_rows, _read, _slug  # noqa: E402

ANCHOR = "why-a-prod-ashn-must-fit-the-ashn-map"


@pytest.mark.parametrize("guard", ["ASHN name not on api.cloud.appspace.com",
                                   "No instance host for the ASHN map",
                                   "Instance host not in lowercase",
                                   "One ashn, two instance hosts"])
def test_each_new_gate_has_a_guards_row_with_no_override(guard):
    rows = [r for r in _guard_rows() if _cells(r)[0] == f"**{guard}**"]
    assert len(rows) == 1 and _cells(rows[0])[3] == "none", rows
    assert f"internals.md#{ANCHOR}" in rows[0]


def test_the_internals_section_exists_and_is_listed():
    internals = _read("docs/internals.md")
    assert ANCHOR in {_slug(l.lstrip("#")) for l in internals.splitlines() if l.startswith("#")}
    assert f"](#{ANCHOR})" in internals
