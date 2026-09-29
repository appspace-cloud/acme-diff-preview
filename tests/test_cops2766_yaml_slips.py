"""COPS-2766 block 4, item 2: a YAML slip fails the build.

Three slips change config, and no diff line shows them clearly:
  - a duplicate key: only the last copy is kept, the first is dropped with no
    error (acme-config-prod #3583, COPR-31148: a login whitelist lost for 53 h);
  - a bare `key:`: it is null, so Helm deletes that chart default;
  - a `microservices.definitions` that is not a non-empty map: it deletes every
    image name the chart ships (COPR-31637). 2.121.0 missed it after a `---`
    and for `[]`, `""` or `0`.

Only the first YAML document counts, as Helm and ArgoCD read it. A duplicate or
a bare key counts only when the PR adds it, so the old slips on main stay green.
The wipe counts at head alone, as before. Without a merge preview the base is
main, not the merge base, so only the wipe is checked.
"""
import os
import subprocess
import sys

import pytest
import yaml
from hypothesis import given, settings, strategies as st

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import diff_preview as m  # noqa: E402
import logsink  # noqa: E402
import yaml_hygiene as yh  # noqa: E402

from test_coverage_orchestration import (  # noqa: E402,F401
    world, _mk_pr, PATH_MAP, BASE_SHA, PR_SHA, IDENTITY, IDENTITY_YAML, ANCILLARY)

REAL_FIX_STUCK = m.fix_stuck_inprogress     # the world fixture stubs it
SK = ("acme-config-dev", 991)
MERGED = "feed0001aabb"
ERR = object()
F = "gcp/prod/private-cloud/na1-a/monthly/pv-x-a/customer.yaml"


def _keys(s, kind):
    return dict(s[kind])


# ── (a) the pure walk ────────────────────────────────────────────────────

def test_a_top_level_duplicate_is_found_with_both_lines():
    s = yh.slips("a: 1\nb: 2\na: 3\n")
    assert _keys(s, "dup") == {"a": 1} and s["lines"][("dup", "a")] == [(3, 1)]


def test_a_nested_duplicate_has_a_dotted_path():
    s = yh.slips("appspace:\n  zeroPods: false\n  x: 1\n  zeroPods: true\n")
    assert _keys(s, "dup") == {"appspace.zeroPods": 1}
    assert s["lines"][("dup", "appspace.zeroPods")] == [(4, 2)]


def test_a_duplicate_inside_list_items_uses_brackets():
    s = yh.slips("l:\n  - n: 1\n    n: 2\n  - n: 3\n")
    assert _keys(s, "dup") == {"l[].n": 1}


def test_a_third_copy_counts_twice():
    s = yh.slips("a: 1\na: 2\na: 3\n")
    assert _keys(s, "dup") == {"a": 2} and s["lines"][("dup", "a")] == [(2, 1), (3, 1)]


def test_merge_keys_are_not_duplicates():
    s = yh.slips("base: &b\n  x: 1\no:\n  <<: *b\n  <<: *b\n  x: 2\n")
    assert _keys(s, "dup") == {} and _keys(s, "null") == {}


def test_an_alias_is_walked_once_and_does_not_loop():
    s = yh.slips("a: &x\n  k: 1\n  k: 2\nb: *x\nc: &y [*y]\n")
    assert _keys(s, "dup") == {"a.k": 1}


def test_a_bare_key_is_null_but_an_explicit_null_is_not():
    s = yh.slips("appspace:\n  probes:\n  a: null\n  b: ~\n  c: \"\"\n  d: ''\n")
    assert _keys(s, "null") == {"appspace.probes": 1}
    assert s["lines"][("null", "appspace.probes")] == [(2, None)]


def test_a_slip_only_in_the_second_document_is_not_read():
    s = yh.slips("a: 1\n---\na: 1\na: 2\nb:\n")
    assert _keys(s, "dup") == {} and _keys(s, "null") == {}


def test_a_leading_document_marker_still_reads_the_first_document():
    s = yh.slips("---\nappspace:\n  autosync: false\n  autosync: true\n")
    assert _keys(s, "dup") == {"appspace.autosync": 1}


def test_yaml_that_does_not_parse_gives_none():
    assert yh.slips("a: [1\n") is None


@pytest.mark.parametrize("body", ["", None, "# only a comment\n"])
def test_an_empty_file_has_no_slip(body):
    s = yh.slips(body)
    assert (_keys(s, "dup"), _keys(s, "null"), s["wipe"], s["lines"]) == ({}, {}, False, {})


def test_a_complex_key_does_not_crash():
    assert _keys(yh.slips("? [a, b]\n: 1\n? [a, b]\n: 2\n"), "dup") == {"?": 1}


_VALUE = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=8),
    lambda kids: st.lists(kids, max_size=3) | st.dictionaries(st.text(max_size=12), kids, max_size=4),
    max_leaves=12)
_DOC = _VALUE | st.fixed_dictionaries({"appspace": st.fixed_dictionaries({
    "microservices": st.dictionaries(st.sampled_from(["definitions", "x"]), _VALUE)})})


@settings(max_examples=500, deadline=None)
@given(body=st.text(max_size=200))
def test_property_any_text_gives_slips_or_none(body):
    s = yh.slips(body)
    assert s is None or set(s) == {"dup", "null", "wipe", "lines"}


@settings(max_examples=500, deadline=None)
@given(doc=_DOC)
def test_property_a_dumped_value_has_no_slip_and_the_wipe_matches_the_loaded_value(doc):
    # safe_dump writes one copy of each key and an explicit `null`.
    s = yh.slips(yaml.safe_dump(doc))
    assert _keys(s, "dup") == {} and _keys(s, "null") == {}
    a = doc.get("appspace") if isinstance(doc, dict) else None
    ms = a.get("microservices") if isinstance(a, dict) else None
    defs = ms.get("definitions", {"k": 1}) if isinstance(ms, dict) else {"k": 1}
    assert s["wipe"] is not (isinstance(defs, dict) and bool(defs))


# ── (b) the wipe ─────────────────────────────────────────────────────────

WIPES = {
    "bare, then a second document": "appspace:\n  microservices:\n    definitions:\n---\nx: 1\n",
    "a list": "appspace:\n  microservices:\n    definitions: []\n",
    "an empty string": "appspace:\n  microservices:\n    definitions: \"\"\n",
    "a number": "appspace:\n  microservices:\n    definitions: 0\n",
    "an empty map": "appspace:\n  microservices:\n    definitions: {}\n",
    "an explicit null": "appspace:\n  microservices:\n    definitions: null\n",
}


@pytest.mark.parametrize("body", WIPES.values(), ids=list(WIPES))
def test_a_definitions_that_is_not_a_non_empty_map_is_a_wipe(body):
    # 2.121.0 returned False for the first four (checked on origin/main).
    assert m._values_wipes_definitions(body) is True
    assert yh.slips(body)["lines"][("wipe", yh.DEFINITIONS)] == [(3, None)]


def test_a_wipe_through_a_merge_key_is_a_wipe():
    # 2.121.0 blocked it, because yaml.safe_load resolves `<<`.
    body = "base: &b\n  definitions: {}\nappspace:\n  microservices:\n    <<: *b\n"
    assert m._values_wipes_definitions(body) is True
    assert yh.slips(body)["lines"][("wipe", yh.DEFINITIONS)] == [(2, None)]
    over = body + "    definitions:\n      account: {}\n"
    assert m._values_wipes_definitions(over) is False, "a key of its own wins over the merge"


def test_a_merge_key_that_is_not_a_map_does_not_parse():
    assert yh.slips("appspace:\n  microservices:\n    <<: 1\n") is None


@pytest.mark.parametrize("body", [
    "appspace:\n  microservices:\n    definitions:\n      account:\n        image: {}\n",
    "appspace:\n  microservices: {}\n",
    "appspace:\n  microservices:\n",
    "appspace: 1\n",
    "- a\n",
])
def test_a_populated_or_absent_definitions_is_not_a_wipe(body):
    assert m._values_wipes_definitions(body) is False


# ── (c) the detector: head against base ─────────────────────────────────

def _serve(monkeypatch, files):
    """_bb_fetch_status from `files[sha][path]`; ERR is a failed read."""
    def fetch(path, sha, repo=None):
        v = files.get(sha, {}).get(path)
        if v is ERR:
            return None, m.BB_ERROR
        return (None, m.BB_NOT_FOUND) if v is None else (v, m.BB_OK)
    monkeypatch.setattr(m, "_bb_fetch_status", fetch)
    m._vf_cache.clear()     # a test may serve new bodies at the same sha


def _hits(monkeypatch, head, base, changed=None, renames=None, base_sha="base1"):
    _serve(monkeypatch, {"head1": head, "base1": base})
    hits = m._detect_yaml_slips(changed or sorted(set(head) | set(base)), renames or {},
                                "head1", base_sha, repo="acme-config-prod")
    return [(h["path"], h["kind"], h["key"], h["line"], h["first_line"]) for h in hits]


DUP = "appspace:\n  zeroPods: false\n  zeroPods: true\n"


def test_an_old_duplicate_stays_green(monkeypatch):
    assert _hits(monkeypatch, {F: DUP + "  suffix: a\n"}, {F: DUP}) == []


def test_one_more_copy_of_an_old_duplicate_is_new(monkeypatch):
    assert _hits(monkeypatch, {F: DUP + "  zeroPods: x\n"}, {F: DUP}) == [
        (F, "dup", "appspace.zeroPods", 4, 2)]


def test_a_new_file_counts_every_slip(monkeypatch):
    assert _hits(monkeypatch, {F: DUP + "  probes:\n"}, {}) == [
        (F, "dup", "appspace.zeroPods", 3, 2), (F, "null", "appspace.probes", 4, None)]


def test_a_renamed_file_compares_with_its_old_path(monkeypatch):
    old = F.replace("monthly", "weekly")
    assert _hits(monkeypatch, {F: DUP}, {old: DUP}, changed=[old, F], renames={old: F}) == []


def test_a_move_bitbucket_did_not_pair_compares_with_the_deleted_twin(monkeypatch):
    # The same env folder and file name, deleted in this PR (acme-config-prod #4592).
    old = F.replace("monthly", "weekly")
    other = F.replace("pv-x-a", "pv-y-a")
    assert _hits(monkeypatch, {F: DUP}, {old: DUP, other: "a: 1\n"},
                 changed=[other, old, F]) == [], "pv-y-a is another env, so no twin"
    assert _hits(monkeypatch, {F: DUP + "  zeroPods: x\n"}, {old: DUP}, changed=[old, F]) == [
        (F, "dup", "appspace.zeroPods", 4, 2)]


def test_a_renamed_old_path_is_not_a_twin(monkeypatch):
    old = F.replace("monthly", "weekly")
    other = F.replace("monthly", "weekly2")
    assert _hits(monkeypatch, {F: DUP, other: "a: 1\n"}, {old: DUP}, changed=[old, other, F],
                 renames={old: other}) == [(F, "dup", "appspace.zeroPods", 3, 2)]


def test_a_base_that_does_not_parse_checks_only_the_wipe(monkeypatch):
    # A PR that fixes bad YAML on main is not blocked for the slips already there.
    logged = []
    monkeypatch.setattr(logsink, "log", lambda msg, *a, **k: logged.append(msg))
    head = DUP + "  microservices:\n    definitions: {}\n"
    assert _hits(monkeypatch, {F: head}, {F: "a: [1\n"}) == [(F, "wipe", yh.DEFINITIONS, 5, None)]
    assert logged == [f"[yaml-slips] {F}: duplicate or bare keys not checked, "
                      "the base does not parse"]


def test_a_head_that_does_not_parse_is_left_to_the_render(monkeypatch):
    assert _hits(monkeypatch, {F: "a: [1\n"}, {}) == []


def test_a_deleted_head_is_skipped(monkeypatch):
    assert _hits(monkeypatch, {}, {F: DUP}) == []


@pytest.mark.parametrize("side", ["head1", "base1"])
def test_a_failed_read_on_either_side_raises(monkeypatch, side):
    files = {"head1": {F: DUP}, "base1": {F: "a: 1\n"}}
    files[side][F] = ERR
    _serve(monkeypatch, files)
    with pytest.raises(m.ValueFileUnreadable, match="pv-x-a/customer.yaml"):
        m._detect_yaml_slips([F], {}, "head1", "base1", repo="r")


@pytest.mark.parametrize("path", [".ci/azure-pipelines-build.yaml", "docs/x.yaml", "x.yml"])
def test_outside_the_config_trees_only_the_wipe_counts(monkeypatch, path):
    assert _hits(monkeypatch, {path: DUP + "  probes:\n"}, {}) == []
    assert _hits(monkeypatch, {path: WIPES["a list"]}, {}) == [
        (path, "wipe", yh.DEFINITIONS, 3, None)]


def test_a_wipe_counts_at_head_alone(monkeypatch):
    body = WIPES["bare, then a second document"]
    assert _hits(monkeypatch, {F: body}, {F: body}) == [(F, "wipe", yh.DEFINITIONS, 3, None)]


def test_a_bare_definitions_is_one_wipe_not_also_a_bare_key(monkeypatch):
    body = "appspace:\n  probes:\n  microservices:\n    definitions:\n"
    assert _hits(monkeypatch, {F: body}, {}) == [
        (F, "null", "appspace.probes", 2, None), (F, "wipe", yh.DEFINITIONS, 4, None)]


def test_non_value_files_are_never_read(monkeypatch):
    _serve(monkeypatch, {})
    monkeypatch.setattr(m, "_bb_fetch_status", lambda *a, **k: pytest.fail("read"))
    assert m._detect_yaml_slips(["docs/README.md", "acme-utils/x.sh"], {}, "h", "b") == []


def test_with_no_base_only_the_wipe_counts_and_it_is_logged(monkeypatch):
    logged = []
    monkeypatch.setattr(logsink, "log", lambda msg, *a, **k: logged.append(msg))
    assert _hits(monkeypatch, {F: DUP, ANCILLARY: "a: 1\n"}, {}, base_sha=None) == []
    assert logged == [f"[yaml-slips] {F}: duplicate or bare keys not checked, "
                      "no base to compare with"]


def test_a_file_with_no_slip_reads_no_base(monkeypatch):
    _serve(monkeypatch, {"head1": {F: "appspace:\n  zeroPods: false\n"}, "base1": {F: ERR}})
    assert m._detect_yaml_slips([F], {}, "head1", "base1", repo="r") == []


def test_the_wipe_wrapper_keeps_its_contract(monkeypatch):
    _serve(monkeypatch, {"h": {F: DUP, ANCILLARY: WIPES["a number"]}})
    assert m._detect_wiped_definitions([F, ANCILLARY], "h", repo="r") == [ANCILLARY]


# ── (d) the prod shapes of the replay ────────────────────────────────────

AUTH = ("appspace:\n  microservices:\n    definitions:\n      authentication:\n        env:\n"
        "          appspace__login__allowedDomains__1:\n            value: \"westqurna1.net\"\n"
        "          appspace__login__allowedDomains__2:\n            value: \"sitco.sa\"\n")
RESV = ("appspace:\n  microservices:\n    definitions:\n      reservation-background:\n"
        "        env:\n          A:\n            value: prod\n"
        "        resources:\n          requests:\n            cpu: 100m\n")
DEVICES = "".join(f"      {s}:\n        image:\n          tag: 1.106.0\n"
                  for s in ("device", "device-background", "devicegateway"))
DEFS = "appspace:\n  microservices:\n    definitions:\n"
USER = DEFS + "      user:\n        replicas: 20\n"
HPA = ("      user:\n        hpa:\n          behavior:\n            scaleDown:\n"
       "              selectPolicy: Max\n              policies:\n")
PLATFORM = "      platform:\n        replicas: 0\n        hpa:\n          enabled: false\n"
DPDHL = "---\nappspace:\n  hostingID: \"00000038\"\n  customerName: dpdhl\n"

SHAPES = {
    "#3583 allowedDomains__2": (
        "gcp/prod/public-cloud/na1-a/cl-prod-b/constellation/customer.yaml",
        AUTH, AUTH + "          appspace__login__allowedDomains__2:\n            value: \"alrayan.com\"\n",
        [("dup", "appspace.microservices.definitions.authentication.env."
                 "appspace__login__allowedDomains__2")]),
    "#4322 bnym--aec1 env": (
        "gcp/aec/private-cloud/na2-a/pv-bnym--aec1-a/customer.yaml",
        RESV, RESV + "        env:\n          B:\n            value: dev\n",
        [("dup", "appspace.microservices.definitions.reservation-background.env")]),
    "#3289 usbank-c devices": (
        "gcp/aec/private-cloud/na1-b/pv-usbank-c/customer.yaml",
        DEFS + DEVICES, DEFS + DEVICES + DEVICES.replace("1.106.0", "1.107.1"),
        [("dup", f"appspace.microservices.definitions.{s}")
         for s in ("device", "device-background", "devicegateway")]),
    "#3245 atlas apppublishing": (
        "azure/prod/private-cloud/na1-a/accelerated/pv-atlas-a/customer.yaml",
        None, DEFS + "      apppublishing:\n      library:\n        env: {}\n",
        [("null", "appspace.microservices.definitions.apppublishing")]),
    "#3411 upstate policies": (
        "gcp/prod/private-cloud/na3-a/weekly/pv-upstate-oneup-b/customer.yaml",
        USER, DEFS + HPA,
        [("null", "appspace.microservices.definitions.user.hpa.behavior.scaleDown.policies")]),
    "#4647 bnym-b platform pin": (
        "gcp/prod/private-cloud/na2-a/monthly-friday/pv-bnym-b/customer.yaml",
        DEFS + "      crebroker:\n        hpa:\n          disabled: true\n",
        DEFS + "      crebroker:\n        hpa:\n          disabled: true\n" + PLATFORM, []),
    "#4639 dpdhl-c pause": (
        "gcp/prod/private-cloud/eu1-b/monthly-friday/pv-dpdhl-c/customer.yaml",
        DPDHL, DPDHL.replace("appspace:\n", "appspace:\n  # paused\n  autosync: false\n"), []),
}


@pytest.mark.parametrize("path,base,head,want", SHAPES.values(), ids=list(SHAPES))
def test_the_replayed_prod_shapes(monkeypatch, path, base, head, want):
    got = _hits(monkeypatch, {path: head}, {path: base} if base else {}, changed=[path])
    assert [(k, key) for _p, k, key, _l, _f in got] == want


PROD = os.path.expanduser("~/gitprojects/acme-config-prod")
PROD_PRS = {
    "3583": ("6b24610635d71eeb63c2fa3be923f3180758ab51", ["dup"]),
    "4322": ("aed834c3e6b44dd4be7221c278128d01cb5fad2a", ["dup"]),
    "3289": ("270c8ca0f21cdc8e8569176fb0695dd1ba9547d6", ["dup", "dup", "dup"]),
    "3245": ("446eafdc577ba5c13a168de806ddcf7817e3346f", ["null"]),
    "3411": ("11a9830814fa3f4c31c905c48575382f8b598cfc", ["null"]),
    "3299": ("b2bf12b35ebfa406c613740dfd47e8550a415ea2", ["null"]),
    "4647": ("937f897469c71407c23ded29f5ff8c17ffa9c466", []),
    "4639": ("dc60d7ce8e1bd18660a696e09e599acc89a1496d", []),
}


@pytest.mark.local_components
@pytest.mark.parametrize("pr", PROD_PRS)
def test_the_real_prod_prs(monkeypatch, pr):
    """The 6 slip PRs of the replay block, and #4647 and #4639 stay quiet,
    read from the real merge commits."""
    if not os.path.isdir(os.path.join(PROD, ".git")):
        pytest.skip("acme-config-prod not checked out")

    def git(*a):
        return subprocess.run(["git", "-C", PROD, *a], capture_output=True, text=True)
    sha, want = PROD_PRS[pr]
    if git("cat-file", "-e", f"{sha}^{{commit}}").returncode:
        pytest.skip(f"#{pr} is not in the local clone")
    base = git("rev-parse", f"{sha}^1").stdout.strip()

    def fetch(path, s, repo=None):
        r = git("show", f"{s}:{path}")
        return (r.stdout, m.BB_OK) if r.returncode == 0 else (None, m.BB_NOT_FOUND)
    monkeypatch.setattr(m, "_bb_fetch_status", fetch)
    changed = git("diff", "--name-only", "--no-renames", base, sha).stdout.split()
    hits = m._detect_yaml_slips(changed, {}, sha, base, repo="acme-config-prod")
    assert [h["kind"] for h in hits] == want, hits


# ── (e) process_pr: the block, and an old slip that stays green ──────────

@pytest.fixture()
def slip_pr(world, monkeypatch):
    """The PR changes pv-orch-a/customer.yaml; `files[sha][path]`, read at the merge preview."""
    sinks, plan = world
    files = {BASE_SHA: {IDENTITY: IDENTITY_YAML}, MERGED: {IDENTITY: IDENTITY_YAML}}
    _serve(monkeypatch, files)
    monkeypatch.setattr(m, "_merge_preview", lambda repo, base, pr: (MERGED, []))
    monkeypatch.setattr(m, "get_pr_changed_files", lambda pr_id, repo=None: ([IDENTITY], {}))
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)
    return sinks, files


def _blocked(sinks, desc):
    body = sinks.upserts[-1]
    assert sinks.statuses[-1] == ("FAILED", desc)
    assert len(desc) <= 255
    assert m._extract_status_token(body) == "blocked"
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA)
    assert sinks.diff_calls == [], "a slip is blocked before any render"
    assert "\u2014" not in body and "\u2013" not in body
    return body


def test_process_pr_blocks_a_new_duplicate_key(slip_pr):
    sinks, files = slip_pr
    files[BASE_SHA][IDENTITY] = IDENTITY_YAML + "  zeroPods: false\n"
    files[MERGED][IDENTITY] = IDENTITY_YAML + "  zeroPods: false\n  zeroPods: false\n"
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    body = _blocked(sinks, "BLOCKED: YAML slip in pv-orch-a/customer.yaml line 5: "
                           "duplicate key appspace.zeroPods - see PR comment")
    assert f"- `{IDENTITY}` line 5: duplicate key `appspace.zeroPods` (first copy at line 4)" in body
    assert "COPR-31148" in body and "Keep one copy" in body
    assert "COPR-31637" not in body, "only the kinds this PR has are explained"
    assert f"**Status:** \u26d4 Blocked: YAML slip in `{IDENTITY}`\n" in body


def test_process_pr_blocks_a_new_bare_key_and_counts_the_rest(slip_pr):
    sinks, files = slip_pr
    files[MERGED][IDENTITY] = IDENTITY_YAML + "  probeOverrides:\n  hpa:\n"
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    body = _blocked(sinks, "BLOCKED: YAML slip in pv-orch-a/customer.yaml line 4: "
                           "bare key appspace.probeOverrides (+1 more) - see PR comment")
    assert "`appspace.hpa` (it reads as null)" in body and "write `null`" in body


def test_process_pr_blocks_a_wipe_after_a_second_document(slip_pr, monkeypatch):
    sinks, files = slip_pr
    monkeypatch.setattr(m, "get_pr_changed_files", lambda pr_id, repo=None: ([ANCILLARY], {}))
    files[MERGED][ANCILLARY] = WIPES["bare, then a second document"]
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    body = _blocked(sinks, "BLOCKED: YAML slip in pv-orch-a/cicd-versions.yaml line 3: "
                           "empty microservices.definitions (wipes image names) - see PR comment")
    assert "`appspace.microservices.definitions` is empty or not a map" in body
    assert "COPR-31637" in body and "Old duplicate" not in body


def test_process_pr_keeps_a_file_with_an_old_bare_key_green(slip_pr):
    # The pv-atlas-a shape on main today: a bare apppublishing, and an unrelated edit.
    sinks, files = slip_pr
    old = IDENTITY_YAML + "  microservices:\n    definitions:\n      apppublishing:\n"
    files[BASE_SHA][IDENTITY] = old
    files[MERGED][IDENTITY] = old + "  zeroPods: false\n"
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    assert sinks.statuses[-1][0] == "SUCCESSFUL", sinks.statuses
    assert m._extract_status_token(sinks.upserts[-1]) == "clean"
    assert sorted(set(sinks.diff_calls)) == ["pv-orch-a-ms", "pv-orch-a-ss"]


def test_process_pr_without_a_merge_preview_checks_only_the_wipe(slip_pr, monkeypatch):
    # The base is then main, not the merge base: a stale branch would show
    # the slips that main already removed as new ones.
    sinks, files = slip_pr
    monkeypatch.setattr(m, "_merge_preview", lambda repo, base, pr: (None, None))
    files[PR_SHA] = {IDENTITY: IDENTITY_YAML + "  zeroPods: false\n  zeroPods: false\n"}
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    assert sinks.statuses[-1][0] == "SUCCESSFUL", sinks.statuses
    assert m._extract_status_token(sinks.upserts[-1]) == "clean"


def test_process_pr_retries_when_the_base_cannot_be_read(slip_pr, monkeypatch):
    sinks, files = slip_pr
    monkeypatch.setattr(m, "_retry_backoff", {})
    files[BASE_SHA][IDENTITY] = ERR
    files[MERGED][IDENTITY] = IDENTITY_YAML + "  probes:\n"
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    assert sinks.statuses[-1][1].startswith("Diff unavailable (infrastructure) - will retry")
    assert m._extract_status_token(sinks.upserts[-1]) == "transient"
    assert SK not in m._seen


def test_the_block_cuts_a_long_description_and_counts_the_other_files():
    key = "appspace." + "k" * 300
    hits = [{"path": F, "kind": "dup", "key": key, "line": 9, "first_line": 2},
            {"path": ANCILLARY, "kind": "null", "key": "appspace.hpa", "line": 3, "first_line": None}]
    desc, body = m._yaml_slip_block(hits, PR_SHA, None)
    assert len(desc) == 255 and desc.startswith(
        "BLOCKED: YAML slip in pv-x-a/customer.yaml line 9: duplicate key appspace.kkk")
    assert desc.endswith("k - see PR comment")
    assert f"**Status:** ⛔ Blocked: YAML slip in `{F}` (+1 more)\n" in body
    assert "[base:" not in body and m._extract_status_token(body) == "blocked"


# ── (f) the recovery reads the block as red ──────────────────────────────

def test_fix_stuck_turns_the_slip_block_red(slip_pr, monkeypatch):
    sinks, files = slip_pr
    files[MERGED][IDENTITY] = IDENTITY_YAML + "  probes:\n"
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    posted = []
    monkeypatch.setattr(m, "http", lambda *a, **k: {"state": "INPROGRESS"})
    monkeypatch.setattr(m, "post_build_status", lambda sha, state, desc, pr_id=None, repo=None:
                        posted.append((state, desc)) or "ok")
    assert REAL_FIX_STUCK(PR_SHA, 991, sinks.upserts[-1], repo="acme-config-dev") == "ok"
    assert posted == [("FAILED", "Blocked - merging would break the environment (see comment)")]
