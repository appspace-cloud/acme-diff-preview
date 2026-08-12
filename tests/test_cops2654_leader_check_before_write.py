"""COPS-2654: a lease flip mid-iteration lets two pods write to the same PRs.

Leadership is checked exactly once, immediately before `main_iteration()`:

    while not _shutdown:
        if _should_run_iteration(_leader):
            main_iteration()

and nowhere inside it. The lease is `lease_duration=15`,
`renew_deadline=10`, so if renewals fail for more than 15 seconds the
standby legitimately takes over while the previous leader carries its
current iteration to completion. Both then post comments on the same PRs,
write the same artifact names, and spend the SHARED Bitbucket token that
COPS-2543 and COPS-2564 both exist because of - at exactly the moment the
cluster is already unhealthy, where 429-driven retries make it worse.

Measured exposure before writing any of this: `last_iteration_s = 6.9` in
steady state, so an ordinary cycle is a handful of seconds and a flip
landing inside it is unlikely. A fleet PR runs for minutes, and those are
the expensive iterations to duplicate. Low probability, bounded blast
radius, worth closing cheaply.

The guard sits at two chokepoints rather than at the five `upsert_comment`
call sites, because guarding five call sites is how one gets missed:

  * `process_pr` entry - stops PRs that have not started yet, which bounds
    the wasted API spend, not only the writes;
  * `upsert_comment` and `_save_diff_ui_artifact` - stops writes for PRs
    already in flight.

Deliberately NOT an iteration abort: that adds a partial-state path which
has to interact with the SIGTERM drain and the partial-batch safety check,
which is more risk than this problem justifies.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_preview as m  # noqa: E402


class _Elector:
    def __init__(self, leader=True):
        self._leader = leader

    def is_leader(self):
        return self._leader


def _posted(monkeypatch):
    """Record every Bitbucket write upsert_comment would make."""
    calls = []
    monkeypatch.setattr(m, "http",
                        lambda *a, **k: calls.append((a, k)) or {"id": 1})
    return calls


# -- the guard --------------------------------------------------------------

def test_a_lost_lease_stops_comment_writes(monkeypatch):
    """THE gate. The standby has taken over; this pod must stop writing."""
    monkeypatch.setattr(m, "_leader", _Elector(leader=False))
    calls = _posted(monkeypatch)
    m.upsert_comment(42, "# body", None, repo="acme-config-dev")
    assert not calls, (
        "a pod that no longer holds the lease posted a comment anyway")


def test_the_real_leader_still_writes_normally(monkeypatch):
    """The happy path must be untouched: no extra latency, no behaviour
    change, no API call for the check."""
    monkeypatch.setattr(m, "_leader", _Elector(leader=True))
    calls = _posted(monkeypatch)
    m.upsert_comment(42, "# body", None, repo="acme-config-dev")
    assert calls, "the leader must still post"


def test_single_instance_mode_is_unaffected(monkeypatch):
    """No elector wired means no HA: off-cluster runs and single-replica
    deployments must not be gated by a lease that does not exist."""
    monkeypatch.setattr(m, "_leader", None)
    calls = _posted(monkeypatch)
    m.upsert_comment(42, "# body", None, repo="acme-config-dev")
    assert calls, "single-instance mode must still post"


def test_a_lost_lease_stops_artifact_writes(monkeypatch):
    """The bucket is last-write-wins, so a demoted pod can overwrite the
    new leader's artifact with its own older render."""
    monkeypatch.setattr(m, "_leader", _Elector(leader=False))
    monkeypatch.setattr(m, "DIFF_UI_ENABLED", True)
    saved = []
    monkeypatch.setattr(m.diff_ui, "save_artifact",
                        lambda *a, **k: saved.append(a) or "/tmp/x")
    ok = m._save_diff_ui_artifact("acme-config-dev", 42, "abc1234", "# body")
    assert not saved, "a demoted pod wrote an artifact"
    assert ok is False, "the caller must see that no page was written"


def test_a_lost_lease_skips_pr_processing_entirely(monkeypatch):
    """Stopping at the write still pays for the diff. PRs that have not
    started yet should not be computed at all - that is where the shared
    token is actually spent."""
    monkeypatch.setattr(m, "_leader", _Elector(leader=False))
    fetched = []
    monkeypatch.setattr(m, "find_existing_comment",
                        lambda *a, **k: fetched.append(1) or (None, None, ""))
    pr = {"id": 42, "title": "t", "source": {"commit": {"hash": "a" * 12},
                                             "branch": {"name": "b"}},
          "destination": {"branch": {"name": "main"}}}
    m.process_pr(pr, {}, base_sha="b" * 12, repo="acme-config-dev")
    assert not fetched, (
        "a demoted pod started processing a PR instead of skipping it")


# -- the check must be cheap and safe ---------------------------------------

def test_the_check_makes_no_api_call(monkeypatch):
    """is_leader() reads cached elector state. If the guard ever starts
    hitting the API server it would add load during exactly the partition
    it exists to handle."""
    seen = []

    class Counting(_Elector):
        def is_leader(self):
            seen.append(1)
            return True

    monkeypatch.setattr(m, "_leader", Counting())
    monkeypatch.setattr(m, "http", lambda *a, **k: {"id": 1})
    m.upsert_comment(42, "# body", None, repo="acme-config-dev")
    assert len(seen) <= 2, f"the guard consulted the elector {len(seen)} times"


def test_an_elector_that_raises_does_not_break_the_write(monkeypatch):
    """Observability and safety checks must never be the thing that breaks
    the run. An elector in a bad state should fail OPEN here: the old
    behaviour (write anyway) is what we had before this ticket."""

    class Broken:
        def is_leader(self):
            raise RuntimeError("elector exploded")

    monkeypatch.setattr(m, "_leader", Broken())
    calls = _posted(monkeypatch)
    m.upsert_comment(42, "# body", None, repo="acme-config-dev")
    assert calls, (
        "a broken elector must not silently stop the service from posting")
