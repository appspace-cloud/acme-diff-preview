"""COPR-32566: block a clone whose names miss its partition token.

acme-config-prod PR #4671 cloned NBC into `gcp/aec/.../pv-nbc--aec1-a` with
`customerName: nbc`. A clone runs on the same cluster and cloud project as
production, so that is production NBC's name; PR #4672 fixed it by hand.
The guard blocks the PR like the COPR-31637 one, and runs again on the fix push.
"""
import os
import subprocess
import sys

import pytest
import yaml
from hypothesis import example, given, settings, strategies as st

os.environ.setdefault("BB_USER", "test-user")
os.environ.setdefault("BB_TOKEN", "test-token")
os.environ.setdefault("ARGOCD_PASS", "test-pass")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as dp  # noqa: E402
import identity  # noqa: E402
from app_meta import _extract_comment_sha, _extract_status_token  # noqa: E402
from test_coverage_orchestration import world, _mk_pr, BASE_SHA  # noqa: E402,F401

AEC = "gcp/aec/private-cloud/na1-b"
USB = f"{AEC}/pv-usbank-c/customer.yaml"
USB_BAD = ("appspace:\n  customerName: usbank\n  suffix: c\n  instanceName: pv-usbank-c\n"
           "  infra:\n    deployLinuxServicesK8s:\n      svc:\n"
           "        instanceName: pv-usbank-svc-c\n")
USB_FIXED = f"{AEC}/pv-usbank--aec1-c/customer.yaml"
USB_GOOD = ("appspace:\n  customerName: usbank--aec1\n  suffix: c\n"
            "  instanceName: pv-usbank--aec1-c\n  infra:\n    deployLinuxServicesK8s:\n"
            "      svc:\n        instanceName: pv-usbank--aec1-svc-c\n")


def _misses(path, body):
    return identity._partition_token_misses(path, yaml.safe_load(body))[1]


# --- which paths are clones, and which token they need ----------------------

@pytest.mark.parametrize("path,tok", [
    (USB, "--aec1"),
    ("azure/aec/private-cloud/na1-a/pv-atlas-a/customer.yaml", "--aec1"),
    (f"{AEC}/monthly/pv-x--aec2-a/customer.yaml", "--aec2"),
    (f"{AEC}/pv-x--stage1-a/customer.yaml", "--aec1"),
    (f"{AEC}/pv-x--sbx2-a/customer.yaml", "--aec1"),
    ("gcp/prod/private-cloud/au1-b/sandbox/pv-ecudev--sbx1-a/customer.yaml", "--sbx1"),
    ("gcp/prod/private-cloud/au1-b/sandbox/pv-ecudev-a/customer.yaml", "--sbx1"),
    ("gcp/prod/private-cloud/eu1-b/weekly/pv-haleon--aec1-a/customer.yaml", "--aec1"),
    (f"{AEC}/monthly/config.yaml", "--aec1"),
    # Not clones: production, and a real name that merely contains "-aec".
    ("gcp/prod/private-cloud/na1-b/pv-nbc-b/customer.yaml", None),
    ("gcp/prod/private-cloud/sa1-a/monthly/pv-sami-aec-a/customer.yaml", None),
    ("azure/stage/private-cloud/na1-a/custom/pv-corporate--stage1-a/customer.yaml", None),
    ("gcp/dev/private-cloud/ap1/custom/pv-orch-a/customer.yaml", None),
    ("customer.yaml", None),
])
def test_expected_token(path, tok):
    assert identity._expected_partition_token(path) == tok


# --- what is flagged, and the fix suggested ---------------------------------

def test_the_nbc_incident_is_flagged():
    path = "gcp/aec/private-cloud/na2-a/monthly/pv-nbc--aec1-a/customer.yaml"
    body = ("appspace:\n  customerName: nbc\n  suffix: a\n  instanceName: pv-nbc--aec1-a\n"
            "  infra:\n    deployLinuxServicesK8s:\n      svc:\n"
            "        instanceName: pv-nbc--aec1-svc-a\n")
    assert _misses(path, body) == [("customerName", "nbc", "nbc--aec1")]


@pytest.mark.parametrize("path,body,expected", [
    # bb71a62b8 (PR #830): production's pv-heb-a moved under aec/.
    ("gcp/aec/private-cloud/na2-a/pv-heb-a/customer.yaml",
     "appspace:\n  customerName: heb--aec1\n  instanceName: pv-heb-a\n",
     [("folder", "pv-heb-a", "pv-heb--aec1-a"), ("instanceName", "pv-heb-a", "pv-heb--aec1-a")]),
    # a7e35d012: a new clone with production's names.
    ("gcp/aec/private-cloud/na2-a/weekly/pv-blackrock-a/customer.yaml",
     "appspace:\n  customerName: blackrock\n  instanceName: pv-blackrock-a\n"
     "  infra:\n    deployLinuxServicesK8s:\n      svc:\n        enabled: true\n",
     [("folder", "pv-blackrock-a", "pv-blackrock--aec1-a"),
      ("customerName", "blackrock", "blackrock--aec1"),
      ("instanceName", "pv-blackrock-a", "pv-blackrock--aec1-a")]),
])
def test_past_incidents_are_flagged(path, body, expected):
    assert _misses(path, body) == expected


def test_a_production_copy_with_no_token_anywhere():
    assert _misses(USB, USB_BAD) == [
        ("folder", "pv-usbank-c", "pv-usbank--aec1-c"),
        ("customerName", "usbank", "usbank--aec1"),
        ("instanceName", "pv-usbank-c", "pv-usbank--aec1-c"),
        ("svc.instanceName", "pv-usbank-svc-c", "pv-usbank--aec1-svc-c"),
    ]


def test_mongo_and_rabbit_vm_names_need_the_token():
    # KCC/ASO name the VMs straight from these lists: production's pv-bnym-mongo1-a
    # runs in the same project and zone as the clone.
    path = "gcp/aec/private-cloud/na2-a/monthly/pv-bnym--aec1-a/customer.yaml"
    body = ("appspace:\n  customerName: bnym--aec1\n  infra:\n    deployLinuxServicesK8s:\n"
            "      mongo:\n        instances:\n          - name: pv-bnym-mongo1-a\n"
            "            internalIpAddress: 10.128.2.39\n          - name: pv-bnym--aec1-mongo2-a\n"
            "      rabbit:\n        instances:\n          - pv-bnym-rab1-a\n          - {}\n          - null\n")
    assert _misses(path, body) == [
        ("mongo.instances", "pv-bnym-mongo1-a", "pv-bnym--aec1-mongo1-a"),
        ("rabbit.instances", "pv-bnym-rab1-a", "pv-bnym--aec1-rab1-a"),
    ]


def test_a_correct_clone_is_clean():
    assert _misses(USB_FIXED, USB_GOOD) == []


def test_subdomain_and_ping_host_need_the_token_too():
    path = f"{AEC}/pv-hsbc--aec1-a/customer.yaml"
    body = ("appspace:\n  customerName: hsbc--aec1\n  customerSubdomain: hsbc\n"
            "  microservices:\n    acmePingScaler:\n"
            "      pingHost: pv-hsbc-a-rabbit-private.cloud.appspace.com\n")
    assert _misses(path, body) == [
        ("customerSubdomain", "hsbc", "hsbc--aec1"),
        ("pingHost", "pv-hsbc-a-rabbit-private.cloud.appspace.com",
         "pv-hsbc--aec1-a-rabbit-private.cloud.appspace.com"),
    ]


def test_near_miss_tokens_are_corrected_not_doubled():
    gsk = f"{AEC}/custom/pv-gsk--aec1-a/customer.yaml"
    assert _misses(gsk, "appspace:\n  customerName: gsk--aec1\n  instanceName: pv-gsk-aec1-a\n") == [
        ("instanceName", "pv-gsk-aec1-a", "pv-gsk--aec1-a")]
    ecu = "gcp/prod/private-cloud/au1-b/sandbox/pv-ecudev--sbx1-a/customer.yaml"
    assert _misses(ecu, "appspace:\n  customerName: ecudev--sbx\n") == [
        ("customerName", "ecudev--sbx", "ecudev--sbx1")]
    dlt = f"{AEC}/pv-deloitte-aec1-a/customer.yaml"
    assert _misses(dlt, "appspace:\n  customerName: deloitte--aec1\n") == [
        ("folder", "pv-deloitte-aec1-a", "pv-deloitte--aec1-a")]


def test_a_real_name_containing_aec_is_kept():
    path = f"{AEC}/pv-sami-aec--aec1-a/customer.yaml"
    body = "appspace:\n  customerName: sami-aec\n  instanceName: pv-sami-aec-a\n"
    assert _misses(path, body) == [
        ("customerName", "sami-aec", "sami-aec--aec1"),
        ("instanceName", "pv-sami-aec-a", "pv-sami-aec--aec1-a"),
    ]


def test_the_bnym_incident_is_flagged():
    # 45dd45cfb: the subdomain was production's; `cloud-bnym` has no safe guess.
    path = "gcp/aec/private-cloud/na2-a/pv-bnym-b/customer.yaml"
    body = "appspace:\n  customerName: bnym\n  customerSubdomain: bny\n  instanceName: cloud-bnym\n"
    assert _misses(path, body) == [
        ("folder", "pv-bnym-b", "pv-bnym--aec1-b"),
        ("customerName", "bnym", "bnym--aec1"),
        ("customerSubdomain", "bny", "bny--aec1"),
        ("instanceName", "cloud-bnym", None),
    ]


def test_the_other_tier_token_is_replaced_not_kept():
    # A sandbox clone copied from the AEC one, and the reverse.
    sbx = "gcp/prod/private-cloud/na3-a/sandbox/pv-merck--sbx1-b/customer.yaml"
    body = ("appspace:\n  customerName: merck--aec1\n  instanceName: pv-merck--aec1-b\n"
            "  infra:\n    deployLinuxServicesK8s:\n      svc:\n"
            "        instanceName: pv-merck--aec1-svc-b\n")
    assert _misses(sbx, body) == [
        ("customerName", "merck--aec1", "merck--sbx1"),
        ("instanceName", "pv-merck--aec1-b", "pv-merck--sbx1-b"),
        ("svc.instanceName", "pv-merck--aec1-svc-b", "pv-merck--sbx1-svc-b"),
    ]
    aec = f"{AEC}/pv-gsk--sbx1-a/customer.yaml"
    assert _misses(aec, "appspace:\n  customerName: gsk--sbx1\n") == [
        ("folder", "pv-gsk--sbx1-a", "pv-gsk--aec1-a"),
        ("customerName", "gsk--sbx1", "gsk--aec1"),
    ]
    stage = f"{AEC}/pv-x--stage1-a/customer.yaml"
    assert _misses(stage, "appspace:\n  customerName: x--aec1\n") == [
        ("folder", "pv-x--stage1-a", "pv-x--aec1-a")]


def test_a_name_too_long_for_the_token_gets_no_other_suggestion():
    # The other names follow the new, shorter name, which is not known yet.
    path = f"{AEC}/pv-westinghousenuclear-a/customer.yaml"
    body = "appspace:\n  customerName: westinghousenuclear\n  instanceName: pv-westinghousenuclear-a\n"
    assert _misses(path, body) == [
        ("folder", "pv-westinghousenuclear-a", None),
        ("customerName", "westinghousenuclear", "westinghousenuclear--aec1"),
        ("instanceName", "pv-westinghousenuclear-a", None),
    ]


@pytest.mark.parametrize("name,shorten", [("abcdefghijklmn", False), ("abcdefghijklmno", True)])
def test_the_shorten_line_starts_above_14_characters(name, shorten):
    path = f"{AEC}/pv-{name}-a/customer.yaml"
    hits = [(path, "--aec1", _misses(path, f"appspace:\n  customerName: {name}\n"))]
    _, body = dp._partition_token_block(hits, "aabbccddeeff", BASE_SHA)
    assert ("shorten the customer name" in body) is shorten
    assert (f"`{name}--aec1`" in body) is not shorten


def test_a_near_miss_before_a_dot_is_not_doubled():
    path = f"{AEC}/pv-hsbc--aec1-a/customer.yaml"
    body = ("appspace:\n  customerName: hsbc--aec1\n  microservices:\n    acmePingScaler:\n"
            "      pingHost: pv-hsbc-aec1.cloud.appspace.com\n")
    assert _misses(path, body) == [("pingHost", "pv-hsbc-aec1.cloud.appspace.com", None)]


def test_without_customer_name_the_folder_gives_the_stem():
    path = "gcp/aec/private-cloud/na2-a/pv-bnym-b/customer.yaml"
    assert _misses(path, "appspace:\n  instanceName: pv-bnym-b\n") == [
        ("folder", "pv-bnym-b", "pv-bnym--aec1-b"),
        ("instanceName", "pv-bnym-b", "pv-bnym--aec1-b"),
    ]


def test_no_suggestion_when_the_name_does_not_contain_the_stem():
    path = f"{AEC}/pv-acme--aec1-a/customer.yaml"
    assert _misses(path, "appspace:\n  customerName: acme--aec1\n  instanceName: pv-other-a\n") == [
        ("instanceName", "pv-other-a", None)]


def test_cohort_config_is_checked_only_for_names_it_sets():
    path = f"{AEC}/monthly/config.yaml"
    assert _misses(path, "appspace:\n  version: 2603.2.19\n") == []
    assert _misses(path, "appspace:\n  customerName: x\n") == [("customerName", "x", "x--aec1")]


@pytest.mark.parametrize("body", [
    "", "- a\n", "appspace:\n", "appspace: []\n",
    "appspace:\n  customerName: ''\n  instanceName:\n  customerSubdomain: {a: 1}\n",
    "appspace:\n  customerSubdomain: false\n  instanceName: 0\n",
    "appspace.customerName: nbc\n",  # helm does not read a dotted key as nested
])
def test_absent_or_empty_names_are_not_flagged(body):
    assert _misses(f"{AEC}/pv-nbc--aec1-a/customer.yaml", body) == []


def test_a_dotted_key_does_not_hide_the_nested_one():
    body = "appspace.customerName: nbc--aec1\nappspace:\n  customerName: nbc\n"
    assert _misses(f"{AEC}/pv-nbc--aec1-a/customer.yaml", body) == [
        ("customerName", "nbc", "nbc--aec1")]


def test_a_numeric_name_is_read_as_text():
    assert _misses(f"{AEC}/pv-3ds--aec1-a/customer.yaml", "appspace:\n  customerName: 3\n") == [
        ("customerName", "3", "3--aec1")]


def test_a_non_clone_path_is_never_checked():
    assert identity._partition_token_misses(
        "gcp/prod/private-cloud/na1-b/pv-nbc-b/customer.yaml",
        {"appspace": {"customerName": "nbc"}}) == (None, [])


_ROOTS = ("gcp/aec/private-cloud/na2-a", "azure/aec/private-cloud/na1-a",
          "gcp/prod/private-cloud/au1-b/sandbox", "gcp/prod/private-cloud/eu1-b/weekly")
_TOKS = st.sampled_from(("", "--aec1", "-aec1", "--aec", "--sbx1", "-sbx1", "--aec2"))
_JUNK = st.one_of(st.none(), st.integers(), st.booleans(), st.text(max_size=12),
                  st.lists(st.text(max_size=8), max_size=3))


@st.composite
def _clone_file(draw):
    """A clone customer.yaml whose names carry random, missing or wrong tokens."""
    stem = draw(st.from_regex(r"[a-z][a-z0-9]{0,8}(-[a-z0-9]{1,5})?", fullmatch=True))
    sfx = draw(st.sampled_from("abc"))
    root = draw(st.sampled_from(_ROOTS))
    folder_tok = draw(st.sampled_from(("--aec1", "--sbx1", "--aec2")) if root.endswith("weekly") else _TOKS)
    app = {
        "customerName": stem + draw(_TOKS),
        "instanceName": f"pv-{stem}{draw(_TOKS)}-{sfx}",
        "infra": {"deployLinuxServicesK8s": {
            "svc": {"instanceName": f"pv-{stem}{draw(_TOKS)}-svc-{sfx}"},
            "mongo": {"instances": [{"name": f"pv-{stem}{draw(_TOKS)}-mongo1-{sfx}"}]}}},
        "microservices": {"acmePingScaler": {
            "pingHost": f"pv-{stem}{draw(_TOKS)}-{sfx}-rabbit-private.cloud.appspace.com"}},
    }
    if draw(st.booleans()):
        app[draw(st.sampled_from(("customerSubdomain", "instanceName")))] = draw(_JUNK)
    return f"{root}/pv-{stem}{folder_tok}-{sfx}/customer.yaml", {"appspace": app}


def _apply(path, doc, misses):
    """The file after the author copies every suggestion."""
    new = {v: n for f, v, n in misses if f != "folder"}

    def swap(node):
        if isinstance(node, dict):
            return {k: swap(v) for k, v in node.items()}
        if isinstance(node, list):
            return [swap(v) for v in node]
        return new.get(str(node), node) if isinstance(node, (str, int)) else node

    folder = path.split("/")[-2]
    fixed = dict((v, n) for f, v, n in misses if f == "folder").get(folder, folder)
    return path.replace(f"/{folder}/", f"/{fixed}/"), swap(doc)


@settings(deadline=None, max_examples=1000)
@given(_clone_file())
# Found by this test: Helm reads `false` as unset, so it is not a name to fix.
@example(("gcp/aec/private-cloud/na2-a/pv-a-a/customer.yaml",
          {"appspace": {"customerName": "a", "customerSubdomain": False}}))
def test_property_suggestions_carry_the_right_token_and_converge(case):
    path, doc = case
    tok, misses = identity._partition_token_misses(path, doc)
    other = "--sbx" if tok.startswith("--aec") else "--aec"
    assert all(n is None or (tok in n and other not in n) for _, _, n in misses), misses
    if misses and all(n for _, _, n in misses):
        assert identity._partition_token_misses(*_apply(path, doc, misses))[1] == []


# --- reading the changed files ----------------------------------------------

def _fetch(files, calls=None):
    def fake(path, sha, repo=None):
        if calls is not None:
            calls.append(path)
        return files.get(path, (None, dp.BB_NOT_FOUND))
    return fake


def test_detector_reads_only_clone_value_files(monkeypatch):
    calls = []
    monkeypatch.setattr(dp, "_bb_fetch_cached", _fetch({USB: (USB_BAD, dp.BB_OK)}, calls))
    hits = dp._detect_partition_token_misses(
        [USB, f"{AEC}/pv-usbank-c/README.md",
         "gcp/prod/private-cloud/na1-b/pv-nbc-b/customer.yaml"], "sha1")
    assert calls == [USB]
    assert [(p, t) for p, t, _ in hits] == [(USB, "--aec1")]


def test_detector_checks_cicd_versions_too(monkeypatch):
    # It is the last values file, so a name set there wins the merge.
    cicd = f"{AEC}/pv-usbank--aec1-c/cicd-versions.yaml"
    monkeypatch.setattr(dp, "_bb_fetch_cached", _fetch({cicd: ("appspace:\n  instanceName: pv-usbank-c\n", dp.BB_OK)}))
    assert dp._detect_partition_token_misses([cicd], "sha1") == [
        (cicd, "--aec1", [("instanceName", "pv-usbank-c", "pv-usbank--aec1-c")])]


def test_detector_skips_the_old_side_of_a_move(monkeypatch):
    monkeypatch.setattr(dp, "_bb_fetch_cached", _fetch({USB_FIXED: (USB_GOOD, dp.BB_OK)}))
    assert dp._detect_partition_token_misses([USB, USB_FIXED], "sha1") == []


def test_detector_skips_malformed_yaml(monkeypatch):
    monkeypatch.setattr(dp, "_bb_fetch_cached", _fetch({USB: ("appspace: [\n", dp.BB_OK)}))
    assert dp._detect_partition_token_misses([USB], "sha1") == []


def test_an_unreadable_file_is_retried_never_passed(monkeypatch):
    monkeypatch.setattr(dp, "_bb_fetch_cached", _fetch({USB: (None, dp.BB_ERROR)}))
    with pytest.raises(dp.ValueFileUnreadable) as exc:
        dp._detect_partition_token_misses([USB], "sha1234567")
    assert dp._is_transient_exception(exc.value)
    assert USB in str(exc.value)


@pytest.mark.parametrize("repo", ["acme-config-prod", "acme-config-stage", "acme-config-dev"])
def test_current_main_of_the_config_repos_has_no_hits(repo):
    root = os.path.expanduser(f"~/gitprojects/{repo}")
    if not os.path.isdir(os.path.join(root, ".git")):
        pytest.skip(f"{repo} not checked out")

    def git(*args):
        return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True).stdout

    paths = [p for p in git("ls-tree", "-r", "--name-only", "origin/main").splitlines()
             if p.endswith((".yaml", ".yml")) and identity._expected_partition_token(p)]
    hits = {}
    for p in paths:
        misses = identity._partition_token_misses(p, yaml.safe_load(git("show", f"origin/main:{p}")))[1]
        if misses:
            hits[p] = misses
    assert hits == {}
    assert paths or repo != "acme-config-prod", "prod has clones: the scan read nothing"


# --- the message --------------------------------------------------------------

def test_blocked_comment_tells_what_to_fix():
    hits = [(USB, "--aec1", identity._partition_token_misses(USB, yaml.safe_load(USB_BAD))[1])]
    desc, body = dp._partition_token_block(hits, "aabbccddeeff", BASE_SHA)
    assert desc == ("BLOCKED: pv-usbank-c needs --aec1 in folder, customerName, instanceName, "
                    "svc.instanceName \u2014 fix and push")
    for line in ("\u26d4 **Blocked: this clone uses production names.**",
                 f"`{USB}`",
                 "- folder: `pv-usbank-c` \u2192 `pv-usbank--aec1-c`",
                 "- `customerName`: `usbank` \u2192 `usbank--aec1`",
                 "- `instanceName`: `pv-usbank-c` \u2192 `pv-usbank--aec1-c`",
                 "- `svc.instanceName`: `pv-usbank-svc-c` \u2192 `pv-usbank--aec1-svc-c`",
                 f"git mv {AEC}/pv-usbank-c {AEC}/pv-usbank--aec1-c",
                 ("Fix these, commit, and push again. This check runs again by itself "
                  "on the new commit."),
                 "**Status:** \u26d4 Blocked \u2014 clone names miss `--aec1`"):
        assert line in body.splitlines(), line
    assert _extract_status_token(body) == "blocked"
    assert _extract_comment_sha(body) == "aabbccdd"
    assert body.rstrip().endswith(f"[blocked] [base:{BASE_SHA[:8]}]*")
    # Never the one render error the new-env path turns green.
    assert "missing required value" not in body.lower()


def test_a_name_too_long_for_the_token_says_to_shorten_it():
    path = f"{AEC}/pv-westinghousenuclear--aec1-a/customer.yaml"
    hits = [(path, "--aec1", [("customerName", "westinghousenuclear", "westinghousenuclear--aec1")])]
    _, body = dp._partition_token_block(hits, "aabbccddeeff", "")
    assert ("- `customerName`: `westinghousenuclear` \u2192 shorten the customer name "
            "to 14 characters or less, then add `--aec1`") in body.splitlines()
    assert "[base:" not in body


def test_without_a_suggestion_it_says_where_the_token_goes():
    path = f"{AEC}/pv-acme--aec1-a/customer.yaml"
    sbx = "gcp/prod/private-cloud/au1-b/sandbox/pv-ecudev--sbx1-a/customer.yaml"
    hits = [(path, "--aec1", [("instanceName", "pv-other-a", None)]),
            (sbx, "--sbx1", [("folder", "pv-odd", None)])]
    desc, body = dp._partition_token_block(hits, "aabbccddeeff", BASE_SHA)
    assert "- `instanceName`: `pv-other-a` \u2192 add `--aec1` after the customer name" in body
    assert "- folder: `pv-odd` \u2192 add `--sbx1` after the customer name" in body
    assert "git mv" not in body
    assert desc == ("BLOCKED: pv-acme--aec1-a, pv-ecudev--sbx1-a needs --aec1 or --sbx1 "
                    "in instanceName, folder \u2014 fix and push")
    assert "Without `--aec1` or `--sbx1` it can reuse" in body


def test_long_values_are_cut_in_the_comment():
    host = "pv-acme-a-" + "x" * 300
    hits = [(f"{AEC}/pv-acme--aec1-a/customer.yaml", "--aec1", [("pingHost", host, None)])]
    _, body = dp._partition_token_block(hits, "aabbccddeeff", BASE_SHA)
    line = next(ln for ln in body.splitlines() if ln.startswith("- `pingHost`"))
    assert f"`{host[:99]}\u2026`" in line and len(line) < 200


def test_many_environments_fit_the_status_and_the_comment():
    hits = [(f"{AEC}/monthly/pv-customer{i:03d}-long-name-a/customer.yaml", "--aec1",
             [("folder", f"pv-customer{i:03d}-long-name-a", f"pv-customer{i:03d}-long-name--aec1-a"),
              ("customerName", f"customer{i:03d}-long-name", f"customer{i:03d}-long-name--aec1")])
            for i in range(40)]
    desc, body = dp._partition_token_block(hits, "aabbccddeeff", BASE_SHA)
    assert desc == ("BLOCKED: pv-customer000-long-name-a, pv-customer001-long-name-a (+38 more) "
                    "needs --aec1 in folder, customerName \u2014 fix and push")
    wide = [(f"{AEC}/{'p' * 300}{i}/customer.yaml", "--aec1", [("folder", f"{'p' * 300}{i}", None)])
            for i in range(3)]
    wide_desc, _ = dp._partition_token_block(wide, "aabbccddeeff", "")
    assert len(wide_desc) == 255 and wide_desc.endswith("\u2014 fix and push")
    assert len(body.encode()) < dp.MAX_COMMENT_BYTES
    assert dp._truncate_comment(body) == body


# --- end to end through process_pr ------------------------------------------

@pytest.fixture()
def clone_pr(world, monkeypatch):
    """A PR world whose comment persists between runs, like Bitbucket's."""
    sinks, plan = world
    store = {"ids": []}

    def upsert(pr_id, body, existing_id=None, repo=None, **kw):
        store["ids"].append(existing_id)
        store["id"], store["body"] = existing_id or 123, body
        sinks.upserts.append(body)
        return store["id"]

    def find(pr_id, repo=None):
        b = store.get("body")
        return (store["id"], _extract_comment_sha(b), b) if b else (None, "", "")

    files = {}
    monkeypatch.setattr(dp, "upsert_comment", upsert)
    monkeypatch.setattr(dp, "find_existing_comment", find)
    monkeypatch.setattr(dp, "_bb_fetch_status",
                        lambda path, sha, repo=None: files.get(path, (None, dp.BB_NOT_FOUND)))
    monkeypatch.setattr(dp, "_vf_cache", {})
    monkeypatch.setattr(dp, "_retry_backoff", {})
    path_map = {p: ["pv-orch-a-ms", "pv-orch-a-ss"] for p in (USB, USB_FIXED)}
    return sinks, store, files, path_map


def _changed(monkeypatch, paths, renames=None):
    monkeypatch.setattr(dp, "get_pr_changed_files", lambda pr_id, repo=None: (paths, renames or {}))


def test_process_pr_blocks_before_any_diff(clone_pr, monkeypatch):
    sinks, store, files, path_map = clone_pr
    files[USB] = (USB_BAD, dp.BB_OK)
    _changed(monkeypatch, [USB])
    dp.process_pr(_mk_pr(pr_id=4671), path_map, base_sha=BASE_SHA)
    state, desc = sinks.statuses[-1]
    assert (state, desc) == ("FAILED", "BLOCKED: pv-usbank-c needs --aec1 in folder, customerName, "
                                       "instanceName, svc.instanceName \u2014 fix and push")
    assert "[blocked]" in store["body"] and "pv-usbank--aec1-c" in store["body"]
    assert sinks.diff_calls == [], "a blocked clone must not be rendered"
    assert list(dp._seen.values()) == [(_mk_pr()["source"]["commit"]["hash"], BASE_SHA)]


def test_process_pr_blocks_a_new_environment_before_the_new_env_logic(clone_pr, monkeypatch):
    # 4 of the 5 incidents were new environments: the new-env path must not decide.
    sinks, store, files, path_map = clone_pr
    files[USB] = (USB_BAD, dp.BB_OK)
    _changed(monkeypatch, [USB])
    dp.process_pr(_mk_pr(pr_id=4671), {}, base_sha=BASE_SHA)
    assert sinks.statuses == [("FAILED", "BLOCKED: pv-usbank-c needs --aec1 in folder, customerName, "
                                         "instanceName, svc.instanceName \u2014 fix and push")]
    assert "this clone uses production names" in store["body"]


def test_process_pr_moving_production_into_aec_is_blocked(clone_pr, monkeypatch):
    # The move leaves the prod path in `changed`; at the PR head it is a 404.
    sinks, store, files, path_map = clone_pr
    prod = "gcp/prod/private-cloud/na1-b/pv-usbank-c/customer.yaml"
    files[USB] = (USB_BAD, dp.BB_OK)
    _changed(monkeypatch, [prod, USB], {prod: USB})
    dp.process_pr(_mk_pr(pr_id=4671), path_map, base_sha=BASE_SHA)
    assert sinks.statuses[-1][0] == "FAILED"
    assert f"`{USB}`" in store["body"] and prod not in store["body"]


def test_process_pr_a_folder_only_rename_is_blocked(clone_pr, monkeypatch):
    # The names keep the token, but the folder loses it (blackrock shape).
    sinks, store, files, path_map = clone_pr
    old = f"{AEC}/pv-blackrock--aec1-a/customer.yaml"
    new = f"{AEC}/pv-blackrock-a/customer.yaml"
    files[new] = ("appspace:\n  customerName: blackrock--aec1\n  instanceName: pv-blackrock--aec1-a\n",
                  dp.BB_OK)
    _changed(monkeypatch, [old, new], {old: new})
    dp.process_pr(_mk_pr(pr_id=4671), path_map, base_sha=BASE_SHA)
    assert sinks.statuses[-1][0] == "FAILED"
    assert "- folder: `pv-blackrock-a` \u2192 `pv-blackrock--aec1-a`" in store["body"]
    assert f"git mv {AEC}/pv-blackrock-a {AEC}/pv-blackrock--aec1-a" in store["body"]
    assert "`customerName`" not in store["body"]


def test_process_pr_reads_the_merge_preview_not_the_branch(clone_pr, monkeypatch):
    # The guard judges main + PR: only the merge preview has the bad name here.
    sinks, store, files, path_map = clone_pr
    merged = "f00d" * 10
    monkeypatch.setattr(dp, "_merge_preview", lambda repo, base, pr: (merged, []))
    monkeypatch.setattr(dp, "_bb_fetch_status", lambda path, sha, repo=None: (
        (USB_BAD if sha == merged else USB_GOOD, dp.BB_OK) if path == USB_FIXED else (None, dp.BB_NOT_FOUND)))
    _changed(monkeypatch, [USB_FIXED])
    dp.process_pr(_mk_pr(pr_id=4671), path_map, base_sha=BASE_SHA)
    assert sinks.statuses[-1][0] == "FAILED" and "`customerName`: `usbank`" in store["body"]


def test_process_pr_unreadable_file_is_red_and_retried(clone_pr, monkeypatch):
    sinks, store, files, path_map = clone_pr
    files[USB] = (None, dp.BB_ERROR)
    _changed(monkeypatch, [USB])
    dp.process_pr(_mk_pr(pr_id=4671), path_map, base_sha=BASE_SHA)
    state, desc = sinks.statuses[-1]
    assert state == "FAILED" and desc.startswith("Diff unavailable (infrastructure) - will retry")
    assert _extract_status_token(store["body"]) == "transient"
    assert not dp._seen, "a transient failure must stay unseen so it retries"
    # COPS-2546: the retry is spaced out, not repeated every iteration.
    assert [bo[2] for bo in dp._retry_backoff.values()] == [_mk_pr()["source"]["commit"]["hash"]]
    posted = len(sinks.statuses)
    dp.process_pr(_mk_pr(pr_id=4671), path_map, base_sha=BASE_SHA)
    assert len(sinks.statuses) == posted
    # Once Bitbucket answers, the same commit is checked and the comment edited.
    dp._retry_backoff.clear()
    files[USB] = (USB_BAD, dp.BB_OK)
    dp.process_pr(_mk_pr(pr_id=4671), path_map, base_sha=BASE_SHA)
    assert _extract_status_token(store["body"]) == "blocked" and store["ids"] == [None, 123]


def test_process_pr_rechecks_by_itself_after_the_fix_push(clone_pr, monkeypatch):
    sinks, store, files, path_map = clone_pr
    files[USB] = (USB_BAD, dp.BB_OK)
    _changed(monkeypatch, [USB])
    pr = _mk_pr(pr_id=4671)
    dp.process_pr(pr, path_map, base_sha=BASE_SHA)
    assert sinks.statuses[-1][0] == "FAILED"
    posted = len(sinks.upserts)

    # A pod restart on the same commit keeps the verdict: nothing re-posted.
    dp._seen.clear()
    statuses = len(sinks.statuses)
    dp.process_pr(pr, path_map, base_sha=BASE_SHA)
    assert len(sinks.upserts) == posted and len(sinks.statuses) == statuses

    # A partial fix is a new commit: checked again, still blocked, same comment.
    files.clear()
    files[USB] = (USB_BAD.replace("customerName: usbank", "customerName: usbank--aec1"), dp.BB_OK)
    pr2 = _mk_pr(pr_id=4671)
    pr2["source"]["commit"]["hash"] = "0011223344ff"
    dp.process_pr(pr2, path_map, base_sha=BASE_SHA)
    assert sinks.statuses[-1][0] == "FAILED"
    assert "`customerName`" not in store["body"] and "`instanceName`" in store["body"]
    assert _extract_comment_sha(store["body"]) == "00112233"

    # The full fix (folder renamed, names fixed): the block goes away.
    files.clear()
    files[USB_FIXED] = (USB_GOOD, dp.BB_OK)
    files[f"{AEC}/config.yaml"] = ("appspace:\n  version: 2603.2.19\n", dp.BB_OK)  # COPS-2552
    _changed(monkeypatch, [USB, USB_FIXED], {USB: USB_FIXED})
    pr3 = _mk_pr(pr_id=4671)
    pr3["source"]["commit"]["hash"] = "99887766aabb"
    dp.process_pr(pr3, path_map, base_sha=BASE_SHA)
    assert sinks.statuses[-1][0] == "SUCCESSFUL"
    assert _extract_status_token(store["body"]) == "clean"
    assert store["ids"][1:] == [123, 123], "the same comment is edited in place"
