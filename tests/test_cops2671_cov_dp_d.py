"""COPS-2671 (pass D): the orchestrator's damage-control branches.

Every line closed here belongs to a path that only runs when something has
already gone wrong, or when a rarely-true flag flips. That is exactly why
they were dark: the existing orchestration suites drive `process_pr` and
`main_iteration` down the happy path, where none of these fire.

What they are, and what breaks if they stop working:

  * COPS-2617 base-supersede at ENTRY (the `_base_superseded_by` branch).
    `main` moving under a snapshot is noticed before a single app is
    rendered. The existing COPS-2617/COPS-2633 tests all exercise the
    predicate `_base_superseded_by` directly; nobody drove `process_pr`
    into the abort it guards, so neither the livelock counter bump nor
    the "render nothing, mark nothing seen" contract was pinned.

  * the three "a side panel must never break the comment" guards. The
    input-changes and appspace-state guards are covered; the VM panel's
    was not, and the `paused_apps` (COPS-2655 autosync) guard was not
    either. Both exist so a Bitbucket hiccup in a decorative panel cannot
    cost the reviewer the whole comment.

  * COPS-2609 `fallback_inline`. The comment offers a link to the
    full-diff page BEFORE the page is written. When the write fails the
    comment has to be re-rendered without the URL, or every reviewer is
    sent to a 404. Nothing tested the re-render, so the fallback the
    later phases lean on was itself unverified.

  * COPS-2660 broken-arming build status. A PR that strips the Linux VM
    config while arming decommission diffs CLEANLY, so this is the only
    branch that can turn it red. The comment side is covered by
    test_cops2660_arming_strip; the Bitbucket build status was not.

  * COPS-2647 pending-upload reconcile in `main_iteration`, both the
    "it healed something" log and the "it blew up, keep going" guard.

  * the empty-discovery re-login (COPS-2668) when `argocd_login` itself
    raises -- the case where ArgoCD is down hard, not just RBAC-narrowed.

  * `_CLEAR_MAIN_RENDER_ON_TIP_MOVE`, the COPS-2631 escape hatch. The
    flag being False is pinned in two places; the behaviour it selects
    when someone turns it on never ran.
"""
import os
import sys

import pytest

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import comment_render  # noqa: E402
import diff_ui  # noqa: E402
import diff_preview as m  # noqa: E402
import logsink  # noqa: E402


REPO = "acme-config-dev"
ENV_DIR = "gcp/dev/private-cloud/ap1/custom/pv-cov671-a"
IDENTITY = f"{ENV_DIR}/customer.yaml"
ANCILLARY = f"{ENV_DIR}/cicd-versions.yaml"
APPS = ["pv-cov671-a-ms", "pv-cov671-a-ss"]
PATH_MAP = {IDENTITY: list(APPS), ANCILLARY: list(APPS)}

PR_SHA = "c0ffee110022"
BASE_SHA = "ba5e0011aa33"
NEWER_BASE = "9911eeff7744"

# The COPS-2660 shape, reduced: a live VM declared at base, and an arming
# commit that adds allowDeletion while deleting the role block the arming
# is supposed to act through.
LIVE_VM_YAML = """appspace:
  customerName: cov671
  infra:
    deployLinuxServicesK8s:
      enabled: true
      svc:
        enabled: true
        instances:
          - pv-cov671-svc-a
"""
STRIPPED_YAML = """appspace:
  customerName: cov671
  decommission: true
  infra:
    deployLinuxServicesK8s:
      defaults:
        allowDeletion: true
"""
# The runbook-correct arming commit: same intent, VM block kept intact.
ARMED_OK_YAML = """appspace:
  customerName: cov671
  decommission: true
  infra:
    deployLinuxServicesK8s:
      enabled: true
      defaults:
        allowDeletion: true
      svc:
        enabled: true
        instances:
          - pv-cov671-svc-a
"""


def _mk_pr(pr_id=671, sha=PR_SHA):
    return {
        "id": pr_id,
        "title": "[COPS-2671] coverage pass D",
        "source": {"commit": {"hash": sha}, "branch": {"name": "feature/cov671"}},
        "destination": {"branch": {"name": "main"}},
    }


class Sinks:
    def __init__(self):
        self.upserts = []     # (body, artifact_url)
        self.statuses = []    # (state, description)
        self.diff_calls = []  # apps argocd_diff was asked for
        self.logs = []        # (severity, message, labels)

    def events(self):
        return [lab.get("event") for _s, _m2, lab in self.logs if "event" in lab]

    def messages(self):
        return [msg for _s, msg, _l in self.logs]


def _reset_state():
    m._seen.clear()
    m._force_recompute.clear()
    with m._supersede_lock:
        m._pr_superseded.clear()
        m._pr_supersede_aborts.clear()
        m._base_superseded.clear()
        m._base_observed.clear()
    with m._main_render_lock:
        m._main_render_cache.clear()
    diff_ui.reset_pending_uploads()


@pytest.fixture(autouse=True)
def _restore_counters():
    """`_diff_stats` is process-global and several tests here move it on
    purpose (the fallback counter). Snapshot it so no later module inherits
    a number this file wrote."""
    snapshot = dict(m._diff_stats)
    yield
    m._diff_stats.update(snapshot)


@pytest.fixture()
def world(monkeypatch):
    """A synthetic single-PR world around the REAL process_pr."""
    sinks = Sinks()
    _reset_state()
    m._app_chart_map.update({a: "appspace-ms" for a in APPS})
    m._app_chart_revision_map.update({a: "2603.0.1-dev" for a in APPS})

    monkeypatch.setattr(logsink, "log",
                        lambda msg, severity="INFO", **lab:
                        sinks.logs.append((severity, str(msg), lab)))
    monkeypatch.setattr(logsink, "debug", lambda *a, **k: None)

    # Default: only the ancillary file moved, so the appspace-state panel
    # stays out of the way unless a test opts in.
    monkeypatch.setattr(m, "get_pr_changed_files",
                        lambda pr_id, repo=None: ([ANCILLARY], {}))
    monkeypatch.setattr(m, "find_existing_comment",
                        lambda pr_id, repo=None: (None, "", ""))
    monkeypatch.setattr(m, "upsert_comment",
                        lambda pr_id, body, existing_id=None, repo=None,
                        artifact_url="", **kw:
                        sinks.upserts.append((body, artifact_url)) or 1)
    monkeypatch.setattr(m, "post_build_status",
                        lambda pr_sha, state, description, pr_id=None, repo=None:
                        sinks.statuses.append((state, description)))
    monkeypatch.setattr(m, "fix_stuck_inprogress", lambda *a, **k: None)
    monkeypatch.setattr(m, "_touch_progress", lambda: None)

    # No Bitbucket: every file read is a clean 404 unless a test serves it.
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda path, sha, repo=None: (None, m.BB_NOT_FOUND))

    plan = {}

    def fake_argocd_diff(app, pr_sha, main_sha, chart_revision=None,
                         changed_paths=None, renames=None):
        sinks.diff_calls.append(app)
        return plan.get(app, m.DiffResult("", [], 0, False, "",
                                          m.OUT_NO_DIFF, ""))

    monkeypatch.setattr(m, "argocd_diff", fake_argocd_diff)
    yield sinks, plan
    _reset_state()


def _serve(monkeypatch, files):
    """Serve a {(path, sha): content} map through the Bitbucket fetch seam."""
    def fake(path, sha, repo=None):
        v = files.get((path, sha))
        return (v, m.BB_OK) if v is not None else (None, m.BB_NOT_FOUND)
    monkeypatch.setattr(m, "_bb_fetch_status", fake)


# ── COPS-2617: the base moved before the render even started ─────────────
#
# Lines 9125/9126/9131. `_base_superseded_by` is well covered as a
# predicate; the abort it drives inside process_pr was not.

def test_a_base_that_moved_before_the_render_costs_no_diff_and_no_comment(world):
    """The whole point of catching it at ENTRY: a 564-app render against a
    dead base is never paid for."""
    sinks, _plan = world
    m._record_base_hint(REPO, "main", NEWER_BASE)
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA, repo=REPO)
    assert sinks.diff_calls == [], "a dead base must not be rendered"
    assert sinks.upserts == [], "no comment may be published against a dead base"


def test_the_entry_abort_leaves_the_pr_unseen_so_the_new_base_gets_rendered(world):
    """_seen is the dedup that would otherwise swallow the PR forever: if
    the abort marked it, the correct render against the new base would be
    skipped as 'already processed'."""
    sinks, _plan = world
    m._record_base_hint(REPO, "main", NEWER_BASE)
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA, repo=REPO)
    assert (REPO, 671) not in m._seen

    # Now the poller observes the new tip, which retires the hint, and the
    # very same PR renders normally against it.
    m._note_base_observed(REPO, "main", NEWER_BASE)
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=NEWER_BASE, repo=REPO)
    assert sorted(set(sinks.diff_calls)) == sorted(APPS)
    assert len(sinks.upserts) == 1


def test_the_entry_abort_burns_the_livelock_budget(world):
    """A merge train advances the base continuously. Without counting the
    abort against SUPERSEDE_MAX_CONSECUTIVE_ABORTS a large PR would abort
    forever and the reviewer would get nothing at all."""
    sinks, _plan = world
    m._record_base_hint(REPO, "main", NEWER_BASE)
    for _ in range(m.SUPERSEDE_MAX_CONSECUTIVE_ABORTS):
        m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA, repo=REPO)
        assert sinks.upserts == []
    with m._supersede_lock:
        assert m._pr_supersede_aborts[(REPO, 671)] == \
            m.SUPERSEDE_MAX_CONSECUTIVE_ABORTS
    # Budget spent: the guard now lets the render through rather than
    # livelocking, even though the hint is still pending.
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA, repo=REPO)
    assert len(sinks.upserts) == 1, "the livelock guard must eventually publish"


def test_the_entry_abort_is_reported_as_a_base_supersede(world):
    """It has to be distinguishable from the PR's-own-commit supersede in
    the logs: the two have different causes and different fixes."""
    sinks, _plan = world
    m._record_base_hint(REPO, "main", NEWER_BASE)
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA, repo=REPO)
    entries = [(sev, msg, lab) for sev, msg, lab in sinks.logs
               if lab.get("event") == "base_superseded"]
    assert entries, f"no base_superseded event in {sinks.events()}"
    _sev, msg, lab = entries[0]
    assert lab["stage"] == "entry", lab
    assert lab["new_sha"] == NEWER_BASE[:12] and lab["old_sha"] == BASE_SHA[:12]
    assert NEWER_BASE[:8] in msg and BASE_SHA[:8] in msg


# ── the side panels must never cost the comment ──────────────────────────

def test_a_broken_vm_panel_still_produces_the_normal_comment(world, monkeypatch):
    """Line 9803-9805. The VM panel is decoration on top of the diff; a
    Bitbucket blip inside it must not turn the comment into the generic
    error comment."""
    sinks, plan = world
    plan[APPS[0]] = m.DiffResult(
        "--- main\n+++ pr", [("Deployment/webx", "-replicas: 2\n+replicas: 3")],
        1, True, "", m.OUT_DIFF, "")

    def boom(*a, **k):
        raise ConnectionResetError("bitbucket dropped the VM panel read")

    monkeypatch.setattr(m, "_summarize_vm_changes", boom)
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA, repo=REPO)

    assert len(sinks.upserts) == 1
    body = sinks.upserts[0][0]
    assert "Deployment/webx" in body, "the real diff must survive the panel failure"
    assert "Error processing diff" not in body
    assert [s for s, _ in sinks.statuses][-1] == "SUCCESSFUL"
    assert any("vm-changes panel failed" in msg and "dropped the VM panel read" in msg
               for msg in sinks.messages()), sinks.messages()


def test_a_broken_vm_panel_drops_only_the_vm_panel(world, monkeypatch):
    """Proves the swallow is scoped: with the panel working the comment
    carries it, and with it broken the comment loses exactly that."""
    sinks, plan = world
    plan[APPS[0]] = m.DiffResult(
        "--- main\n+++ pr", [("Deployment/webx", "-a\n+b")], 1, True, "",
        m.OUT_DIFF, "")
    monkeypatch.setattr(m, "_summarize_vm_changes",
                        lambda *a, **k: [comment_render._VM_PANEL_DANGER_HDR,
                                         "", "- disk shrink on `pv-cov671-svc-a`"])
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA, repo=REPO)
    with_panel = sinks.upserts[0][0]
    assert comment_render._VM_PANEL_DANGER_HDR in with_panel

    _reset_state()
    sinks.upserts.clear()
    monkeypatch.setattr(m, "_summarize_vm_changes",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA, repo=REPO)
    without_panel = sinks.upserts[0][0]
    assert comment_render._VM_PANEL_DANGER_HDR not in without_panel
    assert "Deployment/webx" in without_panel


def test_a_broken_autosync_check_still_produces_the_comment(world, monkeypatch):
    """Lines 9831/9834 (COPS-2655). The pause annotation is an addition to
    the comment; failing to compute it must return the service to its
    pre-COPS-2655 silence, not cost the comment."""
    sinks, plan = world
    plan[APPS[0]] = m.DiffResult(
        "--- main\n+++ pr", [("Deployment/webx", "-a\n+b")], 1, True, "",
        m.OUT_DIFF, "")
    # The identity file is readable, so _paused_apps_for gets as far as
    # evaluating the flag -- and that is where it blows up.
    _serve(monkeypatch, {(IDENTITY, PR_SHA): "appspace:\n  autosync: false\n"})
    monkeypatch.setattr(m, "_autosync_paused",
                        lambda flat: (_ for _ in ()).throw(
                            RuntimeError("autosync predicate exploded")))

    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA, repo=REPO)

    assert len(sinks.upserts) == 1
    body = sinks.upserts[0][0]
    assert "Deployment/webx" in body
    assert "NOT applied until auto-sync resumes" not in body, (
        "a failed check must not guess that the environment is frozen")
    entries = [lab for _s, _m2, lab in sinks.logs
               if lab.get("event") == "autosync_check_failed"]
    assert entries, f"the failure must be diagnosable: {sinks.events()}"


def test_a_working_autosync_check_does_annotate_the_comment(world, monkeypatch):
    """The control for the test above: without the failure the pause really
    does reach the comment, so the assertion there is about the guard and
    not about a panel that never renders."""
    sinks, plan = world
    plan[APPS[0]] = m.DiffResult(
        "--- main\n+++ pr", [("Deployment/webx", "-a\n+b")], 1, True, "",
        m.OUT_DIFF, "")
    _serve(monkeypatch, {(IDENTITY, PR_SHA): "appspace:\n  autosync: false\n"})
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA, repo=REPO)
    assert "NOT applied until auto-sync resumes" in sinks.upserts[0][0]


# ── COPS-2609: the full-diff page failed to save ─────────────────────────

@pytest.fixture()
def ui_on(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "DIFF_UI_ENABLED", True)
    monkeypatch.setattr(m, "DIFF_UI_DIR", str(tmp_path))
    monkeypatch.setattr(m, "DIFF_UI_BASE_URL", "https://diff.example.test")
    monkeypatch.setattr(m, "_leader", None)
    return "https://diff.example.test"


def test_a_failed_page_save_republishes_the_comment_without_the_link(
        world, ui_on, monkeypatch):
    """Lines 9875/9877/9878/9880. The link is minted before the page is
    written, so a failed write would otherwise send every reviewer of the
    PR to a 404 they cannot distinguish from a page nobody linked."""
    sinks, plan = world
    plan[APPS[0]] = m.DiffResult(
        "--- main\n+++ pr", [("Deployment/webx", "-a\n+b")], 1, True, "",
        m.OUT_DIFF, "")
    monkeypatch.setattr(m.diff_ui, "save_artifact",
                        lambda *a, **k: (_ for _ in ()).throw(
                            OSError("[Errno 28] No space left on device")))

    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA, repo=REPO)

    assert len(sinks.upserts) == 1, "exactly one comment, the re-rendered one"
    body, url_kw = sinks.upserts[0]
    assert ui_on not in body, (
        "the published comment still points at a page that was never written")
    assert url_kw == "", "upsert must not be handed a URL either"
    assert "Deployment/webx" in body, "the hunks have to stay inline instead"
    # The size in the log must describe the comment that was actually
    # posted. The two renders differ (the inline one is the larger), so a
    # stale figure here would misreport every fallback in the metrics.
    posted = [lab for _s, _m2, lab in sinks.logs
              if lab.get("event") == "comment_posted"]
    assert posted, sinks.events()
    assert posted[0]["comment_kb"] == round(len(body.encode()) / 1024, 1)


def test_a_successful_page_save_keeps_the_link(world, ui_on):
    """Control: the fallback must fire ONLY on the failure path, otherwise
    the assertion above would pass for a comment that never links at all."""
    sinks, plan = world
    plan[APPS[0]] = m.DiffResult(
        "--- main\n+++ pr", [("Deployment/webx", "-a\n+b")], 1, True, "",
        m.OUT_DIFF, "")
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA, repo=REPO)
    body, url_kw = sinks.upserts[0]
    assert ui_on in body and ui_on in url_kw


def test_the_inline_fallback_is_counted(world, ui_on, monkeypatch):
    """It is a silent degradation otherwise: the comment looks fine and the
    page is simply missing. /diff-preview/stats has to show it."""
    sinks, plan = world
    plan[APPS[0]] = m.DiffResult(
        "--- main\n+++ pr", [("Deployment/webx", "-a\n+b")], 1, True, "",
        m.OUT_DIFF, "")
    before = m._diff_stats["comment_fallback_inline"]
    monkeypatch.setattr(m.diff_ui, "save_artifact",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA, repo=REPO)
    assert m._diff_stats["comment_fallback_inline"] == before + 1


# ── COPS-2660: broken arming must turn the build status red ──────────────

def test_stripping_the_vm_config_while_arming_fails_the_build(world, monkeypatch):
    """Lines 9969/9973. This shape diffs CLEANLY -- the VM CRs just vanish
    from the render -- so every other branch in the chain would post
    SUCCESSFUL. acme-config-dev PR #7113 shipped exactly that: the comment
    said DO NOT MERGE and Bitbucket said '1 of 1 build passed'."""
    sinks, _plan = world
    monkeypatch.setattr(m, "get_pr_changed_files",
                        lambda pr_id, repo=None: ([IDENTITY], {}))
    _serve(monkeypatch, {(IDENTITY, BASE_SHA): LIVE_VM_YAML,
                         (IDENTITY, PR_SHA): STRIPPED_YAML})

    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA, repo=REPO)

    assert comment_render._DECOM_VM_STRIP_HDR in sinks.upserts[0][0], (
        "precondition: this PR must be the broken-arming shape")
    state, desc = sinks.statuses[-1]
    assert state == "FAILED", sinks.statuses
    assert "orphaned" in desc.lower(), desc
    assert "arming broken" in desc.lower(), desc


def test_the_same_arming_done_correctly_stays_green(world, monkeypatch):
    """Control: arming decommission is not itself a build failure. Only the
    broken shape is, so the branch cannot be a blanket red on decommission."""
    sinks, _plan = world
    monkeypatch.setattr(m, "get_pr_changed_files",
                        lambda pr_id, repo=None: ([IDENTITY], {}))
    _serve(monkeypatch, {(IDENTITY, BASE_SHA): LIVE_VM_YAML,
                         (IDENTITY, PR_SHA): ARMED_OK_YAML})
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA, repo=REPO)
    assert comment_render._DECOM_VM_STRIP_HDR not in sinks.upserts[0][0]
    state, desc = sinks.statuses[-1]
    assert state != "FAILED" or "arming broken" not in desc.lower(), \
        (state, desc)


# ── main_iteration: the COPS-2647 upload reconcile ───────────────────────

def _iteration_edges(monkeypatch, sinks, path_map=None, login=None):
    monkeypatch.setattr(logsink, "log",
                        lambda msg, severity="INFO", **lab:
                        sinks.logs.append((severity, str(msg), lab)))
    monkeypatch.setattr(logsink, "debug", lambda *a, **k: None)
    monkeypatch.setattr(m, "_prune_helm_cache", lambda: None)
    monkeypatch.setattr(m, "discover_path_app_map",
                        lambda: PATH_MAP if path_map is None else path_map)
    monkeypatch.setattr(m, "argocd_login", login or (lambda: None))
    monkeypatch.setattr(m, "_argocd_token", "", raising=False)
    monkeypatch.setattr(m, "_touch_progress", lambda: None)
    monkeypatch.setattr(m, "mirror_sync", lambda repo: None)
    monkeypatch.setattr(m, "http",
                        lambda method, url, **kw: {"target": {"hash": BASE_SHA}})
    monkeypatch.setattr(m, "get_open_prs", lambda repo=None: [])
    processed = []
    monkeypatch.setattr(m, "process_pr",
                        lambda pr, pm, base_sha="", repo=None:
                        processed.append(pr["id"]))
    return processed


def test_a_pending_artifact_upload_is_reconciled_at_the_top_of_the_iteration(
        monkeypatch, tmp_path):
    """Lines 10130-10132. A failed upload leaves the PREVIOUS commit in the
    bucket while the leader serves the current one, so the two pods present
    different diffs for the same URL until this heals it."""
    sinks = Sinks()
    _reset_state()
    _iteration_edges(monkeypatch, sinks)
    blob = tmp_path / "artifact.html.zst"
    blob.write_bytes(b"rendered page")
    uploaded = []
    monkeypatch.setattr(diff_ui, "_gcs_upload",
                        lambda bucket, name, payload:
                        uploaded.append((bucket, name, payload)) or True)
    diff_ui._note_pending_upload("bkt", "diff/acme/1/abc.html.zst",
                                 str(blob), REPO, 1)
    assert diff_ui.pending_upload_count() == 1

    m.main_iteration()

    assert uploaded == [("bkt", "diff/acme/1/abc.html.zst", b"rendered page")]
    assert diff_ui.pending_upload_count() == 0, "a healed upload must be forgotten"
    assert any("Re-uploaded 1 artifact(s)" in msg for msg in sinks.messages()), \
        sinks.messages()


def test_nothing_pending_says_nothing(monkeypatch):
    """The reconcile is per-iteration; on a healthy pod it must stay silent
    rather than logging a zero every cycle."""
    sinks = Sinks()
    _reset_state()
    _iteration_edges(monkeypatch, sinks)
    m.main_iteration()
    assert not any("Re-uploaded" in msg for msg in sinks.messages()), \
        sinks.messages()


def test_a_broken_reconcile_does_not_stop_the_iteration(monkeypatch):
    """Lines 10133/10134. The reconcile runs for durability, never for the
    correctness of the diffs about to be computed -- so it must not be able
    to take the iteration down with it."""
    sinks = Sinks()
    _reset_state()
    processed = _iteration_edges(monkeypatch, sinks)
    monkeypatch.setattr(m, "get_open_prs", lambda repo=None: [_mk_pr(4242)])
    monkeypatch.setattr(diff_ui, "retry_pending_uploads",
                        lambda: (_ for _ in ()).throw(
                            OSError("bucket credentials rotated")))

    m.main_iteration()

    assert processed == [4242], "the iteration must still process its PRs"
    assert any(sev == "WARNING" and "reconcile failed" in msg
               and "credentials rotated" in msg
               for sev, msg, _l in sinks.logs), sinks.messages()


# ── COPS-2668: empty discovery, and ArgoCD down hard ─────────────────────

def test_an_empty_inventory_with_a_dead_argocd_still_returns_quietly(monkeypatch):
    """Lines 10177-10181. The empty-inventory branch tries a re-login before
    giving up. When ArgoCD is down that login raises too, and an unhandled
    raise here would abort main() -- the poll loop would stop retrying at
    precisely the moment recovery depends on retrying.

    Two separate things are pinned, because the guard is only worth anything
    if the call it guards actually happens: (a) the re-login IS attempted on
    the empty-inventory path, and (b) the exception it throws is swallowed
    rather than escaping main_iteration. Drop the call and (a) fails; drop
    the try/except and (b) fails."""
    sinks = Sinks()
    _reset_state()
    attempts = []

    def dead_login():
        attempts.append("relogin")
        raise RuntimeError("argocd: connection refused")

    processed = _iteration_edges(monkeypatch, sinks, path_map={}, login=dead_login)
    polled = []
    monkeypatch.setattr(m, "http",
                        lambda *a, **k: polled.append(1) or {"target": {"hash": BASE_SHA}})
    monkeypatch.setattr(m, "get_open_prs", lambda repo=None: [_mk_pr(9)])

    m.main_iteration()      # the RuntimeError above must not escape this call

    assert attempts == ["relogin"], (
        "the empty-inventory branch must attempt exactly one re-login before "
        "giving up -- without it the session can never be restored and every "
        "later iteration discovers {} again")
    assert processed == [], "an empty inventory must not comment on any PR"
    assert not polled, "an empty inventory must stop before the Bitbucket poll"
    assert any(sev == "ERROR" and "discovery failure" in msg
               for sev, msg, _l in sinks.logs), sinks.messages()


def test_the_loop_survives_the_dead_argocd_and_runs_again(monkeypatch):
    """The consequence that matters, driven through the real seam: discovery
    is empty BECAUSE the ArgoCD session is gone, so the only thing that can
    end the outage is the re-login inside the empty-inventory branch.

    ArgoCD is modelled the way it actually behaves -- `discover_path_app_map`
    returns {} while the session is dead and the full inventory once a login
    has succeeded -- so the three iterations are:

      1. ArgoCD down: discovery {}, the branch's re-login raises, swallowed.
      2. ArgoCD back: discovery still {} (nobody has logged in yet), the
         branch's re-login now succeeds and restores the session.
      3. discovery returns the inventory again and PRs are processed.

    Iteration 3 can only pass if iteration 2's re-login really ran, so
    deleting or no-op'ing that call turns this red."""
    sinks = Sinks()
    _reset_state()
    argocd = {"reachable": False, "session": False}
    attempts = []

    def flaky_login():
        attempts.append(len(attempts) + 1)
        if not argocd["reachable"]:
            raise RuntimeError("argocd: connection refused")
        argocd["session"] = True

    def discovery():
        # `argocd app list` against a dead session exits 0 with nothing
        # annotated -- the COPS-2668 shape, not an exception.
        return dict(PATH_MAP) if argocd["session"] else {}

    processed = _iteration_edges(monkeypatch, sinks, login=flaky_login)
    monkeypatch.setattr(m, "discover_path_app_map", discovery)
    monkeypatch.setattr(m, "get_open_prs", lambda repo=None: [_mk_pr(11)])

    # 1. ArgoCD is down hard.
    m.main_iteration()
    assert processed == [], "nothing may be commented while the inventory is empty"
    assert len(attempts) == 1, "the outage iteration must still try to re-login"
    assert argocd["session"] is False

    # 2. ArgoCD comes back. Discovery is still empty this cycle, so recovery
    #    has to come from the branch's own re-login.
    argocd["reachable"] = True
    m.main_iteration()
    assert processed == [], "the healing iteration still has no inventory to use"
    assert len(attempts) == 2
    assert argocd["session"] is True, (
        "the empty-inventory branch must re-establish the session; without "
        "that call the pod stays blind forever")

    # 3. With the session restored the inventory is back and work resumes --
    #    unattended, no restart, which is the whole point of the guard.
    m.main_iteration()
    assert processed == [11]


# ── COPS-2631: the tip-move cache-clear escape hatch ─────────────────────

def test_the_render_cache_survives_a_main_tip_move_by_default(monkeypatch):
    """The regression COPS-2631 fixed: unrelated commits on main used to
    wipe every entry of a CONTENT-keyed cache, giving a 0% hit rate."""
    sinks = Sinks()
    _reset_state()
    _iteration_edges(monkeypatch, sinks)
    monkeypatch.setattr(m, "_main_render_sha", {REPO: "0000dead0000"})
    with m._main_render_lock:
        m._main_render_cache["content-key-1"] = ("rendered", "raw", "src")

    m.main_iteration()

    assert "content-key-1" in m._main_render_cache, (
        "a content-keyed entry is still valid after main moves")
    assert m._main_render_sha[REPO] == BASE_SHA, "the tip is still tracked"


def test_turning_the_escape_hatch_on_clears_the_cache_when_the_tip_moves(
        monkeypatch):
    """Lines 10221/10222. The flag exists so the pre-COPS-2631 behaviour can
    be restored without a code change if the content key is ever found to
    be wrong; if it no longer clears, the hatch is a lie."""
    sinks = Sinks()
    _reset_state()
    _iteration_edges(monkeypatch, sinks)
    monkeypatch.setattr(m, "_CLEAR_MAIN_RENDER_ON_TIP_MOVE", True)
    monkeypatch.setattr(m, "_main_render_sha", {REPO: "0000dead0000"})
    with m._main_render_lock:
        m._main_render_cache["content-key-1"] = ("rendered", "raw", "src")

    m.main_iteration()

    assert "content-key-1" not in m._main_render_cache
    assert m._main_render_sha[REPO] == BASE_SHA


def test_the_escape_hatch_does_not_clear_when_the_tip_is_unchanged(monkeypatch):
    """Even switched on, it is a TIP-MOVE clear: a steady main must not
    throw the cache away every single iteration."""
    sinks = Sinks()
    _reset_state()
    _iteration_edges(monkeypatch, sinks)
    monkeypatch.setattr(m, "_CLEAR_MAIN_RENDER_ON_TIP_MOVE", True)
    monkeypatch.setattr(m, "_main_render_sha", {REPO: BASE_SHA})
    with m._main_render_lock:
        m._main_render_cache["content-key-1"] = ("rendered", "raw", "src")

    m.main_iteration()

    assert "content-key-1" in m._main_render_cache
