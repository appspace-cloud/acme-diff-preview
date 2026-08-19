"""COPS-2696: a chart version missing from OCI must not strand the PR.

Observed on acme-config-prod #4359: the preview ran before a just-published
version was visible in the registry, posted the blocked comment, and the head
stayed marked seen — editing the PR did nothing, only an empty commit
re-triggered it. The version was correct the whole time.

The fix is a reclassification, not new machinery: `oci_not_found` keeps the
FAILED status (a missing chart blocks the merge either way) but is treated as
retryable for scheduling, so the existing poll loop + COPS-2546 escalating
backoff retries it and the PR self-heals when the registry catches up.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_preview as m
from vocabulary import (
    PERMANENT_REASONS,
    REASON_INVALID_VERSION,
    REASON_OCI_NOT_FOUND,
    REASON_OCI_PULL,
    SELF_RESOLVING_REASONS,
)

PR_SHA = "c0ffee11" * 5
BASE_SHA = "beef2222" * 5


def _result(outcome, reason=None):
    return m.DiffResult("", [], 0, False, None, outcome, reason,
                        None, None, None, None)


def _footer_token(results):
    body = m.format_comment(PR_SHA, results, base_sha=BASE_SHA)
    for tok in ("[clean]", "[permanent]", "[transient]", "[blocked]"):
        if tok in body:
            return tok
    raise AssertionError("no token in footer:\n" + body[-400:])


def test_oci_not_found_alone_emits_transient():
    """The whole fix: the poll loop only re-runs a seen head on [transient],
    so this token is what lets the registry catching up heal the PR."""
    results = {"pv-x-a-ss": _result(m.OUT_INDETERMINATE, REASON_OCI_NOT_FOUND),
               "pv-x-a-ms": _result(m.OUT_NO_DIFF)}
    assert _footer_token(results) == "[transient]"


def test_oci_mixed_with_unresolvable_stays_permanent():
    """A PR that ALSO pins an invalid version needs a human no matter what the
    registry does; retrying it is wasteful and misleading."""
    results = {"a": _result(m.OUT_INDETERMINATE, REASON_OCI_NOT_FOUND),
               "b": _result(m.OUT_INDETERMINATE, REASON_INVALID_VERSION)}
    assert _footer_token(results) == "[permanent]"


def test_invalid_version_alone_stays_permanent():
    """v2.5.4 closed the bug where invalid-YAML PRs retried forever. That must
    survive this change untouched."""
    results = {"a": _result(m.OUT_INDETERMINATE, REASON_INVALID_VERSION)}
    assert _footer_token(results) == "[permanent]"


def test_soft_transient_reasons_unchanged():
    results = {"a": _result(m.OUT_INDETERMINATE, REASON_OCI_PULL)}
    assert _footer_token(results) == "[transient]"


def test_clean_run_unchanged():
    results = {"a": _result(m.OUT_NO_DIFF)}
    assert _footer_token(results) == "[clean]"


def test_self_resolving_is_a_subset_of_permanent():
    """The status colour derives FAILED from PERMANENT_REASONS; scheduling
    subtracts SELF_RESOLVING_REASONS from it. If someone removes oci_not_found
    from PERMANENT_REASONS, the missing chart would stop blocking the merge —
    this pins the containment so that edit fails loudly here."""
    assert SELF_RESOLVING_REASONS <= PERMANENT_REASONS
    assert SELF_RESOLVING_REASONS == {REASON_OCI_NOT_FOUND}


def test_oci_not_found_keeps_failing_the_build_status():
    """Acceptance criterion 3: a genuinely missing chart still blocks with the
    existing message. The status branch keys on PERMANENT_REASONS (via
    has_blocking_indet), which still contains oci_not_found."""
    assert REASON_OCI_NOT_FOUND in PERMANENT_REASONS
