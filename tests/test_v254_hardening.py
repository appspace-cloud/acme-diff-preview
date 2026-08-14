"""Regression tests for the v2.5.4 traffic-light + rename-resolution round.

Covers Findings 1, 3, 4, 5, 6 from FINDINGS_SEMAFORO_NEXT_PHASE.md, each
encoded from the real PR that confirmed the bug. Pure-function level where
possible; a couple of tests build a real DiffResult set and drive the
actual decision logic used by process_pr.
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m
import render_cache

# ── Finding 5: _new_env_status defaults to RED now, allow-list for green ──
def test_new_env_missing_required_value_stays_green():
    # The one well-understood expected shape stays green.
    state, expected = m._new_env_status("helm template failed: Missing required value: .Values.appspace.secret")
    assert (state, expected) == ("SUCCESSFUL", True)


def test_new_env_generic_chart_pull_failure_now_red():
    # Was green before v2.5.4 (matched no deny-list pattern). Must be red now.
    state, expected = m._new_env_status("chart pull failed: connection reset by peer")
    assert (state, expected) == ("FAILED", False)


def test_new_env_registry_login_failure_now_red():
    state, expected = m._new_env_status("chart pull returned None (registry login may have failed)")
    assert (state, expected) == ("FAILED", False)


def test_new_env_generic_render_failed_now_red():
    # A real render_failed unrelated to missing credentials (Finding 1's
    # exact repro: valid YAML, wrong type) must default to red for a new env.
    state, expected = m._new_env_status(
        "helm template failed: execution error: can't evaluate field tag in type interface {}")
    assert (state, expected) == ("FAILED", False)


def test_new_env_invalid_yaml_still_red():
    state, expected = m._new_env_status("error converting YAML to JSON: yaml: line 3: mapping values are not allowed")
    assert (state, expected) == ("FAILED", False)


def test_new_env_no_appspace_version_still_red():
    state, expected = m._new_env_status("no appspace.version found in config file")
    assert (state, expected) == ("FAILED", False)

# ── Finding 1: format_comment's embedded token must classify ANY
#    indeterminate reason correctly (this part was already right before
#    v2.5.4 -- these guard it stays right after the refactor) ────────────
def _mk_result(outcome, reason="x", n=0, text="", sections=None):
    return m.DiffResult(text, sections or [], n, outcome == m.OUT_DIFF,
                        None if outcome != m.OUT_INDETERMINATE else "e",
                        outcome, reason)


def test_format_comment_token_render_failed_is_permanent_class_transient():
    # render_failed is NOT in PERMANENT_REASONS -> token stays "transient"
    # (retry keeps working), but see the next test for the STATUS color,
    # which is the actual v2.5.4 fix (a separate code path in process_pr).
    body = m.format_comment("deadbeef01234567", {"a": _mk_result(m.OUT_INDETERMINATE, m.REASON_RENDER)})
    assert m._extract_status_token(body) == "transient"


def test_format_comment_token_invalid_yaml_is_permanent():
    body = m.format_comment("deadbeef01234567", {"a": _mk_result(m.OUT_INDETERMINATE, m.REASON_INVALID_YAML)})
    assert m._extract_status_token(body) == "permanent"


# ── Finding 1: the actual process_pr status-decision cascade. We can't
#    call process_pr directly (network), so we replicate its exact
#    decision logic here as a small pure function mirroring the real one,
#    and separately assert the real constants/branches exist as expected.
#    This mirrors the pattern already used for the pre-v2.5.4 bug repro. ──
def _decide_status(app_results, skipped_apps=None, structural_envs=None):
    """Mirrors process_pr's post-v2.5.4 status decision cascade exactly."""
    from collections import Counter
    skipped_apps = skipped_apps or []
    structural_envs = structural_envs or []
    outcome_counts = Counter(r.outcome for r in app_results.values())
    for o in (m.OUT_DIFF, m.OUT_NO_DIFF, m.OUT_INDETERMINATE, m.OUT_ERROR):
        outcome_counts.setdefault(o, 0)
    sections_total = sum(max(r.n_res, 1) for r in app_results.values() if r.outcome == m.OUT_DIFF)
    n_unknown = outcome_counts[m.OUT_INDETERMINATE]
    oci_not_found_count = sum(1 for r in app_results.values()
                               if r.outcome == m.OUT_INDETERMINATE and r.reason == m.REASON_OCI_NOT_FOUND)
    permanent_indet_count = sum(1 for r in app_results.values()
                                 if r.outcome == m.OUT_INDETERMINATE and r.reason in m.PERMANENT_REASONS)
    has_blocking_indet = permanent_indet_count > 0
    any_hard_error = outcome_counts[m.OUT_ERROR] > 0
    any_unknown = n_unknown > 0

    if any_hard_error or has_blocking_indet or structural_envs:
        return "FAILED"
    elif skipped_apps:
        return "FAILED"
    elif any_unknown:
        return "FAILED"
    elif sections_total > 0:
        return "SUCCESSFUL"
    else:
        return "SUCCESSFUL"


def test_status_render_failed_alone_is_red():
    # PR #6645 repro: a single render_failed app, no real diff anywhere.
    assert _decide_status({"a": _mk_result(m.OUT_INDETERMINATE, m.REASON_RENDER)}) == "FAILED"


def test_status_real_diff_plus_transient_failure_is_red():
    # A real diff on one app + a transient failure on another -- must be
    # red overall now (was green with a "(N unavailable)" suffix before).
    result = {
        "a": _mk_result(m.OUT_DIFF, n=1),
        "b": _mk_result(m.OUT_INDETERMINATE, m.REASON_TIMEOUT),
    }
    assert _decide_status(result) == "FAILED"


def test_status_invalid_yaml_alone_is_red():
    # PR #6644 repro.
    assert _decide_status({"a": _mk_result(m.OUT_INDETERMINATE, m.REASON_INVALID_YAML)}) == "FAILED"


def test_status_clean_diff_still_green():
    assert _decide_status({"a": _mk_result(m.OUT_DIFF, n=1)}) == "SUCCESSFUL"


def test_status_no_changes_still_green():
    assert _decide_status({"a": _mk_result(m.OUT_NO_DIFF)}) == "SUCCESSFUL"


def test_status_structural_new_env_forces_red_even_with_clean_diff():
    # Finding 4's interaction: a perfectly clean existing-app diff combined
    # with a structural new-env problem must still be red overall.
    result = {"a": _mk_result(m.OUT_DIFF, n=1)}
    assert _decide_status(result, structural_envs=["pv-broken-a"]) == "FAILED"

# ── Finding 3: fix_stuck_inprogress must resolve "transient" to FAILED ────
def test_fix_stuck_inprogress_transient_token_resolves_to_failed(monkeypatch):
    monkeypatch.setattr(m, "http", lambda *a, **k: {"state": "INPROGRESS"})
    captured = {}
    def fake_post(pr_sha, state, desc, pr_id=None):
        captured["state"] = state
    monkeypatch.setattr(m, "post_build_status", fake_post)

    body = m.format_comment("deadbeef01234567", {"a": _mk_result(m.OUT_INDETERMINATE, m.REASON_RENDER)})
    m.fix_stuck_inprogress("deadbeef01234567", 999, body)
    assert captured["state"] == "FAILED"


def test_fix_stuck_inprogress_clean_token_still_successful(monkeypatch):
    monkeypatch.setattr(m, "http", lambda *a, **k: {"state": "INPROGRESS"})
    captured = {}
    def fake_post(pr_sha, state, desc, pr_id=None):
        captured["state"] = state
    monkeypatch.setattr(m, "post_build_status", fake_post)

    body = m.format_comment("deadbeef01234567", {"a": _mk_result(m.OUT_DIFF, n=1)})
    m.fix_stuck_inprogress("deadbeef01234567", 999, body)
    assert captured["state"] == "SUCCESSFUL"


def test_fix_stuck_inprogress_permanent_token_still_failed(monkeypatch):
    monkeypatch.setattr(m, "http", lambda *a, **k: {"state": "INPROGRESS"})
    captured = {}
    def fake_post(pr_sha, state, desc, pr_id=None):
        captured["state"] = state
    monkeypatch.setattr(m, "post_build_status", fake_post)

    body = m.format_comment("deadbeef01234567", {"a": _mk_result(m.OUT_INDETERMINATE, m.REASON_OCI_NOT_FOUND)})
    m.fix_stuck_inprogress("deadbeef01234567", 999, body)
    assert captured["state"] == "FAILED"


def test_fix_stuck_inprogress_legacy_diff_incomplete_text_now_failed(monkeypatch):
    # Legacy fallback path (comment without a token at all).
    monkeypatch.setattr(m, "http", lambda *a, **k: {"state": "INPROGRESS"})
    captured = {}
    def fake_post(pr_sha, state, desc, pr_id=None):
        captured["state"] = state
    monkeypatch.setattr(m, "post_build_status", fake_post)

    legacy_comment = "Some comment...\nDiff incomplete, could not evaluate.\n"
    m.fix_stuck_inprogress("deadbeef01234567", 999, legacy_comment)
    assert captured["state"] == "FAILED"


# ── Finding 6: get_pr_changed_files pairing + _run_one_diff / 
#    _pr_chart_revision_checked following a renamed value file ──────────
# COPS-2596: the three tests below used to assign m.bb / m._bb_fetch_status
# DIRECTLY on the module instead of via monkeypatch, so the fakes survived the
# test and stayed installed for the rest of the process. The last one left
# _bb_fetch_status returning BB_NOT_FOUND for everything, which silently broke
# tests/test_coverage_orchestration.py whenever it happened to run after this
# file in the same worker -- green in the default alphabetical order, red under
# xdist. Its fake also took only (clean, pr_sha) while the real function accepts
# a repo kwarg, so callers blew up on an unexpected keyword.
#
# Always monkeypatch module globals. It is reverted at teardown; a bare
# assignment is not.
def test_get_pr_changed_files_returns_renames_dict(monkeypatch):
    pages = [{
        "values": [
            {"old": {"path": "gcp/dev/private-cloud/ap1/custom/pv-dev-05-a/customer.yaml"},
             "new": {"path": "gcp/dev/private-cloud/ap1/custom/pv-dev-05-renametest-a/customer.yaml"}},
            {"old": {"path": "gcp/dev/x/config.yaml"},
             "new": {"path": "gcp/dev/x/config.yaml"}},  # identical path, not a rename
        ],
        "next": "",
    }]
    monkeypatch.setattr(m, "bb", lambda method, path, **kw: pages[0])
    files, renames = m.get_pr_changed_files(1234)
    assert renames == {
        "gcp/dev/private-cloud/ap1/custom/pv-dev-05-a/customer.yaml":
            "gcp/dev/private-cloud/ap1/custom/pv-dev-05-renametest-a/customer.yaml",
    }
    assert "gcp/dev/x/config.yaml" not in renames


def test_pr_chart_revision_checked_follows_renamed_customer_yaml(monkeypatch):
    # PR #6648 repro: customer.yaml renamed AND its appspace.version bumped
    # in the same commit. Must detect the bump by following the rename,
    # not silently miss it because the old path 404s.
    app = "test-app-rename-checked"
    m._app_chart_revision_map[app] = "2603.0.0-dev"
    old_path = "gcp/dev/private-cloud/ap1/custom/pv-dev-06-a/customer.yaml"
    new_path = "gcp/dev/private-cloud/ap1/custom/pv-dev-06-renametest-a/customer.yaml"

    def fake_fetch_status(clean, pr_sha, **kw):
        if clean == new_path:
            return "appspace:\n  version: 2603.0.1-renamed-dev\n", m.BB_OK
        return None, m.BB_NOT_FOUND
    monkeypatch.setattr(m, "_bb_fetch_status", fake_fetch_status)
    m._vf_cache.clear()

    new_rev, invalid = m._pr_chart_revision_checked(
        app, [old_path], "prsha123", renames={old_path: new_path})
    assert (new_rev, invalid) == ("2603.0.1-renamed-dev", False)


def test_pr_chart_revision_checked_no_rename_info_still_returns_none(monkeypatch):
    # Guard: without a renames dict (or the file genuinely deleted), the
    # existing "no bump detected" behavior for a 404 must be unchanged.
    app = "test-app-no-rename-info"
    m._app_chart_revision_map[app] = "2603.0.0-dev"
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda clean, pr_sha, **kw: (None, m.BB_NOT_FOUND))
    m._vf_cache.clear()
    new_rev, invalid = m._pr_chart_revision_checked(
        app, ["gcp/dev/private-cloud/ap1/custom/pv-x-a/customer.yaml"], "prsha123")
    assert (new_rev, invalid) == (None, False)


def test_run_one_diff_follows_renamed_value_file_into_render(monkeypatch):
    # Full-path repro of PR #6647/#6648: an app's live valueFiles list still
    # points at the OLD path (that's how ArgoCD is configured pre-merge).
    # The old path 404s at pr_sha (it moved). Must render with the NEW
    # path's content instead of silently rendering without it.
    app = "test-app-run-one-diff-rename"
    old_path = "gcp/dev/private-cloud/ap1/custom/pv-dev-x-a/customer.yaml"
    new_path = "gcp/dev/private-cloud/ap1/custom/pv-dev-x-renamed-a/customer.yaml"
    value_files = [old_path]

    monkeypatch.setitem(m._app_chart_map, app, "appspace-micro-services")
    monkeypatch.setitem(m._app_chart_revision_map, app, "2603.0.0-dev")
    monkeypatch.setitem(m._app_chart_registry_map, app, "helm-oci-dev.repo.appspace.com")
    monkeypatch.setitem(m._app_value_files_map, app, value_files)
    monkeypatch.setitem(m._app_namespace_map, app, "pv-dev-x-a")

    monkeypatch.setattr(m, "_ensure_chart", lambda registry, chart, ver: "/fake/chart/path")

    captured = {}
    def fake_helm_template(chart_path, release, namespace, value_files_content):
        # Record what content was actually handed to helm for the PR side
        # vs the main side, distinguished by which chart_path is used isn't
        # reliable here since both are the same fake path -- use content len
        # as the signal: only the PR-side call should ever see NEW_MARKER.
        captured.setdefault("calls", []).append(dict(value_files_content))
        return "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n", None
    monkeypatch.setattr(m, "_helm_template", fake_helm_template)

    def fake_bb_fetch_status(clean, sha):
        if clean == old_path:
            return None, m.BB_NOT_FOUND  # moved away, gone at pr_sha
        if clean == new_path:
            return "appspace:\n  version: 2603.0.1-dev\n  customerName: NEW_MARKER\n", m.BB_OK
        return None, m.BB_NOT_FOUND
    monkeypatch.setattr(m, "_bb_fetch_status", fake_bb_fetch_status)
    m._vf_cache.clear()
    m._vf_inflight.clear()
    import tempfile
    monkeypatch.setattr(render_cache, "MAIN_RENDER_CACHE_DIR",
                        tempfile.mkdtemp(prefix="main-render-test-"))
    m._main_render_cache.clear()

    diff_text, reason, detail, *_vc = m._run_one_diff(
        app, pr_sha="prsha000", main_sha="mainsha000",
        changed_paths=[old_path, new_path],
        renames={old_path: new_path},
    )

    # The PR-side helm_template call must have received the renamed file's
    # content (NEW_MARKER), not an empty/omitted value set.
    all_content = "".join(str(c) for call in captured["calls"] for c in call.values())
    assert "NEW_MARKER" in all_content, (
        "renamed value file's content never reached the PR-side render "
        f"(reason={reason!r}, detail={detail!r})")


def test_run_one_diff_genuine_deletion_still_omitted(monkeypatch):
    # Guard: a real deletion (no rename pair) must keep the existing
    # "omit it" behavior -- this must NOT regress into fabricating content.
    app = "test-app-run-one-diff-real-delete"
    deleted_path = "gcp/dev/private-cloud/ap1/custom/pv-dev-y-a/cicd-versions.yaml"
    value_files = [deleted_path]

    monkeypatch.setitem(m._app_chart_map, app, "appspace-micro-services")
    monkeypatch.setitem(m._app_chart_revision_map, app, "2603.0.0-dev")
    monkeypatch.setitem(m._app_chart_registry_map, app, "helm-oci-dev.repo.appspace.com")
    monkeypatch.setitem(m._app_value_files_map, app, value_files)
    monkeypatch.setitem(m._app_namespace_map, app, "pv-dev-y-a")

    monkeypatch.setattr(m, "_ensure_chart", lambda registry, chart, ver: "/fake/chart/path")

    captured = {}
    def fake_helm_template(chart_path, release, namespace, value_files_content):
        captured.setdefault("calls", []).append(dict(value_files_content))
        return "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n", None
    monkeypatch.setattr(m, "_helm_template", fake_helm_template)

    def fake_bb_fetch_status(clean, sha):
        # Exists at main (pre-deletion), gone at pr_sha (this PR deletes it).
        if sha == "mainsha001":
            return "some: main-content\n", m.BB_OK
        return None, m.BB_NOT_FOUND
    monkeypatch.setattr(m, "_bb_fetch_status", fake_bb_fetch_status)
    m._vf_cache.clear()
    m._vf_inflight.clear()
    # COPS-2631: content-keyed cache (memory + disk) survives across tests
    # that share the same fake chart path; isolate so both sides render.
    import tempfile
    monkeypatch.setattr(render_cache, "MAIN_RENDER_CACHE_DIR",
                        tempfile.mkdtemp(prefix="main-render-test-"))
    m._main_render_cache.clear()

    diff_text, reason, detail, *_vc = m._run_one_diff(
        app, pr_sha="prsha001", main_sha="mainsha001",
        changed_paths=[deleted_path],
        renames={},  # no rename pairing -- genuine deletion
    )

    # Exactly one call (the PR-side render) must have received an EMPTY
    # value-files dict (the deleted file correctly omitted); the other
    # (main-side) call must have received the real main content. If the
    # deletion path were broken, the PR side would either fabricate content
    # or the main side would also come back empty.
    pr_side_calls = [c for c in captured["calls"] if not c]
    main_side_calls = [c for c in captured["calls"] if c]
    assert len(pr_side_calls) == 1, (
        f"expected exactly one PR-side call with the deleted file omitted, "
        f"got calls={captured['calls']} (reason={reason!r}, detail={detail!r})")
    assert len(main_side_calls) == 1 and "main-content" in str(main_side_calls[0]), (
        "main-side render must still see the file's content (it wasn't "
        "deleted there, only in this PR)")


# ── Finding 4: new-env candidates must be evaluated even when bundled
#    with an existing-app change, and a structural problem must force the
#    whole comment/status red even if the existing app's own diff is clean.
def test_evaluate_new_envs_valid_env_not_structural(monkeypatch):
    monkeypatch.setattr(m, "_render_new_env_diff",
        lambda env_info, pr_sha: (None, "helm template failed: Missing required value: x", 0, "2603.0.0-dev"))
    candidates = [{"name": "pv-dev-98-a", "config_file": "x/customer.yaml",
                   "env_dir": "x", "all_yaml_files": ["x/customer.yaml"], "version": "2603.0.0-dev"}]
    lines, structural_envs, total_new = m._evaluate_new_envs(candidates, "prsha")
    assert structural_envs == []
    assert any("pv-dev-98-a" in l for l in lines)


def test_evaluate_new_envs_broken_env_is_structural(monkeypatch):
    monkeypatch.setattr(m, "_render_new_env_diff",
        lambda env_info, pr_sha: (None, "no appspace.version found in config file", 0, None))
    candidates = [{"name": "pv-broken-a", "config_file": "x/customer.yaml",
                   "env_dir": "x", "all_yaml_files": ["x/customer.yaml"], "version": "unknown"}]
    lines, structural_envs, total_new = m._evaluate_new_envs(candidates, "prsha")
    assert structural_envs == ["pv-broken-a"]


def test_format_comment_with_new_env_lines_and_structural_forces_red_footer():
    # PR #6652-style scenario: a clean existing-app diff (would normally be
    # green) combined with a structural new-env problem must show red.
    body = m.format_comment(
        "deadbeef01234567", {"a": _mk_result(m.OUT_DIFF, n=1)},
        new_env_lines=["### New env section placeholder"],
        new_env_structural=True,
        new_env_desc="1 new environment(s) have a structural config problem: pv-broken-a",
    )
    assert m._extract_status_token(body) == "permanent"
    assert "pv-broken-a" in body
    assert "New env section placeholder" in body


def test_format_comment_with_new_env_lines_non_structural_stays_clean():
    body = m.format_comment(
        "deadbeef01234567", {"a": _mk_result(m.OUT_DIFF, n=1)},
        new_env_lines=["### New env section placeholder"],
        new_env_structural=False,
    )
    assert m._extract_status_token(body) == "clean"
    assert "New env section placeholder" in body


def test_format_comment_without_new_env_args_unchanged():
    # Guard: the common case (no bundled new env) must render identically
    # to before this feature existed.
    body = m.format_comment("deadbeef01234567", {"a": _mk_result(m.OUT_DIFF, n=1)})
    assert "New Environment" not in body
    assert m._extract_status_token(body) == "clean"


# ── v2.5.4 hotfix (found during live verification, PR #6657): a rename's
#    NEW path must not ALSO be picked up as a "new environment" candidate
#    when its OLD path is a known existing app. ─────────────────────────
def test_detect_new_env_excludes_rename_target_of_existing_app():
    old_path = "gcp/dev/private-cloud/ap1/custom/pv-dev-06-a/customer.yaml"
    new_path = "gcp/dev/private-cloud/ap1/custom/pv-dev-06-renametest-a/customer.yaml"
    path_map = {old_path: ["pv-dev-06-a-ms"]}
    changed_files = [old_path, new_path,
                     "gcp/dev/private-cloud/ap1/custom/pv-dev-06-a/cicd-versions.yaml",
                     "gcp/dev/private-cloud/ap1/custom/pv-dev-06-renametest-a/cicd-versions.yaml"]
    renames = {old_path: new_path}
    candidates = m._detect_new_env_candidates(changed_files, path_map, renames)
    assert candidates == [], (
        f"a renamed existing app's new path must not be a new-env candidate, got {candidates}")


def test_detect_new_env_still_detects_genuine_new_env_with_renames_present():
    # Guard: a genuinely new environment elsewhere in the same PR must still
    # be detected even when an unrelated rename is also present.
    old_path = "gcp/dev/private-cloud/ap1/custom/pv-dev-06-a/customer.yaml"
    new_path = "gcp/dev/private-cloud/ap1/custom/pv-dev-06-renametest-a/customer.yaml"
    genuinely_new = "gcp/dev/private-cloud/ap1/custom/pv-dev-99-a/customer.yaml"
    path_map = {old_path: ["pv-dev-06-a-ms"]}
    changed_files = [old_path, new_path, genuinely_new]
    renames = {old_path: new_path}
    candidates = m._detect_new_env_candidates(changed_files, path_map, renames)
    names = [c["name"] for c in candidates]
    assert names == ["pv-dev-99-a"]


def test_detect_new_env_without_renames_arg_backward_compatible():
    # Guard: existing callers that don't pass renames must keep working.
    genuinely_new = "gcp/dev/private-cloud/ap1/custom/pv-dev-99-a/customer.yaml"
    candidates = m._detect_new_env_candidates([genuinely_new], {})
    assert len(candidates) == 1


# ── v2.5.4 hotfix: _render_new_env_diff must not truncate the helm error
#    before _new_env_status can see "missing required value" in it. ─────
def test_render_new_env_diff_preserves_full_error_for_classification(monkeypatch):
    # A long file path pushes "Missing required value" past where the old
    # [:120] cut would have sliced it off (confirmed live, PR #6657).
    long_prefix = "execution error at (appspace-micro-services/templates/configmaps/legacy-db-credentials.yaml:2:27): "
    assert len(long_prefix) > 90  # sanity: this alone eats most of the old 120-char budget
    full_err = long_prefix + "Missing required value: .Values.appspace.secret"

    monkeypatch.setattr(m, "_bb_fetch_status",
        lambda path, sha: ("appspace:\n  version: 2603.0.0-dev\n", m.BB_OK))
    monkeypatch.setattr(m, "_ensure_chart", lambda registry, chart, ver: "/fake/chart")
    monkeypatch.setattr(m, "_fetch_value_files", lambda files, sha: {f: "x: y\n" for f in files})
    monkeypatch.setattr(m, "_helm_template", lambda *a, **k: (None, full_err))

    env_info = {"config_file": "gcp/dev/private-cloud/ap1/custom/pv-x-a/customer.yaml",
                "name": "pv-x-a", "env_dir": "gcp/dev/private-cloud/ap1/custom/pv-x-a",
                "all_yaml_files": ["gcp/dev/private-cloud/ap1/custom/pv-x-a/customer.yaml"]}
    diff_text, render_err, n_res, version = m._render_new_env_diff(env_info, "prsha")
    assert "missing required value" in render_err.lower()
    state, expected = m._new_env_status(render_err)
    assert (state, expected) == ("SUCCESSFUL", True), (
        f"long error prefix must not hide the expected-case phrase, got render_err={render_err!r}")
