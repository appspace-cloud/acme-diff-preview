"""v2.12.0 — guard against a null/empty microservices.definitions map (COPR-31637).

Incident: a `cicd-versions.yaml` (an ArgoCD Helm value file, last in the
valueFiles list) was committed with

    appspace:
      microservices:
        definitions:

i.e. the `definitions:` key present but with NO children -> YAML null. Because
that file is merged LAST over the chart's own values, `merge` collapses the
entire microservices.definitions map to null, wiping every per-service
`image.name` override (appspace-platformservice, appspace-webhookservice,
appspace-screenshot, ...). Every affected microservice then falls back to the
helper's derived `appspace-<key>` name, which for these services is a
repository that has never held a single image -> ImagePullBackOff across the
whole environment.

Root cause commit: 1015bc622 "remove CICD" (deleted the children, left the
key). This guard makes acme-diff-preview BLOCK any PR that reintroduces the
pattern, with a red build status and an explicit danger explanation.

Confirmed RED against v2.11.0 (function did not exist).
"""
import os
import sys

import pytest

os.environ.setdefault("BB_USER", "test-user")
os.environ.setdefault("BB_TOKEN", "test-token")
os.environ.setdefault("ARGOCD_PASS", "test-pass")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as dp


# --- The pure YAML-shape classifier -----------------------------------------

def test_null_definitions_is_flagged():
    body = "appspace:\n  microservices:\n    definitions:\n"
    assert dp._values_wipes_definitions(body) is True


def test_empty_map_definitions_is_flagged():
    body = "appspace:\n  microservices:\n    definitions: {}\n"
    assert dp._values_wipes_definitions(body) is True


def test_definitions_with_children_is_ok():
    body = (
        "appspace:\n"
        "  microservices:\n"
        "    definitions:\n"
        "      account:\n"
        "        image:\n"
        "          tag: 1.2.3\n"
    )
    assert dp._values_wipes_definitions(body) is False


def test_file_without_definitions_key_is_ok():
    # Absent key must NOT be flagged — merge leaves the chart's map intact.
    body = "appspace:\n  microservices:\n    repository: some/repo\n"
    assert dp._values_wipes_definitions(body) is False


def test_no_microservices_block_is_ok():
    body = "appspace:\n  version: 2601.4.18\n"
    assert dp._values_wipes_definitions(body) is False


def test_malformed_yaml_is_not_flagged():
    # A parse error must not be reported as the dangerous pattern (avoid false
    # blocks on unrelated syntax mistakes; those fail elsewhere in the render).
    body = "appspace:\n  microservices:\n    definitions:\n  : : bad"
    assert dp._values_wipes_definitions(body) is False


def test_empty_file_is_ok():
    assert dp._values_wipes_definitions("") is False


def test_non_mapping_yaml_is_ok():
    # A YAML doc that parses to a non-dict (list/scalar) must not be flagged.
    assert dp._values_wipes_definitions("- just\n- a\n- list\n") is False
    assert dp._values_wipes_definitions("42\n") is False


# --- The changed-files scan (fetches each candidate at the PR sha) ----------

def test_scan_flags_only_wiped_files(monkeypatch):
    good = (
        "appspace:\n  microservices:\n    definitions:\n"
        "      account:\n        image:\n          tag: 1.0.0\n"
    )
    wiped = "appspace:\n  microservices:\n    definitions:\n"

    def fake_fetch(path, sha, repo=None):
        if path.endswith("pv-broken/cicd-versions.yaml"):
            return wiped, dp.BB_OK
        if path.endswith("pv-ok/cicd-versions.yaml"):
            return good, dp.BB_OK
        return None, dp.BB_NOT_FOUND

    monkeypatch.setattr(dp, "_bb_fetch_status", fake_fetch)

    changed = [
        "gcp/qa/private-cloud/ap1/custom/pv-broken/cicd-versions.yaml",
        "gcp/qa/private-cloud/ap1/custom/pv-ok/cicd-versions.yaml",
        "gcp/qa/private-cloud/ap1/custom/pv-broken/customer.yaml",  # not a values file
    ]
    hits = dp._detect_wiped_definitions(changed, "deadbeef", repo="acme-config-dev")
    assert hits == ["gcp/qa/private-cloud/ap1/custom/pv-broken/cicd-versions.yaml"]


def test_scan_ignores_non_value_files(monkeypatch):
    # Only *.yaml/*.yml value files are inspected; a README with the text
    # must never be fetched or flagged.
    called = []

    def fake_fetch(path, sha, repo=None):
        called.append(path)
        return "appspace:\n  microservices:\n    definitions:\n", dp.BB_OK

    monkeypatch.setattr(dp, "_bb_fetch_status", fake_fetch)
    hits = dp._detect_wiped_definitions(["docs/README.md"], "sha", repo="r")
    assert hits == []
    assert called == []


def test_scan_transient_fetch_error_does_not_block(monkeypatch):
    # A transient fetch error must NOT be treated as the dangerous pattern —
    # blocking a merge on a flaky network read would be worse than the miss.
    def fake_fetch(path, sha, repo=None):
        return None, dp.BB_ERROR

    monkeypatch.setattr(dp, "_bb_fetch_status", fake_fetch)
    hits = dp._detect_wiped_definitions(
        ["gcp/qa/private-cloud/ap1/custom/pv-x/cicd-versions.yaml"],
        "sha", repo="r")
    assert hits == []


# --- Integration: process_pr must BLOCK the merge on a wiped definitions ------

from test_coverage_orchestration import world, _mk_pr, PATH_MAP, BASE_SHA  # noqa: E402,F401


def test_process_pr_blocks_on_wiped_definitions(world, monkeypatch):
    sinks, plan = world
    wiped_file = "gcp/qa/private-cloud/ap1/custom/pv-qa11-a/cicd-versions.yaml"
    monkeypatch.setattr(dp, "get_pr_changed_files",
                        lambda pr_id, repo=None: ([wiped_file], {}))
    monkeypatch.setattr(dp, "_bb_fetch_status",
                        lambda path, sha, repo=None:
                        ("appspace:\n  microservices:\n    definitions:\n", dp.BB_OK))
    dp.process_pr(_mk_pr(pr_id=31637), PATH_MAP, base_sha=BASE_SHA)
    states = [s for s, _ in sinks.statuses]
    assert states[-1] == "FAILED", sinks.statuses
    body = sinks.upserts[-1]
    assert "blocked" in body.lower()
    assert "definitions" in body
    assert wiped_file in body


def test_process_pr_does_not_block_when_definitions_populated(world, monkeypatch):
    sinks, plan = world
    good_file = "gcp/qa/private-cloud/ap1/custom/pv-ok-a/cicd-versions.yaml"
    monkeypatch.setattr(dp, "get_pr_changed_files",
                        lambda pr_id, repo=None: ([good_file], {}))
    monkeypatch.setattr(dp, "_bb_fetch_status",
                        lambda path, sha, repo=None:
                        ("appspace:\n  microservices:\n    definitions:\n"
                         "      account:\n        image:\n          tag: 1.0.0\n", dp.BB_OK))
    dp.process_pr(_mk_pr(pr_id=31638), PATH_MAP, base_sha=BASE_SHA)
    # Must NOT be blocked: no FAILED "blocked" comment.
    assert not any("[blocked]" in b for b in sinks.upserts), sinks.upserts


# --- Coverage: the input-changes panel must never break the comment ----------

def test_input_changes_panel_exception_does_not_break_comment(world, monkeypatch):
    # When _summarize_input_changes raises, process_pr must swallow it, log a
    # warning, and still post the diff comment + a SUCCESSFUL status (the panel
    # is best-effort). Exercises the defensive except branch.
    sinks, plan = world
    plan["pv-orch-a-ms"] = dp.DiffResult(
        "--- main\n+++ pr", [("Deployment/webx", "-replicas: 2\n+replicas: 3")],
        1, True, "", dp.OUT_DIFF, "")

    def boom(*a, **k):
        raise RuntimeError("panel exploded")

    monkeypatch.setattr(dp, "_summarize_input_changes", boom)
    dp.process_pr(_mk_pr(pr_id=31639), PATH_MAP, base_sha=BASE_SHA)

    assert len(sinks.upserts) == 1
    states = [s for s, _ in sinks.statuses]
    assert states[-1] == "SUCCESSFUL", states
