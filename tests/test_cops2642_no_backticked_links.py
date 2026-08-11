"""COPS-2642: no markdown link may have backticked link text.

Bitbucket does not render a link whose text is code: it emits the
<code> and drops the anchor. Confirmed against the rendered DOM of a
real merged PR (acme-config-prod #4098) — zero <a> elements in the
comment table — and pinned down with a four-way probe:

    [`x`](url) inside a table  -> no link
    [x](url)   inside a table  -> link
    [`x`](url) outside a table -> no link
    [x](url)   outside a table -> link

Backticks are the cause; tables render links fine.

Since 2.49.0 the App cell was written as [`app`](url), and COPS-2636 and
COPS-2640 then removed the other pointers because "the row already
carries the link". It did not. Operators had no working per-app deep
link at all.

Every check that let this through counted the link in the MARKDOWN,
where it is present and well formed. So this guard is written as a
general rule over the rendered comment rather than a check of one call
site: any future link site is covered without anyone remembering.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as dp  # noqa: E402

URL = "https://argocd.appspace.com/diff/acme-config-prod/4098/370ea29f"

# A markdown link whose text starts or ends with a backtick.
BACKTICKED_LINK = re.compile(r"\[[^\]]*`[^\]]*\]\([^)]+\)")


def _changed(name="x", n=3):
    hdrs = ["/apps/Deployment %s-%d" % (name, i) for i in range(n)]
    secs = [(h, "  image: acme/%s:1" % name) for h in hdrs]
    return dp.DiffResult("\n".join("--- %s" % h for h in hdrs), secs,
                         n, True, None, dp.OUT_DIFF, None)


def _unchanged():
    return dp.DiffResult("", [], 0, False, None, dp.OUT_NO_DIFF, None)


def _comment(results, **kw):
    return dp.format_comment("a" * 40, results, base_sha="b" * 40, **kw)


def test_app_cell_link_is_clickable(monkeypatch):
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    out = _comment({"pv-myschroders-a-ms": _changed("ms", 7)},
                   artifact_url=URL)
    assert "| [pv-myschroders-a-ms](%s#app-pv-myschroders-a-ms) |" % URL in out
    assert "[`pv-myschroders-a-ms`](" not in out


def test_no_link_anywhere_has_backticked_text(monkeypatch):
    """The general rule, over every shape the comment can take."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    shapes = {
        "small": {"pv-a-ms": _changed("a"), "pv-b-ss": _unchanged()},
        "large": {**{"pv-l%02d-a-ms" % i: _changed("l%02d" % i, 3 + i)
                     for i in range(14)}, "pv-u-ss": _unchanged()},
        "same_shape_group": {"pv-g%02d-a-ms" % i: _changed("same", 9)
                             for i in range(8)},
        "risky": {"pv-r-a-ms": _changed("r", 4)._replace(
            deleted_resources=["/apps/Deployment r-0"])},
    }
    for name, results in shapes.items():
        for kwargs in ({"artifact_url": URL},
                       {"artifact_url": URL,
                        "profile": dp.RenderProfile("page",
                                                    is_complete_record=True,
                                                    inline_diffs=True)},
                       {}):
            out = _comment(results, **kwargs)
            bad = BACKTICKED_LINK.findall(out)
            assert not bad, (
                "%s (%s): Bitbucket drops these links entirely: %s"
                % (name, "page" if "profile" in kwargs else "comment",
                   bad[:3]))
