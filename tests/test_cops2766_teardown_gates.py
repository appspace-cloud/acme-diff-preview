"""COPS-2766 block 2: a teardown that leaves things running fails the build.

The merge summary said DO NOT MERGE for an orphan teardown (a folder removal
with no cascade armed), for every public-cloud teardown (with or without the
decommission flag, which arms nothing there) and for a purge that deletes
user content a surviving environment uses. The build stayed green for all.
Arming the flag on cl-* deletes nothing, so that PR stays green.

Now each one is a merge gate: the build is FAILED with the token [blocked]
until a commit message of the PR has `Confirm-Teardown: <env>`. The commits
are read only when a gate can be lifted, and an unreadable commit list is a
retry, never a lift.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import comment_render as cr  # noqa: E402
import diff_preview as m  # noqa: E402
import identity  # noqa: E402

from test_coverage_orchestration import (  # noqa: E402,F401
    world, _mk_pr, PATH_MAP, BASE_SHA, PR_SHA, IDENTITY, IDENTITY_YAML)

REAL_FIX_STUCK = m.fix_stuck_inprogress
SK = ("acme-config-dev", 991)
TRAILER = "Confirm-Teardown: pv-orch-a"


@pytest.fixture(autouse=True)
def no_vertex(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)


# ── (a) the trailer parser ───────────────────────────────────────────────

@pytest.mark.parametrize("message,expected", [
    ("Confirm-Teardown: pv-x-a", {"confirm-teardown: pv-x-a"}),
    ("confirm-TEARDOWN: PV-X-A", {"confirm-teardown: pv-x-a"}),
    ("Confirm-Teardown: `pv-x-a`", {"confirm-teardown: pv-x-a"}),
    ("`Confirm-Teardown: pv-x-a`", {"confirm-teardown: pv-x-a"}),
    ("> Confirm-Teardown: pv-x-a", {"confirm-teardown: pv-x-a"}),
    ("  * Confirm-Teardown: pv-x-a", {"confirm-teardown: pv-x-a"}),
    ("Confirm-Teardown: pv-x-a.", {"confirm-teardown: pv-x-a"}),
    ("Confirm-Rename: a/pv-x-a → b/pv-x-a", {"confirm-rename: a/pv-x-a -> b/pv-x-a"}),
    ("Confirm-Rename: a->b", {"confirm-rename: a -> b"}),
    ("Remove two envs\n\nConfirm-Teardown: pv-x-a\nConfirm-Decommission: pv-x-a\n"
     "Confirm-IP-Release: pv-y-a",
     {"confirm-teardown: pv-x-a", "confirm-decommission: pv-x-a",
      "confirm-ip-release: pv-y-a"}),
    ("Confirm-Teardown pv-x-a", set()),
    ("Please Confirm-Teardown: pv-x-a", set()),
    ("Confirm-Purge: pv-x-a", set()),
    (None, set()),
])
def test_confirmations(message, expected):
    assert identity._confirmations([message]) == expected


def test_confirmations_read_every_message():
    assert identity._confirmations(["fix", "Confirm-Teardown: a", ""]) == {
        "confirm-teardown: a"}


def test_confirmed_renames_is_the_same_parser():
    """The identity guard keeps its pairs; now in lower case."""
    msgs = ["Confirm-Rename: PV-A-A -> pv-b-a", "Confirm-Teardown: pv-a-a",
            "Confirm-Rename: pv-a-a -> pv-b-a now"]
    assert identity._confirmed_renames(msgs) == {("pv-a-a", "pv-b-a")}


# ── the gate table ───────────────────────────────────────────────────────

def _gate(kind, env="pv-x-a", lifted=False, **kw):
    return {"kind": kind, "env": env, "arg": env, "lifted": lifted, **kw}


def test_every_gate_kind_has_text_and_a_known_token():
    for kind, (text, trailer, token) in cr.GATES.items():
        assert text and token in ("blocked", "transient"), kind
        assert trailer is None or trailer.startswith("Confirm-"), kind


def test_gate_trailer_names_the_env_or_the_arg():
    assert cr.gate_trailer(_gate("orphan")) == "Confirm-Teardown: pv-x-a"
    assert cr.gate_trailer(_gate("cl_rename", arg="a/cl-x -> b/cl-x")) == \
        "Confirm-Rename: a/cl-x -> b/cl-x"
    assert cr.gate_trailer(_gate("shrink", env="")) == ""


def test_gate_token_reads_only_open_gates_and_blocked_wins():
    assert cr.gate_token(None) == ""
    assert cr.gate_token([_gate("orphan", lifted=True)]) == ""
    assert cr.gate_token([_gate("not_live")]) == "transient"
    assert cr.gate_token([_gate("not_live"), _gate("orphan")]) == "blocked"
    assert cr.open_gates([_gate("not_live"), _gate("public"),
                          _gate("orphan", lifted=True)]) == [_gate("public"),
                                                             _gate("not_live")]


def test_status_description_names_the_line_to_add():
    desc = cr.gate_status_description([_gate("not_live"), _gate("orphan"),
                                       _gate("public", env="cl-y")])
    assert desc.startswith("Blocked - " + cr.GATES["orphan"][0] + " in pv-x-a.")
    assert "add 'Confirm-Teardown: pv-x-a' to a commit message" in desc
    assert "(+2 more)" in desc and len(desc) <= 255
    assert "commit" not in cr.gate_status_description([_gate("shrink", env="")])


def test_merge_summary_blocks_an_open_gate_and_shows_a_lifted_one():
    out = cr._build_merge_summary({}, {}, None, None, None, None, False,
                                  gates=[_gate("orphan"),
                                         _gate("public", env="cl-y", lifted=True)])
    text = "\n".join(out)
    assert "⛔ **DO NOT MERGE**" in text
    assert (f"- ⛔ **{cr.GATES['orphan'][0]}** in `pv-x-a` - to merge "
            f"anyway, add `Confirm-Teardown: pv-x-a` to a commit message") in out
    assert (f"- ☑️ **Confirmed in a commit:** `Confirm-Teardown: cl-y` "
            f"({cr.GATES['public'][0]})") in out
    shrink = "\n".join(cr._build_merge_summary(
        {}, {}, None, None, None, None, False, gates=[_gate("shrink", env="")]))
    assert f"**{cr.GATES['shrink'][0]}**\n" in shrink + "\n"


def test_a_lifted_gate_alone_is_a_review():
    out = cr._build_merge_summary({}, {}, None, None, None, None, False,
                                  gates=[_gate("orphan", lifted=True)])
    assert out[2].startswith("⚠️ **Review before merging**")
    assert out[4].startswith("- ☑️ **Confirmed in a commit:**")


# ── (c) the evaluator records the gates ──────────────────────────────────

PV_ENV = "pv-foo-c"
PV_IDENT = "gcp/prod/private-cloud/na2-a/monthly/pv-foo-c/customer.yaml"
CL_IDENT = "gcp/prod/public-cloud/na1/cl-prod-b/api/config.yaml"
LIVE = "appspace:\n  customerName: foo\n"
ARMED = LIVE + "  decommission: true\n"
PURGE = ARMED + "  decommissionPurgeData: true\n"


def _evaluate(monkeypatch, base_yaml, cand):
    with m._vf_cache_lock:
        m._vf_cache.clear()      # one sha pair, several base files
    m._yaml_cache.clear()
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda p, s, **kw: (None, m.BB_NOT_FOUND) if s == "prsha"
                        else (base_yaml, m.BB_OK))
    monkeypatch.setattr(m, "_render_main_side_resources", lambda app, sha: {})
    monkeypatch.setattr(m, "_cascade_finalizer_live", lambda apps: None)
    monkeypatch.setattr(m, "_teardown_hold_met", lambda *a, **k: True)
    lines, envs = m._evaluate_env_decommissions([cand], "prsha", "mainsha")
    assert envs == [cand["env_name"]]
    return lines


def _pv():
    return {"env_name": PV_ENV, "identity_file": PV_IDENT, "apps": [f"{PV_ENV}-ms"]}


def test_private_teardown_with_no_cascade_is_an_orphan_gate(monkeypatch):
    cand = _pv()
    _evaluate(monkeypatch, LIVE, cand)
    assert cand["gates"] == [{"kind": "orphan", "env": PV_ENV}]


def test_a_correctly_armed_cascade_has_no_gate(monkeypatch):
    for base in (ARMED, PURGE):
        cand = _pv()
        _evaluate(monkeypatch, base, cand)
        assert cand["gates"] == []


@pytest.mark.parametrize("base", [LIVE, ARMED])
def test_public_cloud_teardown_is_always_a_public_gate(monkeypatch, base):
    """The flag arms nothing on cl-*, so it changes nothing here. The env is
    the constellation, so one trailer covers all of its blocks."""
    cand = {"env_name": "cl-prod-b", "identity_file": CL_IDENT,
            "apps": ["cl-prod-b-api-glb"], "block": "api"}
    _evaluate(monkeypatch, base, cand)
    assert cand["gates"] == [{"kind": "public", "env": "cl-prod-b"}]


def test_purge_of_shared_user_content_is_a_shared_uc_gate(monkeypatch):
    monkeypatch.setattr(m, "_shared_user_content_owners",
                        lambda ident, sha, repo=None: ({}, {"pv-foo-b": {
                            "buckets": ["b"], "fqdns": [], "unproven": False}}))
    cand = _pv()
    _evaluate(monkeypatch, PURGE, cand)
    assert cand["gates"] == [{"kind": "shared_uc", "env": PV_ENV}]
    # Not armed: abandon, nothing is destroyed today, so no gate for it.
    cand = _pv()
    _evaluate(monkeypatch, ARMED, cand)
    assert cand["gates"] == []


def test_a_candidate_that_is_not_deleted_gets_no_gates(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_status", lambda p, s, **kw: (LIVE, m.BB_OK))
    cand = _pv()
    assert m._evaluate_env_decommissions([cand], "prsha", "mainsha") == ([], [])
    assert "gates" not in cand


# ── _merge_gates ─────────────────────────────────────────────────────────

def test_merge_gates_keeps_one_gate_per_kind_and_env():
    cands = [{"gates": [{"kind": "public", "env": "cl-prod-b"}]},
             {"gates": [{"kind": "public", "env": "cl-prod-b"},
                        {"kind": "cl_rename", "env": "cl-prod-b", "arg": "a -> b"}]},
             {"env_name": "not-confirmed"}]
    assert m._merge_gates(cands) == [
        {"kind": "public", "env": "cl-prod-b", "arg": "cl-prod-b", "lifted": False},
        {"kind": "cl_rename", "env": "cl-prod-b", "arg": "a -> b", "lifted": False}]
    assert m._merge_gates(None) == []


def test_arming_the_flag_on_cl_is_not_a_gate(world, monkeypatch):
    """The flag arms nothing on cl-*, so setting it deletes nothing: a
    warning, the build stays green and the commits are never read."""
    sinks, _plan = world
    ident = "gcp/stage/public-cloud/na1/cl-adapter-a/customer.yaml"
    live = "appspace:\n  customerName: adapter\n"
    monkeypatch.setattr(m, "_bb_fetch_status", lambda p, s, repo=None: (
        (live + "  decommission: true\n" if s == "prsha" else live), m.BB_OK)
        if p == ident else (None, m.BB_NOT_FOUND))
    monkeypatch.setattr(m, "_merged_kcc_flat_for_env", lambda *a, **k: {})
    panel = m._summarize_appspace_state_changes(
        [ident], "prsha", "mainsha", {ident: ["cl-adapter-a-ms"]})
    assert cr._DECOM_PUBLIC_CLOUD_NOOP_HDR in "\n".join(panel)
    monkeypatch.setattr(m, "_summarize_appspace_state_changes", lambda *a, **k: panel)
    monkeypatch.setattr(m, "_bb_fetch_status", lambda path, sha, repo=None:
                        (IDENTITY_YAML, m.BB_OK) if path.endswith(IDENTITY)
                        else (None, m.BB_NOT_FOUND))
    monkeypatch.setattr(m, "_pr_commit_messages",
                        lambda *a: pytest.fail("no gate, no commit read"))
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    assert m._extract_status_token(sinks.upserts[-1]) == "clean"
    assert sinks.statuses[-1][0] == "SUCCESSFUL"


# ── format_comment ───────────────────────────────────────────────────────

def _decom(gates):
    return m.format_comment(
        "a" * 40, {"pv-x-a-ms": m.DiffResult("", [], 0, False, None,
                                             m.OUT_DECOMMISSIONED, "")},
        base_sha="b" * 40, gates=gates)


def test_format_comment_open_gate_is_blocked_with_a_footer_suffix():
    body = _decom([_gate("orphan")])
    assert m._extract_status_token(body) == "blocked"
    assert (f"| ⛔ BLOCKED - {cr.GATES['orphan'][0]}, see comment") in body
    assert "`Confirm-Teardown: pv-x-a` to a commit message" in body


def test_format_comment_lifted_gate_is_clean_and_transient_gate_retries():
    assert m._extract_status_token(_decom([_gate("orphan", lifted=True)])) == "clean"
    assert m._extract_status_token(_decom([_gate("not_live")])) == "transient"
    assert m._extract_status_token(_decom(None)) == "clean"


def test_a_permanent_error_outranks_a_gate():
    body = m.format_comment(
        "a" * 40, {"pv-x-a-ms": m.DiffResult("", [], 0, False, "boom", m.OUT_ERROR, "")},
        base_sha="b" * 40, gates=[_gate("orphan")])
    assert m._extract_status_token(body) == "permanent"


# ── (d) process_pr ───────────────────────────────────────────────────────

@pytest.fixture()
def teardown(world, monkeypatch):
    """The PR removes pv-orch-a/customer.yaml; main has no cascade armed."""
    sinks, _plan = world
    monkeypatch.setattr(m, "get_pr_changed_files",
                        lambda pr_id, repo=None: ([IDENTITY], {}))
    monkeypatch.setattr(m, "_bb_fetch_status", lambda path, sha, repo=None:
                        (IDENTITY_YAML, m.BB_OK) if path == IDENTITY and sha == BASE_SHA
                        else (None, m.BB_NOT_FOUND))
    monkeypatch.setattr(m, "_render_main_side_resources", lambda app, sha: {})
    monkeypatch.setattr(m, "_shared_user_content_lines", lambda *a, **k: [])
    calls = []

    def commits(messages):
        def read(repo, pr_id, base_sha, pr_sha):
            calls.append((repo, pr_id, base_sha, pr_sha))
            if isinstance(messages, Exception):
                raise messages
            return messages
        monkeypatch.setattr(m, "_pr_commit_messages", read)
        m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
        return sinks.upserts[-1], sinks.statuses[-1]
    return commits, calls


def test_orphan_teardown_without_the_trailer_is_blocked(teardown):
    commits, calls = teardown
    body, (state, desc) = commits(["Remove pv-orch-a"])
    assert calls == [("acme-config-dev", 991, BASE_SHA, PR_SHA)]
    assert m._extract_status_token(body) == "blocked"
    assert state == "FAILED" and f"add '{TRAILER}' to a commit message" in desc
    assert f"`{TRAILER}` to a commit message" in body
    assert "ENVIRONMENT DECOMMISSION" in body, "the panel is still there"
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA)
    assert SK not in m._retry_backoff


def test_the_trailer_lifts_it(teardown):
    commits, _calls = teardown
    body, (state, desc) = commits([f"Remove pv-orch-a\n\n{TRAILER}\n"])
    assert m._extract_status_token(body) == "clean"
    assert state == "SUCCESSFUL", desc
    assert f"☑️ **Confirmed in a commit:** `{TRAILER}`" in body
    assert "BLOCKED" not in body
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA)


def test_a_trailer_for_another_env_does_not(teardown):
    commits, _calls = teardown
    body, (state, _desc) = commits(["Confirm-Teardown: pv-other-a"])
    assert m._extract_status_token(body) == "blocked" and state == "FAILED"


def test_unreadable_commits_retry_and_never_lift(teardown):
    commits, _calls = teardown
    body, (state, desc) = commits(m.PrCommitsUnreadable("commit list is not ready"))
    assert m._extract_status_token(body) == "transient"
    assert state == "FAILED" and "will retry" in desc
    assert SK not in m._seen and SK in m._retry_backoff


def test_a_transient_gate_is_red_and_retries(world, monkeypatch):
    sinks, _plan = world
    monkeypatch.setattr(m, "_merge_gates", lambda *a: [_gate("not_live", env="pv-orch-a")])
    monkeypatch.setattr(m, "_pr_commit_messages", lambda *a: pytest.fail("no trailer to read"))
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    assert m._extract_status_token(sinks.upserts[-1]) == "transient"
    assert sinks.statuses[-1][0] == "FAILED"
    assert SK not in m._seen and SK in m._retry_backoff


def test_fix_stuck_inprogress_keeps_a_blocked_teardown_red(teardown, monkeypatch):
    commits, _calls = teardown
    body, _status = commits([])
    posted = []
    monkeypatch.setattr(m, "http", lambda *a, **k: {"state": "INPROGRESS"})
    monkeypatch.setattr(m, "post_build_status",
                        lambda sha, state, d, pr_id=None, repo=None:
                        posted.append(state) or "ok")
    assert REAL_FIX_STUCK(PR_SHA, 991, body) == "ok"
    assert posted == ["FAILED"]


# ── (e) no gate, no commit read ──────────────────────────────────────────

def test_a_pr_with_no_gate_never_reads_the_commits(world, monkeypatch):
    sinks, plan = world
    monkeypatch.setattr(m, "_pr_commit_messages",
                        lambda *a: pytest.fail("a PR with no gate read its commits"))
    plan["pv-orch-a-ms"] = m.DiffResult(
        "--- main\n+++ pr", [("Deployment/webx", "-replicas: 2\n+replicas: 3")],
        1, True, "", m.OUT_DIFF, "")
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    assert m._extract_status_token(sinks.upserts[-1]) == "clean"
    assert sinks.statuses[-1][0] == "SUCCESSFUL"


@pytest.mark.parametrize("result,desc", [
    (m.DiffResult("", [], 0, False, "helm: boom", m.OUT_ERROR, ""),
     "Diff failed: helm: boom - check PR comment"),
    (m.DiffResult("", [], 0, False, "bad yaml", m.OUT_INDETERMINATE,
                  m.REASON_INVALID_YAML), None),
])
def test_an_older_guard_keeps_its_status_text(world, monkeypatch, result, desc):
    """A gate never hides the FAILED text an older guard had before it."""
    sinks, plan = world
    monkeypatch.setattr(m, "_merge_gates", lambda *a: [_gate("orphan", env="pv-orch-a")])
    monkeypatch.setattr(m, "_pr_commit_messages", lambda *a: [])
    plan["pv-orch-a-ms"] = result
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    state, got = sinks.statuses[-1]
    assert state == "FAILED" and not got.startswith("Blocked"), got
    assert got == (desc or m._permanent_failure_status_description(
        {"pv-orch-a-ms": result}))
    assert "| ⛔ BLOCKED" in sinks.upserts[-1], "the comment still shows the gate"


# ── a blocked gate next to a transient app ───────────────────────────────
# A design choice, pinned here. Like a permanent error next to a transient
# one, the PR is marked seen and not retried: every blocked gate clears only
# with a new commit (a trailer, a fixed size) or a move of main (a pause
# lifted), and both run every app again.

TIMEOUT = m.DiffResult("", [], 0, False, "slow", m.OUT_INDETERMINATE, m.REASON_TIMEOUT)


def test_comment_status_token_puts_a_blocked_gate_before_a_transient_app():
    args = ({"pv-x-a-ms": TIMEOUT}, False, None, False, False, False)
    assert m._comment_status_token(*args, [_gate("orphan")]) == "blocked"
    assert m._comment_status_token(*args, [_gate("not_live")]) == "transient"
    assert m._comment_status_token(*args, None) == "transient"


def test_a_blocked_gate_with_a_transient_app_is_seen_not_retried(world, monkeypatch):
    sinks, plan = world
    monkeypatch.setattr(m, "_retry_backoff", {})
    monkeypatch.setattr(m, "_merge_gates", lambda *a: [_gate("orphan", env="pv-orch-a")])
    monkeypatch.setattr(m, "_pr_commit_messages", lambda *a: [])
    plan["pv-orch-a-ms"] = TIMEOUT
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    assert m._extract_status_token(sinks.upserts[-1]) == "blocked"
    assert sinks.statuses[-1][0] == "FAILED"
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA) and SK not in m._retry_backoff
