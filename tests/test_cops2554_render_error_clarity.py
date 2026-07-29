"""helm `required()` failures with a custom message must classify correctly.

Live PR 3823 (acme-config-prod): bumping pv-heb--aec1-a to chart 2603.1.5
failed with `Missing Image Tag on => platform`, a chart author's own custom
required() message (`required (print "Missing Image Tag on => " $name) $tag`).
_render_reason only recognized the literal substrings "is required",
"required value" or "nil pointer evaluating", none of which appear in this
message, so it fell through to the generic REASON_RENDER bucket. That bucket:
  - retries 5 times (up to a minute) even though the failure is 100%
    deterministic and cannot resolve without a new commit,
  - then shows only "diff unavailable (helm template failed to render the
    chart with these values)" in the PR comment, hiding the real message,
  - and is not a PERMANENT_REASON, so the PR is backed off and retried
    forever instead of blocking cleanly.

Auditing the chart's own templates for other required() calls turned up more
messages that would ALSO have been silently misclassified the same way:
  - "Cloud instance not found for this deployment" (no "required" at all)
  - "A valid appspace.prefix entry required!" (has the word, wrong phrase)

The fix matches Helm's actual, message-independent signature for a template
`required()` failure ("execution error at (") instead of guessing keywords
from a message chart authors can word however they like.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m

# The exact stderr from live PR 3823.
LIVE_ERR = ('Error: execution error at (appspace-micro-services/templates/'
            'configmaps/micro-versions-info.yaml:16:49): Missing Image Tag '
            'on => platform')

NO_REQUIRED_WORD = ('execution error at (appspace-micro-services/templates/'
                     'configmaps/legacy-db-credentials.yaml:74:32): Cloud '
                     'instance not found for this deployment')

WRONG_PHRASE = ('execution error at (appspace-micro-services/templates/'
                'cloudRunService.yaml:19:8): A valid appspace.prefix entry '
                'required!')

NIL_PTR = ('template: x/templates/y.yaml:15:124: executing "x" at '
           '<$microservice.image.tag>: nil pointer evaluating interface {}.tag')


def test_custom_required_message_is_classified_correctly():
    assert m._render_reason(LIVE_ERR) == m.REASON_MISSING_REQUIRED


def test_required_message_with_no_required_word_is_still_caught():
    assert m._render_reason(NO_REQUIRED_WORD) == m.REASON_MISSING_REQUIRED


def test_required_message_with_a_different_phrasing_is_caught():
    assert m._render_reason(WRONG_PHRASE) == m.REASON_MISSING_REQUIRED


def test_nil_pointer_is_still_its_own_case():
    """Regression guard: the structural fix must not swallow the (different,
    already-working) nil-pointer classification."""
    assert m._render_reason(NIL_PTR) == m.REASON_MISSING_REQUIRED


def test_yaml_errors_are_unaffected():
    """Regression guard against the existing, working YAML classification."""
    assert m._render_reason("Error: error converting YAML to JSON") == m.REASON_INVALID_YAML
    assert m._render_reason("yaml: line 42: did not find expected key") == m.REASON_INVALID_YAML


def test_a_generic_chart_failure_stays_generic():
    """Not every render failure is a required()/nil-pointer problem."""
    assert m._render_reason("chart requires kubeVersion >= 1.25") == m.REASON_RENDER
    assert m._render_reason("") == m.REASON_RENDER


# ── PERMANENT classification: stop retrying and stop the endless backoff ────

def test_missing_required_is_a_permanent_reason():
    """A required() failure cannot resolve without a new commit. Retrying it
    (up to 5 times, ~60s) and then backing it off forever wastes time and
    never tells anyone it needs a code change, not patience."""
    assert m.REASON_MISSING_REQUIRED in m.PERMANENT_REASONS
    assert m.REASON_MISSING_REQUIRED not in m.RETRYABLE_REASONS


def test_argocd_diff_does_not_retry_a_missing_required_failure(monkeypatch):
    """End-to-end: argocd_diff must return on the FIRST attempt, not burn
    through DIFF_RETRIES sleeping for a failure that will never change."""
    calls = {"n": 0}
    def fake_run(*a, **k):
        calls["n"] += 1
        return (None, m.REASON_MISSING_REQUIRED, LIVE_ERR)
    monkeypatch.setattr(m, "_run_one_diff", fake_run)
    monkeypatch.setattr(m.time, "sleep", lambda *_a, **_k: None)
    result = m.argocd_diff("pv-heb--aec1-a-ms", "prsha", "mainsha")
    assert calls["n"] == 1, "a permanent reason must not be retried"
    assert result.outcome == m.OUT_INDETERMINATE
    assert result.reason == m.REASON_MISSING_REQUIRED


# ── values.schema.json validation failures: same class of confusing error ──

SCHEMA_ERR = (
    "values don't meet the specifications of the schema(s) in the "
    "following chart(s):\nappspace-micro-services:\n"
    "- appspace.customerName: Invalid type. Expected: string, given: null\n"
    "- (root): appspace.instance is required"
)


def test_schema_validation_failure_gets_its_own_reason():
    """appspace-micro-services ships values.schema.json. If it ever gains a
    required/typed field, a violation must not fall into generic REASON_RENDER
    either -- same class of confusing message this ticket is about."""
    assert m._render_reason(SCHEMA_ERR) == m.REASON_SCHEMA_INVALID


def test_schema_failure_is_permanent_too():
    assert m.REASON_SCHEMA_INVALID in m.PERMANENT_REASONS


def test_schema_explanation_lists_each_violation_on_its_own_line():
    lines = m._explain_schema_error(SCHEMA_ERR)
    joined = "\n".join(lines)
    assert "appspace.customerName" in joined
    assert "appspace.instance is required" in joined
    for l in lines:
        assert l.startswith("> "), l
        assert "\n" not in l


def test_schema_failure_is_rendered_in_the_pr_comment():
    """Wired check: the reason must actually reach the comment builder, not
    just exist as a classification with nowhere to go."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "src",
                            "diff_preview.py")).read()
    assert "REASON_SCHEMA_INVALID" in src.replace(
        "REASON_SCHEMA_INVALID = ", "", 1)
    assert "_explain_schema_error(" in src.replace(
        "def _explain_schema_error(", "", 1), "explanation is never rendered"
