"""COPS-2766 block 3, row 13: a PR that only adds environments.

Before, that path built its own comment: no merge summary, no gates and no
status lead. Now format_new_env_comment gives it the summary and the gates
of a diff comment, with the same token and status rules. There is no new
rule here: the later block 3 checks add their gates through
_merge_gates(extra=), and _lift_gates reads the commits only when an open
gate has a trailer.
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import comment_render as cr  # noqa: E402
import diff_preview as m  # noqa: E402

from test_coverage_orchestration import (  # noqa: E402,F401
    world, _mk_pr, PATH_MAP, BASE_SHA, PR_SHA)

REAL_FIX_STUCK = m.fix_stuck_inprogress     # the world fixture stubs it

ENV = "pv-new-x-a"
ENV_DIR = f"gcp/dev/private-cloud/ap1/custom/{ENV}"
IDENT = f"{ENV_DIR}/customer.yaml"
COHORT = "gcp/dev/private-cloud/ap1/custom/config.yaml"
FILES = {IDENT: "appspace:\n  customerName: new-x\n  version: 2603.0.1-dev\n",
         COHORT: "appspace:\n  tier: custom\n"}
CAND = {"name": ENV, "config_file": IDENT, "env_dir": ENV_DIR,
        "all_yaml_files": [IDENT]}
RENDERED = (
    "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api-gateway\n"
    "---\napiVersion: v1\nkind: Service\nmetadata:\n  name: api-gateway\n"
    "---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: platform-config\n"
    "data:\n  region: ap1\n")
CLEAN_DESC = "1 new environment(s), ~3 resource(s) to create"
STRUCTURAL_DESC = f"1 new environment(s) have a structural config problem: {ENV}"
IP = {"kind": "ip", "env": ENV}
TRAILER = f"Confirm-IP-Release: {ENV}"
SK = (m.BB_REPO, 991)
RED = ("⛔", "\U0001f6a8", "❌")


@pytest.fixture(autouse=True)
def no_vertex(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)


def _stub_new_env(monkeypatch, render=(RENDERED, None, 3, "2603.0.1-dev")):
    """pv-new-x-a exists only at the PR head, and it renders 3 resources."""
    monkeypatch.setattr(m, "_bb_fetch_status", lambda path, sha, repo=None:
                        (FILES[path], m.BB_OK) if sha == PR_SHA and path in FILES
                        else (None, m.BB_NOT_FOUND))
    monkeypatch.setattr(m, "_render_new_env_diff", lambda env_info, pr_sha: render)


@pytest.fixture()
def newenv(world, monkeypatch):
    """A PR that adds pv-new-x-a and touches no live app."""
    sinks, _plan = world
    monkeypatch.setattr(m, "get_pr_changed_files", lambda pr_id, repo=None: ([IDENT], {}))
    monkeypatch.setattr(m, "_detect_new_env_candidates", lambda *a, **k: [dict(CAND)])
    monkeypatch.setattr(m, "_pr_commit_messages",
                        lambda *a: pytest.fail("commits read with no trailer to find"))
    _stub_new_env(monkeypatch)
    real_merge = m._merge_gates

    def run(extra=(), messages=None, render=None):
        if render:
            _stub_new_env(monkeypatch, render)
        monkeypatch.setattr(m, "_merge_gates", lambda *a, **k:
                            real_merge(*a, **{**k, "extra": [dict(g) for g in extra]}))
        if messages is not None:
            def read(*a):
                if isinstance(messages, Exception):
                    raise messages
                return list(messages)
            monkeypatch.setattr(m, "_pr_commit_messages", read)
        m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
        return sinks.upserts[-1], sinks.statuses[-1]
    return run


def _outside_fences(body):
    return re.sub(r"```.*?```", "", body, flags=re.S)


# ── process_pr on the new-env-only path ─────────────────────────────────

def test_a_clean_new_env_gets_the_summary_and_stays_green(newenv):
    body, (state, desc) = newenv()
    assert m._extract_status_token(body) == "clean"
    assert state == "SUCCESSFUL" and desc == CLEAN_DESC
    assert "- \U0001f195 **New environment** in this PR" in body
    assert cr._VERDICTS[cr._SEV_ROUTINE] in body
    assert body.index(cr.MERGE_SUMMARY_HDR) < body.index("New Environment(s) Detected")
    assert body.index("New Environment(s) Detected") < body.index("Full rendered output")
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA)


def test_a_structural_new_env_is_blocked_as_before(newenv):
    body, (state, desc) = newenv(render=(None, "template: boom at line 3", 0, None))
    assert m._extract_status_token(body) == "blocked"
    assert state == "FAILED" and desc == STRUCTURAL_DESC
    assert "its configuration did not validate" in body
    assert f"structural problem that must be fixed before merge: `{ENV}`" in body
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA)


def test_structural_wins_over_an_open_gate(newenv):
    body, (state, desc) = newenv(extra=[IP], messages=[],
                                 render=(None, "template: boom at line 3", 0, None))
    assert m._extract_status_token(body) == "blocked"
    assert state == "FAILED" and desc == STRUCTURAL_DESC
    assert f"add `{TRAILER}` to a commit message" in body


def test_an_open_gate_blocks_and_names_its_trailer(newenv):
    body, (state, desc) = newenv(extra=[IP], messages=["Add pv-new-x-a"])
    assert m._extract_status_token(body) == "blocked"
    assert state == "FAILED"
    assert desc == cr.gate_status_description([dict(IP, lifted=False)])
    assert (f"⛔ **A static IP or DNS record is released** in `{ENV}` - to "
            f"merge anyway, add `{TRAILER}` to a commit message") in body
    # The footer carries the exact status, so fix_stuck_inprogress posts it again.
    assert body.count(cr.gate_footer([dict(IP, lifted=False)])) == 1
    assert m._GATE_FOOTER_RE.findall(body) == [desc]
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA)


def test_a_lifted_gate_is_green_with_no_red_icon(newenv):
    body, (state, desc) = newenv(extra=[IP], messages=[f"Add it\n\n{TRAILER}\n"])
    assert m._extract_status_token(body) == "clean"
    assert state == "SUCCESSFUL"
    lead = f"⚠️ Confirmed in a commit: {TRAILER} (A static IP or DNS record is released)"
    assert desc == f"{lead} | {CLEAN_DESC}"
    assert f"☑️ **Confirmed in a commit:** `{TRAILER}`" in body
    assert not any(r in _outside_fences(body) for r in RED)


def test_unreadable_commits_retry_and_never_lift(newenv):
    body, (state, desc) = newenv(extra=[IP], messages=m.PrCommitsUnreadable("not ready"))
    assert m._extract_status_token(body) == "transient"
    assert state == "FAILED" and "will retry" in desc
    assert SK not in m._seen and SK in m._retry_backoff


def test_a_transient_gate_is_red_and_retries(newenv):
    gate = {"kind": "not_live", "env": ENV}
    body, (state, desc) = newenv(extra=[gate])
    assert m._extract_status_token(body) == "transient"
    assert state == "FAILED"
    assert desc == cr.gate_status_description([dict(gate, lifted=False)])
    assert SK not in m._seen and SK in m._retry_backoff


@pytest.mark.parametrize("extra,messages", [
    ((), None),
    ([IP], []),
    ([IP], [TRAILER]),
])
def test_fix_stuck_posts_the_same_state_and_lead(newenv, monkeypatch, extra, messages):
    body, (state, desc) = newenv(extra=extra, messages=messages)
    posted = []
    monkeypatch.setattr(m, "http", lambda *a, **k: {"state": "INPROGRESS"})
    monkeypatch.setattr(m, "post_build_status",
                        lambda sha, st, d, pr_id=None, repo=None:
                        posted.append((st, d)) or "ok")
    assert REAL_FIX_STUCK(PR_SHA, 991, body) == "ok"
    assert posted[0][0] == state
    if state == "SUCCESSFUL":   # only a green status leads with a finding
        lead = cr.status_lead(body)
        assert posted[0][1].startswith(lead) and desc.startswith(lead)
    else:                       # the gate footer gives back the exact status
        assert posted[0][1] == desc


# ── the pure pieces ──────────────────────────────────────────────────────

def test_merge_gates_takes_ready_gates_with_the_same_dedup():
    extra = [IP, dict(IP, why="again"), {"kind": "ip", "env": "pv-b-a", "arg": "b"},
             {"kind": "shrink", "env": ""}]
    lines = [cr._VM_PANEL_DANGER_HDR, f"- x {m._VM_SHRINK_REASON}"]
    assert m._merge_gates(None, None, None, lines, None, extra=extra) == [
        {"kind": "shrink", "env": "", "arg": "", "lifted": False},
        {"kind": "ip", "env": ENV, "arg": ENV, "lifted": False},
        {"kind": "ip", "env": "pv-b-a", "arg": "b", "lifted": False}]
    assert m._merge_gates(None) == []


def test_lift_gates_reads_the_commits_only_for_a_trailer(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "_pr_commit_messages",
                        lambda *a: calls.append(a) or [f"x\n{TRAILER}", "Confirm-IP-Release: pv-b-a"])
    shrink = [{"kind": "shrink", "env": "", "arg": "", "lifted": False}]
    assert m._lift_gates(shrink, "r", 7, "b", "p") is shrink
    assert calls == [] and shrink[0]["lifted"] is False
    gates = m._merge_gates(None, extra=[IP, {"kind": "ip", "env": "pv-c-a"}])
    assert [g["lifted"] for g in m._lift_gates(gates, "r", 7, "b", "p")] == [True, False]
    assert calls == [("r", 7, "b", "p")]
    assert m._lift_gates([], "r", 7, "b", "p") == [] and len(calls) == 1


# ── golden ───────────────────────────────────────────────────────────────

GOLDEN_DIR = os.path.join(os.path.dirname(__file__), "golden")
G_PR_SHA = "abc12345def67890abc12345def67890abc12345"
G_BASE_SHA = "0000111122223333444455556666777788889999"


def test_golden_new_env_only(monkeypatch):
    """The real _evaluate_new_envs output, framed by format_new_env_comment."""
    monkeypatch.setattr(m, "_ts", lambda: "2026-01-01 00:00 UTC")
    monkeypatch.setattr(m, "_repo_for_sha", lambda sha: "acme-config-dev")
    monkeypatch.setattr(m, "_bb_fetch_status", lambda path, sha, repo=None:
                        (FILES[path], m.BB_OK) if path in FILES else (None, m.BB_NOT_FOUND))
    monkeypatch.setattr(m, "_render_new_env_diff",
                        lambda env_info, pr_sha: (RENDERED, None, 3, "2603.0.1-dev"))
    lines, structural, total, full = m._evaluate_new_envs(
        [dict(CAND)], G_PR_SHA, with_full_output=True)
    body, state, desc = m.format_new_env_comment(
        G_PR_SHA, lines, full, structural, [], 1, total, G_BASE_SHA)
    assert (state, desc) == ("SUCCESSFUL", CLEAN_DESC)
    path = os.path.join(GOLDEN_DIR, "new_env_only.md")
    if os.environ.get("UPDATE_GOLDEN") == "1":
        with open(path, "w") as f:
            f.write(body)
        pytest.skip("golden rewritten: new_env_only")
    with open(path) as f:
        assert body == f.read(), "the new-env-only comment changed: read the diff"
