"""An unreadable value file must fail the render, never render as absent (COPS-2668).

This is the finding that publishes a *confidently wrong diff*, which the
codebase itself names as the worst thing it can do.

`_fetch_value_files` had two paths that turned "I could not read this file"
into "this file does not exist":

1. The singleflight waiter. A thread that joins an in-flight fetch waits 30s
   and then returns whatever is in the cache — `None` on timeout. The shared
   Bitbucket 429 pause is up to 60s BY DESIGN, so an ordinary rate limit puts
   every waiter past that timeout at once.

2. The fetcher's own BB_ERROR branch (429/5xx after retries). It appended the
   path to a local `unreadable` list that fed one WARNING log and nothing else,
   then returned the empty content anyway. The log's own text admits the
   consequence: "the render will look like a missing required value".

Both then flow into `helm template` as an absent file. Two outcomes, both bad:
the chart has a `required` on it and the PR gets a permanent, author-blaming
"missing required value"; or it does not, and helm happily renders a DIFFERENT
manifest, which is diffed and published as fact. A value file that failed to
download is indistinguishable, in the output, from one the author deleted.

The fix is fail-closed: no definitive answer for a requested file means the
render does not happen. `ValueFileUnreadable` is transient, so the PR is
retried rather than blamed.

Absence must keep working, though — a 404 is completely normal (a new cluster
not yet merged to main), and turning those into failures would break every
new-environment PR. That is what the last tests here hold.
"""
import os
import sys
import threading

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
import diff_preview as m


@pytest.fixture(autouse=True)
def _clean_vf_state():
    with m._vf_cache_lock:
        m._vf_cache.clear()
        m._vf_inflight.clear()
    yield
    with m._vf_cache_lock:
        m._vf_cache.clear()
        m._vf_inflight.clear()


SHA = "deadbeefcafe0123"


# ── 1. the fetcher's own transport failure ───────────────────────────────

def test_bb_error_raises_instead_of_rendering_as_absent(monkeypatch):
    """A 429/5xx that outlived its retries must not reach helm as absence."""
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda path, sha: (None, m.BB_ERROR))
    with pytest.raises(m.ValueFileUnreadable):
        m._fetch_value_files(["$config/env/values.yaml"], SHA)


def test_bb_error_names_the_unreadable_path(monkeypatch):
    """The operator has to be able to tell which file, or the message is noise."""
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda path, sha: (None, m.BB_ERROR))
    with pytest.raises(m.ValueFileUnreadable) as ei:
        m._fetch_value_files(["$config/env/customer.yaml"], SHA)
    assert "customer.yaml" in str(ei.value)


def test_one_unreadable_among_good_files_still_fails(monkeypatch):
    """Partial success is the dangerous case: the render would silently use a
    different value set."""
    def _fetch(path, sha):
        if "broken" in path:
            return None, m.BB_ERROR
        return "key: value\n", m.BB_OK
    monkeypatch.setattr(m, "_bb_fetch_status", _fetch)
    with pytest.raises(m.ValueFileUnreadable):
        m._fetch_value_files(["$config/a/ok.yaml", "$config/a/broken.yaml"], SHA)


# ── 2. the singleflight waiter ───────────────────────────────────────────

def test_singleflight_waiter_timeout_is_not_absence(monkeypatch):
    """A waiter that gives up on a slow fetcher must not report the file gone.

    Simulated by pre-seeding the in-flight map with an Event nobody sets, and
    shrinking the wait so the test does not take 30s.
    """
    key = (SHA, "env/values.yaml")
    with m._vf_cache_lock:
        m._vf_inflight[key] = threading.Event()   # never set
    monkeypatch.setattr(m, "VF_SINGLEFLIGHT_WAIT", 0.05, raising=False)
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda p, s: pytest.fail("waiter must not fetch"))

    with pytest.raises(m.ValueFileUnreadable):
        m._fetch_value_files(["$config/env/values.yaml"], SHA)


# ── 3. genuine absence must keep working ─────────────────────────────────

def test_not_found_is_still_plain_absence(monkeypatch):
    """A 404 is normal (new cluster not yet on main) and must not raise."""
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda path, sha: (None, m.BB_NOT_FOUND))
    out = m._fetch_value_files(["$config/env/values.yaml"], SHA)
    assert out == {}, "an absent file is simply not in the result"


def test_mixed_present_and_absent_is_fine(monkeypatch):
    def _fetch(path, sha):
        if "missing" in path:
            return None, m.BB_NOT_FOUND
        return "key: value\n", m.BB_OK
    monkeypatch.setattr(m, "_bb_fetch_status", _fetch)
    out = m._fetch_value_files(
        ["$config/a/present.yaml", "$config/a/missing.yaml"], SHA)
    assert list(out) == ["$config/a/present.yaml"]


def test_all_present_returns_everything(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda path, sha: ("key: value\n", m.BB_OK))
    out = m._fetch_value_files(["$config/a/one.yaml", "$config/a/two.yaml"], SHA)
    assert len(out) == 2


# ── 4. the failure must be retryable, not the author's fault ─────────────

def test_unreadable_is_classified_transient():
    assert m._is_transient_exception(m.ValueFileUnreadable("x")), (
        "a value file we could not download is an outage, not a broken PR")


# ── 5. one dict, one lock ────────────────────────────────────────────────

def test_inflight_dict_has_a_single_guard():
    """`_vf_inflight` was inserted into under `_vf_cache_lock` and popped under
    `_vf_inflight_lock` — two locks for one dict, so the check-and-insert was
    not actually atomic against the removal."""
    import ast
    import inspect
    import textwrap

    used = set()
    for fn in (m._fetch_value_files, m._bb_fetch_cached):
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        for node in ast.walk(tree):
            if isinstance(node, ast.With):
                for item in node.items:
                    ctx = item.context_expr
                    if isinstance(ctx, ast.Name) and ctx.id.startswith("_vf_"):
                        used.add(ctx.id)

    assert used == {"_vf_cache_lock"}, (
        "_vf_inflight and _vf_cache must be guarded by exactly one lock, or "
        "the check-and-insert is not atomic against the pop; locks in use: %r"
        % sorted(used))
