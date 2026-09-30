"""COPS-2766 block 3, item 1 (row 3, C04): an AEC clone that starts running.

A clone starts with a copy of the production data, so a running clone can call
the customer live integrations: SSO, webhooks, mail and connected apps
(AE-15507). It must start asleep (`zeroPods: true`), and wake only after its
data is cleaned. Now two shapes are the `clone_wake` gate, lifted by
`Confirm-Clone-Sanitized: <env>`: a new clone that is not asleep, and a live
clone whose value chain goes from zeroPods true to not true (its own
customer.yaml, or a config.yaml above it). A failed read retries the PR and
is never a gate; values that cannot be parsed are a gate.
"""
import os
import posixpath
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import comment_render as cr  # noqa: E402
import diff_preview as m  # noqa: E402
import identity  # noqa: E402

from test_coverage_orchestration import (  # noqa: E402,F401
    world, _mk_pr, PATH_MAP, BASE_SHA, PR_SHA)

SPOKE = "gcp/aec/private-cloud/na1-b"
COHORT = f"{SPOKE}/monthly/config.yaml"
X = f"{SPOKE}/monthly/pv-x--aec1-a/customer.yaml"
Y = f"{SPOKE}/monthly/pv-y--aec1-a/customer.yaml"
Z = f"{SPOKE}/monthly/pv-z--aec1-a/customer.yaml"
W = f"{SPOKE}/monthly/pv-w--aec1-a/customer.yaml"
PROD = f"{SPOKE}/monthly/pv-x-a/customer.yaml"
OLD_X = f"{SPOKE}/weekly/pv-x--aec1-a/customer.yaml"
ENV = "pv-x--aec1-a"
TRAILER = f"Confirm-Clone-Sanitized: {ENV}"
TEXT = cr.GATES["clone_wake"][0]
SK = (m.BB_REPO, 991)
H, B = PR_SHA, BASE_SHA


def _doc(zero=None, name="x--aec1"):
    return ("appspace:\n" + (f"  zeroPods: {zero}\n" if zero is not None else "")
            + f"  customerName: {name}\n  version: 2603.2.19\n")


COHORT_DOC = "appspace:\n  version: 2603.2.19\n"


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


def _no_chain(monkeypatch):
    monkeypatch.setattr(m, "_merged_kcc_flat_for_env",
                        lambda *a, **k: pytest.fail("the value chain was read"))


def _detect(changed, renames=None, path_map=None, new=(), base_sha=B):
    cands = [{"name": p.split("/")[-2], "config_file": p,
              "env_dir": posixpath.dirname(p), "all_yaml_files": [p]} for p in new]
    return m._detect_clone_wakes(changed, renames or {}, path_map or {}, cands, H, base_sha)


def _gate(env=ENV, why="new clone", to=None):
    return {"kind": "clone_wake", "env": env, "why": why, "to": to}


# ── the trailer ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("line", [
    f"Confirm-Clone-Sanitized: {ENV}",
    "confirm-clone-sanitized: `PV-X--AEC1-A`.",
    f"  > `Confirm-Clone-Sanitized: {ENV}`"])
def test_the_trailer_reads_in_any_case_and_with_backticks(line):
    assert identity._confirmations(["Wake it", f"x\n{line}\n"]) == {
        f"confirm-clone-sanitized: {ENV}"}


# ── new clones ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("own,cohort,gates", [
    (_doc(), COHORT_DOC, [_gate()]),
    (_doc("false"), COHORT_DOC, [_gate(to="false")]),
    (_doc("true"), COHORT_DOC, []),
    (_doc(), "appspace:\n  zeroPods: true\n", []),     # asleep from the cohort
])
def test_a_new_clone_that_is_not_asleep_is_a_gate(monkeypatch, own, cohort, gates):
    _serve(monkeypatch, {X: own, COHORT: cohort})
    assert _detect([X], new=[X]) == gates


def test_a_new_clone_under_values_that_cannot_be_parsed_is_a_gate(monkeypatch):
    _serve(monkeypatch, {X: _doc(), "gcp/aec/config.yaml": "appspace: [\n"})
    assert _detect([X], new=[X]) == [_gate(why="values cannot be parsed")]


# ── live clones ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("was,now,why,to", [
    ("true", "false", "zeroPods true to false", "false"),
    ("true", None, "zeroPods true to unset", None),
])
def test_a_live_clone_that_wakes_is_a_gate(monkeypatch, was, now, why, to):
    _serve(monkeypatch, {X: _doc(now)}, {X: _doc(was)})
    assert _detect([X], path_map={X: ["a"]}) == [_gate(why=why, to=to)]


@pytest.mark.parametrize("was,now", [("false", "false"), ("true", "true"), (None, None)])
def test_an_unchanged_zeropods_reads_no_chain(monkeypatch, was, now):
    """A version bump on a clone is the common shape: two own reads, no chain."""
    _serve(monkeypatch, {X: _doc(now).replace("2603.2.19", "2603.2.20")}, {X: _doc(was)})
    _no_chain(monkeypatch)
    assert _detect([X], path_map={X: ["a"]}) == []


@pytest.mark.parametrize("was,now,gates", [
    (None, "false", []), ("false", "true", []),
    ("true", "0", [_gate(why="zeroPods true to 0", to="0")])])
def test_a_changed_zeropods_reads_the_chain(monkeypatch, was, now, gates):
    _serve(monkeypatch, {X: _doc(now)}, {X: _doc(was)})
    assert _detect([X], path_map={X: ["a"]}) == gates


def test_a_live_clone_whose_values_cannot_be_parsed_is_a_gate(monkeypatch):
    _serve(monkeypatch, {X: "appspace: [\n"}, {X: _doc("true")})
    assert _detect([X], path_map={X: ["a"]}) == [_gate(why="values cannot be parsed")]


@pytest.mark.parametrize("now,gates", [
    ("false", [_gate(why="zeroPods true to false", to="false")]), ("true", [])])
def test_a_moved_clone_reads_the_old_path_at_base(monkeypatch, now, gates):
    _serve(monkeypatch, {X: _doc(now)}, {OLD_X: _doc("true")})
    assert _detect([OLD_X, X], {OLD_X: X}, {OLD_X: ["a"]}) == gates


def test_a_removed_clone_is_not_a_wake(monkeypatch):
    _serve(monkeypatch, {}, {X: _doc("true")})
    _no_chain(monkeypatch)
    assert _detect([X], path_map={X: ["a"]}) == []


# ── a config.yaml above the clones ───────────────────────────────────────

def test_a_cohort_that_drops_zeropods_wakes_each_clone_below(monkeypatch):
    base = {COHORT: "appspace:\n  zeroPods: true\n  version: 2603.2.19\n",
            X: _doc(), Y: _doc(name="y--aec1"), Z: _doc("true", "z--aec1"),
            W: _doc(name="w--aec1"), PROD: _doc(name="x")}
    head = dict(base, **{COHORT: COHORT_DOC})
    del head[W]                                   # removed in the same PR
    _serve(monkeypatch, head, base)
    path_map = {p: ["a"] for p in (COHORT, X, Y, Z, W, PROD, OLD_X)}
    assert _detect([COHORT, W], path_map=path_map) == [
        _gate(why="zeroPods true to unset"),
        _gate("pv-y--aec1-a", why="zeroPods true to unset")]


def test_a_cohort_edit_that_keeps_zeropods_reads_no_chain(monkeypatch):
    _serve(monkeypatch, {COHORT: COHORT_DOC.replace("19", "20"), X: _doc()},
           {COHORT: COHORT_DOC, X: _doc()})
    _no_chain(monkeypatch)
    assert _detect([COHORT], path_map={COHORT: ["a"], X: ["a"]}) == []


# ── what is never checked ────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    PROD,                                                         # not a clone
    "gcp/dev/private-cloud/ap1/sandbox/pv-x--sbx1-a/customer.yaml",   # a sandbox
    f"{SPOKE}/monthly/pv-x--aec1-a/cicd-versions.yaml",           # not the identity
    COHORT,                                                       # no clone below
])
def test_other_files_are_never_read(monkeypatch, path):
    monkeypatch.setattr(m, "_bb_fetch_status", lambda *a, **k: pytest.fail("read"))
    assert _detect([path], path_map={path: ["a"], PROD: ["a"]}, new=[path]) == []


def test_with_no_base_only_new_clones_are_checked(monkeypatch):
    _serve(monkeypatch, {X: _doc("false"), Y: _doc(name="y--aec1")}, {X: _doc("true")})
    assert _detect([X, Y], path_map={X: ["a"]}, new=[Y], base_sha="") == [
        _gate("pv-y--aec1-a")]


@pytest.mark.parametrize("error,new", [(X, ()), ("gcp/aec/config.yaml", [X])])
def test_a_failed_read_retries_and_is_never_a_gate(monkeypatch, error, new):
    """Bitbucket could not serve a file: the PR retries (critic, issue 1)."""
    _serve(monkeypatch, {X: _doc("false")}, {X: _doc("true")}, error=(error,))
    with pytest.raises(m.ValueFileUnreadable):
        _detect([X], path_map={} if new else {X: ["a"]}, new=new)


# ── the panel ────────────────────────────────────────────────────────────

MONGO = [
    ("A clone starts with a copy of the production data, so it can call the customer "
    "live integrations: SSO, webhooks, mail and connected apps (AE-15507). `zeroPods` "
    "does not stop the Core VM."), "",
    ("Before you merge, check on the clone Mongo that these are empty, and write the "
    "counts in the PR:"), "",
    "- `passport.passports`", "- `mail.smtpConfigurations`",
    "- `integrationwebhook.webhookSubscriptions`",
    "- `authorization.authorizationregistrations`",
    "- the `applicationintegration` database",
    "- `cacsJwts` in `feedconnector`, `contentconversion`, `userinbox` and `mention`", "",
    "Runbook: https://appspace.atlassian.net/wiki/spaces/cops/pages/66093626 step 2."]


def test_the_panel_names_each_env_and_the_open_trailers():
    gates = m._merge_gates((), extra=[
        _gate(), _gate("pv-y--aec1-a", "zeroPods true to false", "false"),
        _gate("pv-z--aec1-a", "zeroPods true to unset"),
        _gate("pv-w--aec1-a", "values cannot be parsed"), {"kind": "shrink", "env": ""}])
    gates[1]["lifted"] = True
    assert m._clone_wake_lines(gates) == [
        "## \U0001f6a8 AEC CLONE STARTS RUNNING", "",
        f"- `{ENV}` is a new clone and it starts running on merge. Add `zeroPods: true` "
        "under `appspace:` to create it asleep, restore and clean the data, and wake "
        "it in a later PR.",
        ("- `pv-y--aec1-a` starts running with this PR (`zeroPods` goes from `true` to "
        "`false`)."),
        ("- `pv-z--aec1-a` starts running with this PR (`zeroPods` goes from `true` to "
        "unset)."),
        ("- `pv-w--aec1-a` can start running with this PR: its values cannot be parsed, "
        "so `zeroPods` is unknown."),
        "", *MONGO, "", "Then confirm with an empty commit:", "", "```",
        f'git commit --allow-empty -m "{TRAILER}"',
        'git commit --allow-empty -m "Confirm-Clone-Sanitized: pv-z--aec1-a"',
        'git commit --allow-empty -m "Confirm-Clone-Sanitized: pv-w--aec1-a"',
        "git push", "```", ""]


def test_a_confirmed_panel_asks_for_no_commit():
    gates = m._merge_gates((), extra=[_gate()])
    gates[0]["lifted"] = True
    lines = m._clone_wake_lines(gates)
    assert lines[-len(MONGO) - 1:] == MONGO + [""] and "```" not in lines
    assert m._clone_wake_lines(m._merge_gates((), extra=[{"kind": "shrink", "env": ""}])) == []
    assert m._clone_wake_lines(None) == []


# ── process_pr, the new-env-only path ────────────────────────────────────

NEW_FILES = {X: _doc(), COHORT: COHORT_DOC}
RENDERED = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: platform-config\n"
NEW_DESC = (f"Blocked - {TEXT} (new clone) in {ENV}. To merge anyway, add "
            f"'{TRAILER}' to a commit message (see PR comment)")


def _commits(monkeypatch, messages):
    def read(*a):
        if messages is None:
            pytest.fail("commits read with no trailer to find")
        if isinstance(messages, Exception):
            raise messages
        return list(messages)
    monkeypatch.setattr(m, "_pr_commit_messages", read)


@pytest.fixture()
def new_clone(world, monkeypatch):
    """A PR that adds pv-x--aec1-a and touches no live app."""
    sinks, _plan = world
    monkeypatch.setattr(m, "get_pr_changed_files", lambda pr_id, repo=None: ([X], {}))
    monkeypatch.setattr(m, "_render_new_env_diff",
                        lambda env_info, pr_sha: (RENDERED, None, 1, "2603.2.19"))

    def run(messages=None, own=_doc()):
        _serve(monkeypatch, dict(NEW_FILES, **{X: own}))
        _commits(monkeypatch, messages)
        m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
        return sinks.upserts[-1], sinks.statuses[-1]
    return run


def test_a_new_clone_blocks_the_new_env_path(new_clone):
    body, (state, desc) = new_clone(["Add the clone"])
    assert m._extract_status_token(body) == "blocked"
    assert state == "FAILED" and desc == NEW_DESC
    assert (f"- ⛔ **{TEXT} (new clone)** in `{ENV}` - to merge anyway, add "
            f"`{TRAILER}` to a commit message") in body
    assert body.index(cr.MERGE_SUMMARY_HDR) < body.index("AEC CLONE STARTS RUNNING") \
        < body.index("New Environment(s) Detected")
    assert f'git commit --allow-empty -m "{TRAILER}"' in body
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA)


def test_the_trailer_lifts_the_new_clone(new_clone):
    body, (state, desc) = new_clone([f"Add the clone\n\n{TRAILER}\n"])
    assert m._extract_status_token(body) == "clean" and state == "SUCCESSFUL"
    assert f"☑️ **Confirmed in a commit:** `{TRAILER}` ({TEXT} (new clone))" in body
    assert desc.startswith(f"⚠️ Confirmed in a commit: {TRAILER}")
    assert desc.endswith("1 new environment(s), ~1 resource(s) to create")
    assert "AEC CLONE STARTS RUNNING" in body and "git commit --allow-empty" not in body


def test_a_trailer_for_another_env_does_not_lift_it(new_clone):
    body, (state, _desc) = new_clone(["Confirm-Clone-Sanitized: pv-y--aec1-a"])
    assert m._extract_status_token(body) == "blocked" and state == "FAILED"


def test_unreadable_commits_retry_and_never_lift(new_clone):
    body, (state, desc) = new_clone(m.PrCommitsUnreadable("not ready"))
    assert m._extract_status_token(body) == "transient"
    assert state == "FAILED" and "will retry" in desc
    assert SK not in m._seen and SK in m._retry_backoff


def test_a_new_clone_created_asleep_is_clean(new_clone):
    body, (state, _desc) = new_clone(None, _doc("true"))
    assert m._extract_status_token(body) == "clean" and state == "SUCCESSFUL"
    assert "AEC CLONE" not in body


# ── process_pr, the diff path ────────────────────────────────────────────

WAKE_DESC = (f"Blocked - {TEXT} (zeroPods true to false) in {ENV}. To merge anyway, "
             f"add '{TRAILER}' to a commit message (see PR comment)")


@pytest.fixture()
def live(world, monkeypatch):
    """PRs on live clones: `envs` {identity path: (base text, head text)}."""
    sinks, _plan = world

    def run(envs, messages=None, cohorts=()):
        head, base, path_map = {}, {}, {}
        for path, (was, now) in envs.items():
            app = path.split("/")[-2] + "-ms"
            base[path], head[path] = was, now
            path_map[path] = [app]
            cohort = posixpath.dirname(posixpath.dirname(path)) + "/config.yaml"
            head.setdefault(cohort, COHORT_DOC)
            base.setdefault(cohort, COHORT_DOC)
            path_map.setdefault(cohort, []).append(app)
            monkeypatch.setitem(m._app_chart_map, app, "appspace-ms")
            monkeypatch.setitem(m._app_chart_revision_map, app, "2603.2.17")
        for c in cohorts:
            head[c] = COHORT_DOC.replace("19", "20")
        changed = sorted(envs) + list(cohorts)
        monkeypatch.setattr(m, "get_pr_changed_files", lambda pr_id, repo=None: (changed, {}))
        _serve(monkeypatch, head, base)
        _commits(monkeypatch, messages)
        m.process_pr(_mk_pr(), path_map, base_sha=BASE_SHA)
        return sinks.upserts[-1], sinks.statuses[-1]
    return run


def test_a_live_clone_that_wakes_blocks_the_diff_path(live):
    body, (state, desc) = live({X: (_doc("true"), _doc("false"))}, ["Wake it"])
    assert m._extract_status_token(body) == "blocked"
    assert state == "FAILED" and desc == WAKE_DESC
    assert (f"- `{ENV}` starts running with this PR (`zeroPods` goes from `true` to "
            "`false`).") in body
    assert body.index(cr.MERGE_SUMMARY_HDR) < body.index("AEC CLONE STARTS RUNNING")


def test_the_trailer_lifts_the_wake(live):
    body, (state, _desc) = live({X: (_doc("true"), _doc("false"))}, [TRAILER])
    assert m._extract_status_token(body) == "clean" and state == "SUCCESSFUL"
    assert f"☑️ **Confirmed in a commit:** `{TRAILER}`" in body


def test_a_version_bump_on_a_sleeping_clone_is_clean(live):
    body, (state, _desc) = live({X: (_doc("true"), _doc("true").replace("19", "20"))})
    assert m._extract_status_token(body) == "clean" and state == "SUCCESSFUL"
    assert "AEC CLONE" not in body


# acme-config-prod #4660 (0c6042b56): 13 clones woken in one PR, and three
# cohort config.yaml version bumps. The ticket says 14; the merge has 13
# `zeroPods: true` -> `false` lines.
PR_4660 = ["au1-b/hardcoded/pv-deloitte--aec1-a", "eu1-b/monthly/pv-dell--aec1-a",
           "gb1-b/pv-hsbc--aec1-a", "na1-b/monthly/pv-igds--aec1-a",
           "na1-b/monthly/pv-nike--aec1-a", "na1-b/monthly/pv-winston--aec1-a",
           "na2-a/monthly/pv-bnym--aec1-a", "na2-a/monthly/pv-disney--aec1-a",
           "na2-a/monthly/pv-ford--aec1-b", "na2-a/monthly/pv-gmds--aec1-a",
           "na2-a/monthly/pv-heb--aec1-b", "na2-a/monthly/pv-universal--aec1-b",
           "na2-a/weekly/pv-blackrock--aec1-a"]


def test_pr_4660_gives_one_gate_per_clone(live):
    envs = {}
    for d in PR_4660:
        name = re.sub(r"^pv-|-[ab]$", "", d.split("/")[-1])
        envs[f"gcp/aec/private-cloud/{d}/customer.yaml"] = (
            _doc("true", name).replace("19", "17"), _doc("false", name))
    cohorts = [f"gcp/aec/private-cloud/{c}/config.yaml"
               for c in ("na1-b/monthly", "na2-a/monthly", "na2-a/weekly")]
    body, (state, desc) = live(envs, ["Wake the clones"], cohorts)
    assert m._extract_status_token(body) == "blocked" and state == "FAILED"
    assert desc == (f"Blocked - {TEXT} (zeroPods true to false) in pv-deloitte--aec1-a. "
                    "To merge anyway, add 'Confirm-Clone-Sanitized: pv-deloitte--aec1-a' "
                    "to a commit message (+12 more) (see PR comment)")
    assert body.count("git commit --allow-empty -m \"Confirm-Clone-Sanitized:") == 13
    assert body.count(f"- ⛔ **{TEXT} (zeroPods true to false)**") == 13


@pytest.mark.parametrize("env,why", [
    ("pv-universalhollywood--aec1-a", "zeroPods true to false"),   # the longest real name
    ("pv-universalhollywood20--aec1-ab", "zeroPods true t..."),
])
def test_the_pr_4660_status_fits_and_keeps_the_trailer(env, why):
    """13 wakes: only the reason is cut to fit 255 bytes, never the trailer."""
    desc = cr.gate_status_description([_gate(env, "zeroPods true to false")] * 13)
    assert len(desc.encode()) <= 255
    assert desc == (f"Blocked - {TEXT} ({why}) in {env}. To merge anyway, add "
                    f"'Confirm-Clone-Sanitized: {env}' to a commit message (+12 more) "
                    "(see PR comment)")


def _six_gates(env):
    """A wake on env first, then the other 5 gates of blocks 3 and 4 on it."""
    return [_gate(env, "zeroPods true to false")] + [
        {"kind": k, "env": env, "arg": env} for k in
        ("dup_identity", "ashn_copy", "vm_disk", "appset_miss", "legacy_helm")]


def test_a_63_character_env_fits_the_status_with_the_whole_trailer():
    """The namespace maximum: the trailer then comes alone, as it names the env."""
    env = "pv-" + "u" * 54 + "--aec1"
    assert len(env) == 63
    desc = cr.gate_status_description(_six_gates(env))
    assert len(desc.encode()) <= 255
    assert desc == (f"Blocked - {TEXT} (zeroPods true to false). To merge anyway, add "
                    f"'Confirm-Clone-Sanitized: {env}' (+5 more) (see PR comment)")
    footer = cr.gate_footer(_six_gates(env))
    assert m._GATE_FOOTER_RE.findall(f"**Status:** x{footer}\n") == [desc]


def test_a_status_never_goes_over_255_bytes():
    """A name no namespace allows: the rest is cut, and the end stays."""
    desc = cr.gate_status_description(_six_gates("pv-" + "\u00e9" * 150))
    assert len(desc.encode()) <= 255 and desc.startswith(f"Blocked - {TEXT}. To merge")
    assert desc.endswith("... (+5 more) (see PR comment)")


# ── golden ───────────────────────────────────────────────────────────────

GOLDEN_DIR = os.path.join(os.path.dirname(__file__), "golden")
G_PR_SHA = "abc12345def67890abc12345def67890abc12345"
G_BASE_SHA = "0000111122223333444455556666777788889999"


def test_golden_clone_wake(monkeypatch):
    """The diff comment of a live clone that wakes, with its gate open."""
    monkeypatch.setattr(m, "_ts", lambda: "2026-01-01 00:00 UTC")
    monkeypatch.setattr(m, "_repo_for_sha", lambda sha: "acme-config-prod")
    gates = m._merge_gates((), extra=[_gate(why="zeroPods true to false", to="false")])
    results = {f"{ENV}-ms": m.DiffResult(
        "--- main\n+++ pr",
        [("/apps/Deployment pv-x--aec1-a/api-gateway", "-  replicas: 0\n+  replicas: 2")],
        1, True, "", m.OUT_DIFF, "")}
    body = m.format_comment(G_PR_SHA, results, base_sha=G_BASE_SHA,
                            appspace_state_lines=m._clone_wake_lines(gates), gates=gates)
    assert m._extract_status_token(body) == "blocked"
    path = os.path.join(GOLDEN_DIR, "clone_wake.md")
    if os.environ.get("UPDATE_GOLDEN") == "1":
        with open(path, "w") as f:
            f.write(body)
        pytest.skip("golden rewritten: clone_wake")
    with open(path) as f:
        assert body == f.read(), "the clone wake comment changed: read the diff"
