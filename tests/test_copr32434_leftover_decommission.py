"""COPR-32434: leftover Argo apps after an earlier decommission must not block.

A mid-prune Application still appears in discover_path_app_map(). Its
identity file is gone on both main and the PR head, but this PR did not
delete it, so _detect_env_decommission_candidates never sees it. Without
this fix the app enters the normal render pipeline, fails Helm required,
and posts a blocking MISSING REQUIRED VALUE that tells reviewers DO NOT
MERGE on every unrelated PR.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_preview as m  # noqa: E402
from test_coverage_orchestration import (  # noqa: E402
    world, _mk_pr, BASE_SHA,
)


ENV_DIR = "gcp/aec/private-cloud/na1-b/weekly/pv-united--aec1-a"
IDENTITY = f"{ENV_DIR}/customer.yaml"
SHARED = "gcp/aec/private-cloud/na1-b/monthly/config.yaml"

PATH_MAP = {
    IDENTITY: ["pv-united--aec1-a-ms", "pv-united--aec1-a-glb"],
    SHARED: [
        "pv-united--aec1-a-ms",
        "pv-united--aec1-a-glb",
        "pv-alive-a-ms",
    ],
    "gcp/aec/private-cloud/na1-b/monthly/pv-alive-a/customer.yaml": ["pv-alive-a-ms"],
}


def test_leftover_reason_is_neither_permanent_nor_retryable():
    assert m.REASON_LEFTOVER_DECOMMISSION not in m.PERMANENT_REASONS
    assert m.REASON_LEFTOVER_DECOMMISSION not in m.RETRYABLE_REASONS


def test_identity_file_for_app_resolves_env_leaf_not_shared_ancestor():
    assert m._identity_file_for_app("pv-united--aec1-a-ms", PATH_MAP) == IDENTITY
    assert m._identity_file_for_app("pv-united--aec1-a-glb", PATH_MAP) == IDENTITY
    assert m._identity_file_for_app(
        "pv-alive-a-ms", PATH_MAP
    ) == "gcp/aec/private-cloud/na1-b/monthly/pv-alive-a/customer.yaml"
    # Shared monthly config alone must never be treated as an identity file.
    alone = {SHARED: ["pv-united--aec1-a-ms"]}
    assert m._identity_file_for_app("pv-united--aec1-a-ms", alone) is None


def test_skip_leftover_when_identity_absent_on_both_shas(monkeypatch):
    fetches = []

    def fake_fetch(path, sha, repo=None):
        fetches.append((path, sha))
        if path == IDENTITY:
            return None, m.BB_NOT_FOUND
        return "appspace:\n  customerName: alive\n", m.BB_OK

    monkeypatch.setattr(m, "_bb_fetch_cached", fake_fetch)
    leftover = m._apps_to_skip_as_leftover_decommission(
        ["pv-united--aec1-a-ms", "pv-united--aec1-a-glb", "pv-alive-a-ms"],
        PATH_MAP, "base" * 8, "prhd" * 8)
    assert set(leftover) == {"pv-united--aec1-a-ms", "pv-united--aec1-a-glb"}
    assert leftover["pv-united--aec1-a-ms"] == IDENTITY
    # Alive env still has its identity — must stay in the render set.
    assert "pv-alive-a-ms" not in leftover
    # Two apps share one identity file: two shas, not four fetches.
    assert fetches.count((IDENTITY, "base" * 8)) == 1
    assert fetches.count((IDENTITY, "prhd" * 8)) == 1


def test_fail_closed_when_base_still_has_identity(monkeypatch):
    """This PR is deleting the env: base OK, PR 404 — not a leftover."""

    def fake_fetch(path, sha, repo=None):
        if path == IDENTITY and sha.startswith("pr"):
            return None, m.BB_NOT_FOUND
        if path == IDENTITY:
            return "appspace:\n  customerName: united\n", m.BB_OK
        return "x", m.BB_OK

    monkeypatch.setattr(m, "_bb_fetch_cached", fake_fetch)
    leftover = m._apps_to_skip_as_leftover_decommission(
        ["pv-united--aec1-a-ms"], PATH_MAP, "base" * 8, "prhd" * 8)
    assert leftover == {}


def test_fail_closed_on_transient_fetch_error(monkeypatch):
    def fake_fetch(path, sha, repo=None):
        if path == IDENTITY and sha.startswith("base"):
            return None, m.BB_ERROR
        if path == IDENTITY:
            return None, m.BB_NOT_FOUND
        return "x", m.BB_OK

    monkeypatch.setattr(m, "_bb_fetch_cached", fake_fetch)
    leftover = m._apps_to_skip_as_leftover_decommission(
        ["pv-united--aec1-a-ms"], PATH_MAP, "base" * 8, "prhd" * 8)
    assert leftover == {}, "BB_ERROR must never be treated as absence"


def test_already_skipped_confirmed_decommission_is_not_reclassified(monkeypatch):
    def fake_fetch(path, sha, repo=None):
        return None, m.BB_NOT_FOUND

    monkeypatch.setattr(m, "_bb_fetch_cached", fake_fetch)
    leftover = m._apps_to_skip_as_leftover_decommission(
        ["pv-united--aec1-a-ms"], PATH_MAP, "base" * 8, "prhd" * 8,
        already_skipped={"pv-united--aec1-a-ms"})
    assert leftover == {}


def test_format_comment_renders_leftover_informational_not_blocking():
    result = m.DiffResult(
        "", [], 0, False, IDENTITY,
        m.OUT_LEFTOVER_DECOMMISSIONED, m.REASON_LEFTOVER_DECOMMISSION)
    body = m.format_comment(
        "deadbeef01234567",
        {"pv-united--aec1-a-ms": result},
        leftover_lines=m._leftover_decommission_lines(
            {"pv-united--aec1-a-ms": IDENTITY}))
    lower = body.lower()
    assert "leftover" in lower
    assert "prior decommission" in lower or "earlier decommission" in lower
    assert "do not merge" not in lower
    assert "missing required" not in lower
    assert "pv-united--aec1-a-ms" in body
    # Must not reuse the this-PR decommission wording that blames the author.
    assert "environment decommissioned (see warning above)" not in lower


def test_process_pr_skips_leftover_apps_before_normal_diff(world, monkeypatch):
    """Regression for the live shape of acme-config-prod #4591."""
    sinks, plan = world
    # Shared monthly config change matches the zombie app via path_map.
    monkeypatch.setattr(
        m, "get_pr_changed_files",
        lambda pr_id, repo=None: ([SHARED], {}))

    def fake_fetch(path, sha, repo=None):
        if path == IDENTITY:
            return None, m.BB_NOT_FOUND
        if path == SHARED:  # a cohort config carries no customer names
            return "appspace:\n  version: 1.0.0\n", m.BB_OK
        return "appspace:\n  customerName: alive\n  version: 1.0.0\n", m.BB_OK

    monkeypatch.setattr(m, "_bb_fetch_cached", fake_fetch)
    monkeypatch.setattr(m, "_merge_preview", lambda *a, **k: (None, None))
    path_map = {
        IDENTITY: ["pv-united--aec1-a-ms"],
        SHARED: ["pv-united--aec1-a-ms", "pv-alive-a-ms"],
        "gcp/aec/private-cloud/na1-b/monthly/pv-alive-a/customer.yaml":
            ["pv-alive-a-ms"],
    }
    m._app_chart_map.update({
        "pv-united--aec1-a-ms": "appspace-ms",
        "pv-alive-a-ms": "appspace-ms",
    })
    m._app_chart_revision_map.update({
        "pv-united--aec1-a-ms": "2603.0.1",
        "pv-alive-a-ms": "2603.0.1",
    })
    plan["pv-alive-a-ms"] = m.DiffResult(
        "", [], 0, False, None, m.OUT_NO_DIFF, None)
    m.process_pr(_mk_pr(pr_id=32434), path_map, base_sha=BASE_SHA)
    assert "pv-united--aec1-a-ms" not in sinks.diff_calls, (
        "leftover app must never enter the normal diff pipeline")
    assert sinks.upserts, "comment must be posted"
    body = sinks.upserts[-1]
    assert "leftover" in body.lower()
    state, desc = sinks.statuses[-1]
    assert state == "SUCCESSFUL", (
        f"leftover alone must not block the PR; got {state}: {desc}")
