"""COPS-2626: a post-deploy smoke check that renders a REAL artifact and
asserts properties of the result.

Two bugs shipped during the COPS-2607 umbrella and survived 1,449 passing
tests plus in-pod curl verification. Both were found by a human opening
the page in a browser:

  - the page rendered "could not be produced" about ITSELF, live for two
    releases, because process_pr passed artifact_url to the comment render
    and never to the page render. Every test asserted the comment surface,
    and the comment surface was correct.
  - an index entry pointing into the collapsed overflow did nothing when
    clicked. String assertions confirm an anchor EXISTS; they cannot
    confirm a browser can reach it.

The curl check said the markup was present and well formed. It was. It was
also false. So the check this module needs is not "is the markup there"
but "does the markup mean what the page claims", and the tests below are
written the same way: each one builds a page that a presence check would
pass and asserts the smoke check FAILS it.

Each check returns a human readable failure string or None. The runner
returns every failure rather than the first, because a deploy that broke
three properties should say so once.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_ui  # noqa: E402
import smoke  # noqa: E402


SMALL = (
    "## \U0001f52d ACME Diff Preview\n\n"
    "**Commit** `abc12345` \u2192 `main` | `acme-config-prod`\n\n"
    "\u26a0\ufe0f **`pv-alpha-a-ms`** \u2014 2 resource(s) changed\n\n"
    "**`/apps/Deployment apigateway`**\n\n"
    "```diff\n@@ -1,1 +1,1 @@\n-  replicas: 1\n+  replicas: 2\n```\n"
    "**`/apps/Deployment worker`**\n\n"
    "```diff\n@@ -1,1 +1,1 @@\n-  replicas: 3\n+  replicas: 4\n```\n")


def _art(body, repo="acme-config-prod", pr=4019, sha="abc12345"):
    return {"repo": repo, "pr_id": pr, "sha": sha, "body": body,
            "created_utc": "2026-08-08T00:00:00Z",
            "pr_url": "https://bb/pr/%d" % pr}


def _page(body):
    return diff_ui.render_html(_art(body))


# -- the happy path --------------------------------------------------------

def test_a_healthy_page_passes_every_check():
    body = SMALL
    assert smoke.check_page(_page(body), body) == []


def test_the_real_production_artifact_shape_passes():
    """A page with a fence, an index and several anchors is the normal
    case; the check must not cry wolf on it."""
    body = SMALL + ("\u26a0\ufe0f **`pv-beta-b-ms`** \u2014 1 resource(s) changed\n\n"
                    "**`/apps/Deployment api`**\n\nplain line\n")
    assert smoke.check_page(_page(body), body) == []


# -- 1 and 2: the page must never disclaim itself --------------------------

def test_a_page_that_says_it_could_not_be_produced_fails():
    """The exact bug that was live for 2.32.0 and 2.33.0. The markup was
    present and well formed, which is why curl said it was fine."""
    body = SMALL + "The full-diff page could not be produced for this run\n"
    fails = smoke.check_page(_page(body), body)
    assert any("could not be produced" in f for f in fails)


def test_a_page_that_says_the_diff_was_truncated_fails():
    body = SMALL + "diff truncated for display\n"
    fails = smoke.check_page(_page(body), body)
    assert any("truncated" in f for f in fails)


def test_the_disclaimer_check_reads_prose_rows_not_diff_content():
    """The body is PR-controlled. Someone whose values file quotes the
    phrase inside a diff must not be able to fail a good deploy, so the
    check reads the rows the page rendered as prose."""
    body = SMALL + "```diff\n+  note: could not be produced\n```\n"
    assert smoke.check_page(_page(body), body) == []


# -- 3: the index count must match what the index contains -----------------

def test_an_index_whose_stated_count_does_not_match_its_entries_fails():
    page = _page(SMALL).replace("Index: 1 application(s)",
                                "Index: 7 application(s)")
    fails = smoke.check_page(page, SMALL)
    assert any("application" in f and "7" in f for f in fails)


def test_an_index_whose_resource_count_does_not_match_fails():
    page = _page(SMALL).replace("2 resource(s)</summary>",
                                "99 resource(s)</summary>")
    fails = smoke.check_page(page, SMALL)
    assert any("resource" in f for f in fails)


# -- 4: every index link must resolve, exactly once -------------------------
# This is the check that makes a silent 404 impossible, and the one that
# would have caught the unreachable-anchor bug.

def test_an_index_link_with_no_target_fails():
    page = _page(SMALL).replace('id="app-pv-alpha-a-ms"', 'id="app-moved"', 1)
    fails = smoke.check_page(page, SMALL)
    assert any("app-pv-alpha-a-ms" in f and "0" in f for f in fails)


def test_a_duplicated_anchor_target_fails():
    """Two elements with the same id is not a 404, it is worse: the browser
    silently picks one and the reader cannot tell which."""
    page = _page(SMALL).replace(
        '<tbody', '<tr id="app-pv-alpha-a-ms"></tr><tbody', 1)
    fails = smoke.check_page(page, SMALL)
    assert any("app-pv-alpha-a-ms" in f for f in fails)


def test_every_resource_link_is_checked_not_just_the_app_ones():
    page = _page(SMALL)
    target = [i for i in smoke.index_targets(page) if "deployment" in i]
    assert target, "fixture must contain resource anchors"
    broken = page.replace('id="%s"' % target[0], 'id="gone"', 1)
    fails = smoke.check_page(broken, SMALL)
    assert any(target[0] in f for f in fails)


# -- 5: /raw stays byte-identical to the stored body -----------------------

def test_raw_that_differs_from_the_stored_body_fails():
    fails = smoke.check_raw(SMALL + "one extra line\n", SMALL)
    assert fails and "raw" in fails[0]


def test_raw_that_matches_passes():
    assert smoke.check_raw(SMALL, SMALL) == []


# -- 6: the two surfaces still say different things ------------------------

def test_a_comment_carrying_fenced_diff_blocks_fails():
    """Phase E moved every YAML hunk to the page. A comment that grew one
    back means the split regressed."""
    comment = "summary\n```diff\n-a\n+b\n```\n[full output](https://x/#app-a)"
    fails = smoke.check_comment(comment, _page(SMALL))
    assert any("fenced diff" in f for f in fails)


def test_a_comment_whose_deep_link_has_no_anchor_on_the_page_fails():
    comment = "summary\n[open pv-alpha-a-ms](https://x/diff/a/1/b#app-does-not-exist)"
    fails = smoke.check_comment(comment, _page(SMALL))
    assert any("app-does-not-exist" in f for f in fails)


def test_a_comment_with_a_resolvable_deep_link_passes():
    comment = ("summary\n[open pv-alpha-a-ms]"
               "(https://x/diff/a/1/b#app-pv-alpha-a-ms)")
    assert smoke.check_comment(comment, _page(SMALL)) == []


def test_a_comment_with_no_link_at_all_fails():
    assert smoke.check_comment("just a summary", _page(SMALL))


# -- the runner ------------------------------------------------------------

def test_the_runner_reports_every_failure_not_only_the_first():
    page = _page(SMALL)
    page = page.replace("Index: 1 application(s)", "Index: 4 application(s)")
    page = page.replace("plain", "x").replace(
        "</table>", "diff truncated for display</table>", 1)
    fails = smoke.run(page=page, body=SMALL, raw=SMALL + "drift\n",
                      comment="no link here")
    assert len(fails) >= 3, fails


def test_the_runner_is_quiet_when_everything_holds():
    comment = "summary\n[open](https://x/diff/a/1/b#app-pv-alpha-a-ms)"
    assert smoke.run(page=_page(SMALL), body=SMALL, raw=SMALL,
                     comment=comment) == []


def test_the_runner_can_skip_the_comment_when_none_is_supplied():
    """Rendering a stored artifact does not always give us the comment that
    accompanied it. A missing comment is not a failure; silently passing a
    comment check that never ran would be."""
    assert smoke.run(page=_page(SMALL), body=SMALL, raw=SMALL) == []
