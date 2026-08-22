"""A failed check must say which failure it was (COPS-2709).

Bitbucket's checks list shows the build-status description and nothing else.
Driving `process_pr` through every failure shape and printing that string
produced this, four times over:

    1 app(s): invalid config — fix and push again (check PR comment for details)

for a missing required value, a schema violation, a template blowing up and
a name over 63 characters. Four problems, four different fixes, named by
none of them. `Diff failed - check PR comment` named even less, and the OCI
line never said which version was missing.

The information was already there. COPS-2676 put `_short_permanent_error`
into the comment, so the verdict has read "**Missing Image Tag on =>
platform**" for months while the check beside it said "invalid config". This
is the same function on the surface that was left out.
"""
import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m  # noqa: E402


def _res(reason, error, outcome=None):
    return m.DiffResult("", [], 0, False, error,
                        outcome or m.OUT_INDETERMINATE, reason,
                        None, None, None, None, None, None, None, None, None)


# Built per call, never at import. test_v247_improvements reloads
# diff_preview, which rebinds DiffResult to a new class object; a namedtuple
# captured at collection time would then fail the isinstance check inside
# `_result` and unpack as a bare tuple. Cost a real debugging detour once.
def MISSING():
    return _res("missing_required",
                "execution error at (appspace-ms/templates/x.yaml:4:5): "
                "Missing Image Tag on => platform")


def SCHEMA():
    return _res("schema_invalid",
                "values don't meet the specifications of the schema:\n"
                "- at '/microservices/definitions/a': got string, want object")


def TEMPLATE():
    return _res("template_failed",
                "wrong type for value; expected string; "
                "got map[string]interface {}")


def LONGNAME():
    return _res("name_too_long", "name exceeds 63 characters")


def BADYAML():
    return _res("invalid_yaml", "mapping values are not allowed here, line 12")


def OCI():
    return _res("oci_not_found", "appspace-ms:2603.9.9 not found")


def desc(results):
    return m._permanent_failure_status_description(results)


# ── 1. the four that used to be indistinguishable ────────────────────────

def test_each_failure_names_itself():
    got = {
        "missing": desc({"pv-a-ms": MISSING()}),
        "schema": desc({"pv-a-ms": SCHEMA()}),
        "template": desc({"pv-a-ms": TEMPLATE()}),
        "longname": desc({"pv-a-ms": LONGNAME()}),
    }
    assert "Missing Image Tag on => platform" in got["missing"]
    assert "want object" in got["schema"]
    assert "expected string" in got["template"]
    assert "63 characters" in got["longname"]
    assert len(set(got.values())) == 4, \
        "four different problems must not share one description: %r" % got
    for d in got.values():
        assert "invalid config" not in d, d


def test_every_description_names_the_environment_and_an_action():
    d = desc({"pv-uwm-a-ms": MISSING()})
    assert "pv-uwm-a" in d, d
    assert "fix and push" in d, d


# ── 2. the action has to be true ─────────────────────────────────────────

def test_a_missing_chart_version_is_not_something_to_fix_and_push():
    """oci_not_found is self-resolving: the version may simply not have
    published yet and the poll loop keeps retrying (COPS-2696). Telling the
    author to fix and push sends them to change a correct version."""
    d = desc({"pv-a-ms": OCI()})
    assert "appspace-ms:2603.9.9" in d, d
    assert "fix and push" not in d, d
    assert "wait for the registry" in d, d


def test_a_real_config_error_still_says_fix_and_push():
    assert "fix and push" in desc({"pv-a-ms": BADYAML()})


# ── 3. a fleet PR is one problem, not fifty ──────────────────────────────

def test_apps_failing_the_same_way_collapse_and_the_envs_are_counted():
    results = {f"pv-env{i}-a-{part}": MISSING()
               for i in range(6) for part in ("ms", "ss")}
    d = desc(results)
    assert d.count("Missing Image Tag") == 1, d
    assert "+3 more" in d, "six environments, three named: " + d


def test_the_biggest_group_leads_and_the_rest_are_counted():
    results = {"pv-a-ms": MISSING(), "pv-b-ms": MISSING(), "pv-c-ms": SCHEMA()}
    d = desc(results)
    assert d.startswith("Missing Image Tag"), d
    assert "+1 other failure(s)" in d, d


def test_the_description_is_deterministic():
    """The poll loop dedups on the posted status, so an unstable string
    would re-post the same verdict every iteration."""
    results = {"pv-a-ms": MISSING(), "pv-b-ms": SCHEMA()}
    assert desc(results) == desc(dict(reversed(list(results.items()))))


# ── 4. edges ─────────────────────────────────────────────────────────────

def test_nothing_permanent_returns_empty_so_callers_can_fall_back():
    assert desc({}) == ""
    assert desc({"pv-a-ms": _res("timeout", "timed out")}) == "", \
        "a transient reason is not a permanent failure"
    assert desc({"pv-a-ms": m.DiffResult("", [], 0, False, "", m.OUT_NO_DIFF,
                                         None, None, None, None, None, None,
                                         None, None, None, None)}) == ""


def test_a_very_long_error_gives_up_characters_before_the_action_does():
    """post_build_status truncates at 255. The action is the part the reader
    can least afford to lose, so it must not be what falls off the end.

    It takes both halves to get there: `_short_permanent_error` already caps
    at 160, so the line only runs long when the environment names are long
    too. Five real-length customer names is what does it.
    """
    long_error = "wrong type for value; expected string; got " + "x" * 200
    results = {f"pv-averyverylongcustomername{i}-a-ms":
               _res("template_failed", long_error) for i in range(5)}
    d = desc(results)
    assert len(d) <= 255, f"{len(d)}: {d}"
    assert d.endswith("fix and push"), d
    assert "\u2026" in d, "the error is what should have been cut: " + d
    assert "+2 more" in d, d


def test_a_short_error_is_not_truncated():
    """The control. Without it the assertion above passes on a function that
    truncates everything."""
    d = desc({"pv-a-ms": LONGNAME()})
    assert "\u2026" not in d, d
    assert d.startswith("name exceeds 63 characters"), d


# ── 5. the paths that build their own line ───────────────────────────────

def test_a_hard_error_names_the_error(monkeypatch):
    """`Diff failed - check PR comment` told the reader nothing they could
    act on, and the error was on the result all along."""
    statuses = _drive(monkeypatch, {
        "pv-fail-a-ms": m.DiffResult("", [], 0, False, "connection refused",
                                     m.OUT_ERROR, None, None, None, None,
                                     None, None, None, None, None, None)})
    state, description = statuses[-1]
    assert state == "FAILED"
    assert "connection refused" in description, description


def test_a_permanent_failure_reaches_the_status_through_process_pr(monkeypatch):
    statuses = _drive(monkeypatch, {"pv-fail-a-ms": MISSING()})
    state, description = statuses[-1]
    assert state == "FAILED"
    assert "Missing Image Tag on => platform" in description, description
    assert "invalid config" not in description


_IDENT = "gcp/dev/private-cloud/ap1/custom/pv-fail-a/customer.yaml"
_YAML = "appspace:\n  customerName: fail\n  version: 2603.1.0\n"
_seq = [0]


def _drive(monkeypatch, plan):
    """Run the real orchestrator once, returning the statuses it posted.

    A fresh sha pair per call: every cache in this service is keyed on
    (path, sha), so a shared one lets an earlier case answer for a later.
    """
    _seq[0] += 1
    pr_sha, base_sha = f"{_seq[0]:02d}aabbccdd", f"{_seq[0]:02d}ddccbbaa"
    statuses = []
    files = {(_IDENT, base_sha): _YAML, (_IDENT, pr_sha): _YAML}
    m._seen.clear()
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda p, s, repo=None: (files[(p, s)], m.BB_OK)
                        if (p, s) in files else (None, m.BB_NOT_FOUND))
    monkeypatch.setattr(m, "get_pr_changed_files",
                        lambda pr_id, repo=None: ([_IDENT], {}))
    monkeypatch.setattr(m, "find_existing_comment",
                        lambda pr_id, repo=None: (None, "", ""))
    monkeypatch.setattr(m, "upsert_comment",
                        lambda pr_id, body, existing_id=None, repo=None,
                        **kw: 1)
    monkeypatch.setattr(m, "post_build_status",
                        lambda sha, state, description, pr_id=None,
                        repo=None: statuses.append((state, description)))
    monkeypatch.setattr(m, "fix_stuck_inprogress", lambda *a, **k: None)
    monkeypatch.setattr(m, "_touch_progress", lambda: None)
    monkeypatch.setattr(m, "argocd_diff",
                        lambda app, s, main_sha, chart_revision=None,
                        changed_paths=None, renames=None: plan.get(app))
    monkeypatch.setitem(m._app_chart_map, "pv-fail-a-ms", "appspace-ms")
    monkeypatch.setitem(m._app_chart_revision_map, "pv-fail-a-ms", "2603.1.0")
    m.process_pr({"id": 909 + _seq[0], "title": "t",
                  "source": {"commit": {"hash": pr_sha},
                             "branch": {"name": "f"}},
                  "destination": {"branch": {"name": "main"}}},
                 {_IDENT: ["pv-fail-a-ms"]}, base_sha=base_sha)
    return [s for s in statuses if s[0] != "INPROGRESS"]
