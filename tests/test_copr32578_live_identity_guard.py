"""COPR-32578: block a PR that renames a live private-cloud environment.

acme-config-prod PR #4672 changed `customerName: nbc` to `nbc--aec1` on a live
env. The ApplicationSet names apps and namespace `pv-<customerName>-<suffix>`,
so it made new apps in a new namespace and deleted the old ones without
pruning; the old namespace kept managing shared GCP objects (COPR-32565).
A planned rename passes only with a `Confirm-Rename:` commit and auto-sync
paused on main first.
"""
import os
import subprocess
import sys

import pytest
from hypothesis import given, settings, strategies as st

os.environ.setdefault("BB_USER", "test-user")
os.environ.setdefault("BB_TOKEN", "test-token")
os.environ.setdefault("ARGOCD_PASS", "test-pass")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as dp  # noqa: E402
import identity  # noqa: E402
from app_meta import _extract_comment_sha, _extract_status_token  # noqa: E402
from test_coverage_orchestration import world, _mk_pr, BASE_SHA, PR_SHA  # noqa: E402,F401

HEAD = "head0001"
BASE = "base0001"
ERR = object()

AEC = "gcp/aec/private-cloud/na2-a/monthly"
NBC = f"{AEC}/pv-nbc--aec1-a/customer.yaml"
NBC_APPS = ["pv-nbc-a-glb", "pv-nbc-a-ms", "pv-nbc-a-ss"]
COHORT = f"{AEC}/config.yaml"
COHORT_BODY = "appspace:\n  version: 2603.2.19\n"
NBC_MAIN = "appspace:\n  customerName: nbc\n  instanceName: pv-nbc-a\n  version: 2603.1.38\n"
NBC_PR = NBC_MAIN.replace("customerName: nbc", "customerName: nbc--aec1")


def _doc(**appspace):
    return {"appspace": appspace}


# --- the identity the ApplicationSet computes ---------------------------------

@pytest.mark.parametrize("customer,cohort,expected", [
    (_doc(customerName="nbc"), None, ("nbc", "a")),
    (_doc(customerName="nbc", suffix="b"), None, ("nbc", "b")),
    (_doc(customerName="nbc"), _doc(suffix="c"), ("nbc", "c")),
    # mergo WithOverride: a key present in customer.yaml wins, even empty.
    (_doc(customerName="nbc", suffix=""), _doc(suffix="c"), ("nbc", "a")),
    (_doc(customerName="nbc", suffix=None), _doc(suffix="c"), ("nbc", "a")),
    (_doc(suffix="b"), _doc(customerName="x"), ("x", "b")),
    ({}, None, ("<no value>", "a")),
    ({"appspace": ["not", "a", "map"]}, None, ("<no value>", "a")),
    (_doc(customerName="nbc", suffix=1), None, ("nbc", "1")),
    (_doc(customerName=True), None, ("true", "a")),
])
def test_identity_is_what_the_applicationset_computes(customer, cohort, expected):
    assert identity._appset_identity(customer, cohort) == expected


@pytest.mark.parametrize("message,expected", [
    ("Confirm-Rename: pv-nbc-a -> pv-nbc--aec1-a", {("pv-nbc-a", "pv-nbc--aec1-a")}),
    ("Rename NBC\n\nconfirm-rename:pv-a-a->pv-b-a\n", {("pv-a-a", "pv-b-a")}),
    ("  * Confirm-Rename: `pv-a-a` → `pv-b-a`", {("pv-a-a", "pv-b-a")}),
    ("Confirm-Rename: na4-a/pv-x-a -> na3-a/pv-x-a", {("na4-a/pv-x-a", "na3-a/pv-x-a")}),
    ("Confirm-Rename: pv-a-a", set()),
    ("Please Confirm-Rename: pv-a-a -> pv-b-a", set()),
    ("Confirm-Rename: pv-a-a -> pv-b-a now", set()),
    ("Confirm-Rename: pv-a-a -> pv-b-a.", {("pv-a-a", "pv-b-a")}),
    (None, set()),
])
def test_confirm_rename_lines(message, expected):
    assert identity._confirmed_renames([message]) == expected


_NS = st.from_regex(r"\A(na[0-9]-[ab]/)?pv-[a-z0-9]+(--(aec|sbx)[0-9])?-[a-z]\Z")


@settings(max_examples=300, deadline=None)
@given(old=_NS, new=_NS)
def test_property_the_command_in_the_comment_confirms_its_own_pair(old, new):
    hit = {"old_id": old, "new_id": new}
    cmd = [l for l in dp._identity_before_merge_steps([{**_hit(), **hit}]) if "Confirm-Rename" in l]
    message = cmd[0].split('-m "', 1)[1].rsplit('"', 1)[0]
    assert identity._confirmed_renames([message]) == {(old, new)}


_YAML = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=8),
    lambda kids: st.lists(kids, max_size=3) | st.dictionaries(st.text(max_size=12), kids, max_size=4),
    max_leaves=12)


@settings(max_examples=300, deadline=None)
@given(customer=_YAML, cohort=_YAML, key=st.sampled_from(["customerName", "suffix", "autosync"]))
def test_property_the_identity_reader_never_raises(customer, cohort, key):
    doc = {"appspace": {key: customer}} if isinstance(customer, (str, int)) else customer
    cn, sfx = identity._appset_identity(doc, cohort)
    assert isinstance(cn, str) and isinstance(sfx, str) and sfx


# --- the detector ---------------------------------------------------------------

@pytest.fixture()
def repo(monkeypatch):
    """base/head file maps; a missing path is a 404, ERR a Bitbucket error."""
    base, head, calls = {COHORT: COHORT_BODY}, {COHORT: COHORT_BODY}, []

    def fake(path, sha, repo=None):
        calls.append(path)
        v = (base if sha == BASE else head).get(path)
        if v is None:
            return None, dp.BB_NOT_FOUND
        return (None, dp.BB_ERROR) if v is ERR else (v, dp.BB_OK)
    monkeypatch.setattr(dp, "_bb_fetch_cached", fake)
    monkeypatch.setattr(dp, "_app_namespace_map", {})
    return base, head, calls


def _detect(changed, renames=None, path_map=None):
    return dp._detect_live_identity_changes(changed, renames or {}, path_map or {}, HEAD, BASE)


def test_the_nbc_incident_is_a_live_rename(repo):
    base, head, _ = repo
    base[NBC], head[NBC] = NBC_MAIN, NBC_PR
    [hit] = _detect([NBC], path_map={NBC: NBC_APPS})
    assert (hit["old_ns"], hit["new_ns"]) == ("pv-nbc-a", "pv-nbc--aec1-a")
    assert (hit["old_id"], hit["new_id"]) == ("pv-nbc-a", "pv-nbc--aec1-a")
    assert hit["apps"] == NBC_APPS and hit["moved_from"] is None
    assert not (hit["decommission"] or hit["paused_base"] or hit["paused_head"] or hit["taken"])


def test_removing_a_suffix_renames_to_a(repo):
    base, head, _ = repo
    base[NBC] = "appspace:\n  customerName: x\n  suffix: b\n"
    head[NBC] = "appspace:\n  customerName: x\n"
    assert [(h["old_ns"], h["new_ns"]) for h in _detect([NBC])] == [("pv-x-b", "pv-x-a")]


@pytest.mark.parametrize("main,pr", [
    (NBC_MAIN, NBC_MAIN.replace("2603.1.38", "2603.2.19")),
    ("appspace:\n  customerName: x\n", "appspace:\n  customerName: x\n  suffix: a\n"),
    ("appspace:\n  customerName: x\n  suffix: ''\n", "appspace:\n  customerName: x\n"),
])
def test_same_identity_is_not_a_rename(repo, main, pr):
    base, head, _ = repo
    base[NBC], head[NBC] = main, pr
    assert _detect([NBC]) == []


def test_the_cohort_fills_a_missing_suffix(repo):
    # customer.yaml drops `suffix: b`, but the cohort says b: still pv-x-b.
    base, head, _ = repo
    base[COHORT] = head[COHORT] = COHORT_BODY + "  suffix: b\n"
    base[NBC] = "appspace:\n  customerName: x\n  suffix: b\n"
    head[NBC] = "appspace:\n  customerName: x\n"
    assert _detect([NBC]) == []


@pytest.mark.parametrize("main,pr", [
    (None, NBC_PR),                       # a new env
    (NBC_MAIN, None),                     # a deletion: the decommission logic owns it
    ("appspace: [\n", NBC_PR),            # YAML the render reports
    (NBC_MAIN, "appspace:\n  customerName: ''\n"),  # the render fails on it (`required`)
    (NBC_MAIN, "appspace:\n  version: 1\n"),
])
def test_unknown_or_missing_sides_are_not_renames(repo, main, pr):
    base, head, _ = repo
    base[NBC], head[NBC] = main, pr
    assert _detect([NBC]) == []


def test_a_new_env_reads_only_main(repo):
    _, head, calls = repo
    head[NBC] = NBC_PR
    assert _detect([NBC]) == [] and calls == [NBC]


@pytest.mark.parametrize("extra", ["  note: 2026-02-30\n", "  mode: !keep yes\n"])
def test_yaml_argocd_reads_but_pyyaml_rejects_is_still_checked(repo, extra):
    # Go's YAML keeps a bad date or an unknown tag as text, so the rename is real.
    base, head, _ = repo
    base[NBC], head[NBC] = NBC_MAIN, NBC_PR + extra
    assert [h["new_ns"] for h in _detect([NBC])] == ["pv-nbc--aec1-a"]


def test_the_token_guard_keeps_its_strict_read(repo):
    base, head, _ = repo
    head[NBC] = NBC_PR + "  note: 2026-02-30\n"
    assert dp._read_first_doc(NBC, HEAD) == (None, "unparsable")
    assert dp._read_first_doc(NBC, HEAD, lenient=True)[1] == "ok"


def test_an_env_without_a_cohort_on_main_is_not_live(repo):
    base, head, _ = repo
    del base[COHORT]
    base[NBC], head[NBC] = NBC_MAIN, NBC_PR
    assert _detect([NBC]) == []


@pytest.mark.parametrize("path", [
    "gcp/prod/public-cloud/na1-a/cl-prod-a/config.yaml",
    "gcp/prod/cl/na1-a/cl-prod-a/customer.yaml",
    "aws/prod/private-cloud/na1-a/pv-amzn-a/customer.yaml",   # no ApplicationSet
    "gcp/prod/private-cloud/customer.yaml",
    f"{AEC}/pv-nbc--aec1-a/cicd-versions.yaml",
])
def test_only_private_cloud_env_files_are_read(repo, path):
    base, head, calls = repo
    base[path], head[path] = NBC_MAIN, NBC_PR
    assert _detect([path]) == [] and calls == []


def test_a_bitbucket_error_is_retried_never_passed(repo):
    base, head, _ = repo
    base[NBC], head[NBC] = ERR, NBC_PR
    with pytest.raises(dp.ValueFileUnreadable) as exc:
        _detect([NBC])
    assert dp._is_transient_exception(exc.value)
    base[NBC], base[COHORT] = NBC_MAIN, ERR
    with pytest.raises(dp.ValueFileUnreadable):
        _detect([NBC])


def test_no_base_sha_means_no_check(repo):
    assert dp._detect_live_identity_changes([NBC], {}, {}, HEAD, None) == []


def test_a_move_that_changes_the_name_is_a_rename(repo):
    # #3821: universalhollywood--aec1 -> universalhol--aec1 in a folder move.
    base, head, _ = repo
    old, new = f"{AEC}/pv-uh--aec1-a/customer.yaml", f"{AEC}/pv-uhol--aec1-a/customer.yaml"
    base[old] = "appspace:\n  customerName: uh--aec1\n"
    head[new] = "appspace:\n  customerName: uhol--aec1\n"
    [hit] = _detect([old, new], {old: new})
    assert (hit["moved_from"], hit["path"]) == (old, new)
    assert (hit["old_id"], hit["new_id"]) == ("pv-uh--aec1-a", "pv-uhol--aec1-a")


def test_a_move_that_keeps_the_name_and_the_spoke_is_not(repo):
    base, head, _ = repo
    old, new = f"{AEC}/pv-x-a/customer.yaml", "gcp/aec/private-cloud/na2-a/weekly/pv-x-a/customer.yaml"
    base[old] = head[new] = "appspace:\n  customerName: x\n"
    head["gcp/aec/private-cloud/na2-a/weekly/config.yaml"] = COHORT_BODY
    assert _detect([old, new], {old: new}) == []


def test_a_move_to_another_spoke_is_a_rename(repo):
    # #4517: same name, another ApplicationSet and cluster.
    base, head, _ = repo
    old = "gcp/prod/private-cloud/na4-a/monthly/pv-lam-a/customer.yaml"
    new = "gcp/prod/private-cloud/na3-a/monthly/pv-lam-a/customer.yaml"
    base["gcp/prod/private-cloud/na4-a/monthly/config.yaml"] = COHORT_BODY
    base[old] = head[new] = "appspace:\n  customerName: lam\n"
    [hit] = _detect([old, new], {old: new})
    assert (hit["old_id"], hit["new_id"]) == ("na4-a/pv-lam-a", "na3-a/pv-lam-a")
    assert hit["old_ns"] == hit["new_ns"] == "pv-lam-a"


@pytest.mark.parametrize("old,new,ids", [
    ("gcp/prod/private-cloud/na1-b/w/pv-x-a/customer.yaml",
     "azure/prod/private-cloud/na1-b/w/pv-x-a/customer.yaml", ("gcp/pv-x-a", "azure/pv-x-a")),
    ("gcp/dev/private-cloud/ap1/w/pv-x-a/customer.yaml",
     "gcp/qa/private-cloud/ap1/w/pv-x-a/customer.yaml", ("dev/pv-x-a", "qa/pv-x-a")),
])
def test_a_move_to_another_cloud_or_tier_is_a_rename(repo, old, new, ids):
    # Same spoke name, but another ApplicationSet and cluster.
    base, head, _ = repo
    base[old.rsplit("/", 2)[0] + "/config.yaml"] = COHORT_BODY
    base[old] = head[new] = "appspace:\n  customerName: x\n"
    [hit] = _detect([old, new], {old: new})
    assert (hit["old_id"], hit["new_id"]) == ids


@pytest.mark.parametrize("decom,hits", [("", 0), ("  decommission: true\n", 1)])
def test_prod_and_aec_share_the_spoke_cluster(repo, decom, hits):
    # Same name, same cluster: the aec apps take over. Only a cascade is dangerous.
    base, head, _ = repo
    old = "gcp/prod/private-cloud/na1-b/w/pv-x-c/customer.yaml"
    new = "gcp/aec/private-cloud/na1-b/w/pv-x-c/customer.yaml"
    base["gcp/prod/private-cloud/na1-b/w/config.yaml"] = COHORT_BODY
    base[old] = "appspace:\n  customerName: x\n  suffix: c\n" + decom
    head[new] = "appspace:\n  customerName: x\n  suffix: c\n"
    found = _detect([old, new], {old: new})
    assert len(found) == hits
    assert all(h["decommission"] and h["old_id"] == "prod/pv-x-c" for h in found)


def test_a_spoke_config_is_the_cohort_of_a_flat_env(repo):
    base, head, _ = repo
    spoke = "gcp/aec/private-cloud/gb1-b/config.yaml"
    env = "gcp/aec/private-cloud/gb1-b/pv-h--aec1-a/customer.yaml"
    base[spoke], head[spoke] = COHORT_BODY, COHORT_BODY + "  suffix: b\n"
    base[env] = head[env] = "appspace:\n  customerName: h--aec1\n"
    assert [h["new_ns"] for h in _detect([spoke], path_map={env: []})] == ["pv-h--aec1-b"]


def test_a_cohort_suffix_renames_the_envs_that_inherit_it(repo):
    base, head, calls = repo
    a, b = f"{AEC}/pv-a-a/customer.yaml", f"{AEC}/pv-b-c/customer.yaml"
    base[a] = head[a] = "appspace:\n  customerName: a\n"
    base[b] = head[b] = "appspace:\n  customerName: b\n  suffix: c\n"
    head[COHORT] = COHORT_BODY + "  suffix: b\n"
    path_map = {a: ["pv-a-a-ms"], b: ["pv-b-c-ms"], "gcp/aec/private-cloud/na2-a/x/pv-z-a/customer.yaml": []}
    [hit] = _detect([COHORT], path_map=path_map)
    assert (hit["path"], hit["new_ns"], hit["via"]) == (a, "pv-a-b", COHORT)
    assert f"`{a}` gets `suffix` `a` \u2192 `b` from `{COHORT}`:" in dp._identity_hit_lines(hit)
    # A cohort change that keeps the identity reads no env file.
    head[COHORT] = COHORT_BODY + "  version: 2603.3.1\n"
    calls.clear()
    assert _detect([COHORT], path_map=path_map) == [] and calls == [COHORT, COHORT]


@pytest.mark.parametrize("main_extra,pr_extra,cohort_extra,flags", [
    ("  decommission: true\n", "", "", {"decommission": True}),
    ("", "  decommission: true\n", "", {"decommission": True}),
    ("", "  decommissionPurgeData: true\n", "", {"decommission": True}),
    ("  decommission: 'True'\n", "", "", {"decommission": True}),
    ("  autosync: false\n", "  autosync: false\n", "", {"paused_base": True, "paused_head": True}),
    ("  autosync: 'false'\n", "", "", {"paused_base": True}),
    # The template tests printf "%v" == "false": a string "False" does not pause.
    ("  autosync: 'False'\n", "  autosync: 'False'\n", "", {}),
    ("", "", "  autosync: false\n", {"paused_base": True, "paused_head": True}),
])
def test_decommission_and_pause_flags(repo, main_extra, pr_extra, cohort_extra, flags):
    base, head, _ = repo
    base[COHORT] = head[COHORT] = COHORT_BODY + cohort_extra
    base[NBC], head[NBC] = NBC_MAIN + main_extra, NBC_PR + pr_extra
    [hit] = _detect([NBC])
    want = {"decommission": False, "paused_base": False, "paused_head": False, **flags}
    assert {k: hit[k] for k in want} == want


def test_a_new_name_already_used_by_another_env(repo, monkeypatch):
    base, head, _ = repo
    base[NBC], head[NBC] = NBC_MAIN, NBC_PR
    monkeypatch.setattr(dp, "_app_namespace_map", {
        "pv-nbc--aec1-a-ms": "pv-nbc--aec1-a", "pv-nbc-a-ms": "pv-nbc-a", "other": "pv-nbc--aec1-a"})
    assert _detect([NBC], path_map={NBC: ["pv-nbc-a-ms"]})[0]["taken"] == ["pv-nbc--aec1-a-ms"]


@pytest.mark.parametrize("hit,confirmed,reason", [
    ({"decommission": True, "paused_base": True, "paused_head": True}, True, "decommission"),
    ({"decommission": False, "paused_base": True, "paused_head": True}, False, "unconfirmed"),
    ({"decommission": False, "paused_base": False, "paused_head": True}, True, "pause_first"),
    ({"decommission": False, "paused_base": True, "paused_head": False}, True, "keep_paused"),
    ({"decommission": False, "paused_base": True, "paused_head": True}, True, None),
])
def test_block_reasons(hit, confirmed, reason):
    hit = {"old_id": "pv-a-a", "new_id": "pv-b-a", **hit}
    assert dp._identity_block_reason(hit, {("pv-a-a", "pv-b-a")} if confirmed else set()) == reason


# --- reading the PR's commit messages -----------------------------------------------

@pytest.fixture()
def mirror(monkeypatch, tmp_path):
    (tmp_path / "objects").mkdir()
    monkeypatch.setattr(dp, "GIT_MIRROR_ENABLED", True)
    monkeypatch.setattr(dp, "_mirror_disabled", False)
    monkeypatch.setattr(dp, "_mirror_path", lambda repo: str(tmp_path))
    monkeypatch.setattr(dp, "_mirror_has_sha", lambda repo, sha: True)
    runs = []

    def run(args, **kw):
        runs.append(args)
        return subprocess.CompletedProcess(args, 0, "one\n\x00Confirm-Rename: a -> b\n\x00", "")
    monkeypatch.setattr(dp, "_git_run", run)
    return runs


def test_commit_messages_come_from_the_mirror(mirror, monkeypatch):
    monkeypatch.setattr(dp, "bb", lambda *a, **k: pytest.fail("the API is not needed"))
    msgs = dp._pr_commit_messages("acme-config-prod", 1, "base1", "head1")
    assert identity._confirmed_renames(msgs) == {("a", "b")}
    assert mirror[0][-1] == "base1..head1"


def test_commit_messages_fall_back_to_the_api_with_pages(mirror, monkeypatch):
    monkeypatch.setattr(dp, "_git_run", lambda args, **kw: subprocess.CompletedProcess(args, 128, "", "bad"))
    pages = {
        "pullrequests/1/commits?pagelen=100": {
            "values": [{"hash": "head1" + "0" * 35, "message": "fix"}],
            "next": f"{dp._bb_api_base('acme-config-prod')}/pullrequests/1/commits?page=2"},
        "pullrequests/1/commits?page=2": {"values": [{"hash": "c2", "message": "Confirm-Rename: a -> b"}]},
    }
    monkeypatch.setattr(dp, "bb", lambda method, path, repo=None: pages[path])
    assert dp._pr_commit_messages("acme-config-prod", 1, "base1", "head1") == ["fix", "Confirm-Rename: a -> b"]


def test_a_commit_list_without_the_head_is_retried(monkeypatch):
    # Right after a push Bitbucket can still list the old commits.
    monkeypatch.setattr(dp, "GIT_MIRROR_ENABLED", False)
    monkeypatch.setattr(dp, "bb", lambda *a, **k: {"values": [{"hash": "old1", "message": "x"}]})
    with pytest.raises(dp.PrCommitsUnreadable) as exc:
        dp._pr_commit_messages("acme-config-prod", 1, "base1", "head1")
    assert dp._is_transient_exception(exc.value)


@pytest.mark.parametrize("error", [OSError("reset"), ValueError("bad json")])
def test_an_api_error_is_retried(monkeypatch, error):
    monkeypatch.setattr(dp, "GIT_MIRROR_ENABLED", False)

    def boom(*a, **k):
        raise error
    monkeypatch.setattr(dp, "bb", boom)
    with pytest.raises(dp.PrCommitsUnreadable):
        dp._pr_commit_messages("acme-config-prod", 1, "base1", "head1")


@pytest.mark.parametrize("code,transient", [(403, False), (404, False), (502, True)])
def test_a_client_error_fails_and_a_server_error_retries(monkeypatch, code, transient):
    monkeypatch.setattr(dp, "GIT_MIRROR_ENABLED", False)

    def boom(*a, **k):
        raise dp.urllib.error.HTTPError("u", code, "x", {}, None)
    monkeypatch.setattr(dp, "bb", boom)
    with pytest.raises(Exception) as exc:
        dp._pr_commit_messages("acme-config-prod", 1, "base1", "head1")
    assert dp._is_transient_exception(exc.value) is transient


def test_endless_pages_are_retried(monkeypatch):
    monkeypatch.setattr(dp, "GIT_MIRROR_ENABLED", False)
    monkeypatch.setattr(dp, "_BB_MAX_PAGES", 3)
    monkeypatch.setattr(dp, "bb", lambda *a, **k: {"values": [], "next": "https://api.bitbucket.org/x"})
    with pytest.raises(dp.PrCommitsUnreadable, match="too many pages"):
        dp._pr_commit_messages("acme-config-prod", 1, "base1", "head1")


# --- the message ----------------------------------------------------------------------

def _hit(reason="unconfirmed", **kw):
    h = {"path": NBC, "moved_from": None, "old": ("nbc", "a"), "new": ("nbc--aec1", "a"),
         "old_ns": "pv-nbc-a", "new_ns": "pv-nbc--aec1-a", "old_id": "pv-nbc-a",
         "new_id": "pv-nbc--aec1-a", "apps": NBC_APPS, "decommission": False,
         "paused_base": False, "paused_head": False, "taken": [], "reason": reason}
    return {**h, **kw}


def test_the_blocked_comment_explains_and_gives_the_steps():
    desc, body = dp._identity_change_block([_hit()], PR_SHA, BASE_SHA)
    assert desc == ("BLOCKED: renames live env pv-nbc-a to pv-nbc--aec1-a — "
                    "revert, or follow the migration steps in the comment")
    assert "`customerName` `nbc` → `nbc--aec1`" in body
    assert "`pv-nbc-a-ms`" in body and "`pv-nbc--aec1-a-ms`" in body
    assert 'git commit --allow-empty -m "Confirm-Rename: pv-nbc-a -> pv-nbc--aec1-a"' in body
    assert "`pv-nbc-a-<name>` to `pv-nbc--aec1-a-<name>`" in body and "mongodb-password" in body
    assert "acme-ping-scaler" in body and "deletion-policy=abandon" in body
    assert "COPR-32565" in body
    assert _extract_status_token(body) == "blocked"
    assert _extract_comment_sha(body) == PR_SHA[:8] and f"[base:{BASE_SHA[:8]}]" in body


@pytest.mark.parametrize("reason,desc_tail,text", [
    ("decommission", "remove decommission first", "remove the old one in its own PR"),
    ("pause_first", "pause auto-sync in a separate PR first", "not paused on `main`"),
    ("keep_paused", "keep autosync: false in this PR", "does not have `autosync: false`"),
])
def test_each_reason_says_what_to_do(reason, desc_tail, text):
    desc, body = dp._identity_change_block([_hit(reason)], PR_SHA, BASE_SHA)
    assert desc.endswith(desc_tail) and text in body
    assert "Confirm-Rename" not in body, "only an unconfirmed rename needs the commit"


def test_a_move_to_another_spoke_skips_the_secret_copy():
    hit = _hit(new=("nbc", "a"), new_ns="pv-nbc-a", old_id="na4-a/pv-nbc-a", new_id="na3-a/pv-nbc-a",
               moved_from=NBC.replace("na2-a", "na4-a"))
    _, body = dp._identity_change_block([hit], PR_SHA, BASE_SHA)
    assert "(another cluster, `na4-a` → `na3-a`)" in body
    assert "Confirm-Rename: na4-a/pv-nbc-a -> na3-a/pv-nbc-a" in body
    assert "Secret Manager" not in body
    # Never a namespace name like na4-a/pv-nbc-a: the new env has the same name elsewhere.
    assert "`pv-nbc-a` in the old `na4-a` (check your kubectl context" in body
    assert "- namespace `pv-nbc-a` in `na4-a` \u2192 `pv-nbc-a` in `na3-a`" in body
    assert "the secrets and most GCP objects keep their names" in body


def test_every_renamed_env_gets_the_after_merge_steps():
    hits = [_hit(), _hit(old_ns="pv-x-b", new_ns="pv-y-b", old_id="pv-x-b", new_id="pv-y-b",
                     old=("x", "b"), new=("y", "b"), path=f"{AEC}/pv-x-b/customer.yaml")]
    _, body = dp._identity_change_block(hits, PR_SHA, BASE_SHA)
    assert "renames 2 live environments" in body and "live environments `pv-nbc-a`, `pv-x-b`" in body
    assert "In the old namespace (`pv-nbc-a`, `pv-x-b`)" in body
    assert f"In `{NBC}`, `{AEC}/pv-x-b/customer.yaml`, add `autosync: false`" in body


def test_a_customer_name_change_warns_about_the_data():
    _, body = dp._identity_change_block([_hit()], PR_SHA, BASE_SHA)
    assert "new, empty content bucket and BigQuery dataset" in body
    assert "Keep the old content bucket and BigQuery dataset until their data is copied" in body
    suffix_only = _hit(new=("nbc", "b"), new_ns="pv-nbc-b", new_id="pv-nbc-b")
    assert "empty content bucket" not in dp._identity_change_block([suffix_only], PR_SHA, BASE_SHA)[1]


def test_a_taken_name_and_azure_are_called_out():
    hit = _hit(taken=["pv-nbc--aec1-a-ms"], path="azure/aec/private-cloud/na1-a/pv-x/customer.yaml")
    _, body = dp._identity_change_block([hit], PR_SHA, BASE_SHA)
    assert "Do not merge: two environments would share one namespace" in body
    assert "split it into two PRs" in body
    assert "For Azure, ask CloudOps before you merge" in body


def test_a_confirmed_rename_still_shows_the_secret_copy():
    for reason in ("pause_first", "keep_paused"):
        assert "check that the 8 secrets are copied to `pv-nbc--aec1-a-<name>`" in \
            dp._identity_change_block([_hit(reason)], PR_SHA, BASE_SHA)[1]
    lines = "\n".join(dp._identity_migration_lines([_hit(None)]))
    assert "check that the 8 secrets are copied" in lines and "empty content bucket" in lines


def test_many_hits_fit_the_status():
    hits = [_hit(old_id="pv-%s-a" % ("x" * 60 + str(i)), new_id="pv-y%d-a" % i, taken=["t"])
            for i in range(9)]
    desc, body = dp._identity_change_block(hits, PR_SHA, BASE_SHA)
    assert len(desc) <= 255 and desc.endswith("follow the migration steps in the comment")
    assert body.count("Confirm-Rename:") == 9 and "live apps already use" in body


def test_the_notice_for_a_confirmed_rename():
    assert dp._identity_migration_lines([]) == []
    lines = "\n".join(dp._identity_migration_lines([_hit(None, paused_base=True, paused_head=True)]))
    assert dp._IDENTITY_MIGRATION_HDR in lines and "Remove `autosync: false`" in lines


# --- end to end through process_pr ---------------------------------------------------

ENV = "gcp/prod/private-cloud/na1-b/weekly/pv-orch-a/customer.yaml"
ENV_COHORT = "gcp/prod/private-cloud/na1-b/weekly/config.yaml"
ORCH = "appspace:\n  customerName: orch\n  version: 2603.2.19\n"
ORCH2 = ORCH.replace("orch", "orch2")
PAUSE = "  autosync: false\n"
CONFIRM = "Confirm-Rename: pv-orch-a -> pv-orch2-a"


@pytest.fixture()
def rename_pr(world, monkeypatch):
    """`mains[base_sha][path]` is main, `files[path]` the PR; the comment persists."""
    sinks, _ = world
    store = {"ids": [], "messages": []}
    mains = {BASE_SHA: {ENV: ORCH, ENV_COHORT: COHORT_BODY}}
    files = {ENV: ORCH2, ENV_COHORT: COHORT_BODY}

    def upsert(pr_id, body, existing_id=None, repo=None, **kw):
        store["ids"].append(existing_id)
        store["id"], store["body"] = existing_id or 123, body
        sinks.upserts.append(body)
        return store["id"]

    def find(pr_id, repo=None):
        b = store.get("body")
        return (store["id"], _extract_comment_sha(b), b) if b else (None, "", "")

    def fetch(path, sha, repo=None):
        v = (mains[sha] if sha in mains else files).get(path)
        return (None, dp.BB_NOT_FOUND) if v is None else ((None, dp.BB_ERROR) if v is ERR else (v, dp.BB_OK))

    def messages(repo, pr_id, base_sha, pr_sha):
        store["read"] = store.get("read", 0) + 1
        if store["messages"] is ERR:
            raise dp.PrCommitsUnreadable("unreadable")
        return store["messages"]

    monkeypatch.setattr(dp, "upsert_comment", upsert)
    monkeypatch.setattr(dp, "find_existing_comment", find)
    monkeypatch.setattr(dp, "_bb_fetch_status", fetch)
    monkeypatch.setattr(dp, "_pr_commit_messages", messages)
    monkeypatch.setattr(dp, "_vf_cache", {})
    monkeypatch.setattr(dp, "_retry_backoff", {})
    monkeypatch.setattr(dp, "_app_namespace_map", {})
    monkeypatch.setattr(dp, "get_pr_changed_files", lambda pr_id, repo=None: ([ENV], {}))
    return sinks, store, mains, files, {ENV: ["pv-orch-a-ms", "pv-orch-a-ss"]}


def _pr(sha=PR_SHA):
    pr = _mk_pr(pr_id=4672)
    pr["source"]["commit"]["hash"] = sha
    return pr


def test_process_pr_blocks_the_nbc_shape_before_any_diff(rename_pr):
    sinks, store, mains, files, path_map = rename_pr
    dp.process_pr(_pr(), path_map, base_sha=BASE_SHA)
    assert sinks.statuses[-1] == ("FAILED", "BLOCKED: renames live env pv-orch-a to pv-orch2-a "
                                            "— revert, or follow the migration steps in the comment")
    assert f'"{CONFIRM}"' in store["body"] and "`pv-orch2-a-ms`" in store["body"]
    assert sinks.diff_calls == [], "a blocked rename must not be rendered"
    assert list(dp._seen.values()) == [(PR_SHA, BASE_SHA)]


def test_process_pr_a_confirmed_rename_still_needs_the_pause_on_main(rename_pr):
    sinks, store, mains, files, path_map = rename_pr
    store["messages"] = [CONFIRM]
    files[ENV] = ORCH2 + PAUSE  # a pause in the rename PR does not reach the old apps
    dp.process_pr(_pr(), path_map, base_sha=BASE_SHA)
    assert sinks.statuses[-1][1].endswith("pause auto-sync in a separate PR first")
    assert sinks.diff_calls == []


def test_process_pr_a_confirmed_paused_rename_goes_to_the_diff(rename_pr):
    sinks, store, mains, files, path_map = rename_pr
    store["messages"] = ["Rename orch", CONFIRM]
    mains[BASE_SHA][ENV], files[ENV] = ORCH + PAUSE, ORCH2 + PAUSE
    dp.process_pr(_pr(), path_map, base_sha=BASE_SHA)
    assert sorted(set(sinks.diff_calls)) == ["pv-orch-a-ms", "pv-orch-a-ss"]
    assert sinks.statuses[-1][0] == "SUCCESSFUL"
    assert "Planned rename of a live environment" in store["body"]
    assert "- namespace `pv-orch-a` \u2192 `pv-orch2-a`" in store["body"]
    assert "After the merge:" in store["body"]


def test_process_pr_the_rename_pr_must_keep_the_pause(rename_pr):
    sinks, store, mains, files, path_map = rename_pr
    store["messages"] = [CONFIRM]
    mains[BASE_SHA][ENV] = ORCH + PAUSE
    dp.process_pr(_pr(), path_map, base_sha=BASE_SHA)
    assert sinks.statuses[-1][1].endswith("keep autosync: false in this PR")


def test_process_pr_decommission_blocks_even_when_confirmed(rename_pr):
    sinks, store, mains, files, path_map = rename_pr
    store["messages"] = [CONFIRM]
    mains[BASE_SHA][ENV] = ORCH + PAUSE + "  decommission: true\n"
    files[ENV] = ORCH2 + PAUSE
    dp.process_pr(_pr(), path_map, base_sha=BASE_SHA)
    assert sinks.statuses[-1][1].endswith("remove decommission first")


def test_process_pr_unreadable_commits_are_red_and_retried(rename_pr):
    sinks, store, mains, files, path_map = rename_pr
    store["messages"] = ERR
    dp.process_pr(_pr(), path_map, base_sha=BASE_SHA)
    state, desc = sinks.statuses[-1]
    assert state == "FAILED" and desc.startswith("Diff unavailable (infrastructure) - will retry")
    assert _extract_status_token(store["body"]) == "transient"
    assert not dp._seen, "a transient failure must stay unseen so it retries"


def test_process_pr_rechecks_after_the_pause_and_the_confirm_commit(rename_pr, monkeypatch):
    sinks, store, mains, files, path_map = rename_pr
    dp.process_pr(_pr(), path_map, base_sha=BASE_SHA)
    assert sinks.statuses[-1][1].endswith("follow the migration steps in the comment")

    # The same commit is not checked again.
    posted = len(sinks.statuses)
    dp.process_pr(_pr(), path_map, base_sha=BASE_SHA)
    assert len(sinks.statuses) == posted

    # The confirm commit alone: a new sha, checked again, now asks for the pause.
    store["messages"] = [CONFIRM]
    dp.process_pr(_pr("0011223344ff"), path_map, base_sha=BASE_SHA)
    assert sinks.statuses[-1][1].endswith("pause auto-sync in a separate PR first")

    # The pause PR merges: main moves, so the same PR commit runs again, and its
    # merge preview carries main's pause.
    mains["2233445566aa"] = {ENV: ORCH + PAUSE, ENV_COHORT: COHORT_BODY}
    mains["feed0001"] = {ENV: ORCH2 + PAUSE, ENV_COHORT: COHORT_BODY}
    monkeypatch.setattr(dp, "_merge_preview", lambda repo, base, pr: ("feed0001", []))
    dp.process_pr(_pr("0011223344ff"), path_map, base_sha="2233445566aa")
    assert sinks.statuses[-1][0] == "SUCCESSFUL", sinks.statuses
    assert store["ids"][1:] == [123, 123], "the same comment is edited in place"


def test_process_pr_the_token_guard_speaks_first(rename_pr, monkeypatch):
    # Dropping --aec1 from a live clone is a rename and a token miss.
    sinks, store, mains, files, path_map = rename_pr
    clone = "gcp/aec/private-cloud/na2-a/monthly/pv-nbc--aec1-a/customer.yaml"
    mains[BASE_SHA].update({clone: "appspace:\n  customerName: nbc--aec1\n", COHORT: COHORT_BODY})
    files.update({clone: "appspace:\n  customerName: nbc\n", COHORT: COHORT_BODY})
    monkeypatch.setattr(dp, "get_pr_changed_files", lambda pr_id, repo=None: ([clone], {}))
    dp.process_pr(_pr(), {clone: ["pv-nbc--aec1-a-ms"]}, base_sha=BASE_SHA)
    assert sinks.statuses[-1][1].startswith("BLOCKED: pv-nbc--aec1-a needs --aec1")
    assert "read" not in store


def test_process_pr_other_changes_read_no_commits(rename_pr, monkeypatch):
    sinks, store, mains, files, path_map = rename_pr
    files[ENV] = ORCH.replace("2603.2.19", "2603.3.1")
    dp.process_pr(_pr(), path_map, base_sha=BASE_SHA)
    assert sinks.statuses[-1][0] == "SUCCESSFUL" and "read" not in store


# --- history, against the local config repos ------------------------------------------

@pytest.mark.local_components
@pytest.mark.parametrize("sha,old,new", [
    ("e5b6072eb", "pv-nbc-a", "pv-nbc--aec1-a"),              # #4672, COPR-32565
    ("50ff35b5e", "pv-usbank-a", "pv-usbank--aec1-a"),        # #3287
    ("7b9a66f8e", "pv-deloitte-a", "pv-deloitte--aec1-a"),    # #844
    ("25f357078", "pv-onr-a", "pv-onr-b"),                    # #2245, a suffix change
    ("c15d6fa818", "pv-universalhollywood--aec1-a", "pv-universalhol--aec1-a"),  # #3821, a move
])
def test_past_prod_renames_are_caught(monkeypatch, sha, old, new):
    root = os.path.expanduser("~/gitprojects/acme-config-prod")
    if not os.path.isdir(os.path.join(root, ".git")):
        pytest.skip("acme-config-prod not checked out")

    def git(*args):
        return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True)

    def fetch(path, at, repo=None):
        r = git("show", f"{at}:{path}")
        return (r.stdout, dp.BB_OK) if r.returncode == 0 else (None, dp.BB_NOT_FOUND)
    monkeypatch.setattr(dp, "_bb_fetch_cached", fetch)
    monkeypatch.setattr(dp, "_app_namespace_map", {})
    changed, renames = [], {}
    for line in git("diff", "--name-status", "-M", f"{sha}^1", sha).stdout.splitlines():
        f = line.split("\t")
        if f[0].startswith("R"):
            renames[f[1]] = f[2]
        changed += f[1:]
    hits = dp._detect_live_identity_changes(changed, renames, {}, sha, f"{sha}^1")
    assert [(h["old_ns"], h["new_ns"]) for h in hits] == [(old, new)]
