"""COPS-2629 part 2: applications whose change has the same SHAPE collapse
into one line in the comment.

On acme-config-prod PR #4026, 22 `-glb` applications each rendered:

    warning **`pv-asi-b-glb`** - 9 resource(s) changed

    [Full hunks for `pv-asi-b-glb`](.../#app-pv-asi-b-glb)

44 lines saying the same thing 22 times, because one version bump touched
the same 9 resources in every environment.

Fingerprint grouping (COPS-2579) cannot help: each rendered diff contains
its own customer's names, so no two are byte-identical and every app forms
its own group. The routine-bump rollup cannot help either, because these
diffs are not provably version-only.

What IS identical is the SHAPE: the same resource headers, the same count.
Saying "22 applications changed the same 9 resources" is a true statement
about shape, and the values behind it live on the page.

Two limits, both load-bearing:

  - Grouping keys on the exact set of section headers, not on the count
    alone. Two apps that both changed 9 resources are not the same change
    unless they changed the SAME 9.
  - A risky app is never grouped. Deleted resources, zeroed replicas, VM
    changes and version downgrades keep their own block, because the
    entire point of those blocks is that someone reads them individually.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_preview as dp  # noqa: E402

URL = "https://argocd.appspace.com/diff/acme-config-prod/4026/ffba80a2"


def _sections(headers, body="  replicas: 2"):
    # (header, body) pairs -- the shape the real diff path produces and
    # _format_app_diff_block reads. An earlier version of this file used
    # dicts, which meant these tests passed against a fiction while the
    # full suite raised on every real diff.
    return [(h, body) for h in headers]


HDRS9 = ["/apps/Deployment %s" % n for n in
         ("api", "worker", "web", "cache", "queue", "cron", "auth", "cdn",
          "search")]


def _changed(headers=None, n_res=None, name="x", **kw):
    hdrs = headers or HDRS9
    secs = _sections(hdrs, body="  image: acme/%s:2603.1.14" % name)
    return dp.DiffResult(
        "\n".join("--- %s" % h for h in hdrs), secs, n_res or len(hdrs),
        True, None, dp.OUT_DIFF, None, **kw)


def _glb_set(n, headers=None):
    return {"pv-c%02d-glb" % i: _changed(headers, name="c%02d" % i)
            for i in range(n)}


def _comment(results, **kw):
    return dp.format_comment("a" * 40, results, base_sha="b" * 40,
                             artifact_url=URL, **kw)


# -- the measured #4026 shape ----------------------------------------------

def test_twenty_two_same_shape_apps_collapse_to_one_statement(monkeypatch):
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    results = _glb_set(22)
    out = _comment(results)
    assert "22 application(s) changed the same 9 resource(s)" in out
    # No per-app header survives: the old shape was "**`app`** - N
    # resource(s) changed", once per application.
    assert out.count("resource(s) changed") == 0
    assert out.count("[Full hunks for") == 0  # names carry the links now


def test_the_collapsed_line_states_how_many_apps_it_covers(monkeypatch):
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    out = _comment(_glb_set(22))
    assert "22 application(s)" in out


def test_the_collapsed_line_still_links_to_the_page(monkeypatch):
    """Relocation, not loss: whatever the comment stops showing has to be
    one click away."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    assert URL in _comment(_glb_set(22))


def test_a_sample_of_the_app_names_is_still_visible(monkeypatch):
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    results = _glb_set(22)
    out = _comment(results)
    assert len([a for a in results if a in out]) >= 4


# -- shape means the SAME resources, not the same count --------------------

def test_same_count_but_different_resources_does_not_collapse(monkeypatch):
    """Nine resources and nine resources are not one change unless they are
    the same nine. Collapsing on the count alone would hide a genuinely
    different change inside a group that claims to describe it."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    other = ["/apps/StatefulSet %s" % n for n in
             ("a", "b", "c", "d", "e", "f", "g", "h", "i")]
    results = _glb_set(5)
    results.update({"pv-odd-%d-glb" % i: _changed(other, name="odd%d" % i)
                    for i in range(5)})
    out = _comment(results)
    assert out.count("changed the same 9 resource(s)") == 2


def test_two_apps_do_not_form_a_group(monkeypatch):
    """COPS-2605 settled this for the routine-bump rollup and it applies
    here unchanged: below INPUT_ROLLUP_MIN_SERVICES, collapsing costs the
    reader per-app detail and saves nothing, because a two-app comment was
    never the problem this ticket is about.

    This test asserted the opposite when it was written. The full suite
    caught the contradiction: test_no_rollup_below_three_groups had
    already pinned the rule."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    out = _comment(_glb_set(2))
    assert "application(s) changed the same" not in out
    # COPS-2636: the two apps render as their own linked table rows.
    assert "[pv-c00-glb]" in out and "[pv-c01-glb]" in out


def test_three_apps_are_enough_to_form_a_group(monkeypatch):
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    out = _comment(_glb_set(3))
    assert "3 application(s) changed the same 9 resource(s)" in out


def test_each_grouped_app_name_is_its_own_deep_link(monkeypatch):
    """COPS-2622 requires every app pointer to land on that app's section.
    A single group link would strip that from every member but one, so the
    names themselves are the pointers."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    out = _comment(_glb_set(22))
    # COPS-2640: the pointer moved into the Changeset overview row; the
    # group lists members as the plain one-line roster. COPS-2622 still
    # holds -- every member reachable through its own linked row.
    assert "| [pv-c00-glb](%s#app-pv-c00-glb)" % URL in out
    assert "| [pv-c21-glb](%s#app-pv-c21-glb)" % URL in out
    roster = next(l for l in out.split("\n")
                  if l.startswith(">") and "pv-c00-glb" in l)
    assert "pv-c07-glb" in roster and "more" in roster
    assert "- [pv-c00-glb]" not in out


def test_a_single_app_renders_exactly_as_before(monkeypatch):
    """One app is not a group, and its existing block is what every golden
    asserts."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    out = _comment({"pv-solo-a-glb": _changed(name="solo")})
    # COPS-2636 moved the per-app line into its Changeset overview row.
    assert "[pv-solo-a-glb](%s#app-pv-solo-a-glb)" % URL in out
    assert "application(s) changed the same" not in out


def _collapsed_names(out):
    """The text of the group-summary lines, where a risky app must never
    end up (COPS-2629's actual safety property)."""
    return "\n".join(ln for ln in out.splitlines()
                     if "application(s) changed the same" in ln
                     or "Identical diff across" in ln)


# -- risk always wins ------------------------------------------------------

def test_an_app_with_a_deleted_resource_keeps_its_own_block(monkeypatch):
    """The whole purpose of a deletion block is that someone reads it for
    that environment. Folding it into "and 21 others" is exactly the
    outcome this service exists to prevent."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    results = _glb_set(21)
    risky = dp.DiffResult(
        "\n".join("--- %s" % h for h in HDRS9), _sections(HDRS9), 9, True,
        None, dp.OUT_DIFF, None, None, ["/apps/Deployment api"])
    results["pv-risky-a-glb"] = risky
    out = _comment(results)
    # COPS-2651: this asserted the app's header line, which was a PROXY for
    # the property in the docstring -- that the deletion is readable for
    # THIS environment. The header never said a deletion happened, nor
    # which resource; with COMMENT_INLINE_EVIDENCE_LINES at its default of
    # 0 it said only the app name and a count, both of which its Changeset
    # overview row already carried. The property is asserted directly now,
    # and it is served by the dedicated deletion panel, which is strictly
    # more informative than the header ever was.
    assert "pv-risky-a-glb" in out and "/apps/Deployment api" in out, (
        "the deleted resource must be named for this environment")
    assert "RESOURCE(S) DELETED" in out, "the deletion panel must render"
    assert "21 application(s) changed the same 9 resource(s)" in out, (
        "the other 21 still collapse")
    assert "pv-risky-a-glb" not in _collapsed_names(out), (
        "the risky app must never be folded into the group summary")


def test_a_zeroed_replica_app_keeps_its_own_block(monkeypatch):
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    results = _glb_set(21)
    results["pv-zero-a-glb"] = dp.DiffResult(
        "\n".join("--- %s" % h for h in HDRS9), _sections(HDRS9), 9, True,
        None, dp.OUT_DIFF, None, None, None, ["/apps/Deployment api"])
    out = _comment(results)
    # COPS-2651, same reasoning as the deletion case above: assert that the
    # zeroed environment is readable on its own, not that one specific line
    # is the thing that makes it readable.
    assert "pv-zero-a-glb" in out
    assert "pv-zero-a-glb" not in _collapsed_names(out), (
        "a zeroed-replica app must never be folded into the group summary")
    assert "21 application(s) changed the same 9 resource(s)" in out


# -- the two-surface contract ----------------------------------------------

def test_the_page_still_renders_every_app_separately(monkeypatch):
    """is_complete_record keeps one block per application, because the page
    is the record. Nothing is collapsed out of both surfaces."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    page = _comment(_glb_set(22),
                    profile=dp.RenderProfile("page", is_complete_record=True,
                                             inline_diffs=True))
    assert page.count("9 resource(s) changed") == 22


def test_the_totals_still_count_every_application(monkeypatch):
    """Collapsing is a display decision. The headline counts must keep
    describing the changeset, not the number of groups."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    out = _comment(_glb_set(22))
    assert "22" in out
