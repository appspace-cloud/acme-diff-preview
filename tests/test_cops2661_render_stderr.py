"""COPS-2661: a render failure must teach the fix, not just report the fact.

acme-config-prod PR #4244 (new HEB rabbit VM) failed Diff Preview with only:

    ❔ pv-heb-a-ss — diff unavailable (helm template failed to render the
    chart with these values)

The actual bug was one line in customer.yaml: `rabbit.instances` was a
string instead of a list, and the chart ranges over it. Helm said exactly
that in stderr, `_helm_template` captured it into `DiffResult.error` -- and
the comment path for REASON_RENDER threw it away, printing only the generic
hint. The author could not tell WHAT was wrong.

Same class of silence COPS-2554 fixed for `required()` and schema failures,
extended to the bucket everything else lands in. And the same retry tax:
REASON_RENDER is retryable, so a deterministic values bug was retried with
backoff before settling on the same opaque line -- a template EXECUTION
failure is a pure local computation and can never succeed on retry, so it
gets its own permanent reason.
"""
import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as dp  # noqa: E402
import schema_errors  # noqa: E402
import vocabulary  # noqa: E402

# The #4244 shape, verbatim from Go's text/template via helm.
ERR_RANGE = ("template: appspace-supporting-services/templates/"
             "kcc-linux-services/compute-rabbit.yaml:7:16: executing "
             '"appspace-supporting-services/templates/kcc-linux-services/'
             'compute-rabbit.yaml" at <.Values.appspace.infra.'
             "deployLinuxServicesK8s.rabbit.instances>: range can't iterate "
             "over pv-heb-svc-a")

ERR_REQUIRED = ("execution error at (appspace-micro-services/templates/"
                "deployment.yaml:14:3): Missing Image Tag on => platform")

ERR_SCHEMA = ("values don't meet the specifications of the schema(s):\n"
              "- at '/appspace/x': got null, want object")


def _res(error, reason, outcome=None):
    return dp.DiffResult("", [], 0, False, error,
                         outcome or dp.OUT_INDETERMINATE, reason)


def _comment(results, **kw):
    return dp.format_comment("a" * 40, results, base_sha="b" * 40, **kw)


# ── classification ──────────────────────────────────────────────────────────

def test_a_template_execution_failure_gets_its_own_permanent_reason():
    assert schema_errors._render_reason(ERR_RANGE) == vocabulary.REASON_TEMPLATE
    assert vocabulary.REASON_TEMPLATE in vocabulary.PERMANENT_REASONS
    assert vocabulary.REASON_TEMPLATE not in vocabulary.RETRYABLE_REASONS


def test_required_and_schema_classification_is_untouched():
    """COPS-2554's classifications must win over the new signature: a
    required() failure and a nil-pointer BOTH also carry template/executing
    phrasing in some helm versions, and their dedicated clarity paths are
    strictly better than the generic stderr quote."""
    assert schema_errors._render_reason(ERR_REQUIRED) == \
        vocabulary.REASON_MISSING_REQUIRED
    nil = ('template: x/templates/y.yaml:8:19: executing "t" at '
           "<.Values.a.b>: nil pointer evaluating interface {}.b")
    assert schema_errors._render_reason(nil) == \
        vocabulary.REASON_MISSING_REQUIRED
    assert schema_errors._render_reason(ERR_SCHEMA) == \
        vocabulary.REASON_SCHEMA_INVALID


def test_unrecognised_failures_stay_soft_render():
    """Anything without the execution signature keeps the retryable reason:
    the permanence promotion must never catch a transient it cannot prove."""
    assert schema_errors._render_reason("some weird chart pull explosion") == \
        vocabulary.REASON_RENDER


# ── the per-app comment block ───────────────────────────────────────────────

def test_the_4244_shape_shows_the_stderr_and_the_fix(monkeypatch):
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    out = _comment({"pv-heb-a-ss": _res(ERR_RANGE, vocabulary.REASON_TEMPLATE)})
    assert "range can't iterate over pv-heb-svc-a" in out, \
        "the captured helm stderr must reach the author"
    assert "compute-rabbit.yaml" in out, "the template path says which chart read it"
    assert "customer.yaml" in out, "the fix hint must say where to fix it"
    assert "TEMPLATE EXECUTION FAILED" in out


def test_soft_render_failures_also_quote_their_error(monkeypatch):
    """Even the genuinely-unclassified bucket must show what it has. The
    generic hint stays as the headline; the stderr stops being discarded."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    err = "chart exploded in a way nobody classified yet"
    out = _comment({"pv-x-a-ss": _res(err, vocabulary.REASON_RENDER)})
    assert "diff unavailable" in out
    assert err in out


def test_a_soft_failure_with_no_error_keeps_the_old_line(monkeypatch):
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    out = _comment({"pv-x-a-ss": _res("", vocabulary.REASON_RENDER)})
    assert "diff unavailable" in out


def test_template_failures_block_the_merge_summary(monkeypatch):
    """PERMANENT means the deployer fails the same way, which is the
    existing definition of the summary's cannot-render blocker."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    out = _comment({"pv-heb-a-ss": _res(ERR_RANGE, vocabulary.REASON_TEMPLATE)})
    assert "DO NOT MERGE" in out
    assert "cannot render" in out


def test_missing_required_block_is_unchanged(monkeypatch):
    """The COPS-2554 clarity path must render exactly as before, not fall
    through to the new generic stderr quote."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    out = _comment({"pv-y-a-ms": _res(ERR_REQUIRED,
                                      vocabulary.REASON_MISSING_REQUIRED)})
    assert "MISSING REQUIRED VALUE" in out
    assert "Missing Image Tag on => platform" in out
    assert "TEMPLATE EXECUTION FAILED" not in out
