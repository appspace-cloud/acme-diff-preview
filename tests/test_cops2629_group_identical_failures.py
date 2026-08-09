"""COPS-2629: identical failures collapse into one block in the comment.

Measured on acme-config-prod PR #4026, a monthly maintenance bump of
na2-a/monthly from 2603.0.17-rev3 to 2603.1.14 (22 environments, 44
applications). The comment came to 314 lines and 19,581 bytes, and 52% of
it was duplicated text: the MISSING REQUIRED VALUE block, its chart
template line and its three lines of remediation advice, repeated 22 times
byte for byte. Of 110 quoted advice lines, 5 were distinct.

The operator's takeaway was one sentence: chart 2603.1.14 needs a value
the monthly line does not set, so 22 environments will not render. One
problem, one fix. The comment stated it 22 times and buried the headline.

This is the same move COPS-2579 made for identical diffs and COPS-2612
made for clean apps, applied to the surface that had been left out:
failures.

The two-surface contract from COPS-2612 is the constraint that makes this
safe. Grouping happens ONLY on the comment. The full-diff page keys on
is_complete_record and keeps naming every environment with its own block,
because "which environment failed and why" is exactly the question the
page exists to answer. Nothing may be collapsed out of both surfaces.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_preview as dp  # noqa: E402


# The real helm stderr from PR #4026, one per environment. Identical apart
# from nothing at all: the chart, the template and the line are the same.
ERR_4026 = ('execution error at (acme/templates/configmaps/'
            'micro-versions-info.yaml:16:20): Missing Image Tag on '
            '=> platform')

ERR_OTHER = ('execution error at (acme/templates/deployment.yaml:8:3): '
             'Missing Registry on => core')


def _res(error=ERR_4026, reason=None):
    return dp.DiffResult("", [], 0, False, error, dp.OUT_INDETERMINATE,
                         reason or dp.REASON_MISSING_REQUIRED)


def _fail_set(n, err=ERR_4026, prefix="pv-cust"):
    return {"%s-%02d-ms" % (prefix, i): _res(err) for i in range(n)}


def _comment(results, **kw):
    return dp.format_comment("a" * 40, results, base_sha="b" * 40, **kw)


# -- the measured PR #4026 shape -------------------------------------------

def test_twenty_two_identical_failures_state_the_fix_once(monkeypatch):
    """The headline number from #4026: the same three lines of remediation
    advice appeared 22 times. Once is the contract."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    out = _comment(_fail_set(22))
    assert out.count("**Fix:** add the missing value") == 1
    assert out.count("If this PR changed the chart version") == 1
    assert out.count("Chart template:") == 1


def test_the_error_message_itself_is_stated_once(monkeypatch):
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    out = _comment(_fail_set(22))
    assert out.count("Missing Image Tag on => platform") == 1


def test_the_comment_names_a_sample_and_accounts_for_the_rest(monkeypatch):
    """The comment lists environments compactly, not exhaustively: eight
    names plus a count of the remainder. Completeness is the PAGE's job,
    which test_the_page_still_names_every_environment_separately pins.

    What must never happen is a group that hides how many it covers. The
    operator has to be able to tell "22 broken" from "8 broken" without
    opening anything."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    results = _fail_set(22)
    out = _comment(results)
    named = [a for a in results if a in out]
    assert len(named) == 8, "expected the standard 8-name sample, got %d" % len(named)
    assert "(+14 more)" in out
    assert "22 environments cannot render" in out


def test_the_group_states_how_many_environments_it_covers(monkeypatch):
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    out = _comment(_fail_set(22))
    assert "22" in out


# -- grouping is by error signature, not by count --------------------------

def test_two_distinct_errors_produce_two_groups(monkeypatch):
    """Different failures are different problems. Grouping must key on the
    error signature, or a second, unrelated breakage hides inside the
    first one's block."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    results = _fail_set(5)
    results.update(_fail_set(3, err=ERR_OTHER, prefix="pv-other"))
    out = _comment(results)
    assert out.count("Missing Image Tag on => platform") == 1
    assert out.count("Missing Registry on => core") == 1


def test_a_single_failure_renders_exactly_as_before(monkeypatch):
    """One app is not a group. The existing single-app wording is what
    every current test and golden asserts, and it must not move."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    out = _comment({"pv-solo-a-ms": _res()})
    assert "`pv-solo-a-ms`" in out
    assert "MISSING REQUIRED VALUE" in out
    assert "environments cannot render" not in out


def test_errors_differing_only_in_noise_still_group(monkeypatch):
    """Same template, same line, same missing value: same problem. If a
    trailing path prefix or whitespace splits the group, the operator gets
    the wall of text back."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    results = _fail_set(4)
    results["pv-noise-a-ms"] = _res(ERR_4026 + "\n")
    results["pv-noise-b-ms"] = _res("  " + ERR_4026)
    out = _comment(results)
    assert out.count("Missing Image Tag on => platform") == 1


# -- the two-surface contract (COPS-2612) ----------------------------------

def test_the_page_still_names_every_environment_separately(monkeypatch):
    """is_complete_record is the page. Grouping is a COMMENT simplification:
    the page is where "which environment failed and why" is answered, so it
    keeps one block per app. Nothing may be collapsed out of both."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    page = _comment(_fail_set(22),
                    profile=dp.RenderProfile("page", is_complete_record=True,
                                              inline_diffs=True))
    assert page.count("Missing Image Tag on => platform") == 22
    assert page.count("**Fix:** add the missing value") == 22


def test_the_comment_points_at_the_page_for_the_full_list(monkeypatch):
    """Anything the comment stops showing has to be reachable. With an
    artifact url present the group must link to it."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    url = "https://argocd.appspace.com/diff/acme-config-prod/4026/ffba80a2"
    out = _comment(_fail_set(22), artifact_url=url)
    assert url in out


# -- the reason this ticket exists: size -----------------------------------

def test_the_comment_shrinks_materially_on_the_4026_shape(monkeypatch):
    """Not a micro-optimisation. The measured comment was 19,581 bytes with
    10,122 of them duplicated advice. Grouping must remove the bulk of it."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    results = _fail_set(22)
    grouped = _comment(results)
    ungrouped = _comment(results,
                         profile=dp.RenderProfile("page", is_complete_record=True,
                                                   inline_diffs=True))
    assert len(grouped) < len(ungrouped) * 0.6, (
        "grouped %d bytes vs per-app %d" % (len(grouped), len(ungrouped)))


def test_the_blocker_count_survives_grouping(monkeypatch):
    """The merge summary's "N app(s) could not be diffed" is derived from
    the same results and must keep counting every environment, not the
    number of groups."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    out = _comment(_fail_set(22))
    assert "22 app(s) could not be diffed" in out
