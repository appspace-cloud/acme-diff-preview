"""COPS-2640: no block repeats a pointer the table row already carries.

Audited on acme-config-prod #4095 (2.51.0): the same-shape group listed
its 8 members as link bullets, each restating a linked table row above;
and the fold blocks ended with a "Full hunks for app" line duplicating
the same row's link — rendered broken by Bitbucket on top (the app name
falls outside the anchor). The fold CONCLUSIONS stay; only pointers go.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as dp  # noqa: E402

URL = "https://argocd.appspace.com/diff/acme-config-prod/4095/26bc1eb80130"


def _same_shape(name, n=9):
    hdrs = ["/apps/Deployment d%d" % i for i in range(n)]
    secs = [(h, "  image: acme/%s:1" % name) for h in hdrs]
    return dp.DiffResult("\n".join("--- %s" % h for h in hdrs), secs,
                         n, True, None, dp.OUT_DIFF, None)


def _fold_result(name, folded=5, needles=2):
    n = folded + needles
    hdrs = ["/apps/Deployment %s-%d" % (name, i) for i in range(n)]
    secs = [(h, "  image: acme/x:1") for h in hdrs]
    r = dp.DiffResult("\n".join("--- %s" % h for h in hdrs), secs,
                      n, True, None, dp.OUT_DIFF, None)
    return r._replace(version_fold={
        "n_foldable": folded, "headers": tuple(hdrs[:folded]),
        "label": "1.90.0-rc.1 \u2192 1.90.0-rc.2",
        "classes": ("image tags", "chart labels")})


def _comment(results, **kw):
    return dp.format_comment("a" * 40, results, base_sha="b" * 40, **kw)


def test_shape_group_lists_members_as_plain_roster(monkeypatch):
    """#4095: 8 link bullets restated 8 linked table rows. The group
    keeps its statement; the members become the one-line roster the
    failure groups already use."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    results = {"pv-g%02d-a-ms" % i: _same_shape("same") for i in range(8)}
    out = _comment(results, artifact_url=URL)
    assert "application(s) changed the same" in out
    tail = out.split("application(s) changed the same")[1]
    assert "- [pv-g00-a-ms]" not in tail
    assert "pv-g00-a-ms" in tail.split("\n\n")[0] + tail.split("\n")[2] \
        or "pv-g00-a-ms" in tail[:600]


def test_every_member_still_reachable_via_its_table_row(monkeypatch):
    """COPS-2622 holds: the pointer moved, it did not disappear."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    results = {"pv-g%02d-a-ms" % i: _same_shape("same") for i in range(8)}
    out = _comment(results, artifact_url=URL)
    for i in range(8):
        assert "| [`pv-g%02d-a-ms`](%s#app-pv-g%02d-a-ms)" % (i, URL, i) in out


def test_fold_block_keeps_conclusions_drops_pointer(monkeypatch):
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    results = {"pv-appspacepoc-a-ms": _fold_result("a")}
    out = _comment(results, artifact_url=URL)
    assert "are the version transition" in out
    assert "Changed for another reason" in out
    assert "[Full hunks for" not in out


def test_page_profile_keeps_every_pointer(monkeypatch):
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    results = {"pv-g%02d-a-ms" % i: _same_shape("same") for i in range(8)}
    results["pv-fold-a-ms"] = _fold_result("f")
    out = _comment(results, artifact_url=URL,
                   profile=dp.RenderProfile("page", is_complete_record=True,
                                            inline_diffs=True))
    # The page has no group blocks at all (group_repeats is off there:
    # it is the complete record), so the guard is that every app still
    # renders its own full block.
    assert out.count("resource(s) changed") >= 9


def test_without_artifact_url_nothing_changes(monkeypatch):
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    results = {"pv-g%02d-a-ms" % i: _same_shape("same") for i in range(8)}
    out = _comment(results)
    assert "application(s) changed the same" in out
    assert "](" not in out.split("application(s) changed the same")[1][:400]
