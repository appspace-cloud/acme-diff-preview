"""Targeted tests for every remaining uncovered line (v2.25.x sweep).

Each test below exists to pin a specific, previously-untested branch found
by a coverage audit at 98%: early-return guards, swallowed-exception paths,
mirror/cache fallbacks, and orchestrator side branches. Grouped by the
function they exercise.
"""
import os, sys, threading
import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m


# ── _sha_eq ──────────────────────────────────────────────────────────────────

def test_sha_eq_empty_side_is_false():
    assert m._sha_eq("", "abc") is False
    assert m._sha_eq("abc", "") is False


# ── _record_supersede_hint: total exception swallow ─────────────────────────

def test_record_supersede_hint_swallows_storage_failure(monkeypatch):
    class Boom(dict):
        def __setitem__(self, k, v):
            raise MemoryError("simulated pressure on the wake path")
    monkeypatch.setattr(m, "_pr_superseded", Boom())
    # must not raise, must not log; silence IS the contract here
    m._record_supersede_hint("acme-config-dev", 1, "a" * 12)


# ── _maybe_record_supersede_hint: malformed webhook payloads ────────────────

def _hint_env(monkeypatch):
    monkeypatch.setattr(m, "SUPERSEDE_ABORT_ENABLED", True)
    monkeypatch.setattr(m, "BB_WEBHOOK_SECRET", "s3cret")

def test_hint_rejects_boolean_pr_id(monkeypatch):
    _hint_env(monkeypatch)
    body = b'{"pullrequest": {"id": true}, "repository": {}}'
    assert m._maybe_record_supersede_hint("pullrequest:updated", body) is None

def test_hint_rejects_missing_sha(monkeypatch):
    _hint_env(monkeypatch)
    body = b'{"pullrequest": {"id": 5, "source": {}}, "repository": {}}'
    assert m._maybe_record_supersede_hint("pullrequest:updated", body) is None

def test_hint_rejects_full_name_without_slash(monkeypatch):
    _hint_env(monkeypatch)
    body = (b'{"pullrequest": {"id": 5, "source": {"commit": {"hash": "abc123"}}},'
            b' "repository": {"full_name": "noslash"}}')
    assert m._maybe_record_supersede_hint("pullrequest:updated", body) is None


# ── _resolve_git_credential: falsy candidate is skipped ─────────────────────

def test_git_credential_skips_falsy_candidates(monkeypatch):
    monkeypatch.setattr(m, "_git_credential_resolved", False)
    monkeypatch.setattr(m, "_GIT_USER_CANDIDATES", ["", "real-user"])
    calls = []
    class R:  # ls-remote succeeds for the real user
        returncode = 0
        stderr = ""
    monkeypatch.setattr(m, "_git_run",
                        lambda args, timeout=60, auth_header=None:
                        calls.append(auth_header) or R())
    monkeypatch.setattr(m, "_git_auth_header", lambda u: f"hdr-{u}")
    m._resolve_git_credential("https://example.invalid/x.git")
    assert calls == ["hdr-real-user"]      # the empty candidate never probed
    assert m._git_credential_resolved is True


# ── mirror_sync: disabled / mkdir failure / fetch failure / fetch success ───

class _GitR:
    def __init__(self, rc, stderr=""):
        self.returncode, self.stderr = rc, stderr

def _mirror_env(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "GIT_MIRROR_ENABLED", True)
    monkeypatch.setattr(m, "_mirror_disabled", False)
    monkeypatch.setattr(m, "GIT_MIRROR_DIR", str(tmp_path))
    monkeypatch.setattr(m, "_mirror_ready", {})
    monkeypatch.setattr(m, "_resolve_git_credential", lambda url: None)

def test_mirror_sync_disabled_is_a_noop(monkeypatch):
    monkeypatch.setattr(m, "GIT_MIRROR_ENABLED", False)
    assert m.mirror_sync("acme-config-dev") is None

def test_mirror_sync_mkdir_failure_falls_back(monkeypatch, tmp_path):
    _mirror_env(monkeypatch, tmp_path)
    def boom(path, exist_ok=False):
        raise PermissionError("read-only fs")
    monkeypatch.setattr(m.os, "makedirs", boom)
    assert m.mirror_sync("acme-config-dev") is None

def test_mirror_sync_clone_ok_then_fetch_fails(monkeypatch, tmp_path):
    _mirror_env(monkeypatch, tmp_path)
    seen = []
    def fake_git(args, **kw):
        seen.append(args[0])
        if args[0] == "clone":
            # simulate a successful clone: the objects dir appears
            os.makedirs(os.path.join(args[-1], "objects"), exist_ok=True)
            return _GitR(0)
        return _GitR(1, "remote hung up")          # the fetch fails
    monkeypatch.setattr(m, "_git_run", fake_git)
    m.mirror_sync("acme-config-dev")
    assert seen[0] == "clone" and "--git-dir" in str(seen)
    assert m._mirror_ready.get("acme-config-dev") is True   # clone marked ready

def test_mirror_sync_fetch_success_purges_sha_presence_cache(monkeypatch, tmp_path):
    _mirror_env(monkeypatch, tmp_path)
    path = m._mirror_path("acme-config-dev")
    os.makedirs(os.path.join(path, "objects"), exist_ok=True)   # already cloned
    monkeypatch.setattr(m, "_git_run", lambda args, **kw: _GitR(0))
    monkeypatch.setattr(m, "_mirror_sha_seen",
                        {("acme-config-dev", "aaa"): False,
                         ("acme-config-prod", "bbb"): False})
    m.mirror_sync("acme-config-dev")
    assert ("acme-config-dev", "aaa") not in m._mirror_sha_seen
    assert ("acme-config-prod", "bbb") in m._mirror_sha_seen
    assert m._mirror_ready.get("acme-config-dev") is True


# ── _git_read_file: git binary not runnable ─────────────────────────────────

def test_git_read_file_returns_none_when_git_not_runnable(monkeypatch, tmp_path):
    _mirror_env(monkeypatch, tmp_path)
    path = m._mirror_path("acme-config-dev")
    os.makedirs(os.path.join(path, "objects"), exist_ok=True)
    monkeypatch.setattr(m, "_mirror_has_sha", lambda repo, sha: True)
    monkeypatch.setattr(m, "_git_run", lambda args, timeout=30: None)
    assert m._git_read_file("acme-config-dev", "a" * 12, "x/customer.yaml") is None


# ── _start_oci_selfcheck_loop: disabled interval ────────────────────────────

@pytest.mark.no_thread_stub
def test_oci_selfcheck_disabled_interval_returns_immediately(monkeypatch):
    monkeypatch.setattr(m, "OCI_SELFCHECK_INTERVAL", 0)
    before = threading.active_count()
    assert m._start_oci_selfcheck_loop() is None
    assert threading.active_count() == before


# ── _bb_fetch_cached: singleflight non-fetcher paths ────────────────────────

class _EventFillsCache:
    """wait() simulates the fetcher completing while we were blocked."""
    def __init__(self, key, content):
        self.key, self.content = key, content
    def wait(self, timeout=None):
        with m._vf_cache_lock:
            m._vf_cache[self.key] = self.content
        return True

class _EventNoResult:
    def wait(self, timeout=None):
        return False           # fetcher timed out / transient error

def test_bb_fetch_cached_waiter_reads_fetchers_result(monkeypatch):
    key = ("shaX", "w/one.yaml")
    monkeypatch.setattr(m, "_vf_cache", {})
    monkeypatch.setattr(m, "_vf_inflight", {key: _EventFillsCache(key, "CONTENT")})
    c, st = m._bb_fetch_cached("w/one.yaml", "shaX")
    assert (c, st) == ("CONTENT", m.BB_OK)

def test_bb_fetch_cached_waiter_falls_back_to_own_fetch(monkeypatch):
    key = ("shaY", "w/two.yaml")
    monkeypatch.setattr(m, "_vf_cache", {})
    monkeypatch.setattr(m, "_vf_inflight", {key: _EventNoResult()})
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda fp, sha, **kw: ("OWN", m.BB_OK))
    assert m._bb_fetch_cached("w/two.yaml", "shaY") == ("OWN", m.BB_OK)


# ── _cap_helm_error: overflow note ──────────────────────────────────────────

def test_cap_helm_error_appends_more_lines_note():
    err = "\n".join(f"- at '/spec/x{i}': wrong type" for i in range(m._SCHEMA_ERROR_MAX_LINES + 7))
    out = m._cap_helm_error(err)
    assert "and 7 more lines" in out


# ── _redact_rendered_manifest: Secret without a data section ────────────────

def test_redact_manifest_secret_without_data_uses_keyname_redaction():
    doc = ("kind: Secret\napiVersion: v1\nmetadata:\n  name: empty-secret\n"
           "type: Opaque\n")
    out = m._redact_rendered_manifest(doc)
    assert "empty-secret" in out and "kind: Secret" in out


# ── _resolve_effective_pr_chart_revision ────────────────────────────────────

def test_effective_revision_none_without_value_files(monkeypatch):
    monkeypatch.setattr(m, "_app_value_files_map", {})
    assert m._resolve_effective_pr_chart_revision("appx", "sha") is None

def test_effective_revision_none_when_value_fetch_raises(monkeypatch):
    monkeypatch.setattr(m, "_app_value_files_map", {"appx": ["$config/a/customer.yaml"]})
    def boom(files, sha):
        raise RuntimeError("bitbucket flaked")
    monkeypatch.setattr(m, "_fetch_value_files", boom)
    assert m._resolve_effective_pr_chart_revision("appx", "sha") is None

def _rename_env(monkeypatch):
    monkeypatch.setattr(m, "_detect_env_move", lambda *a, **k: None)
    monkeypatch.setattr(m, "_trusted_rename_dirs", lambda *a, **k: set())
    monkeypatch.setattr(m, "_effective_chart_version", lambda vfs, vals: "9.9.9")

def test_effective_revision_fills_renamed_leaf_via_fetch(monkeypatch):
    _rename_env(monkeypatch)
    vf = "$config/a/customer.yaml"
    monkeypatch.setattr(m, "_app_value_files_map", {"appx": [vf]})
    monkeypatch.setattr(m, "_fetch_value_files", lambda files, sha: {vf: None})
    monkeypatch.setattr(m, "_is_trusted_rename", lambda *a, **k: True)
    monkeypatch.setattr(m, "_vf_cache", {})
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda fp, sha, **kw: ("appspace:\n  version: 9.9.9\n", m.BB_OK))
    renames = {"a/customer.yaml": "b/customer.yaml"}
    out = m._resolve_effective_pr_chart_revision("appx", "prsha",
                                                 main_sha="mainsha", renames=renames)
    assert out == "9.9.9"
    assert m._vf_cache[("prsha", "b/customer.yaml")]      # fetch was cached

def test_effective_revision_rename_fill_cached_and_skip_branches(monkeypatch):
    _rename_env(monkeypatch)
    vf_ok      = "$config/filled/customer.yaml"     # already has a value
    vf_norn    = "$config/plain/customer.yaml"      # not a rename old-side
    vf_untrust = "$config/u/customer.yaml"          # rename exists, untrusted
    vf_cached  = "$config/c/customer.yaml"          # rename target already cached
    monkeypatch.setattr(m, "_app_value_files_map",
                        {"appx": [vf_ok, vf_norn, vf_untrust, vf_cached]})
    monkeypatch.setattr(m, "_fetch_value_files",
                        lambda files, sha: {vf_ok: "appspace: {}", vf_norn: None,
                                            vf_untrust: None, vf_cached: None})
    monkeypatch.setattr(m, "_is_trusted_rename",
                        lambda old, new, *a, **k: not old.startswith("u/"))
    monkeypatch.setattr(m, "_vf_cache",
                        {("prsha", "c2/customer.yaml"): "appspace:\n  version: 9.9.9\n"})
    renames = {"u/customer.yaml": "u2/customer.yaml",
               "c/customer.yaml": "c2/customer.yaml"}
    out = m._resolve_effective_pr_chart_revision("appx", "prsha",
                                                 main_sha="mainsha", renames=renames)
    assert out == "9.9.9"


# ── _section_name / _is_rename_of edge shapes ───────────────────────────────

def test_section_name_header_without_space_is_empty():
    assert m._section_name("headerwithoutspace") == ""

def test_is_rename_of_identical_names_is_false():
    assert m._is_rename_of("app aa/Deployment/same-name",
                           "app bb/Deployment/same-name") is False


# ── helm error explainers ───────────────────────────────────────────────────

def test_explain_required_error_generic_value_block_head():
    err = ('template: chart/templates/cm.yaml:15:124: executing "x" at '
           '<.Values.global.zone>: nil pointer evaluating interface {}.zone')
    out = "\n".join(m._explain_required_error(err))
    assert "that value block is missing or empty" in out

def test_explain_schema_error_without_violation_bullets():
    out = m._explain_schema_error("something exploded\nwith no bullets")
    assert out == ["> something exploded"]


# ── _augment_renames_with_identity_moves: old-side fetch/identity misses ───

def test_identity_moves_skip_unreadable_or_anonymous_old_side(monkeypatch):
    base, pr = "mainsha", "prsha"
    old_gone  = "gcp/dev/pc/ap1/custom/pv-gone-a/customer.yaml"
    old_anon  = "gcp/dev/pc/ap1/custom/pv-anon-a/customer.yaml"
    added     = "gcp/dev/pc/ap1/custom/pv-new-a/customer.yaml"
    table = {
        (old_gone, pr): (None, m.BB_NOT_FOUND),
        (old_anon, pr): (None, m.BB_NOT_FOUND),
        (added, pr): ("appspace:\n  customerName: pv-new\n", m.BB_OK),
        (old_gone, base): (None, m.BB_ERROR),               # unreadable at main
        (old_anon, base): ("plainkey: 1\n", m.BB_OK),       # declares no identity
    }
    monkeypatch.setattr(m, "_bb_fetch_cached",
                        lambda f, sha, repo=None: table[(f, sha)])
    out = m._augment_renames_with_identity_moves(
        [old_gone, old_anon, added], {}, {old_gone: ["a"], old_anon: ["b"]},
        base, pr)
    assert out == {}     # neither old side could be paired


# ── _cascade_retention_reason: non key:value lines are skipped ──────────────

def test_cascade_retention_skips_bare_lines_then_matches_policy():
    doc = "justaword\nhelm.sh/resource-policy: keep\n"
    assert m._cascade_retention_reason("v1/ConfigMap", doc) == m._CASCADE_KEEP_POLICY_REASON


# ── _format_app_diff_block: header-only mode ────────────────────────────────

def test_format_app_diff_block_without_diff_body():
    out = m._format_app_diff_block("appx", [], "", show_diff=False, n_res=3)
    assert out[0].startswith("\u26a0\ufe0f") and len(out) == 2


# ── _summarize_appspace_state_changes edge branches ─────────────────────────

_ID = "gcp/dev/pc/ap1/custom/pv-flags-a/customer.yaml"

def _flags_fetch(monkeypatch, old_txt, new_txt):
    table = {(_ID, "prsha"): (new_txt, m.BB_OK), (_ID, "mainsha"): (old_txt, m.BB_OK)}
    monkeypatch.setattr(m, "_bb_fetch_cached",
                        lambda f, sha, repo=None: table[(f, sha)])

def test_state_changes_duplicate_file_reported_once_and_purge_removal(monkeypatch):
    _flags_fetch(monkeypatch,
                 "appspace:\n  decommission: true\n  decommissionPurgeData: true\n",
                 "appspace:\n  decommission: true\n")
    lines = m._summarize_appspace_state_changes(
        [_ID, _ID], "prsha", "mainsha", {_ID: ["pv-flags-a-ms"]})
    body = "\n".join(lines)
    assert body.count("decommissionPurgeData") == 1     # duplicate path deduped
    assert "no longer purged" in body

def test_state_changes_unparseable_yaml_is_skipped(monkeypatch):
    _flags_fetch(monkeypatch, "::\nnot: [valid", "::\nnot: [valid")
    assert m._summarize_appspace_state_changes(
        [_ID], "prsha", "mainsha", {_ID: ["pv-flags-a-ms"]}) == []


# ── format_comment: renamed-resources overflow note ─────────────────────────

def test_format_comment_caps_renamed_list_at_ten():
    pairs = [(f"app x/Job/name-old{i}", f"app x/Job/name-new{i}") for i in range(11)]
    res = m.DiffResult("--- a\n+++ b", [("x/Job/n", "-a\n+b")], 1, True, "",
                       m.OUT_DIFF, "", renamed_resources=pairs)
    body = m.format_comment("c" * 12, {"appx": res})
    assert "11 resource(s) RENAMED" in body
    assert "*(+1 more)*" in body


# ── process_pr orchestrator side branches ───────────────────────────────────

_PM_ID   = "gcp/dev/pc/ap1/custom/pv-orch-x/customer.yaml"
_PATHMAP = {_PM_ID: ["pv-orch-x-ms"]}
_PR_SHA  = "aabbccddeeff0011"
_BASE    = "112233445566mm77"

def _mk_pr(pr_id):
    return {"id": pr_id, "title": "synthetic",
            "source": {"commit": {"hash": _PR_SHA}, "branch": {"name": "b"}},
            "destination": {"branch": {"name": "main"}}}

def _orch(monkeypatch):
    """Common harness: stub every network-touching edge of process_pr."""
    m._seen.clear(); m._force_recompute.clear()
    sinks = {"upserts": [], "statuses": [], "artifacts": []}
    m._app_chart_map.update({"pv-orch-x-ms": "appspace-ms"})
    m._app_chart_revision_map.update({"pv-orch-x-ms": "2603.0.1-dev"})
    monkeypatch.setattr(m, "get_pr_changed_files", lambda pr_id, repo=None: ([_PM_ID], {}))
    monkeypatch.setattr(m, "find_existing_comment", lambda pr_id, repo=None: (None, "", ""))
    monkeypatch.setattr(m, "fix_stuck_inprogress", lambda *a, **k: None)
    monkeypatch.setattr(m, "_touch_progress", lambda: None)
    monkeypatch.setattr(m, "upsert_comment",
                        lambda pr_id, body, existing_id=None, repo=None:
                        sinks["upserts"].append(body) or 1)
    monkeypatch.setattr(m, "post_build_status",
                        lambda pr_sha, state, description, pr_id=None, repo=None:
                        sinks["statuses"].append((state, description)))
    monkeypatch.setattr(m, "_save_diff_ui_artifact",
                        lambda *a, **k: sinks["artifacts"].append(1))
    monkeypatch.setattr(m, "_detect_env_decommission_candidates", lambda *a, **k: [])
    monkeypatch.setattr(m, "_detect_new_env_candidates", lambda *a, **k: [])
    monkeypatch.setattr(m, "_summarize_input_changes", lambda *a, **k: [])
    monkeypatch.setattr(m, "_changed_files_with_bad_names", lambda *a, **k: {})
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda fp, sha, **kw: ("appspace:\n  customerName: orch-x\n", m.BB_OK))
    monkeypatch.setattr(m, "argocd_diff",
                        lambda app, pr_sha, main_sha, chart_revision=None,
                               changed_paths=None, renames=None:
                        m.DiffResult("", [], 0, False, "", m.OUT_NO_DIFF, ""))
    return sinks

def test_process_pr_transient_backoff_skips_the_run(monkeypatch):
    sinks = _orch(monkeypatch)
    monkeypatch.setattr(m, "_backoff_should_skip", lambda sk, pr_sha: True)
    try:
        m.process_pr(_mk_pr(9001), _PATHMAP, base_sha=_BASE)
    finally:
        m._seen.clear(); m._force_recompute.clear()
    assert sinks["upserts"] == [] and sinks["artifacts"] == []

def test_process_pr_blocks_apps_with_invalid_customer_name(monkeypatch):
    sinks = _orch(monkeypatch)
    monkeypatch.setattr(m, "_changed_files_with_bad_names",
                        lambda *a, **k: {_PM_ID: "customerName too long for GCP"})
    try:
        m.process_pr(_mk_pr(9002), _PATHMAP, base_sha=_BASE)
    finally:
        m._seen.clear(); m._force_recompute.clear()
    body = sinks["upserts"][0]
    # the app is blocked as a permanent indeterminate, never a green no-diff
    assert "pv-orch-x-ms" in body and "[permanent]" in body
    assert "could not be evaluated" in body
    assert sinks["statuses"][-1][0] == "FAILED"

def test_process_pr_moves_missing_cohort_and_state_panel_failure(monkeypatch):
    sinks = _orch(monkeypatch)
    monkeypatch.setattr(m, "_moves_missing_cohort",
                        lambda renames, pr_sha, repo=None: [{"env": "pv-moved-a"}])
    monkeypatch.setattr(m, "_moves_missing_cohort_lines",
                        lambda blocks: ["MOVED_COHORT_SENTINEL"])
    def boom(*a, **k):
        raise RuntimeError("panel exploded")
    monkeypatch.setattr(m, "_summarize_appspace_state_changes", boom)
    try:
        m.process_pr(_mk_pr(9003), _PATHMAP, base_sha=_BASE)
    finally:
        m._seen.clear(); m._force_recompute.clear()
    body = sinks["upserts"][0]
    assert "MOVED_COHORT_SENTINEL" in body       # spliced despite the panel failure
    state, desc = sinks["statuses"][-1]
    assert state == "FAILED" and "moved environment(s)" in desc
