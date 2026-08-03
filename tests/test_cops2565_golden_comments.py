"""Golden-comment corpus: freeze what the reviewer actually reads (COPS-2565).

Why this exists
---------------
The last four bug tickets in this repo (COPS-2552, 2554, 2563, 2564) share one
property that matters more than their content: every single one was found in
production, on a real PR, by a human noticing the comment looked wrong. None
was caught by a test. The diff engine itself was fine in all four cases; what
broke was how an edge case got CLASSIFIED and PRESENTED.

So the thing worth freezing is not the rendered Kubernetes manifests, it is the
comment. `format_comment` is the last thing between our logic and the reviewer's
judgement, and it is where those four bugs would have shown up.

What is frozen, and what is deliberately not
--------------------------------------------
Frozen: the entire comment body for each scenario, byte for byte.

Normalised, because they are not behaviour:
  * the footer timestamp (`_ts`), pinned to a fixed instant;
  * the AI summary, stubbed off. It is a non-deterministic remote call, and
    AI_SUMMARY_ENABLED is already an operator switch (COPS-2555).

Nothing else is normalised. In particular resource counts, section ordering,
the deletion block, the schema-failure block, the traffic light and the footer
ARE part of the contract, because every one of those has been wrong at least
once in production.

The blind-refresh trap
----------------------
A golden corpus that people regenerate on every red build protects nothing. Two
guards, on purpose:
  1. Regenerating is explicit and separate: UPDATE_GOLDEN=1 pytest <this file>.
     It is never automatic and never a side effect of a normal run.
  2. The goldens are committed markdown, so a regeneration shows up in review as
     a readable prose diff. A reviewer can see "the deletion block disappeared"
     without running anything.
If a golden changes and nobody can explain WHY in the PR description, that is
the signal to stop, not to re-run the generator.
"""
import os
import sys

import pytest

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m

GOLDEN_DIR = os.path.join(os.path.dirname(__file__), "golden")
FIXED_TS = "2026-01-01 00:00 UTC"
PR_SHA = "abc12345def67890abc12345def67890abc12345"
BASE_SHA = "0000111122223333444455556666777788889999"


@pytest.fixture(autouse=True)
def deterministic(monkeypatch):
    """Pin the only two non-deterministic inputs. Everything else is contract."""
    monkeypatch.setattr(m, "_ts", lambda: FIXED_TS)
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)
    monkeypatch.setattr(m, "_repo_for_sha", lambda sha: "acme-config-prod")


def _result(text="", sections=None, n_res=0, has_diff=False, error=None,
            outcome=None, reason=None, version_change=None,
            deleted_resources=None, replicas_zeroed=None, fingerprint=None):
    return m.DiffResult(text, sections if sections is not None else [], n_res,
                        has_diff, error, outcome or m.OUT_NO_DIFF, reason,
                        version_change, deleted_resources, replicas_zeroed,
                        fingerprint)


def _assert_golden(name: str, body: str):
    """Compare against the committed golden, or rewrite it when explicitly asked."""
    os.makedirs(GOLDEN_DIR, exist_ok=True)
    path = os.path.join(GOLDEN_DIR, f"{name}.md")
    if os.environ.get("UPDATE_GOLDEN") == "1":
        with open(path, "w") as f:
            f.write(body)
        pytest.skip(f"golden rewritten: {name}")
    if not os.path.exists(path):
        pytest.fail(
            f"no golden for {name!r}. Review the output below, then commit it "
            f"with UPDATE_GOLDEN=1:\n\n{body}")
    expected = open(path).read()
    if body != expected:
        import difflib
        delta = "\n".join(difflib.unified_diff(
            expected.splitlines(), body.splitlines(),
            fromfile=f"golden/{name}.md (committed)", tofile="produced now",
            lineterm=""))
        pytest.fail(
            f"the comment a reviewer would read changed for {name!r}.\n"
            f"If this change is intended, say WHY in the PR description and "
            f"regenerate with UPDATE_GOLDEN=1.\n\n{delta}")


# ── Scenarios. Each one maps to a bug that reached production. ──────────────

MINUS_ONLY = (            # COPS-2563: PR 3829 called this "110 deleted"
    "--- \n+++ \n@@ -20,7 +20,6 @@\n"
    "     app.kubernetes.io/name: broadcast\n spec:\n   \n"
    "-  replicas: 2\n   \n   strategy:\n")
TRUE_DELETION = (         # a real full deletion: no context lines at all
    "--- \n+++ \n@@ -1,5 +0,0 @@\n"
    "-apiVersion: v1\n-kind: Service\n-metadata:\n-  name: gone\n-spec: {}\n")
ORDINARY = (
    "--- \n+++ \n@@ -10,7 +10,7 @@\n     spec:\n       containers:\n"
    "-        image: appspace-ms:2603.0.0\n"
    "+        image: appspace-ms:2603.1.0\n         ports:\n")
SCHEMA_ERR = (
    "Error: values don't meet the specifications of the schema(s) in the "
    "following chart(s):\nappspace-micro-services:\n"
    + "\n".join(f"- at '/appspace/microservices/definitions/svc-{i:02d}': "
                f"got null, want object" for i in range(53)) + "\n")


def test_golden_ordinary_version_bump():
    """The 83.7% case. If this ever changes shape, everything else is suspect."""
    body = m.format_comment(PR_SHA, {
        "pv-acme-a-ms": _result(ORDINARY, [("/apps/Deployment broadcast", ORDINARY)],
                                1, True, outcome=m.OUT_DIFF),
        "pv-acme-a-ss": _result(outcome=m.OUT_NO_DIFF),
    }, base_sha=BASE_SHA)
    _assert_golden("ordinary_version_bump", body)


def test_golden_minus_only_change_shows_no_deletion_block():
    """COPS-2563. Removing a `replicas:` line is not a deletion. This is the
    scenario that shipped as '110 RESOURCE(S) DELETED' on a live prod PR."""
    body = m.format_comment(PR_SHA, {
        "pv-acme-a-ms": _result(
            MINUS_ONLY,
            [(f"/apps/Deployment svc-{i}", MINUS_ONLY) for i in range(6)],
            6, True, outcome=m.OUT_DIFF,
            deleted_resources=m._detect_deleted_resources(
                [(f"/apps/Deployment svc-{i}", MINUS_ONLY) for i in range(6)])),
    }, base_sha=BASE_SHA)
    assert "RESOURCE(S) DELETED" not in body, "regression of COPS-2563"
    _assert_golden("minus_only_no_deletion_block", body)


def test_golden_true_deletion_still_shouts():
    """The other half of COPS-2563: a real deletion must stay loud. A fix that
    silences this would be worse than the bug it replaced."""
    secs = [("/v1/Service gone", TRUE_DELETION),
            ("/apps/Deployment svc-0", MINUS_ONLY)]
    body = m.format_comment(PR_SHA, {
        "pv-acme-a-ms": _result(TRUE_DELETION + MINUS_ONLY, secs, 2, True,
                                outcome=m.OUT_DIFF,
                                deleted_resources=m._detect_deleted_resources(secs)),
    }, base_sha=BASE_SHA)
    assert "RESOURCE(S) DELETED" in body
    _assert_golden("true_deletion_shouts", body)


def test_golden_schema_failure_is_readable():
    """COPS-2564: 53 violations used to be cut mid-path at 400 chars."""
    body = m.format_comment(PR_SHA, {
        "pv-glencore-c-ms": _result(error=m._cap_helm_error(SCHEMA_ERR),
                                    outcome=m.OUT_INDETERMINATE,
                                    reason=m.REASON_SCHEMA_INVALID),
    }, base_sha=BASE_SHA)
    assert "definitions/svc-00" in body
    assert "more violation(s)" in body, "the remainder count must be stated"
    _assert_golden("schema_failure_readable", body)


def test_golden_failed_app_never_looks_green():
    """The single most dangerous failure mode: a computation failure rendered
    as 'no changes'. The outcome model exists to prevent exactly this."""
    body = m.format_comment(PR_SHA, {
        "pv-acme-a-ms": _result(outcome=m.OUT_NO_DIFF),
        "pv-acme-a-ss": _result(error="OCI pull failed: connection reset",
                                outcome=m.OUT_INDETERMINATE,
                                reason=m.REASON_OCI_PULL),
    }, base_sha=BASE_SHA)
    assert "\u2705 **No manifest changes**" not in body.split("Status:")[-1], \
        "a failed app must never produce an all-clear status line"
    _assert_golden("failed_app_not_green", body)


def test_golden_all_clean():
    body = m.format_comment(PR_SHA, {
        "pv-acme-a-ms": _result(outcome=m.OUT_NO_DIFF),
        "pv-acme-a-ss": _result(outcome=m.OUT_NO_DIFF),
        "pv-acme-a-glb": _result(outcome=m.OUT_NO_DIFF),
    }, base_sha=BASE_SHA)
    _assert_golden("all_clean", body)


def test_golden_large_pr_switches_to_summary_table():
    """The PR 3837 shape: hundreds of apps sharing the exact same change
    (COPS-2579). The real diff engine would fingerprint all of them
    identically, since their full section lists are byte-for-byte the
    same -- reproduced here explicitly, since this test's `_result()`
    builds a DiffResult directly instead of going through
    _package_sections. Must show ONE full representative diff plus the
    full member list, not 60 individual dumps and not an arbitrary top-N,
    while staying inside Bitbucket's 245KB comment limit."""
    shared_sections = [("/apps/Deployment broadcast", ORDINARY)]
    shared_fp = m._fingerprint_sections(shared_sections)
    results = {}
    for i in range(60):
        results[f"pv-env-{i:03d}-ms"] = _result(
            ORDINARY, shared_sections, 4, True,
            outcome=m.OUT_DIFF, fingerprint=shared_fp)
    body = m.format_comment(PR_SHA, results, base_sha=BASE_SHA)
    assert len(body) < 245_000, f"comment would be rejected: {len(body)} bytes"
    _assert_golden("large_pr_summary_table", body)


def test_golden_version_downgrade_is_flagged():
    body = m.format_comment(PR_SHA, {
        "pv-acme-a-ms": _result(ORDINARY, [("/apps/Deployment b", ORDINARY)],
                                1, True, outcome=m.OUT_DIFF,
                                version_change=("2603.1.0", "2603.0.0")),
    }, base_sha=BASE_SHA)
    _assert_golden("version_downgrade", body)


def test_golden_new_env_rides_along_with_existing_changes():
    """v2.5.4 Finding 4: a clean existing-app diff must never show a green
    check while an unvalidated new environment rode in on the same PR."""
    body = m.format_comment(PR_SHA, {
        "pv-acme-a-ms": _result(outcome=m.OUT_NO_DIFF),
    }, base_sha=BASE_SHA,
        new_env_lines=["", "### \U0001f195 New environment: `pv-brandnew-a`", "",
                       "> Could not be validated: cohort config.yaml missing", ""],
        new_env_structural=True, new_env_desc="1 new environment")
    _assert_golden("new_env_rides_along", body)
