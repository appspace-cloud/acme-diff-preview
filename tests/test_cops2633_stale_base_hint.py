"""A stale COPS-2617 base hint must not abort anything (COPS-2633).

COPS-2617 aborts a render early when `main` moves under it: a merge fires
`pullrequest:fulfilled`, the merge commit is remembered per (repo, branch),
and a PR whose snapshot does not match it is skipped and re-rendered next
pass. The hint is peeked, never popped, and nothing else ever clears it.

That makes plain inequality the wrong test. It cannot tell

    the hint is NEWER than my snapshot   -> genuine supersede, abort
    my snapshot is NEWER than the hint   -> the hint is stale, proceed

apart, and the second case is permanent rather than rare: the config repos
take direct pushes to `main` from release automation, and a direct push
fires no `pullrequest:fulfilled` event. So `main` advances past the last
merge commit and the hint is never corrected again.

Measured on acme-config-stage PR #2802 (2026-08-10). The webhook arrived
2s after the PR was created and the loop woke immediately, then the render
was skipped three times -- `base branch advanced before render started
(96145380 -> bb12eea8)` -- until the livelock guard gave up 3m20s later.
`96145380` was the real tip of main that same iteration; `bb12eea8` was the
older merge commit of PR #2801, an ancestor of it. Every PR on every config
repo pays that penalty for as long as the hint stays stale.

The fix is ordering, not ancestry: `base_sha` is the tip as polled at the
start of the iteration, so a hint only matters if it was recorded AFTER
that poll. No extra Bitbucket calls, and it does not care whether the merge
was a squash, a merge commit, a direct push, or a webhook that never
arrived.
"""
import os
import sys

import pytest

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m

REPO = "acme-config-prod"
BASE = "main"

# Shaped like the real incident: MERGED is the merge commit of an unrelated
# PR, TIP is where an automation push then left main. MERGED is an ancestor
# of TIP, so "they differ" must NOT mean "main moved ahead of me".
MERGED = "bb12eea854a3"
TIP = "96145380874c"
NEXT_TIP = "aa11bb22cc33"


def _clear():
    with m._supersede_lock:
        m._pr_superseded.clear()
        m._pr_supersede_aborts.clear()
        m._base_superseded.clear()
        m._base_observed.clear()


@pytest.fixture(autouse=True)
def _clean_state():
    _clear()
    yield
    _clear()


# --- 1. the incident, reduced ---------------------------------------------

def test_a_hint_older_than_the_observed_tip_does_not_abort():
    """PR #2802 exactly: hint from an earlier merge, then main advances by
    a direct push, then the poller reads the new tip. The PR's snapshot IS
    that tip, so there is nothing to wait for."""
    m._record_base_hint(REPO, BASE, MERGED)
    m._note_base_observed(REPO, BASE, TIP)
    assert m._base_superseded_by(REPO, BASE, TIP) is None


def test_the_stale_hint_is_dropped_not_merely_ignored():
    """It poisoned every PR in the repo because it was kept forever. One
    observation must retire it, so later PRs never consult it again."""
    m._record_base_hint(REPO, BASE, MERGED)
    m._note_base_observed(REPO, BASE, TIP)
    assert (REPO, BASE) not in m._base_superseded


def test_no_pr_pays_the_three_iteration_penalty():
    """The user-visible cost: three skipped iterations before the first
    comment. Every pass after the observation must be allowed to render."""
    m._record_base_hint(REPO, BASE, MERGED)
    m._note_base_observed(REPO, BASE, TIP)
    sk = (REPO, 2802)
    for _ in range(m.SUPERSEDE_MAX_CONSECUTIVE_ABORTS + 1):
        assert m._base_superseded_by(REPO, BASE, TIP, sk=sk) is None
    with m._supersede_lock:
        assert sk not in m._pr_supersede_aborts, \
            "a stale hint must not burn the livelock budget either"


# --- 2. what COPS-2617 was for still works -------------------------------

def test_a_merge_after_the_poll_still_aborts():
    """The whole point of COPS-2617: the poller read the tip, then a merge
    landed mid-iteration. That hint is newer than the snapshot and must
    still abort the render."""
    m._note_base_observed(REPO, BASE, TIP)
    m._record_base_hint(REPO, BASE, NEXT_TIP)
    assert m._base_superseded_by(REPO, BASE, TIP) == NEXT_TIP


def test_a_merge_after_the_poll_survives_the_next_observation_of_the_old_tip():
    """Bitbucket's refs read can lag a merge it already announced. The
    hint is only retired by an observation that came after it, never by
    one that came before."""
    m._note_base_observed(REPO, BASE, TIP)
    m._record_base_hint(REPO, BASE, NEXT_TIP)
    assert m._base_superseded_by(REPO, BASE, TIP) == NEXT_TIP
    assert (REPO, BASE) in m._base_superseded


def test_a_burst_of_merges_after_the_poll_still_coalesces():
    m._note_base_observed(REPO, BASE, TIP)
    for sha in (NEXT_TIP, "dd44ee55ff66", "ee55ff66aa77"):
        m._record_base_hint(REPO, BASE, sha)
    assert m._base_superseded_by(REPO, BASE, TIP) == "ee55ff66aa77"
    assert len(m._base_superseded) == 1


# --- 3. observations are scoped exactly like hints ------------------------

def test_an_observation_of_one_repo_does_not_retire_another_repos_hint():
    m._record_base_hint(REPO, BASE, MERGED)
    m._note_base_observed("acme-config-dev", BASE, TIP)
    assert m._base_superseded_by(REPO, BASE, TIP) == MERGED


def test_an_observation_of_one_branch_does_not_retire_another_branchs_hint():
    m._record_base_hint(REPO, BASE, MERGED)
    m._note_base_observed(REPO, "release/2603", TIP)
    assert m._base_superseded_by(REPO, BASE, TIP) == MERGED


# --- 4. a retired hint is retired for everyone ---------------------------

def test_a_retired_hint_cannot_abort_any_snapshot():
    """Once main is known to have moved past a hint, that hint is not
    evidence about anything any more. Aborting some other snapshot with it
    would report `TIP -> MERGED`, which is the bug's own backwards message.
    Callers really do all pass the polled tip, so nothing is lost."""
    m._record_base_hint(REPO, BASE, MERGED)
    m._note_base_observed(REPO, BASE, TIP)
    assert m._base_superseded_by(REPO, BASE, "0000dead0000") is None


def test_an_unobserved_branch_keeps_the_pre_fix_answer():
    """The change is scoped to what the poller has actually seen. With no
    observation at all the COPS-2617 behaviour is untouched."""
    m._record_base_hint(REPO, BASE, MERGED)
    assert m._base_superseded_by(REPO, BASE, TIP) == MERGED


def test_an_observation_matching_the_hint_is_not_a_supersede():
    """The ordinary merge flow: the hint is the new tip, the poller then
    reads that same tip, and the PR renders against it."""
    m._record_base_hint(REPO, BASE, NEXT_TIP)
    m._note_base_observed(REPO, BASE, NEXT_TIP)
    assert m._base_superseded_by(REPO, BASE, NEXT_TIP) is None


# --- 5. the failure mode becomes visible ---------------------------------

def test_dropping_a_stale_hint_is_counted():
    """This bug was silent for as long as it existed. Same reasoning as the
    COPS-2575/2576 webhook counters: if it happens again, it shows up on
    /diff-preview/stats instead of only in a log nobody reads."""
    before = m._bb_webhook_stats.get("base_hints_stale_dropped", 0)
    m._record_base_hint(REPO, BASE, MERGED)
    m._note_base_observed(REPO, BASE, TIP)
    assert m._bb_webhook_stats["base_hints_stale_dropped"] == before + 1


def test_an_ordinary_observation_counts_nothing():
    before = m._bb_webhook_stats.get("base_hints_stale_dropped", 0)
    m._note_base_observed(REPO, BASE, TIP)
    m._note_base_observed(REPO, BASE, NEXT_TIP)
    assert m._bb_webhook_stats["base_hints_stale_dropped"] == before


# --- 6. the switch and the neighbours ------------------------------------

def test_the_feature_switch_disables_observations_too(monkeypatch):
    monkeypatch.setattr(m, "SUPERSEDE_ABORT_ENABLED", False)
    m._note_base_observed(REPO, BASE, TIP)
    assert m._base_superseded_by(REPO, BASE, TIP) is None
    assert not m._base_observed


def test_an_observation_never_raises():
    """It runs on the poll path, which must never be broken by a hint
    bookkeeping problem."""
    for repo, branch, sha in ((None, BASE, TIP), (REPO, None, TIP),
                              (REPO, BASE, None), (REPO, BASE, "")):
        m._note_base_observed(repo, branch, sha)


def test_the_own_branch_supersede_path_is_untouched():
    sk = (REPO, 2802)
    m._record_supersede_hint(REPO, 2802, NEXT_TIP)
    m._note_base_observed(REPO, BASE, TIP)
    assert m._superseded(sk, TIP) == NEXT_TIP


# --- 7. end to end through the webhook and the poller --------------------

# The webhook path checks the slug against REPOS, which outside production
# holds only acme-config-dev.
WH_REPO = "acme-config-dev"


def _merge_payload(sha):
    import json
    return json.dumps({
        "repository": {"full_name": f"{m.BB_WORKSPACE}/{WH_REPO}"},
        "pullrequest": {
            "id": 2801,
            "source": {"commit": {"hash": "ffffffffffff"}},
            "destination": {"branch": {"name": BASE},
                            "commit": {"hash": MERGED}},
            "merge_commit": {"hash": sha},
        },
    }).encode()


def test_a_merge_then_an_automation_push_lets_the_next_pr_render(monkeypatch):
    """The production sequence end to end: PR #2801 merges, release
    automation pushes straight to main (no webhook), the poller reads the
    new tip, and PR #2802 must render on the first pass."""
    monkeypatch.setattr(m, "BB_WEBHOOK_SECRET", "s")
    m._maybe_record_supersede_hint("pullrequest:fulfilled", _merge_payload(MERGED))
    assert m._base_superseded_by(WH_REPO, BASE, MERGED) is None, \
        "sanity: the hint is the merge commit itself"

    m._note_base_observed(WH_REPO, BASE, TIP)   # automation push, then poll
    assert m._base_superseded_by(WH_REPO, BASE, TIP, sk=(WH_REPO, 2802)) is None


def test_the_poll_loop_reports_every_tip_it_reads(monkeypatch):
    """The fix only works if the poller actually publishes what it saw.
    Guard the wiring, not just the helper."""
    seen = []
    monkeypatch.setattr(m, "_note_base_observed",
                        lambda repo, branch, sha: seen.append((repo, branch, sha)))
    monkeypatch.setattr(m, "REPOS", {WH_REPO: {"scopes": []}})
    monkeypatch.setattr(m, "discover_path_app_map", lambda: {})
    monkeypatch.setattr(m, "mirror_sync", lambda repo: None)
    monkeypatch.setattr(m, "get_open_prs", lambda repo: [])
    monkeypatch.setattr(m, "http",
                        lambda *a, **k: {"target": {"hash": TIP}})
    m.main_iteration()
    assert (WH_REPO, BASE, TIP) in seen
