"""Render profiles: one object instead of a magic integer (COPS-2609, phase B).

Two surfaces are produced by the same function today -- the PR comment and
the full-diff page -- and the only thing separating them is `readable_budget`.
That single integer does far more than its name says: its truthiness also
decides whether repeated diffs are grouped, whether version-transition noise
is folded, whether the overview table is capped, and whether the appendix
collapses into a pointer. None of that is readable from a call site, and
none of it is testable as a contract.

Phases C, D and E need to express things that integer cannot say at all:
no body cap, no section cap, no input panel, no fences. So this phase turns
the difference between the two surfaces into a value.

This phase must be behaviour-neutral apart from the link added in
test_cops2607b_full_diff_link.py. The 18 goldens are the real proof of that,
since they are byte-compared against output produced before the refactor;
the tests here prove the alias mapping, the immutability of the shared
profile objects, and that the switches phases C-E will flip are actually
wired to something.
"""
import os
import sys

import pytest

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m

SHA = "a2e6383e7c1d4f5a6b7c8d9e0f1a2b3c4d5e6f70"
URL = "https://argocd.appspace.com/diff/acme-config-prod/3899/abc123def456"
BODY = "   metadata:\n-    replicas: 2\n+    replicas: 3\n"


def _changed(n_apps=1):
    """Apps with a real diff: the shape that exercises fences, grouping and
    the intra-app budget all at once."""
    return {f"pv-foo-{i}-ss": m.DiffResult(
        BODY, [(f"/apps/Deployment pv-foo-{i}/web", BODY)], 1, True, None,
        m.OUT_DIFF, "diff") for i in range(n_apps)}


def _bulky(n_apps=12, lines=110):
    """Apps with DISTINCT diffs, big enough in total to cross
    COMMENT_READABLE_BYTES.

    Two properties this fixture has to have, both learned the hard way:

    - **Distinct.** Byte-identical diffs are collapsed into one
      representative by the COPS-2579 fingerprint grouping, which runs on
      both surfaces and is not budget-gated. A fixture of identical apps
      renders the same on both profiles for a perfectly correct reason and
      proves nothing about the budget.
    - **Not redactable.** Bodies are redacted at display time, so a field
      named `key`/`pass`/`auth` collapses to [REDACTED] and a 4KB body
      becomes ten bytes. The budget is measured after that, so a redacted
      fixture never crosses it however big it looks here.
    """
    def _body(i):
        return "".join(f"+    replica-count-{i}-{j}: 7\n" for j in range(lines))
    return {f"pv-bulk-{i:02d}-ss": m.DiffResult(
        _body(i), [(f"/apps/ConfigMap pv-bulk-{i:02d}/data", _body(i))],
        1, True, None, m.OUT_DIFF, "diff") for i in range(n_apps)}


def _render(results=None, **kw):
    return m.format_comment(SHA, results or _changed(), **kw)


# --- 1. the two profiles are exactly the two bodies we render today ------

def test_comment_profile_is_what_a_bare_call_renders():
    """The default call site (the PR comment) and the named COMMENT profile
    must be the same thing. If they ever diverge, one of the two surfaces
    is being rendered by accident rather than by contract."""
    assert _render() == _render(profile=m.COMMENT_PROFILE)


def test_full_profile_is_what_readable_budget_zero_renders():
    """`readable_budget=0` is how process_pr builds the persisted page, and
    how eleven existing tests build it. That call must keep working and must
    mean exactly the FULL profile."""
    assert (_render(readable_budget=0)
            == _render(profile=m.FULL_PROFILE))


def test_the_two_profiles_actually_differ():
    """Guard against a refactor that quietly collapses both surfaces into
    one. Named after the observable difference rather than a byte compare:
    past COMMENT_READABLE_BYTES the comment folds ordinary apps into a
    pointer, and the page -- the thing that pointer points at -- must not."""
    results = _bulky()
    comment = _render(results)
    page = _render(results, profile=m.FULL_PROFILE)
    assert "omitted here to keep this comment scannable" in comment
    assert "omitted here to keep this comment scannable" not in page
    assert len(page) > len(comment)


# --- 2. the deprecated keyword keeps working ----------------------------

def test_custom_budget_stays_on_the_comment_profile():
    """Three existing tests pass arbitrary budgets (8000, 6000, 2500) to
    exercise the fold. Those are a COMMENT render with a tighter budget, not
    a different surface: grouping and folding must stay on."""
    p = m.RenderProfile.from_readable_budget(8000)
    assert p.readable_budget == 8000
    assert p.group_repeats is True
    assert p.version_fold is True


def test_zero_budget_is_the_full_surface():
    p = m.RenderProfile.from_readable_budget(0)
    assert p.readable_budget == 0
    assert p.group_repeats is False, "the page shows every app, never a group"
    assert p.version_fold is False, "the page folds nothing"


def test_none_budget_is_the_module_default():
    p = m.RenderProfile.from_readable_budget(None).resolved()
    assert p.readable_budget == m.COMMENT_READABLE_BYTES


def test_the_budget_is_read_at_render_time_not_at_import(monkeypatch):
    """COMMENT_READABLE_BYTES is an _env_int. Pinning it into the profile
    object at import made the module attribute decorative: the env var was
    read once and every later change to it -- a test patching it, an
    operator raising it -- was silently ignored. Caught by the
    readable_budget_collapse golden, which patches it to 2500 and expects
    the fold to move."""
    monkeypatch.setattr(m, "COMMENT_READABLE_BYTES", 2_500)
    assert m.RenderProfile.from_readable_budget(
        None).resolved().readable_budget == 2_500
    results = _bulky()
    tight = _render(results)
    monkeypatch.setattr(m, "COMMENT_READABLE_BYTES", 500_000)
    assert len(_render(results)) > len(tight), \
        "a bigger budget must fold less, or the constant is being ignored"


def test_passing_both_is_refused_rather_than_guessed():
    """Silently letting one win would make a caller believe it set a budget
    it did not set. Phases C-E move call sites to profiles one at a time, so
    the overlap window is exactly when this must be loud."""
    with pytest.raises(TypeError):
        _render(profile=m.FULL_PROFILE, readable_budget=8000)


# --- 3. the shared profiles are constants, not scratch space ------------

def test_profiles_are_frozen():
    """COMMENT_PROFILE is a module-level object shared by every PR rendered
    by every worker thread. A render that could mutate it would leak the
    change into unrelated PRs, and the symptom would be a comment that
    folds differently depending on what was rendered before it."""
    import dataclasses
    profile = m.COMMENT_PROFILE  # fetched first: a missing attribute would
    with pytest.raises(dataclasses.FrozenInstanceError):  # pass this vacuously
        profile.readable_budget = 1


def test_a_derived_profile_leaves_the_original_alone():
    derived = m.COMMENT_PROFILE.replace(readable_budget=1234)
    assert derived.readable_budget == 1234
    assert m.COMMENT_PROFILE.resolved().readable_budget == \
        m.COMMENT_READABLE_BYTES


# --- 4. the switches phases C-E will flip are wired ---------------------

def test_inline_diffs_off_removes_the_fences():
    """Phase E flips this. It is inert today, and that is the point: E must
    be a profile change, not another surgery on format_comment."""
    off = m.COMMENT_PROFILE.replace(inline_diffs=False)
    assert "```diff" in _render()
    assert "```diff" not in _render(profile=off)


def test_inline_diffs_off_still_names_every_app():
    """Rule 2 of the umbrella: never lose information. Dropping the YAML may
    not drop the fact that the app changed."""
    results = _changed(3)
    out = _render(results, profile=m.COMMENT_PROFILE.replace(inline_diffs=False))
    for app in results:
        assert app in out


def test_input_panel_off_removes_the_panel():
    lines = ["### \U0001f9ea Why this changed", "", "- a cause line", ""]
    on = _render(input_change_lines=lines)
    off = _render(input_change_lines=lines,
                  profile=m.COMMENT_PROFILE.replace(input_panel=False))
    assert "a cause line" in on
    assert "a cause line" not in off


def test_body_max_chars_comes_from_the_profile():
    """Phase C removes this cap on the page only. It has to be reachable
    from the profile first, or C has to edit the render again."""
    big = {"pv-foo-a-ss": m.DiffResult(
        "x" * 20_000, [("/apps/ConfigMap pv-foo-a/big", "+" + "x" * 20_000)],
        1, True, None, m.OUT_DIFF, "diff")}
    capped = _render(big, profile=m.COMMENT_PROFILE.replace(body_max_chars=500))
    assert "diff truncated for display" in capped
    uncapped = _render(big, profile=m.COMMENT_PROFILE.replace(
        body_max_chars=10 ** 9))
    assert "diff truncated for display" not in uncapped


# --- 5. observability: phases C-E are verified against these numbers ----

def _stat(key):
    with m._diff_stats_lock:
        return m._diff_stats.get(key)


def test_comment_bytes_and_fences_are_recorded():
    body = _render(_changed(2))
    m._record_comment_stats(body, m.COMMENT_PROFILE)
    assert _stat("comment_bytes") == len(body.encode("utf-8"))
    assert body.count("```diff") > 0, "the fixture must actually have fences"
    assert _stat("comment_fences") == body.count("```diff")


def test_the_high_water_mark_survives_a_smaller_comment():
    """Phase E's acceptance is "no comment ever reaches MAX_COMMENT_BYTES".
    A last-value gauge cannot prove that -- whatever PR happened to render
    last would answer the question. The maximum can."""
    m._record_comment_stats(_render(_changed(8)), m.COMMENT_PROFILE)
    peak = _stat("comment_max_bytes")
    m._record_comment_stats(_render(_changed(1)), m.COMMENT_PROFILE)
    assert _stat("comment_max_bytes") == peak
    assert _stat("comment_bytes") < peak


# --- 6. the link must never point at a page that was not written --------

def test_save_reports_success_and_failure(tmp_path, monkeypatch):
    """The comment promises the page before the page is written. Today the
    save swallows its own failure, so a run that failed to persist still
    posts a comment linking to a 404. The caller has to be able to tell."""
    monkeypatch.setattr(m, "DIFF_UI_ENABLED", True)
    monkeypatch.setattr(m, "DIFF_UI_DIR", str(tmp_path))
    assert m._save_diff_ui_artifact("acme-config-prod", 1, SHA, "body") is True

    def _boom(*a, **kw):
        raise OSError("no space left on device")

    monkeypatch.setattr(m.diff_ui, "save_artifact", _boom)
    assert m._save_diff_ui_artifact("acme-config-prod", 2, SHA, "body") is False


def test_save_reports_failure_when_the_page_is_switched_off(monkeypatch):
    """A disabled page is not a failure, but it is still no page: the caller
    must take the same fallback branch."""
    monkeypatch.setattr(m, "DIFF_UI_ENABLED", False)
    assert m._save_diff_ui_artifact("acme-config-prod", 3, SHA, "body") is False


def test_the_fallback_is_counted():
    """If this counter is not zero in production, some reviewer is reading a
    comment that quietly stopped being backed by a page. Phase E turns that
    from an annoyance into missing information, so it needs a number now."""
    before = _stat("comment_fallback_inline") or 0
    m._record_comment_stats(_render(), m.COMMENT_PROFILE, fallback_inline=True)
    assert _stat("comment_fallback_inline") == before + 1
    m._record_comment_stats(_render(), m.COMMENT_PROFILE)
    assert _stat("comment_fallback_inline") == before + 1
