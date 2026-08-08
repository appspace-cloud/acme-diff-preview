"""The comment must always link to the full-diff page (COPS-2609, phase B).

Measured gap: every URL in the body of acme-config-prod #3899 belongs to the
rendered YAML itself. There is no link to the full-diff page anywhere in the
comment body -- it exists only in the Bitbucket build-status area, which is a
different surface a reviewer has to know about.

The links that do exist today are all conditional: a truncation note, a
per-app "full hunks" pointer, a rollup line. On a comment where nothing was
truncated and nothing was folded, none of them render and the page is
unreachable from the comment. Confirmed on a real 2026-08-07 comment:
`grep -c "/diff/"` returned 0.

Phase E cannot remove inline YAML until this link is unconditional, or the
information is simply gone. That makes this the foundation phase, and the
fallback below is the safety property the later phases lean on: when the
page cannot be produced, the comment must say so rather than quietly point
nowhere.
"""
import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m

URL = "https://argocd.appspace.com/diff/acme-config-prod/3899/abc123def456"
SHA = "a2e6383e7c1d4f5a6b7c8d9e0f1a2b3c4d5e6f70"


def _clean_result():
    """One app with no changes: the shape that renders the shortest comment,
    and the one where every conditional link is absent today."""
    return {"pv-foo-a-ss": m.DiffResult("", [], 0, False, None,
                                        m.OUT_NO_DIFF, "clean")}


def _changed_result():
    body = "   metadata:\n-    replicas: 2\n+    replicas: 3\n"
    return {"pv-foo-a-ss": m.DiffResult(
        body, [("/apps/Deployment pv-foo-a/web", body)], 1, True, None,
        m.OUT_DIFF, "diff")}


def _body(results, artifact_url=URL, **kw):
    return m.format_comment(SHA, results, artifact_url=artifact_url, **kw)


# --- 1. the gap: the link must be unconditional --------------------------

def test_clean_comment_links_to_the_page():
    """The case that has no link at all today: nothing truncated, nothing
    folded, so no conditional pointer fires."""
    out = _body(_clean_result())
    assert URL in out, "a clean comment must still reach the full-diff page"


def test_changed_comment_links_to_the_page():
    out = _body(_changed_result())
    assert URL in out


def test_the_page_link_renders_exactly_once():
    """COPS-2609 rendered this pointer twice, in the header region and again
    near the status line, reasoning that on a long comment the header has
    scrolled away.

    COPS-2612 is the change that made the comment short, so that reason
    expired, and COPS-2622 measured the cost of keeping it: with every app
    also carrying a pointer, a 6-app comment held 8 copies of one URL and a
    fleet bump would have held ~42. One page-level link now, plus one deep
    link per application, which is a link per destination rather than the
    same destination repeated.
    """
    out = _body(_changed_result())
    assert out.count("Full rendered diff (every hunk)") == 1


def test_second_link_is_near_the_status_line():
    out = _body(_changed_result())
    idx = out.find("**Status:**")
    assert idx != -1, "the status line must still exist"
    tail = out[max(0, idx - 400):]
    assert URL in tail, \
        "the second link belongs immediately above the status line, where "\
        "the reviewer actually decides"


# --- 2. never point nowhere ---------------------------------------------

def test_no_url_means_no_fake_link():
    out = _body(_clean_result(), artifact_url="")
    assert "](" not in out.split("Status:")[0] or "/diff/" not in out, \
        "with no artifact URL the comment must not render a link at all"


def test_no_url_says_the_page_is_unavailable():
    """Rule 2 of the umbrella: never lose information silently. If the page
    could not be produced, the comment states it instead of degrading
    quietly -- otherwise a reviewer cannot tell an unavailable page from a
    page nobody linked."""
    out = _body(_clean_result(), artifact_url="")
    low = out.lower()
    assert "could not be produced" in low, \
        "the comment must say the full-diff page could not be produced"
    assert "inlined below" in low, \
        "and must state that the hunks are inline instead, so the reader "\
        "knows the information was not simply lost"


# --- 3. nothing already working may regress -----------------------------

def test_header_line_is_unchanged():
    out = _body(_clean_result())
    assert m._comment_header(SHA) in out, \
        "the one true Commit header must survive verbatim"


def test_status_line_still_present():
    out = _body(_changed_result())
    assert "**Status:**" in out


def test_footer_tokens_still_present():
    """SHA dedup and supersede parse these; losing them breaks the poll
    loop, not just the rendering."""
    out = m.format_comment(SHA, _changed_result(), base_sha="deadbeef" * 5,
                           artifact_url=URL)
    assert "[base:" in out, "the base token drives SHA dedup and supersede"
