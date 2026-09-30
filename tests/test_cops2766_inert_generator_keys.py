"""COPS-2766 block 4, item 5: a key the ApplicationSet never reads in that file.

The ApplicationSets take appspace.version, autosync and decommission only from
the files their generators list: for private cloud the env customer.yaml over
the cohort config.yaml one folder up, for public cloud cl-*/config.yaml and
cl-*/appN/customer.yaml. Written anywhere else, version and autosync do
nothing and the diff stays quiet (COPS-2684: autosync false in a constellation
customer.yaml). decommission there adds no cascade finalizer, but the chart
still reads it, so the teardown is only half armed. A version that is not a
string asks for the wrong chart, in any file. aws/ has no ApplicationSet.
"""
import os
import posixpath
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import diff_preview as m  # noqa: E402
import yaml_hygiene as yh  # noqa: E402

from test_coverage_orchestration import (  # noqa: E402,F401
    world, _mk_pr, PATH_MAP, BASE_SHA, PR_SHA, IDENTITY, IDENTITY_YAML, ANCILLARY)

SK = ("acme-config-dev", 991)
MERGED = "feed0001aabb"
ERR = object()

PV = "gcp/prod/private-cloud/na1-a/monthly/pv-x-a"
ENV = f"{PV}/customer.yaml"
CICD = f"{PV}/cicd-versions.yaml"
ENV_CFG = f"{PV}/config.yaml"
COHORT = "gcp/prod/private-cloud/na1-a/monthly/config.yaml"
EMPTY_COHORT = "gcp/prod/private-cloud/na4-a/monthly/config.yaml"
CL = "gcp/prod/public-cloud/na1-a/cl-prod-b"
CL_ENV = f"{CL}/config.yaml"
CL_APP3 = f"{CL}/app3/customer.yaml"
CL_CONST = f"{CL}/constellation/customer.yaml"
AZ_CICD = "azure/prod/private-cloud/na1-b/monthly/pv-y-a/cicd-versions.yaml"
MAP = {ENV: ["pv-x-a-ms", "pv-x-a-ss"]}


def _body(**kv):
    return "appspace:\n" + "".join(f"  {k}: {v}\n" for k, v in kv.items())


VER = _body(version='"2603.3.10"')
VMAP = 'appspace:\n  zeroPods: false\n  version:\n    AppVersion: "2603.2.0"\n'


def _serve(monkeypatch, files):
    """_bb_fetch_status from `files[sha][path]`, ERR a failed read; returns the reads."""
    calls = []

    def fetch(path, sha, repo=None):
        calls.append((path, sha))
        v = files.get(sha, {}).get(path)
        if v is ERR:
            return None, m.BB_ERROR
        return (None, m.BB_NOT_FOUND) if v is None else (v, m.BB_OK)
    monkeypatch.setattr(m, "_bb_fetch_status", fetch)
    m._vf_cache.clear()     # a test may serve new bodies at the same sha
    return calls


def _hits(monkeypatch, head, base=None, changed=None, renames=None, path_map=MAP,
          base_sha="base1"):
    _serve(monkeypatch, {"head1": head, "base1": base or {}})
    hits = m._detect_inert_generator_keys(changed or sorted(head), renames or {}, path_map,
                                          "head1", base_sha, repo="acme-config-prod")
    return [(h["path"], h["key"], h["why"]) for h in hits]


# ── which files a generator reads ────────────────────────────────────────

@pytest.mark.parametrize("path,want", [
    (ENV, True),
    (COHORT, True),                                  # pv-x-a lives below it
    (EMPTY_COHORT, True),                            # no env below: a fleet bump stays green
    ("gcp/prod/private-cloud/na1-a/config.yaml", True),   # the cohort of an env under the spoke
    (ENV_CFG, False),                                # next to a customer.yaml: an env folder
    (CICD, False),
    ("gcp/config.yaml", False),
    ("gcp/prod/config.yaml", False),
    ("gcp/prod/private-cloud/config.yaml", False),
    (CL_ENV, True),
    (CL_APP3, True),
    (CL_CONST, False),
    (f"{CL}/api/customer.yaml", False),              # the fixed GLB sets read cl-*/config.yaml
    (f"{CL}/constellation/cicd-versions.yaml", False),
    ("gcp/prod/public-cloud/na1-a/config.yaml", False),
    ("azure/prod/private-cloud/na1-b/monthly/pv-y-a/customer.yaml", True),
    (AZ_CICD, False),
    ("aws/prod/private-cloud/na1-a/pv-amzn-a/customer.yaml", None),   # no ApplicationSet
    (".ci/azure-pipelines.yaml", None),
    ("docs/x.yaml", None),
])
def test_the_files_a_generator_reads(monkeypatch, path, want):
    calls = _serve(monkeypatch, {})
    assert m._generator_reads(path, "head1", MAP, [path]) is want
    # Only a config.yaml with no env known next to it or below it is read.
    assert calls == ([(posixpath.dirname(path) + "/customer.yaml", "head1")]
                     if path in (EMPTY_COHORT, "gcp/prod/private-cloud/na1-a/config.yaml") else [])


def test_an_env_folder_config_yaml_found_in_the_changed_files(monkeypatch):
    calls = _serve(monkeypatch, {})
    new_env = "gcp/prod/private-cloud/na1-a/monthly/pv-new-a"
    assert m._generator_reads(f"{new_env}/config.yaml", "head1", {},
                              [f"{new_env}/config.yaml", f"{new_env}/customer.yaml"]) is False
    assert calls == []


def test_a_cohort_with_a_customer_yaml_next_to_it_is_still_read(monkeypatch):
    calls = _serve(monkeypatch, {})
    stray = f"{posixpath.dirname(COHORT)}/customer.yaml"
    assert m._generator_reads(COHORT, "head1", {**MAP, stray: []}, [COHORT]) is True
    assert calls == []


def test_an_env_folder_config_yaml_found_by_a_read(monkeypatch):
    other = "gcp/prod/private-cloud/na1-a/monthly/pv-z-a"
    _serve(monkeypatch, {"head1": {f"{other}/customer.yaml": VER}})
    assert m._generator_reads(f"{other}/config.yaml", "head1", {}, []) is False


def test_a_failed_sibling_read_is_retried(monkeypatch):
    _serve(monkeypatch, {"head1": {f"{posixpath.dirname(EMPTY_COHORT)}/customer.yaml": ERR}})
    with pytest.raises(m.ValueFileUnreadable):
        m._generator_reads(EMPTY_COHORT, "head1", {}, [EMPTY_COHORT])


# ── the detector ─────────────────────────────────────────────────────────

HITS = {
    "COPS-2684 autosync in a constellation customer.yaml": (
        {CL_CONST: _body(autosync="false")}, [(CL_CONST, "autosync", "inert")]),
    "acme-config-dev #6845 version for versions in a constellation customer.yaml": (
        {CL_CONST: VMAP}, [(CL_CONST, "version", "inert")]),
    "a version map in a cl config.yaml": ({CL_ENV: VMAP}, [(CL_ENV, "version", "type")]),
    "version in cicd-versions.yaml": ({CICD: VER}, [(CICD, "version", "inert")]),
    "version in an env-folder config.yaml": ({ENV_CFG: VER}, [(ENV_CFG, "version", "inert")]),
    "version in the private-cloud tier config.yaml": (
        {"gcp/prod/private-cloud/config.yaml": VER},
        [("gcp/prod/private-cloud/config.yaml", "version", "inert")]),
    "decommission in cicd-versions.yaml": (
        {CICD: _body(decommission="true")}, [(CICD, "decommission", "inert")]),
    "decommission in the tier config.yaml": (
        {"gcp/prod/config.yaml": _body(decommission="true")},
        [("gcp/prod/config.yaml", "decommission", "inert")]),
    "an unquoted version in a customer.yaml": (
        {ENV: _body(version="2604.0")}, [(ENV, "version", "type")]),
    "an unquoted version in a cl config.yaml": (
        {CL_ENV: _body(version="2603.3")}, [(CL_ENV, "version", "type")]),
    "a version that is true": ({ENV: _body(version="true")}, [(ENV, "version", "type")]),
    "azure cicd-versions.yaml": ({AZ_CICD: VER}, [(AZ_CICD, "version", "inert")]),
    "a number where it is not read is one hit": (
        {CICD: _body(version="2604.0")}, [(CICD, "version", "inert")]),
    "a null decommission can unarm the chart": (
        {CICD: _body(decommission="")}, [(CICD, "decommission", "inert")]),
    "all three keys": (
        {CICD: _body(version='"2603.3.10"', autosync="false", decommission="true")},
        [(CICD, "version", "inert"), (CICD, "autosync", "inert"),
         (CICD, "decommission", "inert")]),
}


@pytest.mark.parametrize("head,want", HITS.values(), ids=list(HITS))
def test_a_key_the_generator_does_not_read_or_cannot_use(monkeypatch, head, want):
    assert _hits(monkeypatch, head) == want


QUIET = {
    "autosync in a cl app customer.yaml": {CL_APP3: _body(autosync="false")},
    "autosync in a cl config.yaml": {CL_ENV: _body(autosync="false")},
    "version in a cohort config.yaml": {COHORT: VER},
    "version in an empty cohort config.yaml": {EMPTY_COHORT: VER},
    "decommission in a constellation customer.yaml": {CL_CONST: _body(decommission="true")},
    "decommission in a cl cicd-versions.yaml": {
        f"{CL}/constellation/cicd-versions.yaml": _body(decommission="true")},
    "aws has no ApplicationSet": {"aws/prod/private-cloud/na1-a/pv-amzn-a/cicd-versions.yaml": VER},
    "a pipeline file": {".ci/x.yaml": VER},
    "version false in a customer.yaml, the frozen check": {ENV: _body(version="false")},
    "a quoted version in a customer.yaml": {ENV: VER},
    "no generator key": {CICD: _body(zeroPods="false")},
    "appspace is not a map": {CICD: "appspace: [version]\n"},
    "YAML that does not parse": {CICD: "appspace: [\n"},
    "not a value file": {f"{PV}/notes.md": VER},
}


@pytest.mark.parametrize("head", QUIET.values(), ids=list(QUIET))
def test_what_stays_quiet(monkeypatch, head):
    assert _hits(monkeypatch, head) == []


def test_a_deleted_file_is_skipped(monkeypatch):
    assert _hits(monkeypatch, {}, {CICD: VER}, changed=[CICD]) == []


def test_the_same_value_at_base_is_old(monkeypatch):
    assert _hits(monkeypatch, {CICD: VER + "  zeroPods: true\n"}, {CICD: VER}) == []


def test_a_removed_key_is_a_cleanup(monkeypatch):
    assert _hits(monkeypatch, {CICD: _body(zeroPods="true")}, {CICD: VER}) == []


def test_a_changed_value_at_base_is_new(monkeypatch):
    assert _hits(monkeypatch, {CICD: VER}, {CICD: _body(version='"2603.3.9"')}) == [
        (CICD, "version", "inert")]


def test_a_renamed_file_compares_with_its_old_side(monkeypatch):
    old = "gcp/prod/private-cloud/na1-a/weekly/pv-x-a/cicd-versions.yaml"
    assert _hits(monkeypatch, {CICD: VER}, {old: VER}, changed=[old, CICD],
                 renames={old: CICD}) == []


def test_a_base_that_does_not_parse_counts_every_key(monkeypatch):
    assert _hits(monkeypatch, {CICD: VER}, {CICD: "appspace: [\n"}) == [(CICD, "version", "inert")]


def test_with_no_base_every_key_counts(monkeypatch):
    assert _hits(monkeypatch, {CICD: VER}, {CICD: VER}, base_sha=None) == [
        (CICD, "version", "inert")]


def test_the_base_is_read_only_for_a_possible_hit(monkeypatch):
    calls = _serve(monkeypatch, {"head1": {ENV: VER, CICD: _body(zeroPods="true")}})
    assert m._detect_inert_generator_keys([ENV, CICD], {}, MAP, "head1", "base1") == []
    assert [c for c in calls if c[1] == "base1"] == []


@pytest.mark.parametrize("side", ["head1", "base1"])
def test_a_failed_read_is_retried_never_passed(monkeypatch, side):
    files = {"head1": {CICD: VER}, "base1": {}}
    files[side][CICD] = ERR
    _serve(monkeypatch, files)
    with pytest.raises(m.ValueFileUnreadable):
        m._detect_inert_generator_keys([CICD], {}, MAP, "head1", "base1")


REAL_PRS = {
    # Fleet bumps of 16 cohort config.yaml files, and of 3 plus 3 customer.yaml.
    "prod #4706": ("acme-config-prod", "7b6b97135a2dd0ec52dc5aaa9997f64302a57b41", []),
    "prod #4705": ("acme-config-prod", "7ceb57dd0bf0ae700357c9873ec4747efd88d621", []),
    # The only hit of the replay: version.AppVersion where the chart reads versions.
    "dev #6845": ("acme-config-dev", "0fe613c94794351413dd9e93aafe5420ac711092",
                  [("gcp/qa/public-cloud/ap1/cl-qa-15-a/constellation/customer.yaml",
                    "version", "inert")]),
}


@pytest.mark.local_components
@pytest.mark.parametrize("pr", REAL_PRS)
def test_the_real_prs(monkeypatch, pr):
    """Read from the real merge commits, with no path_map: the reads decide."""
    name, sha, want = REAL_PRS[pr]
    clone = os.path.expanduser(f"~/gitprojects/{name}")
    if not os.path.isdir(os.path.join(clone, ".git")):
        pytest.skip(f"{name} not checked out")

    def git(*a):
        return subprocess.run(["git", "-C", clone, *a], capture_output=True, text=True)
    if git("cat-file", "-e", f"{sha}^{{commit}}").returncode:
        pytest.skip(f"{pr} is not in the local clone")
    base = git("rev-parse", f"{sha}^1").stdout.strip()

    def fetch(path, s, repo=None):
        r = git("show", f"{s}:{path}")
        return (r.stdout, m.BB_OK) if r.returncode == 0 else (None, m.BB_NOT_FOUND)
    monkeypatch.setattr(m, "_bb_fetch_status", fetch)
    m._vf_cache.clear()
    changed = git("diff", "--name-only", "--no-renames", base, sha).stdout.split()
    hits = m._detect_inert_generator_keys(changed, {}, {}, sha, base, repo=name)
    assert [(h["path"], h["key"], h["why"]) for h in hits] == want


# ── the block ────────────────────────────────────────────────────────────

def _hit(path, key, value, why="inert"):
    return {"path": path, "key": key, "value": value, "why": why}


def test_the_status_names_the_key_and_the_file():
    desc, body = m._inert_key_block([_hit(CL_CONST, "autosync", False)], PR_SHA, BASE_SHA)
    assert desc == ("BLOCKED: appspace.autosync in cl-prod-b/constellation/customer.yaml is never "
                    "read by the ApplicationSet - see PR comment")
    assert f"- `{CL_CONST}`: `appspace.autosync: false`. The ApplicationSet reads it only from " \
           "`cl-*/config.yaml` (the whole environment) or `cl-*/appN/customer.yaml` " \
           "(one app type)." in body
    assert "COPS-2684" in body and "Move the key to the file named above, or delete it." in body
    assert "half armed" not in body and "Quote it" not in body
    assert "**Blocked: this PR sets a key where ArgoCD does not read it.**" in body
    assert f"**Status:** ⛔ Blocked: `appspace.autosync` in `{CL_CONST}`\n" in body
    assert m._extract_status_token(body) == "blocked" and f"[base:{BASE_SHA[:8]}]" in body
    assert "—" not in body and "–" not in body


@pytest.mark.parametrize("path,want", [
    (CL_CONST, "cl-prod-b/constellation/customer.yaml"),
    (CL_APP3, "cl-prod-b/app3/customer.yaml"),
    (CL_ENV, "cl-prod-b/config.yaml"),
    (CICD, "pv-x-a/cicd-versions.yaml"),
    ("x.yaml", "x.yaml"),
])
def test_a_status_names_the_cl_env_of_a_public_cloud_file(path, want):
    assert m._status_path(path) == want
    assert f" in {want} " in m._inert_key_block([_hit(path, "autosync", False)], PR_SHA, None)[0]
    slip = {"path": path, "kind": "null", "key": "appspace.probes", "line": 2, "first_line": None}
    assert f" in {want} line 2: " in m._yaml_slip_block([slip], PR_SHA, None)[0]


def test_a_decommission_says_the_chart_still_reads_it():
    desc, body = m._inert_key_block([_hit(CICD, "decommission", True),
                                     _hit(CICD, "version", "2603.3.10")], PR_SHA, None)
    assert desc == ("BLOCKED: appspace.decommission in pv-x-a/cicd-versions.yaml is never read "
                    "by the ApplicationSet (+1 more) - see PR comment")
    assert f"- `{CICD}`: `appspace.decommission: true`. The ApplicationSet reads it only from " \
           "the environment `customer.yaml`, or the cohort `config.yaml` one folder up." in body
    assert "half armed" in body and "`decommissionPurgeData`" in body
    assert "Move `decommission` to the environment `customer.yaml`, or delete it." in body
    assert f"- `{CICD}`: `appspace.version: \"2603.3.10\"`. The ApplicationSet reads it only from " \
           "the environment `customer.yaml`" in body
    assert f"**Status:** ⛔ Blocked: `appspace.decommission` in `{CICD}` (+1 more)\n" in body
    assert "[base:" not in body and m._extract_status_token(body) == "blocked"


def test_a_version_that_is_not_a_string():
    desc, body = m._inert_key_block([_hit(ENV, "version", 2604.0, "type")], PR_SHA, BASE_SHA)
    assert desc == "BLOCKED: appspace.version in pv-x-a/customer.yaml is not a string - see PR comment"
    assert "**Blocked: this PR sets `appspace.version` to a value that is not a string.**" in body
    assert f"- `{ENV}`: `appspace.version: 2604.0` is not a string in YAML. " \
           "Quote it: `version: \"2604.0\"`." in body
    assert "`2604.0` becomes `2604`" in body and "COPS-2684" not in body
    assert "—" not in body and "–" not in body


def test_a_number_keeps_the_text_the_author_wrote(monkeypatch):
    # 2603.10 loads as the float 2603.1: quoting that would pin another chart.
    _serve(monkeypatch, {"head1": {ENV: _body(version="2603.10"),
                                   CICD: _body(version='"2603.3.10"', autosync="true")}})
    hits = m._detect_inert_generator_keys([ENV, CICD], {}, MAP, "head1", None)
    assert [(h["key"], h["value"], h["text"]) for h in hits] == [
        ("version", 2603.1, "2603.10"), ("version", "2603.3.10", None), ("autosync", True, "true")]
    _, body = m._inert_key_block(hits, PR_SHA, None)
    assert f"- `{ENV}`: `appspace.version: 2603.10` is not a string in YAML. " \
           "Quote it: `version: \"2603.10\"`." in body
    assert f"- `{CICD}`: `appspace.version: \"2603.3.10\"`. The" in body
    assert f"- `{CICD}`: `appspace.autosync: true`. The" in body
    assert "2603.1`" not in body and '"2603.1"' not in body


@pytest.mark.parametrize("body,want", [
    ("appspace:\n  version: 2604.10\n", "2604.10"),
    ("appspace:\n  version: 1\n  version: 2604.10\n", "2604.10"),    # the last copy
    ("b: &b\n  version: 2604.10\nappspace:\n  <<: *b\n", "2604.10"),  # a merge
    ("appspace:\n  version:\n    AppVersion: x\n", None),
    ("appspace: [\n", None),
    (None, None),
])
def test_the_text_of_a_scalar_as_written(body, want):
    assert yh.scalar_text(body, "appspace", "version") == want


def test_a_version_map_points_to_versions():
    # acme-config-dev #6845 ("Upgrade platformVersion"): the chart reads
    # appspace.versions.AppVersion, so version.AppVersion changed nothing.
    v = {"AppVersion": "2603.2.0"}
    _, body = m._inert_key_block([_hit(CL_CONST, "version", v), _hit(CL_ENV, "version", v, "type")],
                                 PR_SHA, None)
    assert f"- `{CL_CONST}`: `appspace.version: {{\"AppVersion\": \"2603.2.0\"}}`. The " \
           "ApplicationSet reads it only from `cl-*/config.yaml` (the whole environment) or " \
           "`cl-*/appN/customer.yaml` (one app type). For the chart's app versions, the key " \
           "is `appspace.versions`." in body
    assert f"- `{CL_ENV}`: `appspace.version: {{\"AppVersion\": \"2603.2.0\"}}` is not a string " \
           "in YAML. For the chart's app versions, the key is `appspace.versions`." in body
    assert "Quote it" not in body


def test_a_long_description_is_cut():
    desc, _ = m._inert_key_block([_hit(f"{PV}/{'k' * 300}.yaml", "version", "x")], PR_SHA, None)
    assert len(desc) == 255 and desc.endswith("kkk... - see PR comment")
    hit = _hit(PV + "/" + "\u00e9" * 300 + ".yaml", "version", "x")
    desc, _ = m._inert_key_block([hit, hit], PR_SHA, None)
    assert len(desc.encode()) <= 255 and desc.endswith("\u00e9... (+1 more) - see PR comment")


# ── process_pr ───────────────────────────────────────────────────────────

COHORT_DEV = "gcp/dev/private-cloud/ap1/custom/config.yaml"


@pytest.fixture()
def key_pr(world, monkeypatch):
    """`files[sha][path]`, read at the merge preview; the PR changes pv-orch-a files."""
    sinks, plan = world
    files = {BASE_SHA: {IDENTITY: IDENTITY_YAML}, MERGED: {IDENTITY: IDENTITY_YAML}}
    _serve(monkeypatch, files)
    monkeypatch.setattr(m, "_merge_preview", lambda repo, base, pr: (MERGED, []))
    monkeypatch.setattr(m, "get_pr_changed_files", lambda pr_id, repo=None: ([ANCILLARY], {}))
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)
    return sinks, files


def test_process_pr_blocks_a_version_in_cicd_versions(key_pr):
    sinks, files = key_pr
    files[MERGED][ANCILLARY] = _body(version='"2603.3.10"')
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    desc = ("BLOCKED: appspace.version in pv-orch-a/cicd-versions.yaml is never read "
            "by the ApplicationSet - see PR comment")
    assert sinks.statuses[-1] == ("FAILED", desc)
    body = sinks.upserts[-1]
    assert m._extract_status_token(body) == "blocked"
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA)
    assert sinks.diff_calls == [], "blocked before any render"
    assert "—" not in body and "–" not in body


def test_process_pr_keeps_a_customer_yaml_bump_green(key_pr, monkeypatch):
    sinks, files = key_pr
    monkeypatch.setattr(m, "get_pr_changed_files", lambda pr_id, repo=None: ([IDENTITY], {}))
    files[MERGED][IDENTITY] = IDENTITY_YAML.replace("2603.0.1-dev", "2603.0.2-dev")
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    assert sinks.statuses[-1][0] == "SUCCESSFUL", sinks.statuses
    assert m._extract_status_token(sinks.upserts[-1]) == "clean"


def test_process_pr_keeps_a_cohort_bump_green(key_pr, monkeypatch):
    sinks, files = key_pr
    monkeypatch.setattr(m, "get_pr_changed_files", lambda pr_id, repo=None: ([COHORT_DEV], {}))
    files[BASE_SHA][COHORT_DEV] = _body(version='"2603.0.1-dev"')
    files[MERGED][COHORT_DEV] = _body(version='"2603.0.2-dev"')
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    assert sinks.statuses[-1][0] == "SUCCESSFUL", sinks.statuses
    assert m._extract_status_token(sinks.upserts[-1]) == "clean"


def test_process_pr_retries_when_the_base_cannot_be_read(key_pr, monkeypatch):
    sinks, files = key_pr
    monkeypatch.setattr(m, "_retry_backoff", {})
    files[BASE_SHA][ANCILLARY] = ERR
    files[MERGED][ANCILLARY] = _body(version='"2603.3.10"')
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    assert sinks.statuses[-1][1].startswith("Diff unavailable (infrastructure) - will retry")
    assert m._extract_status_token(sinks.upserts[-1]) == "transient"
    assert SK not in m._seen
