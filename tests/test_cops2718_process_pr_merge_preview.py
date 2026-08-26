"""process_pr renders the merge, and a conflict is said in red (COPS-2718).

The helper (`_merge_preview`) is proven against real git in
test_cops2718_merge_preview.py. These tests pin what process_pr DOES with
its three answers, through the same synthetic-world harness the other
orchestrator tests use:

  (sha, [])     -> every content read below happens at the merge preview,
                   while pr_sha stays the PR's identity (header, status);
  (None, [...]) -> red FAILED status + a comment that names the conflicted
                   files and renders NOTHING else — any diff would describe
                   a merge that will never happen;
  (None, None)  -> could not compute (mirror off, fork PR): yesterday's
                   behaviour, reads at the branch tip.

The dedup contract matters as much as the render: a conflict comment must
not be re-posted every iteration, and must refresh when either sha moves.
"""
import os
import sys

import pytest

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import diff_ui  # noqa: E402
import diff_preview as m  # noqa: E402
import logsink  # noqa: E402


REPO = "acme-config-dev"
ENV_DIR = "gcp/dev/private-cloud/ap1/custom/pv-mp2718-a"
CHANGED = f"{ENV_DIR}/cicd-versions.yaml"
APPS = ["pv-mp2718-a-ms"]
PATH_MAP = {CHANGED: list(APPS)}

PR_SHA = "aa11bb22cc33"
BASE_SHA = "ba5e00112233"
NEWER_BASE = "ba5e99887766"
MERGED_SHA = "3e963e963e96"


def _mk_pr(pr_id=2718, sha=PR_SHA):
    return {
        "id": pr_id,
        "title": "[COPS-2718] merge preview",
        "source": {"commit": {"hash": sha}, "branch": {"name": "feature/mp"}},
        "destination": {"branch": {"name": "main"}},
    }


def _reset_state():
    m._seen.clear()
    m._force_recompute.clear()
    with m._supersede_lock:
        m._pr_superseded.clear()
        m._pr_supersede_aborts.clear()
        m._base_superseded.clear()
        m._base_observed.clear()
    m._merge_preview_cache.clear()
    diff_ui.reset_pending_uploads()


@pytest.fixture()
def world(monkeypatch):
    """The synthetic single-PR world around the REAL process_pr, with the
    merge preview under test control."""
    class Sinks:
        def __init__(self):
            self.upserts, self.statuses, self.diff_shas = [], [], []
    sinks = Sinks()
    _reset_state()
    m._app_chart_map.update({a: "appspace-ms" for a in APPS})
    m._app_chart_revision_map.update({a: "2603.0.1-dev" for a in APPS})

    monkeypatch.setattr(logsink, "log", lambda *a, **k: None)
    monkeypatch.setattr(logsink, "debug", lambda *a, **k: None)
    monkeypatch.setattr(m, "get_pr_changed_files",
                        lambda pr_id, repo=None: ([CHANGED], {}))
    monkeypatch.setattr(m, "find_existing_comment",
                        lambda pr_id, repo=None: (None, "", ""))
    monkeypatch.setattr(m, "upsert_comment",
                        lambda pr_id, body, existing_id=None, repo=None,
                        artifact_url="", **kw: sinks.upserts.append(body) or 1)
    monkeypatch.setattr(m, "post_build_status",
                        lambda pr_sha, state, description, pr_id=None, repo=None:
                        sinks.statuses.append((state, description)))
    monkeypatch.setattr(m, "fix_stuck_inprogress", lambda *a, **k: None)
    monkeypatch.setattr(m, "_touch_progress", lambda: None)
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda path, sha, repo=None: (None, m.BB_NOT_FOUND))

    def fake_argocd_diff(app, pr_sha, main_sha, chart_revision=None,
                         changed_paths=None, renames=None):
        sinks.diff_shas.append(pr_sha)
        return m.DiffResult("", [], 0, False, "", m.OUT_NO_DIFF, "")
    monkeypatch.setattr(m, "argocd_diff", fake_argocd_diff)

    previews = {"answer": (None, None)}
    monkeypatch.setattr(m, "_merge_preview",
                        lambda repo, base, pr: previews["answer"])
    yield sinks, previews
    _reset_state()


# ── the conflict: red, named, nothing else ───────────────────────────────

def test_a_conflict_is_a_red_status_and_names_its_files(world):
    sinks, previews = world
    previews["answer"] = (None, ["gcp/config.yaml", f"{ENV_DIR}/customer.yaml"])
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA, repo=REPO)

    assert sinks.statuses and sinks.statuses[-1][0] == "FAILED"
    assert "CONFLICT" in sinks.statuses[-1][1]
    assert len(sinks.upserts) == 1
    body = sinks.upserts[0]
    assert "CONFLICTS with `main`" in body
    assert "gcp/config.yaml" in body and "customer.yaml" in body
    assert "[conflict]" in body, "the state token is what the next pass reads"
    assert f"[base:{BASE_SHA[:8]}]" in body


def test_a_conflict_renders_no_diff_at_all(world):
    """Any rendered diff would describe a merge that will never happen. The
    one unbreakable rule works both ways: never green on failure, and never
    a plausible-looking answer to the wrong question."""
    sinks, previews = world
    previews["answer"] = (None, ["gcp/config.yaml"])
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA, repo=REPO)
    assert sinks.diff_shas == [], "a conflicted PR must not render"


def test_a_conflict_comment_is_not_reposted_every_iteration(world):
    sinks, previews = world
    previews["answer"] = (None, ["gcp/config.yaml"])
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA, repo=REPO)
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA, repo=REPO)
    assert len(sinks.upserts) == 1, "same (pr, base): nothing new to say"


def test_a_conflict_refreshes_when_main_moves(world):
    """main moving can clear OR worsen a conflict; either way yesterday's
    comment no longer answers the question, so it must be recomputed."""
    sinks, previews = world
    previews["answer"] = (None, ["gcp/config.yaml"])
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA, repo=REPO)
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=NEWER_BASE, repo=REPO)
    assert len(sinks.upserts) == 2
    assert f"[base:{NEWER_BASE[:8]}]" in sinks.upserts[-1]


def test_a_resolved_conflict_goes_back_to_a_normal_review(world):
    """The author pushes the resolution: the same comment slot must return
    to a full review, not stay stuck on yesterday's conflict."""
    sinks, previews = world
    previews["answer"] = (None, ["gcp/config.yaml"])
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA, repo=REPO)
    previews["answer"] = (MERGED_SHA, [])
    m.process_pr(_mk_pr(sha="dd44ee55ff66"), PATH_MAP, base_sha=BASE_SHA, repo=REPO)
    assert len(sinks.upserts) == 2
    assert "CONFLICTS" not in sinks.upserts[-1]


# ── the clean merge: content at the preview, identity at the branch ──────

def test_content_reads_happen_at_the_merge_preview(world):
    sinks, previews = world
    previews["answer"] = (MERGED_SHA, [])
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA, repo=REPO)
    assert sinks.diff_shas == [MERGED_SHA], (
        f"the render read at {sinks.diff_shas}, not at the merge preview — "
        f"this is the stale-output bug: main's commits stay invisible")


def test_the_comment_identity_stays_the_real_pr_sha(world):
    """The header token is what the cross-pod dedup and the supersede check
    parse back out. Stamping the synthetic sha there would make every pod
    think the comment belongs to a commit Bitbucket has never heard of."""
    sinks, previews = world
    previews["answer"] = (MERGED_SHA, [])
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA, repo=REPO)
    assert sinks.upserts
    assert PR_SHA[:8] in sinks.upserts[-1]
    assert MERGED_SHA[:8] not in sinks.upserts[-1], (
        "the synthetic sha is plumbing, never identity")


def test_an_uncomputable_preview_degrades_to_the_branch_tip(world):
    """Fork PR, mirror off, pre-2.38 git: yesterday's behaviour, explicitly
    NOT the conflict path — a missing mirror is a fact about the mirror."""
    sinks, previews = world
    previews["answer"] = (None, None)
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA, repo=REPO)
    assert sinks.diff_shas == [PR_SHA]
    assert all(s[0] != "FAILED" for s in sinks.statuses), (
        "degraded is not conflicted; red here would block innocent PRs")
