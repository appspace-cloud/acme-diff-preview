"""COPS-2629 part 3: lead with the blocker, and only call it a blocker
when it actually is one.

On acme-config-prod PR #4026 the single most important fact was that 22
environments would not render at all. The merge summary said so once, as
one REVIEW-level bullet among others, in construction order, and the
verdict on the whole PR read "Review before merging".

Two changes, and the second is the one that needed a judgement call.

FINDINGS SORT BY SEVERITY. Previously they appeared in the order the code
happened to append them, so the worst news could sit below the routine
news. Stable within a severity, so nothing else reorders.

UNDIFFABLE APPS ESCALATE TO BLOCK ONLY WHEN THE REASON IS PERMANENT. The
tempting version of this ticket escalates every "could not be diffed" to
DO NOT MERGE. That would be wrong: one transient timeout among 200 apps
is not a reason to stop a maintenance window, and a verdict that cries
wolf is one people learn to scroll past -- the same failure mode this
umbrella keeps warning about from the other direction.

The line is drawn using PERMANENT_REASONS, which the codebase already
defines as "the deployer would fail the same way". That is precisely the
condition under which merging is unsafe: helm could not render it here,
and it will not render in the cluster either. A missing required value, an
invalid schema, a chart version that is not in the registry: those are
broken configuration, and shipping them breaks the environment. A timeout
is a retry.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_preview as dp  # noqa: E402

ERR = ('execution error at (acme/templates/configmaps/'
       'micro-versions-info.yaml:16:20): Missing Image Tag on => platform')


def _indet(reason, error=ERR):
    return dp.DiffResult("", [], 0, False, error, dp.OUT_INDETERMINATE,
                         reason)


def _changed(name="a"):
    secs = [("/apps/Deployment api", "  image: acme/%s:1" % name)]
    return dp.DiffResult("--- x", secs, 1, True, None, dp.OUT_DIFF, None)


def _comment(results, **kw):
    return dp.format_comment("a" * 40, results, base_sha="b" * 40, **kw)


def _verdict(out):
    for line in out.split("\n"):
        if "DO NOT MERGE" in line:
            return "block"
        if "Review before merging" in line:
            return "review"
        if "Routine" in line:
            return "routine"
    return None


# -- permanent reasons block ------------------------------------------------

def test_environments_that_cannot_render_block_the_merge(monkeypatch):
    """The #4026 case. helm could not render these and the deployer will
    fail the same way, so merging ships a broken environment."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    results = {"pv-c%02d-ms" % i: _indet(dp.REASON_MISSING_REQUIRED)
               for i in range(22)}
    out = _comment(results)
    assert _verdict(out) == "block"


def test_every_permanent_reason_blocks(monkeypatch):
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    for reason in sorted(dp.PERMANENT_REASONS):
        out = _comment({"pv-a-ms": _indet(reason)})
        assert _verdict(out) == "block", "%s did not block" % reason


# -- transient reasons do not ----------------------------------------------

def test_a_transient_timeout_does_not_block(monkeypatch):
    """One retryable failure is not a reason to stop a maintenance window.
    A verdict that cries wolf is one people learn to scroll past."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    out = _comment({"pv-a-ms": _indet(dp.REASON_TIMEOUT, "timed out")})
    assert _verdict(out) == "review"


def test_a_transient_failure_among_many_changes_still_only_reviews(monkeypatch):
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    results = {"pv-c%02d-glb" % i: _changed("c%02d" % i) for i in range(20)}
    results["pv-slow-a-ms"] = _indet(dp.REASON_TIMEOUT, "timed out")
    out = _comment(results)
    assert _verdict(out) == "review"


def test_one_permanent_failure_among_transient_ones_blocks(monkeypatch):
    """Severity is the maximum over findings, and it must stay that way:
    a real blocker hidden among retryable noise is exactly the case the
    reader cannot be left to spot themselves."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    results = {"pv-slow-%d-ms" % i: _indet(dp.REASON_TIMEOUT, "timed out")
               for i in range(5)}
    results["pv-broken-a-ms"] = _indet(dp.REASON_MISSING_REQUIRED)
    out = _comment(results)
    assert _verdict(out) == "block"


# -- the blocker leads ------------------------------------------------------

def test_the_blocking_finding_appears_before_the_others(monkeypatch):
    """Point 4 of the ticket: the worst news must not sit below the
    routine news because of the order the code happens to append it."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    results = {"pv-c%02d-glb" % i: _changed("c%02d" % i) for i in range(6)}
    results.update({"pv-b%02d-ms" % i: _indet(dp.REASON_MISSING_REQUIRED)
                    for i in range(6)})
    out = _comment(results)
    body = out[:out.find("---")]
    blocker = body.find("cannot render")
    assert blocker != -1, body
    for other in ("app(s) change", "environment(s) jumping"):
        pos = body.find(other)
        if pos != -1:
            assert blocker < pos, "%r came before the blocker" % other


def test_the_permanent_finding_says_the_deployer_fails_too(monkeypatch):
    """"could not be diffed" understates it. The reader needs to know this
    is not a gap in the preview, it is a broken environment."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    out = _comment({"pv-a-ms": _indet(dp.REASON_MISSING_REQUIRED)})
    assert "cannot render" in out


def test_a_clean_pr_is_still_routine(monkeypatch):
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    assert _verdict(_comment({"pv-a-glb": _changed()})) == "routine"


# -- COPS-2675: the headline count must match the names beside it ----------

def test_two_failing_apps_of_one_environment_count_as_one_environment(
        monkeypatch):
    """Live on acme-config-prod #4306 (audit PR): the same "Missing Image
    Tag" class that motivated this file hit every -ms app of a cohort at
    once, and a real environment can independently fail on more than one
    of its apps (its -ms AND its -ss, say). `blocked` is a list of APPS,
    so counting it directly over-counts against `_fmt_env_list`, which
    dedupes to the environment names actually shown -- the headline could
    read "3 environment(s)" over a list of only 2 names, with no "+more"
    to account for the gap. pv-a fails on one app; pv-b fails on two.

    COPS-2683: every `cannot render` line (merge summary AND RENDER BLOCKED
    panel) must agree on the environment count.
    """
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    results = {
        "pv-a-ms": _indet(dp.REASON_MISSING_REQUIRED),
        "pv-a-glb": _changed("a"),
        "pv-b-ms": _indet(dp.REASON_MISSING_REQUIRED),
        "pv-b-ss": _indet(dp.REASON_MISSING_REQUIRED),
    }
    out = _comment(results)
    lines = [l for l in out.split("\n") if "cannot render" in l]
    assert lines, out
    for line in lines:
        assert "2 environment" in line, (
            "3 failing APPS across 2 environments must read '2', not '3':\n"
            + line)
        assert "3 environment" not in line, line
    assert "pv-a" in out and "pv-b" in out
