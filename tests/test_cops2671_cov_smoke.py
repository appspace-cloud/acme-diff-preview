"""COPS-2671: the parts of the post-deploy smoke checker nothing ever ran.

tests/test_cops2626_smoke.py covers the five checks in the middle of
src/smoke.py very well. It stops at the two places the checker gives up on
a page, and it never touches the half of the module that actually runs in
a pod. Those were the dark lines:

  * check_page's early exit (`no navigation index on the page`). A page with
    no applications renders no index at all -- _render_index returns "" for
    an empty outline -- so this is not a hypothetical: it is what the
    checker sees the first time it is pointed at a no-op PR. The line
    RETURNS rather than appends, which matters: without an index there are
    no index links to resolve, and running the anchor loop anyway would
    print nothing useful. Nothing proved that the early exit still carries
    the disclaimer failures found before it.

  * `index does not state its own counts`. The counts line is the only
    thing check 3 has to compare against, so an index that lost its summary
    text must be reported, and -- unlike the missing index -- the anchor
    checks must still run, because the links are all still there.

  * the whole CLI: _fetch, from_url, from_file and main. This is the code
    the ticket was written for -- "run it in the pod after Ready" -- and it
    was the only code in the module no test had ever executed. A smoke
    checker whose own runner is untested is a checker that can report
    "smoke ok" for reasons that have nothing to do with the page.

The CLI tests drive the real functions and mock only the transport
(urllib.request.urlopen) and the filesystem (tmp_path), so URL
construction, utf-8 decoding, the timeout, argument validation, the exit
codes and every printed line are asserted as behaviour rather than
stubbed away.
"""
import json
import os
import sys
import urllib.error
import urllib.request

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import pytest  # noqa: E402

import diff_ui  # noqa: E402
import smoke  # noqa: E402


HEADER = ("## \U0001f52d ACME Diff Preview\n\n"
          "**Commit** `abc12345` → `main` | `acme-config-prod`\n\n")

# One application, one resource: the smallest body that still renders an
# index, so the count line and the anchors both exist.
GOOD = (HEADER +
        "⚠️ **`pv-alpha-a-ms`** — 1 resource(s) changed\n\n"
        "**`/apps/Deployment apigateway`**\n\n"
        "```diff\n@@ -1,1 +1,1 @@\n-  replicas: 1\n+  replicas: 2\n```\n")

# No application sections at all -> _render_index emits nothing.
NO_APPS = HEADER + "No application changes were detected in this run.\n"

# No applications AND the page speaking about itself: the 2.32.0 bug on a
# page that also has nowhere to navigate to.
NO_APPS_DISCLAIMING = (
    HEADER + "The full-diff page could not be produced for this run\n")

APP_ANCHOR = "app-pv-alpha-a-ms"


def _art(body, repo="acme-config-prod", pr=4019, sha="abc12345"):
    art = {"repo": repo, "pr_id": pr, "sha": sha,
           "created_utc": "2026-08-08T00:00:00Z",
           "pr_url": "https://bb/pr/%d" % pr}
    if body is not None:
        art["body"] = body
    return art


def _page(body):
    return diff_ui.render_html(_art(body))


def _write_artifact(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(json.dumps(_art(body)), encoding="utf-8")
    return str(path)


# ── a fake pod ───────────────────────────────────────────────────────────
# urlopen is the only seam mocked: _fetch, its context manager, the decode
# and the url building all run for real.

class _FakeResponse:
    def __init__(self, text, log):
        self._text = text
        self._log = log

    def read(self):
        return self._text.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._log.append("closed")
        return False


class _FakePod:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []          # (url, timeout) in order
        self.closes = []

    def __call__(self, url, timeout=None):
        self.calls.append((url, timeout))
        if url not in self.routes:
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        return _FakeResponse(self.routes[url], self.closes)

    @property
    def urls(self):
        return [u for u, _ in self.calls]


def _serve(monkeypatch, routes):
    pod = _FakePod(routes)
    monkeypatch.setattr(urllib.request, "urlopen", pod)
    return pod


# ── check_page: the page has no navigation index at all (line 90-91) ─────

def test_a_page_with_no_applications_is_reported_as_having_no_index():
    """Not a synthetic case: a PR that changes nothing renders an outline
    of zero applications, and _render_index emits "" for that."""
    page = _page(NO_APPS)
    assert 'class="toc"' not in page, "fixture must render no index"
    assert smoke.check_page(page, NO_APPS) == [
        "no navigation index on the page"]


def test_the_missing_index_report_keeps_the_disclaimer_it_already_found():
    """The early exit returns `fails + [...]`, not `[...]`. If it dropped
    the accumulated list, the worst page of all -- one that disclaims
    itself AND cannot be navigated -- would report only the milder half."""
    page = _page(NO_APPS_DISCLAIMING)
    fails = smoke.check_page(page, NO_APPS_DISCLAIMING)
    assert len(fails) == 2, fails
    assert "could not be produced" in fails[0]
    assert fails[1] == "no navigation index on the page"


def test_no_index_means_no_anchor_checks_are_attempted():
    """It is a `return`, and it has to be. This page still carries index
    links whose targets were renamed away; with the index container gone
    the checker must say the one true thing ("there is no index") instead
    of a pile of unresolved-link noise about a list nobody can see."""
    page = _page(GOOD)
    assert 'id="%s"' % APP_ANCHOR in page
    broken = (page.replace('<details class="toc"', '<details class="nav"', 1)
                  .replace('id="%s"' % APP_ANCHOR, 'id="app-renamed"', 1))
    assert 'href="#%s"' % APP_ANCHOR in broken, (
        "fixture must keep the index links so the anchor loop would fire")
    assert smoke.check_page(broken, GOOD) == [
        "no navigation index on the page"]


# ── check_page: an index that no longer states its counts (line 95-96) ───

def test_an_index_that_states_no_counts_is_reported():
    """Check 3 compares the stated count with the entries. A summary that
    stopped stating one silently disables that comparison, so the missing
    line is itself a failure rather than a pass."""
    page = _page(GOOD).replace("Index: 1 application(s), 1 resource(s)",
                               "Contents", 1)
    assert 'class="toc"' in page and "Index:" not in page
    fails = smoke.check_page(page, GOOD)
    assert fails == ["index does not state its own counts"]


def test_a_missing_counts_line_does_not_stop_the_anchor_checks():
    """Unlike the missing index this one appends and carries on: every
    index link is still on the page, so every one of them is still worth
    resolving."""
    page = (_page(GOOD)
            .replace("Index: 1 application(s), 1 resource(s)", "Contents", 1)
            .replace('id="%s"' % APP_ANCHOR, 'id="app-renamed"', 1))
    fails = smoke.check_page(page, GOOD)
    assert "index does not state its own counts" in fails
    assert any(APP_ANCHOR in f and "0 element(s)" in f for f in fails), fails


# ── _fetch: the transport (lines 171-173) ────────────────────────────────

def test_fetch_returns_decoded_text_and_closes_the_response(monkeypatch):
    """The page carries the warning sign and the arrow in the header; a
    fetch that returned bytes, or decoded as latin-1, would compare against
    every check as a different string."""
    pod = _serve(monkeypatch, {"http://pod:8080/x": "⚠️ café"})
    got = smoke._fetch("http://pod:8080/x")
    assert got == "⚠️ café"
    assert pod.closes == ["closed"], "the response must be context-managed"


def test_fetch_bounds_the_wait_on_a_wedged_pod(monkeypatch):
    """A smoke check that hangs forever is a deploy that hangs forever:
    urlopen without a timeout inherits the global default, which is None."""
    pod = _serve(monkeypatch, {"http://pod:8080/x": "hi",
                               "http://pod:8080/y": "hi"})
    smoke._fetch("http://pod:8080/x")
    smoke._fetch("http://pod:8080/y", timeout=5)
    assert [t for _, t in pod.calls] == [30, 5]


# ── from_url: the mode the ticket asks for (lines 176-180) ───────────────

def _good_pod(monkeypatch, base="http://pod:8080"):
    root = base + "/diff/acme-config-prod/4019/abc12345"
    return _serve(monkeypatch, {root: _page(GOOD), root + "/raw": GOOD}), root


def test_from_url_reads_the_page_and_the_raw_of_one_artifact(monkeypatch):
    """The two urls are the contract with the running pod. A trailing slash
    on the base -- what you get from pasting a browser url -- must not
    produce //diff/, which the service 404s."""
    pod, root = _good_pod(monkeypatch)
    assert smoke.from_url("http://pod:8080/", "acme-config-prod", 4019,
                          "abc12345") == []
    assert pod.urls == [root, root + "/raw"]


def test_from_url_reports_a_pod_serving_a_self_disclaiming_page(monkeypatch):
    """The whole point of the url mode: the deployed pod, the stored
    artifact, the real render. This is the 2.32.0 page."""
    root = "http://pod:8080/diff/acme-config-prod/4019/abc12345"
    _serve(monkeypatch, {root: _page(NO_APPS_DISCLAIMING),
                         root + "/raw": NO_APPS_DISCLAIMING})
    fails = smoke.from_url("http://pod:8080", "acme-config-prod", 4019,
                           "abc12345")
    assert any("could not be produced" in f for f in fails), fails


def test_from_url_checks_the_comment_against_the_page_it_fetched(monkeypatch):
    """The comment is supplied by the caller, the page comes off the pod:
    the deep links have to be resolved against what the pod actually
    served, not against a locally rendered copy."""
    _good_pod(monkeypatch)
    resolvable = "[full output](https://x/diff/a/1/b#%s)" % APP_ANCHOR
    assert smoke.from_url("http://pod:8080", "acme-config-prod", 4019,
                          "abc12345", comment=resolvable) == []
    fails = smoke.from_url("http://pod:8080", "acme-config-prod", 4019,
                           "abc12345",
                           comment="[full output](https://x/d/1/s#app-gone)")
    assert any("app-gone" in f for f in fails), fails


def test_from_url_lets_an_unreachable_pod_raise_rather_than_pass(monkeypatch):
    """A pod that 404s the artifact must not come back as an empty failure
    list, which the CLI would print as "smoke ok"."""
    _serve(monkeypatch, {})           # nothing is served
    with pytest.raises(urllib.error.HTTPError):
        smoke.from_url("http://pod:8080", "acme-config-prod", 4019,
                       "abc12345")


# ── from_file: the CI first cut (lines 183-190) ──────────────────────────

def test_from_file_renders_the_artifact_it_was_given(tmp_path):
    """Two artifacts, opposite verdicts, same code path: the file on disk
    is what decides, so the render really happened."""
    clean = _write_artifact(tmp_path, "clean.json", GOOD)
    dirty = _write_artifact(tmp_path, "dirty.json", NO_APPS_DISCLAIMING)
    assert smoke.from_file(clean) == []
    fails = smoke.from_file(dirty)
    assert any("could not be produced" in f for f in fails), fails


def test_from_file_skips_the_raw_check_instead_of_faking_it(tmp_path,
                                                            monkeypatch):
    """from_file fetches nothing, so there is no /raw surface to compare
    against; feeding the stored body in as its own raw would make check 5
    pass for free and let the run claim it verified a surface it never saw.
    A tautological pass is invisible in the verdict -- [] either way -- so
    check_raw is replaced by a witness that shouts when it is reached.

    The from_url leg below is the control: it proves the witness IS wired
    into run() and would fire, so the silence in the from_file leg is the
    check being skipped rather than the seam being missed."""
    path = _write_artifact(tmp_path, "clean.json", GOOD)
    seen = []

    def _witness(raw, body):
        seen.append((raw, body))
        return ["check_raw ran against a /raw that was never fetched"]

    monkeypatch.setattr(smoke, "check_raw", _witness)

    assert smoke.from_file(path) == []
    assert seen == [], (
        "from_file must not run check 5 at all; it reached it with %r" % seen)

    # control: the mode that really does fetch /raw still runs the check.
    _good_pod(monkeypatch)
    assert smoke.from_url("http://pod:8080", "acme-config-prod", 4019,
                          "abc12345") == [
        "check_raw ran against a /raw that was never fetched"]
    assert seen == [(GOOD, GOOD)], seen


def test_from_file_checks_a_comment_against_the_rendered_page(tmp_path):
    path = _write_artifact(tmp_path, "clean.json", GOOD)
    fails = smoke.from_file(path, comment="summary with no link at all")
    assert any("no deep link" in f for f in fails), fails
    ok = "[full output](https://x/diff/a/1/b#%s)" % APP_ANCHOR
    assert smoke.from_file(path, comment=ok) == []


def test_from_file_tolerates_an_artifact_stored_without_a_body(tmp_path):
    """Older stored artifacts predate the body key. Reading one must report
    the empty page it renders, not die with a KeyError inside the checker
    -- a crashed smoke run tells the deployer nothing about the deploy."""
    path = tmp_path / "nobody.json"
    path.write_text(json.dumps(_art(None)), encoding="utf-8")
    assert smoke.from_file(str(path)) == ["no navigation index on the page"]


# ── main: exit codes and what it prints (lines 193-224) ──────────────────

def test_main_is_quiet_and_returns_zero_for_a_clean_artifact(tmp_path,
                                                             capsys):
    path = _write_artifact(tmp_path, "clean.json", GOOD)
    assert smoke.main(["--file", path]) == 0
    out = capsys.readouterr().out
    assert out.startswith("smoke ok on %s" % path), out
    assert "page and raw only" in out, (
        "no comment was supplied; the run must say which surfaces it saw")


def test_main_prints_every_failure_and_returns_one(tmp_path, capsys):
    """Exit 1 and print all of them: a deploy that broke two properties
    should say so once, and the deployer reads stdout, not a return value."""
    path = _write_artifact(tmp_path, "dirty.json", NO_APPS_DISCLAIMING)
    assert smoke.main(["--file", path]) == 1
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "SMOKE FAILED on %s" % path
    bullets = [ln for ln in out.splitlines() if ln.startswith("  - ")]
    assert len(bullets) == 2, out
    assert any("could not be produced" in b for b in bullets)
    assert any("no navigation index" in b for b in bullets)


def test_main_reads_the_comment_file_and_says_it_checked_it(tmp_path,
                                                            capsys):
    path = _write_artifact(tmp_path, "clean.json", GOOD)
    cpath = tmp_path / "comment.md"
    cpath.write_text("summary\n[full output](https://x/d/1/s#%s)\n"
                     % APP_ANCHOR, encoding="utf-8")
    assert smoke.main(["--file", path, "--comment", str(cpath)]) == 0
    out = capsys.readouterr().out
    assert "with comment" in out, out
    assert "page and raw only" not in out


def test_main_fails_when_the_comment_file_contradicts_the_page(tmp_path,
                                                               capsys):
    """--comment is not decoration: the contents of that file have to reach
    check_comment, so a comment that grew a fenced diff back fails the
    run."""
    path = _write_artifact(tmp_path, "clean.json", GOOD)
    cpath = tmp_path / "comment.md"
    cpath.write_text("```diff\n-a\n+b\n```\n[o](https://x/d/1/s#%s)\n"
                     % APP_ANCHOR, encoding="utf-8")
    assert smoke.main(["--file", path, "--comment", str(cpath)]) == 1
    assert "fenced diff" in capsys.readouterr().out


def test_main_url_mode_names_the_pod_and_the_artifact_it_checked(monkeypatch,
                                                                 capsys):
    """The printed target is the only record of WHAT was smoked; a green
    run against the wrong pod is the failure mode this line prevents."""
    _good_pod(monkeypatch)
    assert smoke.main(["--url", "http://pod:8080", "--repo",
                       "acme-config-prod", "--pr", "4019",
                       "--sha", "abc12345"]) == 0
    out = capsys.readouterr().out
    assert "http://pod:8080 /diff/acme-config-prod/4019/abc12345" in out, out


def test_main_url_mode_returns_one_when_the_pod_serves_a_broken_page(
        monkeypatch, capsys):
    root = "http://pod:8080/diff/acme-config-prod/4019/abc12345"
    _serve(monkeypatch, {root: _page(NO_APPS_DISCLAIMING),
                         root + "/raw": NO_APPS_DISCLAIMING})
    assert smoke.main(["--url", "http://pod:8080", "--repo",
                       "acme-config-prod", "--pr", "4019",
                       "--sha", "abc12345"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("SMOKE FAILED on http://pod:8080 /diff/")


def test_main_refuses_a_url_without_repo_pr_and_sha(monkeypatch, capsys):
    """Without all three the root url would be built out of the string
    "None" and 404, which reads like a broken deploy instead of a broken
    invocation."""
    pod = _serve(monkeypatch, {})
    with pytest.raises(SystemExit) as e:
        smoke.main(["--url", "http://pod:8080", "--repo", "acme-config-prod",
                    "--pr", "4019"])
    assert e.value.code == 2
    assert "--url needs --repo, --pr and --sha" in capsys.readouterr().err
    assert pod.calls == [], "it must refuse before touching the pod"


def test_main_refuses_to_run_with_neither_url_nor_file(capsys):
    """No target means nothing was checked. Exiting 0 here would let a
    misconfigured pipeline print success forever."""
    with pytest.raises(SystemExit) as e:
        smoke.main([])
    assert e.value.code == 2
    assert "one of --url or --file is required" in capsys.readouterr().err
