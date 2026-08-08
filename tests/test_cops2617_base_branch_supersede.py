"""Supersede-abort must cover the destination branch too (COPS-2617).

COPS-2575 solved this for a PR's OWN branch: a webhook hint aborts an
in-flight render as soon as a newer commit lands on the source branch,
instead of letting it run for minutes and publish a dead result. The hint
was only ever armed for the PR's own push.

When `main` advances because a DIFFERENT PR merged, every open PR is stale,
and today that is only noticed AFTER a render finishes, by comparing the
`[base:xxxx]` token in the already-published comment. That is a re-render,
not an early abort.

Ground truth from the ticket (acme-config-prod, four merges in ~8 minutes):
of 6 render passes across PRs #3922 and #3923, **4 were rendered against a
base_sha already superseded** before or within seconds of completion.
#3922's comment was rewritten 3 times in 8 minutes purely from unrelated
merges. On dev, two long-lived PRs logged ~22 recompute events each over
3 hours.

The fix reuses the COPS-2575 machinery rather than growing a second one:
same lock, same livelock guard, same "pop, do not blindly clear" discipline.
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
OLD = "cb4a638a1111"
NEW = "7d77df1d2222"
NEWER = "aa00bb11cc22"


@pytest.fixture(autouse=True)
def _clean_state():
    with m._supersede_lock:
        m._pr_superseded.clear()
        m._pr_supersede_aborts.clear()
        getattr(m, "_base_superseded", {}).clear()
    yield
    with m._supersede_lock:
        m._pr_superseded.clear()
        m._pr_supersede_aborts.clear()
        getattr(m, "_base_superseded", {}).clear()


# --- 1. the hint exists at all --------------------------------------------

def test_a_destination_branch_hint_is_recorded_and_read():
    m._record_base_hint(REPO, BASE, NEW)
    assert m._base_superseded_by(REPO, BASE, OLD) == NEW


def test_the_same_sha_is_not_a_supersede():
    m._record_base_hint(REPO, BASE, OLD)
    assert m._base_superseded_by(REPO, BASE, OLD) is None


def test_short_and_long_forms_of_one_sha_are_the_same_sha():
    """Bitbucket sends 12-char hashes in some payloads and 40 in others.
    Comparing them raw would report a supersede on every single poll."""
    m._record_base_hint(REPO, BASE, OLD + "3333333333333333333333333333")
    assert m._base_superseded_by(REPO, BASE, OLD) is None


# --- 2. a burst of merges costs one extra pass, not N ---------------------

def test_a_burst_of_merges_coalesces_to_one_hint():
    """The measured failure: four merges in eight minutes produced four
    recomputes of a 564-app PR. Most recent sha wins; the burst is one
    hint, not one per merge."""
    for sha in (NEW, NEWER, "dd33ee44ff55"):
        m._record_base_hint(REPO, BASE, sha)
    assert m._base_superseded_by(REPO, BASE, OLD) == "dd33ee44ff55"
    assert len(m._base_superseded) == 1


# --- 3. it is scoped, not global ------------------------------------------

def test_a_hint_for_one_repo_does_not_abort_another():
    m._record_base_hint(REPO, BASE, NEW)
    assert m._base_superseded_by("acme-config-dev", BASE, OLD) is None


def test_a_hint_for_one_branch_does_not_abort_another():
    m._record_base_hint(REPO, BASE, NEW)
    assert m._base_superseded_by(REPO, "release/2603", OLD) is None


# --- 4. the livelock guard is shared, not reinvented ----------------------

def test_a_continuously_advancing_base_still_lets_a_pr_publish():
    """A merge train must not starve a PR out of ever getting a comment.
    Same guard COPS-2575 already applies to the PR's own commits."""
    sk = (REPO, 3922)
    m._record_base_hint(REPO, BASE, NEW)
    with m._supersede_lock:
        m._pr_supersede_aborts[sk] = m.SUPERSEDE_MAX_CONSECUTIVE_ABORTS
    assert m._base_superseded_by(REPO, BASE, OLD, sk=sk) is None, \
        "past the abort ceiling the run must be allowed to finish"


def test_below_the_ceiling_it_still_aborts():
    sk = (REPO, 3922)
    m._record_base_hint(REPO, BASE, NEW)
    with m._supersede_lock:
        m._pr_supersede_aborts[sk] = m.SUPERSEDE_MAX_CONSECUTIVE_ABORTS - 1
    assert m._base_superseded_by(REPO, BASE, OLD, sk=sk) == NEW


# --- 5. the master switch still governs everything ------------------------

def test_the_feature_switch_disables_the_new_path_too(monkeypatch):
    monkeypatch.setattr(m, "SUPERSEDE_ABORT_ENABLED", False)
    m._record_base_hint(REPO, BASE, NEW)
    assert m._base_superseded_by(REPO, BASE, OLD) is None


# --- 6. the webhook wiring ------------------------------------------------

# The webhook path checks the slug against REPOS, which outside production
# holds only acme-config-dev. Use a repo that is really configured, or the
# test would pass for the wrong reason (rejected as unknown, not recorded).
WH_REPO = "acme-config-dev"


def _merge_payload(sha):
    import json
    return json.dumps({
        "repository": {"full_name": f"{m.BB_WORKSPACE}/{WH_REPO}"},
        "pullrequest": {
            "id": 4242,
            "source": {"commit": {"hash": "ffffffffffff"}},
            "destination": {"branch": {"name": BASE},
                            "commit": {"hash": OLD}},
            "merge_commit": {"hash": sha},
        },
    }).encode()


def test_a_merged_pr_records_a_destination_hint(monkeypatch):
    monkeypatch.setattr(m, "BB_WEBHOOK_SECRET", "s")
    m._maybe_record_supersede_hint("pullrequest:fulfilled", _merge_payload(NEW))
    assert m._base_superseded_by(WH_REPO, BASE, OLD) == NEW


def test_an_ordinary_pr_update_records_no_destination_hint(monkeypatch):
    """Only a MERGE advances the destination. A push to a PR branch must
    not make every other open PR think main moved."""
    monkeypatch.setattr(m, "BB_WEBHOOK_SECRET", "s")
    m._maybe_record_supersede_hint("pullrequest:updated", _merge_payload(NEW))
    assert m._base_superseded_by(WH_REPO, BASE, OLD) is None


def test_an_unauthenticated_webhook_records_nothing(monkeypatch):
    """Same reasoning as COPS-2575: an unauthenticated POST that can abort
    in-flight renders is a cheap denial of service."""
    monkeypatch.setattr(m, "BB_WEBHOOK_SECRET", "")
    m._maybe_record_supersede_hint("pullrequest:fulfilled", _merge_payload(NEW))
    assert m._base_superseded_by(WH_REPO, BASE, OLD) is None


def test_a_malformed_payload_never_raises(monkeypatch):
    monkeypatch.setattr(m, "BB_WEBHOOK_SECRET", "s")
    for bad in (b"", b"{", b"[]", b'{"repository":1}',
                b'{"repository":{"full_name":"x/y"}}'):
        m._maybe_record_supersede_hint("pullrequest:fulfilled", bad)
    assert m._base_superseded_by(WH_REPO, BASE, OLD) is None


# --- 7. the PR's own supersede path is untouched --------------------------

def test_the_own_branch_supersede_path_still_works():
    sk = (REPO, 3922)
    m._record_supersede_hint(REPO, 3922, NEW)
    assert m._superseded(sk, OLD) == NEW
    assert m._arm_supersede(sk, OLD) == NEW
