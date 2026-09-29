"""COPS-2766: a value file that cannot be read fails closed.

The wiped-definitions guard, the appspace-state panel and the VM panel
skipped a failed Bitbucket read (BB_ERROR) as if the file was absent. The
chart-revision resolution did the same with any exception. Each one can
turn a PR red, so a flaky read gave a green build for a dangerous PR.

Now a failed read raises ValueFileUnreadable and process_pr takes its
transient path: FAILED "Diff unavailable (infrastructure) - will retry",
no _seen, and the backoff retries the PR. A 404 is still an absent file,
and a bug in a panel (not a read) still costs only that panel.
"""
import io
import urllib.error

import pytest

import diff_preview as m

from test_coverage_orchestration import (  # noqa: E402,F401
    world, _mk_pr, PATH_MAP, BASE_SHA, PR_SHA, IDENTITY, IDENTITY_YAML, ANCILLARY)

SK = ("acme-config-dev", 991)
APPS = PATH_MAP[IDENTITY]


def _fetch(error=None, absent=None):
    """The world's reads, with BB_ERROR at the (path, sha) in `error` and a
    404 at the ones in `absent`."""
    def fake(path, sha, repo=None):
        if (path, sha) in (error or ()):
            return None, m.BB_ERROR
        if (path, sha) in (absent or ()) or not path.endswith(IDENTITY):
            return None, m.BB_NOT_FOUND
        return IDENTITY_YAML, m.BB_OK
    return fake


def _assert_retried(sinks):
    assert m._extract_status_token(sinks.upserts[-1]) == "transient"
    state, desc = sinks.statuses[-1]
    assert state == "FAILED"
    assert desc.startswith("Diff unavailable (infrastructure) - will retry"), desc
    assert SK not in m._seen and SK in m._retry_backoff


def _assert_normal_comment(sinks):
    assert m._extract_status_token(sinks.upserts[-1]) == "clean"
    assert sinks.statuses[-1][0] == "SUCCESSFUL", sinks.statuses
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA)


# ── the Bitbucket src URL ────────────────────────────────────────────────

@pytest.mark.parametrize("path,tail", [
    ("gcp/dev/pv x-a/customer.yaml", "gcp/dev/pv%20x-a/customer.yaml"),
    ("gcp/dev/pv-x_a.b/customer.yaml", "gcp/dev/pv-x_a.b/customer.yaml"),
])
def test_the_file_path_is_quoted_in_the_src_url(monkeypatch, path, tail):
    """An unquoted space made http.client raise InvalidURL, a BB_ERROR on
    every retry. Now that is a retry forever, so the path must be quoted.
    A normal path does not change."""
    urls = []

    def urlopen(req, timeout=None):
        urls.append(req.full_url)
        return io.BytesIO(b"appspace: {}\n")
    monkeypatch.setattr(m, "_git_read_file", lambda repo, sha, p: None)
    monkeypatch.setattr(m, "_pooled_urlopen", urlopen)
    assert m._bb_fetch_status(path, "abc123", repo="r") == ("appspace: {}\n", m.BB_OK)
    assert urls == [f"https://api.bitbucket.org/2.0/repositories/"
                    f"{m.BB_WORKSPACE}/r/src/abc123/{tail}"]


# ── the panel readers ────────────────────────────────────────────────────

def _state_panel():
    return m._summarize_appspace_state_changes([IDENTITY], PR_SHA, BASE_SHA,
                                               PATH_MAP, repo="r")


def _vm_panel():
    return m._summarize_vm_changes([IDENTITY], PR_SHA, BASE_SHA, PATH_MAP, {},
                                   repo="r")


@pytest.mark.parametrize("panel", [_state_panel, _vm_panel])
@pytest.mark.parametrize("sha", [PR_SHA, BASE_SHA])
def test_a_panel_raises_on_a_failed_read(monkeypatch, panel, sha):
    monkeypatch.setattr(m, "_bb_fetch_status", _fetch(error={(IDENTITY, sha)}))
    with pytest.raises(m.ValueFileUnreadable, match="pv-orch-a/customer.yaml"):
        panel()


@pytest.mark.parametrize("panel", [_state_panel, _vm_panel])
@pytest.mark.parametrize("sha", [PR_SHA, BASE_SHA])
def test_a_panel_still_skips_an_absent_file(monkeypatch, panel, sha):
    """A file on one side only is new-env or decommission territory."""
    monkeypatch.setattr(m, "_bb_fetch_status", _fetch(absent={(IDENTITY, sha)}))
    empty = [] if panel is _state_panel else m._vm_panel_lines([], set(), [], [])
    assert panel() == empty


# ── _resolve_effective_pr_chart_revision ─────────────────────────────────

# Built in the test, not at collection: a suite that reloads diff_preview
# would leave a stale ValueFileUnreadable class in the parameters.
@pytest.mark.parametrize("make_exc", [
    lambda: m.ValueFileUnreadable("value file unreadable: a/customer.yaml"),
    lambda: urllib.error.HTTPError("https://api.bitbucket.org/x", 503, "down", {}, None),
])
def test_effective_revision_reraises_a_failed_read(monkeypatch, make_exc):
    """None here means "no bump", so the render would use the old chart."""
    exc = make_exc()
    monkeypatch.setitem(m._app_value_files_map, "appx", ["$config/a/customer.yaml"])
    monkeypatch.setattr(m, "_fetch_value_files",
                        lambda files, sha: (_ for _ in ()).throw(exc))
    with pytest.raises(type(exc)):
        m._resolve_effective_pr_chart_revision("appx", "sha")


# ── process_pr ───────────────────────────────────────────────────────────

def test_a_failed_wiped_definitions_read_retries_until_it_heals(world, monkeypatch):
    sinks, _plan = world
    monkeypatch.setattr(m, "_bb_fetch_status", _fetch(error={(ANCILLARY, PR_SHA)}))
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    _assert_retried(sinks)
    assert sinks.diff_calls == [], "the guard runs before any render"
    assert ANCILLARY in sinks.statuses[-1][1]

    # Bitbucket is back and the backoff has run out: the normal pass
    # publishes and clears the backoff.
    monkeypatch.setattr(m, "_bb_fetch_status", _fetch())
    m._retry_backoff[SK][0] = 0
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    _assert_normal_comment(sinks)
    assert SK not in m._retry_backoff
    assert sinks.diff_calls


def test_a_state_panel_read_failure_retries_the_pr(world, monkeypatch):
    sinks, _plan = world
    monkeypatch.setattr(m, "_summarize_appspace_state_changes",
                        lambda *a, **k: (_ for _ in ()).throw(
                            m.ValueFileUnreadable("value file unreadable: x")))
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    _assert_retried(sinks)


def test_a_state_panel_bug_still_produces_the_normal_comment(world, monkeypatch):
    sinks, _plan = world
    monkeypatch.setattr(m, "_summarize_appspace_state_changes",
                        lambda *a, **k: (_ for _ in ()).throw(KeyError("panel bug")))
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    _assert_normal_comment(sinks)


def test_a_chart_revision_read_failure_retries_the_pr(world, monkeypatch):
    """End to end through the real _pr_chart_revision_checked: the PR edits
    customer.yaml, the chain read fails, and the PR must not render with
    the old chart."""
    sinks, _plan = world
    calls = []
    monkeypatch.setattr(m, "get_pr_changed_files",
                        lambda pr_id, repo=None: ([IDENTITY], {}))
    for app in APPS:
        monkeypatch.setitem(m._app_value_files_map, app, [f"$config/{IDENTITY}"])

    def unreadable(files, sha):
        calls.append(files)
        raise m.ValueFileUnreadable("chain unreadable at the PR sha")
    monkeypatch.setattr(m, "_fetch_value_files", unreadable)
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    assert calls, "the chain read must be reached"
    _assert_retried(sinks)
    assert "chain unreadable" in sinks.statuses[-1][1]
    assert sinks.diff_calls == [], "no render with the old chart"


def test_a_chart_revision_bug_still_renders(world, monkeypatch):
    sinks, _plan = world
    monkeypatch.setattr(m, "_pr_chart_revision_checked",
                        lambda *a, **k: (_ for _ in ()).throw(KeyError("bug")))
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    _assert_normal_comment(sinks)
    assert sinks.diff_calls
