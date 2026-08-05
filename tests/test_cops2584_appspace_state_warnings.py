"""appspace.autosync and appspace.decommission must not read as a no-op (COPS-2584).

What the comment says today
----------------------------
Both flags change ArgoCD's behaviour for a whole environment without touching
a single rendered manifest:

  * `appspace.autosync: false` (COPS-2583) pauses automated sync for that
    environment's Applications.
  * `appspace.decommission: true` (COPS-2539) arms the cascade-delete
    finalizer, so a LATER PR that removes the folder will actually delete
    the environment's resources instead of leaving them orphaned.

Neither touches a Helm template, so every existing symptom panel (chart
diff, resource diff) reports nothing, and `_summarize_input_changes` treats
the key exactly like any other line in customer.yaml:

    Config changes in this PR
    `gcp/dev/.../customer.yaml`:
    - added `appspace.autosync` = False

A PR that freezes an environment, or arms it for future deletion, must not
look like a no-op next to a green "no manifest changes" status.

The fix
-------
`_summarize_appspace_state_changes` reads the SAME old/new flattened content
`_summarize_input_changes` already fetches (via the shared `_bb_fetch_cached`
cache -- a second read is a cache hit, not a second network call) for any
identity file (customer.yaml/config.yaml) that is a currently-live
environment's own file (present in path_map) and exists on both sides of the
diff. It mirrors the EXACT parsing semantics the ApplicationSet templatePatch
and `_decommission_cascades`/`_decommission_purges_data` already use in
production, so the warning can never disagree with what ArgoCD will actually
do:

  * autosync is paused iff `str(value).lower() == "false"` -- the ApplicationSet
    templatePatch only checks equality to the literal string "false"; a missing
    key, `true`, or any other value leaves auto-sync on.
  * decommission is armed iff `str(value).lower() == "true"`.
  * purge only matters when decommission is armed too (fail-closed AND, same
    as `_decommission_purges_data`).

Files that exist on only one side of the diff are new-env or
decommission-by-deletion territory, already covered by their own dedicated
panels, and must be skipped here to avoid a duplicate/contradictory message.
"""
import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m

IDENT = "gcp/dev/private-cloud/ap1/custom/pv-dev-07-a/customer.yaml"
APPS = ["pv-dev-07-a-ss", "pv-dev-07-a-ms", "pv-dev-07-a-glb"]
PATH_MAP = {IDENT: APPS}


def _mk_fetch(files_by_sha):
    def fake(path, sha, repo=None):
        v = files_by_sha.get((path, sha))
        return (v, m.BB_OK) if v is not None else (None, m.BB_NOT_FOUND)
    return fake


# -- autosync: pause --------------------------------------------------------

def test_autosync_pause_is_called_out_and_names_the_apps(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_status", _mk_fetch({
        (IDENT, "mainsha"): "appspace:\n  customerName: dev-07\n  version: 1.0.0\n",
        (IDENT, "prsha"):   "appspace:\n  autosync: false\n  customerName: dev-07\n  version: 1.0.0\n",
    }))
    out = "\n".join(m._summarize_appspace_state_changes([IDENT], "prsha", "mainsha", PATH_MAP))
    assert "PAUSED" in out.upper()
    assert "pv-dev-07-a" in out
    for app in APPS:
        assert app in out, f"the warning must name the affected app {app}"
    assert "appspace.autosync" in out


def test_autosync_pause_matches_the_quoted_string_form_too(monkeypatch):
    """The templatePatch condition is `printf "%v" .appspace.autosync == "false"`,
    which is exactly as true for the quoted YAML string "false" as for the
    boolean. The warning must fire identically either way."""
    monkeypatch.setattr(m, "_bb_fetch_status", _mk_fetch({
        (IDENT, "mainsha"): "appspace:\n  customerName: dev-07\n",
        (IDENT, "prsha"):   'appspace:\n  autosync: "false"\n  customerName: dev-07\n',
    }))
    out = "\n".join(m._summarize_appspace_state_changes([IDENT], "prsha", "mainsha", PATH_MAP))
    assert "PAUSED" in out.upper()


def test_autosync_true_is_not_a_pause(monkeypatch):
    """Only the literal false pauses. true is not a transition into paused."""
    monkeypatch.setattr(m, "_bb_fetch_status", _mk_fetch({
        (IDENT, "mainsha"): "appspace:\n  customerName: dev-07\n",
        (IDENT, "prsha"):   "appspace:\n  autosync: true\n  customerName: dev-07\n",
    }))
    out = "\n".join(m._summarize_appspace_state_changes([IDENT], "prsha", "mainsha", PATH_MAP))
    assert out == ""


# -- autosync: resume --------------------------------------------------------

def test_autosync_resume_warns_about_the_accumulated_diff(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_status", _mk_fetch({
        (IDENT, "mainsha"): "appspace:\n  autosync: false\n  customerName: dev-07\n",
        (IDENT, "prsha"):   "appspace:\n  customerName: dev-07\n",
    }))
    out = "\n".join(m._summarize_appspace_state_changes([IDENT], "prsha", "mainsha", PATH_MAP))
    assert "RESUMED" in out.upper()
    assert "pv-dev-07-a" in out


# -- autosync: still paused, unrelated key also changed ----------------------

def test_autosync_still_paused_gets_a_quiet_reminder_not_a_repeat_banner(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_status", _mk_fetch({
        (IDENT, "mainsha"): "appspace:\n  autosync: false\n  version: 1.0.0\n",
        (IDENT, "prsha"):   "appspace:\n  autosync: false\n  version: 1.0.1\n",
    }))
    out = "\n".join(m._summarize_appspace_state_changes([IDENT], "prsha", "mainsha", PATH_MAP))
    assert "Auto-sync PAUSED" not in out, \
        "unchanged state must not repeat the loud transition banner"
    assert "remain" in out.lower() or "still" in out.lower()
    assert "pv-dev-07-a" in out


def test_autosync_never_touched_produces_nothing(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_status", _mk_fetch({
        (IDENT, "mainsha"): "appspace:\n  version: 1.0.0\n",
        (IDENT, "prsha"):   "appspace:\n  version: 1.0.1\n",
    }))
    out = "\n".join(m._summarize_appspace_state_changes([IDENT], "prsha", "mainsha", PATH_MAP))
    assert out == ""


# -- decommission: arm / disarm ----------------------------------------------

def test_decommission_arm_is_loud_but_says_nothing_is_deleted_yet(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_status", _mk_fetch({
        (IDENT, "mainsha"): "appspace:\n  customerName: dev-07\n",
        (IDENT, "prsha"):   "appspace:\n  decommission: true\n  customerName: dev-07\n",
    }))
    out = "\n".join(m._summarize_appspace_state_changes([IDENT], "prsha", "mainsha", PATH_MAP))
    assert "ARMED" in out.upper()
    assert "appspace.decommission" in out
    assert "deletes nothing" in out.lower() or "nothing is deleted" in out.lower() \
        or "nothing by itself" in out.lower()
    for app in APPS:
        assert app in out


def test_decommission_arm_with_purge_gets_the_data_destruction_note(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_status", _mk_fetch({
        (IDENT, "mainsha"): "appspace:\n  customerName: dev-07\n",
        (IDENT, "prsha"):   "appspace:\n  decommission: true\n  decommissionPurgeData: true\n"
                            "  customerName: dev-07\n",
    }))
    out = "\n".join(m._summarize_appspace_state_changes([IDENT], "prsha", "mainsha", PATH_MAP))
    assert "ARMED" in out.upper()
    assert "permanently destroy" in out.lower() or "permanently destroyed" in out.lower()


def test_decommission_disarm_is_informational(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_status", _mk_fetch({
        (IDENT, "mainsha"): "appspace:\n  decommission: true\n  customerName: dev-07\n",
        (IDENT, "prsha"):   "appspace:\n  customerName: dev-07\n",
    }))
    out = "\n".join(m._summarize_appspace_state_changes([IDENT], "prsha", "mainsha", PATH_MAP))
    assert "DISARMED" in out.upper()
    assert "no longer eligible" in out.lower()


def test_purge_only_toggle_on_an_already_armed_env_is_flagged_separately(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_status", _mk_fetch({
        (IDENT, "mainsha"): "appspace:\n  decommission: true\n  customerName: dev-07\n",
        (IDENT, "prsha"):   "appspace:\n  decommission: true\n  decommissionPurgeData: true\n"
                            "  customerName: dev-07\n",
    }))
    out = "\n".join(m._summarize_appspace_state_changes([IDENT], "prsha", "mainsha", PATH_MAP))
    assert "PURGE" in out.upper()
    assert "ARMED" in out.upper()
    assert "DISARMED" not in out.upper()


# -- both flags in one PR ----------------------------------------------------

def test_autosync_and_decommission_can_both_fire_for_the_same_file(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_status", _mk_fetch({
        (IDENT, "mainsha"): "appspace:\n  customerName: dev-07\n",
        (IDENT, "prsha"):   "appspace:\n  autosync: false\n  decommission: true\n"
                            "  customerName: dev-07\n",
    }))
    out = "\n".join(m._summarize_appspace_state_changes([IDENT], "prsha", "mainsha", PATH_MAP))
    assert "PAUSED" in out.upper()
    assert "ARMED" in out.upper()


# -- must not fire outside its lane ------------------------------------------

def test_file_only_on_one_side_is_skipped_new_env_and_deletion_territory(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_status", _mk_fetch({
        (IDENT, "prsha"): "appspace:\n  autosync: false\n  customerName: dev-07\n",
        # absent at mainsha -> BB_NOT_FOUND -> a brand-new environment
    }))
    out = "\n".join(m._summarize_appspace_state_changes([IDENT], "prsha", "mainsha", PATH_MAP))
    assert out == "", "a file that only exists on one side is new-env/decommission territory"


def test_file_not_in_path_map_is_skipped(monkeypatch):
    """A shared ancestor config.yaml (or anything not a live environment's own
    identity file) must never trigger this, even if it happens to carry the
    same key names."""
    shared = "gcp/dev/config.yaml"
    monkeypatch.setattr(m, "_bb_fetch_status", _mk_fetch({
        (shared, "mainsha"): "appspace:\n  customerName: dev\n",
        (shared, "prsha"):   "appspace:\n  autosync: false\n  customerName: dev\n",
    }))
    out = "\n".join(m._summarize_appspace_state_changes([shared], "prsha", "mainsha", PATH_MAP))
    assert out == ""


def test_non_identity_file_is_ignored(monkeypatch):
    f = "gcp/dev/private-cloud/ap1/custom/pv-dev-07-a/cicd-versions.yaml"
    monkeypatch.setattr(m, "_bb_fetch_status", _mk_fetch({
        (f, "mainsha"): "appspace:\n  autosync: true\n",
        (f, "prsha"):   "appspace:\n  autosync: false\n",
    }))
    out = "\n".join(m._summarize_appspace_state_changes([f], "prsha", "mainsha", PATH_MAP))
    assert out == ""


# -- format_comment integration: ordering ------------------------------------

def test_comment_places_appspace_state_before_input_changes_and_diff(monkeypatch):
    res = m.DiffResult("--- a\n+++ b", [("Deployment/x", "-a\n+b")],
                       1, True, "", m.OUT_DIFF, "")
    body = m.format_comment(
        "c" * 12, {"appx": res},
        appspace_state_lines=["### \u23f8\ufe0f Auto-sync PAUSED for `pv-dev-07-a`", "",
                              "paused", ""],
        input_change_lines=["### \U0001f4dd Config changes in this PR", "",
                            "- `appspace.autosync`: absent \u2192 False", ""])
    hdr    = body.index("ACME Diff Preview")
    state  = body.index("PAUSED for `pv-dev-07-a`")
    cause  = body.index("Config changes in this PR")
    symptom = body.index("appx")
    assert hdr < state < cause < symptom, \
        "the state banner is the headline: header, then state, then cause, then symptom"


def test_comment_without_appspace_state_lines_is_unchanged(monkeypatch):
    """Additive-only: a PR with no state transition must render byte-identical
    to before this feature existed. This is what protects every existing
    golden comment."""
    res = m.DiffResult("--- a\n+++ b", [("Deployment/x", "-a\n+b")],
                       1, True, "", m.OUT_DIFF, "")
    body = m.format_comment("c" * 12, {"appx": res})
    assert "PAUSED" not in body.upper()
    assert "ARMED" not in body.upper()


# ── end-to-end: process_pr actually wires this in ───────────────────────────

_ORCH_ENV_DIR  = "gcp/dev/private-cloud/ap1/custom/pv-orch-b"
_ORCH_IDENTITY = f"{_ORCH_ENV_DIR}/customer.yaml"
_ORCH_PATH_MAP = {_ORCH_IDENTITY: ["pv-orch-b-ms", "pv-orch-b-ss"]}
_ORCH_PR_SHA   = "aabbccddeeffprsha"
_ORCH_BASE_SHA = "112233445566mainsha"


def _mk_orch_pr(pr_id=2584):
    return {
        "id": pr_id,
        "title": "[COPS-2584] synthetic autosync-pause PR",
        "source": {"commit": {"hash": _ORCH_PR_SHA}, "branch": {"name": "feature/pause"}},
        "destination": {"branch": {"name": "main"}},
    }


def test_process_pr_end_to_end_posts_the_pause_warning(monkeypatch):
    """Full orchestrator, not just the isolated helper: a real PR that adds
    appspace.autosync: false to a live environment's customer.yaml must post
    a comment containing the pause warning, without touching the ordinary
    diff-computation path."""
    m._seen.clear()
    m._force_recompute.clear()
    m._main_render_cache.clear()
    m._app_chart_map.update({"pv-orch-b-ms": "appspace-ms", "pv-orch-b-ss": "appspace-ss"})
    m._app_chart_revision_map.update({"pv-orch-b-ms": "2603.0.1-dev", "pv-orch-b-ss": "2603.0.1-dev"})

    upserts = []
    statuses = []
    monkeypatch.setattr(m, "get_pr_changed_files",
                        lambda pr_id, repo=None: ([_ORCH_IDENTITY], {}))
    monkeypatch.setattr(m, "find_existing_comment", lambda pr_id, repo=None: (None, "", ""))
    monkeypatch.setattr(m, "upsert_comment",
                        lambda pr_id, body, existing_id=None, repo=None,
                        artifact_url="": upserts.append(body) or 1)
    monkeypatch.setattr(m, "post_build_status",
                        lambda pr_sha, state, description, pr_id=None, repo=None:
                        statuses.append((state, description)))
    monkeypatch.setattr(m, "fix_stuck_inprogress", lambda *a, **k: None)
    monkeypatch.setattr(m, "_touch_progress", lambda: None)
    monkeypatch.setattr(m, "argocd_diff",
                        lambda app, pr_sha, main_sha, chart_revision=None,
                               changed_paths=None, renames=None:
                        m.DiffResult("", [], 0, False, "", m.OUT_NO_DIFF, ""))
    monkeypatch.setattr(m, "_bb_fetch_status", _mk_fetch({
        (_ORCH_IDENTITY, _ORCH_BASE_SHA): "appspace:\n  customerName: orch-b\n",
        (_ORCH_IDENTITY, _ORCH_PR_SHA):   "appspace:\n  autosync: false\n  customerName: orch-b\n",
    }))

    try:
        m.process_pr(_mk_orch_pr(), _ORCH_PATH_MAP, base_sha=_ORCH_BASE_SHA)
    finally:
        m._seen.clear()
        m._force_recompute.clear()

    assert len(upserts) == 1
    body = upserts[0]
    assert "Auto-sync PAUSED" in body
    assert "pv-orch-b" in body
    assert "pv-orch-b-ms" in body and "pv-orch-b-ss" in body
    # a pure state-flag pause must not itself block the PR — no manifest
    # changed, so the ordinary green/no-diff status still applies.
    assert statuses, "a terminal build status must still be posted"
    assert statuses[-1][0] == "SUCCESSFUL", statuses
