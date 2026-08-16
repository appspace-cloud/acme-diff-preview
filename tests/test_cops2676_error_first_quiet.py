"""COPS-2676: fleet permanent render failures are error-first and quiet.

On acme-config-prod #4310 the Missing Image Tag block was correct but sat
~41% into the comment under deletions, a huge Changeset overview, and
routine bump narratives. Operators could not see the real blocker.

Contract:
- Merge summary names the error.
- RENDER BLOCKED panel is immediately under Merge summary.
- Quiet mode (>=3 blocked envs on the comment) collapses overview / bumps.
"""
import os
import sys

import pytest

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m
import render_profile

GOLDEN_DIR = os.path.join(os.path.dirname(__file__), "golden")
FIXED_TS = "2026-01-01 00:00 UTC"
PR_SHA = "abc12345def67890abc12345def67890abc12345"
BASE_SHA = "0000111122223333444455556666777788889999"
ART_URL = "https://diffs.appspace.example/diff/acme-config-prod/42/abc12345"

_MISSING_ERR = (
    "Error: execution error at "
    "(platform/templates/configmaps/micro-versions-info.yaml:16:40): "
    "Missing Image Tag on => platform"
)


@pytest.fixture(autouse=True)
def deterministic(monkeypatch):
    monkeypatch.setattr(m, "_ts", lambda: FIXED_TS)
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)
    monkeypatch.setattr(m, "_repo_for_sha", lambda sha: "acme-config-prod")


def _result(text="", sections=None, n_res=0, has_diff=False, error=None,
            outcome=None, reason=None, version_change=None,
            deleted_resources=None):
    return m.DiffResult(
        text, sections if sections is not None else [], n_res,
        has_diff, error, outcome or m.OUT_NO_DIFF, reason,
        version_change, deleted_resources, None, None, None, None)


def _fail():
    return _result(
        error=_MISSING_ERR, outcome=m.OUT_INDETERMINATE,
        reason=m.REASON_MISSING_REQUIRED)


def _bump(app, n=10, deleted=None):
    text = (
        f"===== {app}/apps/Deployment platform =====\n"
        "- image: x:1.0.0\n+ image: x:2.0.0\n"
    )
    return _result(
        text, [("apps/Deployment platform", text)], n, True, None,
        m.OUT_DIFF, None, ("2603.0.19", "2603.2.0"), deleted)


def _assert_golden(name, body):
    os.makedirs(GOLDEN_DIR, exist_ok=True)
    path = os.path.join(GOLDEN_DIR, name + ".md")
    if os.environ.get("UPDATE_GOLDEN") == "1":
        with open(path, "w") as f:
            f.write(body)
        pytest.skip("golden rewritten: " + name)
    if not os.path.exists(path):
        pytest.fail("no golden for %r. Review the output, then commit it "
                    "with UPDATE_GOLDEN=1:\n\n%s" % (name, body))
    with open(path) as f:
        expected = f.read()
    if body != expected:
        import difflib
        delta = "\n".join(difflib.unified_diff(
            expected.splitlines(), body.splitlines(),
            fromfile="golden/%s.md (committed)" % name,
            tofile="produced now", lineterm=""))
        pytest.fail("the comment a reviewer would read changed for %r.\n"
                    "If intended, say WHY in the PR description and "
                    "regenerate with UPDATE_GOLDEN=1.\n\n%s" % (name, delta))


def test_fleet_missing_image_tag_error_first_quiet():
    results = {}
    for env in ("pv-adl-a", "pv-atea-a", "pv-ato-c", "pv-asi-b"):
        results[f"{env}-ms"] = _fail()
    for env in ("pv-advocate-b", "pv-aexp-a"):
        results[f"{env}-ms"] = _bump(
            env, n=107, deleted=[f"apps/Deployment platform-{env}"])

    body = m.format_comment(
        PR_SHA, results, base_sha=BASE_SHA, artifact_url=ART_URL,
        profile=render_profile.COMMENT_PROFILE)

    head = "\n".join(body.splitlines()[:45])
    assert "Missing Image Tag on => platform" in head
    assert "RENDER BLOCKED" in head
    assert "micro-versions-info.yaml:16" in head
    assert body.index("RENDER BLOCKED") < body.index("collapsed")
    assert "Changeset overview collapsed" in body
    assert "| App | Status |" not in body
    assert "Routine version bump" not in body
    assert "Missing Image Tag on => platform" in body.split("Merge summary")[1][
        :800]

    _assert_golden("cops2676_fleet_missing_image_quiet", body)


def test_single_env_missing_still_detailed_not_quiet():
    results = {"pv-glencore-c-ms": _fail()}
    body = m.format_comment(
        PR_SHA, results, base_sha=BASE_SHA, artifact_url=ART_URL,
        profile=render_profile.COMMENT_PROFILE)
    assert "RENDER BLOCKED" in body
    assert "Changeset overview collapsed" not in body
    assert "Missing Image Tag on => platform" in body
