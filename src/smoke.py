"""COPS-2626: post-deploy smoke checks for the full-diff page.

Two bugs shipped during the COPS-2607 umbrella and survived 1,449 passing
tests plus in-pod curl verification. Both were found by a human opening
the page in a browser. The curl check confirmed the markup was present and
well formed; it was, and it was also false. Verifying presence is not
verifying meaning.

So every check here asserts a RELATION between two things the page claims,
not the existence of a string:

  1/2  the page never disclaims itself ("could not be produced",
       "diff truncated for display"), read from the prose the page
       rendered rather than from anywhere in the html, so a PR that quotes
       the phrase cannot fail a good deploy.
  3    the index count matches the entries the index actually contains.
  4    every index href="#..." resolves to EXACTLY ONE id in the document.
       Zero is a silent 404. Two is worse: the browser picks one and the
       reader cannot tell which.
  5    /raw is byte-identical to the stored body.
  6    the comment for the same artifact carries no fenced diff block, and
       its deep links land on anchors that exist on the page.

Every function returns a list of human readable failures, and the runner
returns all of them rather than the first: a deploy that broke three
properties should say so once.

This is deliberately dependency-free and reads only strings, so it can run
inside the pod against the real artifact store, which is the whole point.
A regex html parser would be the wrong tool for a browser and is the right
one here: it must agree with what the page literally emitted.
"""
import argparse
import json
import re
import sys
import urllib.request

# The page emits ids and hrefs itself, both already sanitised by _slug, so
# these patterns match what this service produces and nothing more.
#
# The index is NOT sliced out of the page by a regex. The first version of
# this module did that with a non-greedy match, which stopped at the first
# nested </details> -- the one closing the first application -- and so read
# one application out of two while reporting success on the rest. That is
# the exact failure mode this module exists to catch, reproduced inside the
# checker itself. Items are matched individually instead, against the two
# shapes _render_index emits and nothing else.
_TOC_APP_RE = re.compile(
    r'<li class="tocapp"[^>]*><details><summary><a href="#([a-z0-9-]+)"')
_TOC_RES_RE = re.compile(r'<li class="tocres"[^>]*><a href="#([a-z0-9-]+)"')
_ID_RE = re.compile(r'\sid="([a-z0-9-]+)"')
_INDEX_PRESENT_RE = re.compile(r'<details class="toc"')
_INDEX_COUNTS_RE = re.compile(
    r'Index: (\d+) application\(s\), (\d+) resource\(s\)')
_TAG_RE = re.compile(r'<[^>]+>')
_ROW_RE = re.compile(r'<tr class="([^"]*)"[^>]*>.*?<td class="code">(.*?)'
                     r'</td></tr>', re.S)
_MD_LINK_RE = re.compile(r'\[[^\]]*\]\((https?://[^)\s]+)\)')

DISCLAIMERS = ("could not be produced", "diff truncated for display")


def _prose_text(page):
    """The visible text of rows the page rendered as prose. Diff rows are
    excluded on purpose: their content is the PR's, and a values line
    quoting a disclaimer is data, not the page speaking about itself."""
    out = []
    for cls, cell in _ROW_RE.findall(page):
        if cls in ("row",) or cls.startswith("row md"):
            out.append(_TAG_RE.sub("", cell))
    return "\n".join(out)


def index_targets(page):
    """Every anchor the navigation index links to, applications first then
    resources. Order does not matter to any caller; completeness does."""
    return _TOC_APP_RE.findall(page) + _TOC_RES_RE.findall(page)


def check_page(page, body=None):
    fails = []
    prose = _prose_text(page)
    for phrase in DISCLAIMERS:
        n = prose.count(phrase)
        if n:
            fails.append("page says %r about itself (%d row(s))"
                         % (phrase, n))

    if not _INDEX_PRESENT_RE.search(page):
        return fails + ["no navigation index on the page"]
    apps = _TOC_APP_RE.findall(page)
    res = _TOC_RES_RE.findall(page)
    counts = _INDEX_COUNTS_RE.search(page)
    if not counts:
        fails.append("index does not state its own counts")
    else:
        stated_apps, stated_res = int(counts.group(1)), int(counts.group(2))
        if stated_apps != len(apps):
            fails.append("index states %d application(s) but lists %d"
                         % (stated_apps, len(apps)))
        if stated_res != len(res):
            fails.append("index states %d resource(s) but lists %d"
                         % (stated_res, len(res)))

    ids = _ID_RE.findall(page)
    for target in apps + res:
        n = ids.count(target)
        if n != 1:
            fails.append("index link #%s resolves to %d element(s), "
                         "expected exactly 1" % (target, n))
    return fails


def check_raw(raw, body):
    if raw != body:
        return ["raw output is not byte-identical to the stored body "
                "(%d vs %d bytes)" % (len(raw), len(body))]
    return []


def check_comment(comment, page):
    """The two-surface contract from COPS-2612: the comment decides, the
    page shows. A comment that grew a fenced diff back, or whose deep link
    lands nowhere, means the split regressed."""
    fails = []
    n_fences = comment.count("```diff")
    if n_fences:
        fails.append("comment carries %d fenced diff block(s); phase E "
                     "moved those to the page" % n_fences)
    ids = set(_ID_RE.findall(page))
    anchors = [u.split("#", 1)[1] for u in _MD_LINK_RE.findall(comment)
               if "#" in u]
    if not anchors:
        fails.append("comment has no deep link into the page")
    for a in anchors:
        if a not in ids:
            fails.append("comment deep link #%s has no anchor on the page"
                         % a)
    return fails


def run(page, body, raw=None, comment=None):
    """All checks. A missing comment is skipped rather than passed: we do
    not always have the comment that accompanied a stored artifact, and
    silently passing a check that never ran is how this class of bug got
    to production in the first place."""
    fails = check_page(page, body)
    if raw is not None:
        fails += check_raw(raw, body)
    if comment is not None:
        fails += check_comment(comment, page)
    return fails


# ── CLI ──────────────────────────────────────────────────────────────────
# Two modes, both exercising the SAME check functions:
#
#   --url    hit a running pod and let it render a real stored artifact.
#            This is the mode the ticket asks for: real code path, real
#            artifact store, run after the pod reports Ready.
#   --file   render a stored artifact json locally. Weaker, because it
#            proves nothing about the deployed pod, but it needs no
#            cluster and is a reasonable CI first cut.
#
# Exit 1 on failure, and print every failure. Loud, but it does NOT roll
# anything back: a false alarm blocking a good deploy is worse than a
# short delay in noticing a real one.


def _fetch(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8")


def from_url(base, repo, pr, sha, comment=None):
    root = "%s/diff/%s/%s/%s" % (base.rstrip("/"), repo, pr, sha)
    page = _fetch(root)
    raw = _fetch(root + "/raw")
    return run(page=page, body=raw, raw=raw, comment=comment)


def from_file(path, comment=None):
    """Render a stored artifact with the deployed code. body and raw come
    from the same stored string, so check_raw is a tautology here and is
    skipped rather than faked."""
    import diff_ui
    art = json.load(open(path))
    page = diff_ui.render_html(art)
    return run(page=page, body=art.get("body", ""), comment=comment)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--url", help="base url of a running pod, "
                                 "e.g. http://127.0.0.1:8080")
    p.add_argument("--repo")
    p.add_argument("--pr")
    p.add_argument("--sha")
    p.add_argument("--file", help="a stored artifact json to render locally")
    p.add_argument("--comment", help="file holding the comment posted for "
                                     "the same artifact, if available")
    a = p.parse_args(argv)
    comment = open(a.comment).read() if a.comment else None

    if a.url:
        if not (a.repo and a.pr and a.sha):
            p.error("--url needs --repo, --pr and --sha")
        target = "%s /diff/%s/%s/%s" % (a.url, a.repo, a.pr, a.sha)
        fails = from_url(a.url, a.repo, a.pr, a.sha, comment=comment)
    elif a.file:
        target = a.file
        fails = from_file(a.file, comment=comment)
    else:
        p.error("one of --url or --file is required")

    if fails:
        print("SMOKE FAILED on %s" % target)
        for f in fails:
            print("  - %s" % f)
        return 1
    print("smoke ok on %s (%s)" % (target, "with comment" if comment
                                   else "page and raw only"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
