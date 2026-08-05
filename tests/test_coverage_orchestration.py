"""Coverage campaign (post v2.5.15), pass D: the orchestration layer.

process_pr() is the heart of the service (~600 lines) and was almost
entirely uncovered: every prior campaign tested the pieces it calls, never
the conductor itself. This builds a synthetic PR world — canned changed
files, a scripted argocd_diff, recorded comment/build-status sinks — and
runs the REAL orchestrator end to end: dedup via _seen, batch submission,
outcome accounting, the traffic-light rule, and comment upserting.
main_iteration() and main() get the same treatment.
"""
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m  # noqa: E402


ENV_DIR   = "gcp/dev/private-cloud/ap1/custom/pv-orch-a"
IDENTITY  = f"{ENV_DIR}/customer.yaml"
ANCILLARY = f"{ENV_DIR}/cicd-versions.yaml"
PATH_MAP  = {
    IDENTITY:  ["pv-orch-a-ms", "pv-orch-a-ss"],
    ANCILLARY: ["pv-orch-a-ms", "pv-orch-a-ss"],
}
PR_SHA   = "aabbccddeeff"
BASE_SHA = "112233445566"


def _mk_pr(pr_id=991):
    return {
        "id": pr_id,
        "title": "[COPS-2498] synthetic orchestration PR",
        "source": {"commit": {"hash": PR_SHA}, "branch": {"name": "feature/synth"}},
        "destination": {"branch": {"name": "main"}},
    }


class Sinks:
    def __init__(self):
        self.upserts = []       # comment bodies
        self.statuses = []      # (state, description)
        self.diff_calls = []    # app names argocd_diff was asked for


@pytest.fixture()
def world(monkeypatch):
    """Synthetic PR world: real orchestrator, scripted I/O edges."""
    sinks = Sinks()

    # Reset the module state the orchestrator reads/writes.
    m._seen.clear()
    m._force_recompute.clear()
    m._main_render_cache.clear()
    stats_backup = dict(m._diff_stats)
    for k in m._diff_stats:
        m._diff_stats[k] = 0 if isinstance(m._diff_stats[k], int) else m._diff_stats[k]
    m._app_chart_map.update({"pv-orch-a-ms": "appspace-ms", "pv-orch-a-ss": "appspace-ss"})
    m._app_chart_revision_map.update({"pv-orch-a-ms": "2603.0.1-dev", "pv-orch-a-ss": "2603.0.1-dev"})

    # I/O edges.
    monkeypatch.setattr(m, "get_pr_changed_files", lambda pr_id, repo=None: ([ANCILLARY], {}))
    monkeypatch.setattr(m, "find_existing_comment", lambda pr_id, repo=None: (None, "", ""))
    monkeypatch.setattr(m, "upsert_comment",
                        lambda pr_id, body, existing_id=None, repo=None, **kw:
                        sinks.upserts.append(body) or 123)
    monkeypatch.setattr(m, "post_build_status",
                        lambda pr_sha, state, description, pr_id=None, repo=None:
                        sinks.statuses.append((state, description)))
    monkeypatch.setattr(m, "fix_stuck_inprogress", lambda *a, **k: None)
    monkeypatch.setattr(m, "_touch_progress", lambda: None)

    # Scripted diff results, keyed by app; default = clean no-diff.
    plan = {}

    def fake_argocd_diff(app, pr_sha, main_sha, chart_revision=None,
                         changed_paths=None, renames=None):
        sinks.diff_calls.append(app)
        return plan.get(app, m.DiffResult("", [], 0, False, "", m.OUT_NO_DIFF, ""))

    monkeypatch.setattr(m, "argocd_diff", fake_argocd_diff)

    yield sinks, plan
    m._diff_stats.update(stats_backup)
    m._seen.clear()
    m._force_recompute.clear()


# ── process_pr scenarios ─────────────────────────────────────────────────

def test_process_pr_happy_diff_posts_comment_and_successful_status(world):
    sinks, plan = world
    plan["pv-orch-a-ms"] = m.DiffResult(
        "--- main\n+++ pr", [("Deployment/webx", "-replicas: 2\n+replicas: 3")],
        1, True, "", m.OUT_DIFF, "")
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)

    assert sorted(set(sinks.diff_calls)) == ["pv-orch-a-ms", "pv-orch-a-ss"]
    assert len(sinks.upserts) == 1
    body = sinks.upserts[0]
    assert "pv-orch-a-ms" in body and "Deployment/webx" in body
    states = [s for s, _ in sinks.statuses]
    assert states[0] == "INPROGRESS" and states[-1] == "SUCCESSFUL", states


def test_process_pr_dedup_skips_unchanged_sha_on_second_run(world):
    sinks, plan = world
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    first_upserts, first_calls = len(sinks.upserts), len(sinks.diff_calls)
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    assert len(sinks.upserts) == first_upserts, "same sha must not recompute/re-post"
    assert len(sinks.diff_calls) == first_calls


def test_process_pr_force_recompute_bypasses_dedup_once(world):
    sinks, plan = world
    pr = _mk_pr()
    m.process_pr(pr, PATH_MAP, base_sha=BASE_SHA)
    first_calls = len(sinks.diff_calls)
    m._force_recompute.add(("acme-config-dev", pr["id"]))
    m.process_pr(pr, PATH_MAP, base_sha=BASE_SHA)
    assert len(sinks.diff_calls) > first_calls, "republish invalidation must recompute"
    assert ("acme-config-dev", pr["id"]) not in m._force_recompute, "the force flag is consumed"


def test_process_pr_indeterminate_render_failure_blocks_with_failed_status(world):
    # Traffic-light rule (v2.5.4/v2.5.5): ANY indeterminate result means the
    # diff was NOT actually computed, so the build status must be FAILED —
    # never a green SUCCESSFUL with a hidden "(1 unavailable)" note.
    sinks, plan = world
    plan["pv-orch-a-ms"] = m.DiffResult(
        "", [], 0, False, "helm template failed: bad values",
        m.OUT_INDETERMINATE, m.REASON_RENDER)
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    states = [s for s, _ in sinks.statuses]
    assert states[-1] == "FAILED", states
    assert len(sinks.upserts) == 1


def test_process_pr_all_no_diff_is_green(world):
    sinks, plan = world
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    states = [s for s, _ in sinks.statuses]
    assert states[-1] == "SUCCESSFUL", states
    assert len(sinks.upserts) == 1


def test_process_pr_downgrade_warning_is_visible_but_not_blocking(world):
    # Explicit decision (v2.5.8): a chart version downgrade stays a visible
    # warning with a SUCCESSFUL status, never a PR blocker.
    sinks, plan = world
    plan["pv-orch-a-ms"] = m.DiffResult(
        "--- main\n+++ pr", [("Application/pv-orch-a-ms", "-2603.0.1\n+2600.0.0")],
        1, True, "", m.OUT_DIFF, "",
        version_change=("2603.0.1-dev", "2600.0.0-dev"))
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    body = sinks.upserts[0]
    assert "downgrade" in body.lower(), body[:400]
    states = [s for s, _ in sinks.statuses]
    assert states[-1] == "SUCCESSFUL", states


def test_process_pr_no_affected_apps_posts_nothing_heavy(world):
    sinks, plan = world
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(m, "get_pr_changed_files",
                   lambda pr_id, repo=None: (["docs/README-only-change.md"], {}))
        m.process_pr(_mk_pr(pr_id=992), PATH_MAP, base_sha=BASE_SHA)
    assert sinks.diff_calls == [], "no affected apps -> no diff computation"


# ── main_iteration ───────────────────────────────────────────────────────

def _quiet_iteration_edges(monkeypatch, prs, recorded):
    monkeypatch.setattr(m, "argocd_login", lambda: None)
    monkeypatch.setattr(m, "discover_path_app_map", lambda: PATH_MAP)
    monkeypatch.setattr(m, "get_open_prs", lambda repo=None: prs)
    monkeypatch.setattr(m, "_prune_helm_cache", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(m, "_touch_progress", lambda: None)
    # main_iteration fetches main's head sha directly via http() to know the
    # base for every diff (and to invalidate the main-side render cache when
    # main moves) — serve it canned.
    monkeypatch.setattr(m, "http",
                        lambda method, url, **kw: {"target": {"hash": BASE_SHA}})

    def fake_process_pr(pr, path_map, base_sha="", repo=None):
        recorded.append(pr["id"])
        if pr.get("title") == "BOOM":
            raise RuntimeError("synthetic per-PR crash")

    monkeypatch.setattr(m, "process_pr", fake_process_pr)


def test_main_iteration_processes_every_open_pr(monkeypatch):
    recorded = []
    _quiet_iteration_edges(monkeypatch, [_mk_pr(1), _mk_pr(2)], recorded)
    m.main_iteration()
    assert sorted(recorded) == [1, 2]


def test_main_iteration_survives_a_single_pr_crash(monkeypatch):
    # One PR blowing up must never take down the loop for the other PRs.
    recorded = []
    boom = _mk_pr(7); boom["title"] = "BOOM"
    _quiet_iteration_edges(monkeypatch, [boom, _mk_pr(8)], recorded)
    m.main_iteration()  # must not raise
    assert 8 in recorded


# ── main ─────────────────────────────────────────────────────────────────

def test_main_runs_iterations_until_interrupted(monkeypatch):
    iterations = []
    monkeypatch.setattr(m, "_start_health_server", lambda port=8080: None)
    monkeypatch.setattr(m, "_start_heartbeat", lambda: None)
    monkeypatch.setattr(m, "argocd_login", lambda: None)
    monkeypatch.setattr(m, "_get_subtask_pool", lambda: None, raising=False)
    monkeypatch.setattr(m, "main_iteration", lambda: iterations.append(1))

    waits = {"n": 0}

    def fake_wait(timeout=None):
        waits["n"] += 1
        if waits["n"] >= 2:
            raise KeyboardInterrupt
        return False

    monkeypatch.setattr(m._wake, "wait", fake_wait)
    with pytest.raises(KeyboardInterrupt):
        m.main()
    assert len(iterations) >= 2
