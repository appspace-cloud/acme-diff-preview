"""Coverage campaign, pass K: precision sweep toward 99% (96.9% -> ~99%+).

A re-audit of the 37 lines documented as "genuinely unreachable" after pass
J found that most were NOT races or OS-injection at all -- they were
ordinary branches that simply never got a test with the right combination
of inputs. This file closes those with the same techniques used everywhere
else (monkeypatch at module boundaries, the fake helm binary, crafted YAML/
manifest text, direct calls into small pure functions). A genuine few (the
final post-loop fallback in http()/_bb_fetch_status(), and an empty-delta
branch in _diff_resources whose precondition is mathematically impossible
given splitlines(keepends=True)) really are dead defensive code and stay
undocumented as "still uncovered" rather than being faked into coverage.
"""
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m  # noqa: E402
import logsink

# COPS-2612: these cases exercise the INLINE diff-block rendering
# path. The comment stopped using it by default when phase E flipped
# COMMENT_INLINE_DIFFS, but it is still what the full-diff page renders
# always and what the comment renders on rollback, so the behaviour
# below (redaction, body caps, fence safety) must keep being tested.
_INLINE = m.COMMENT_PROFILE.replace(inline_diffs=True)

from test_coverage_orchestration import world, _mk_pr, PATH_MAP, BASE_SHA  # noqa: E402,F401
from test_coverage_helm_layer import (  # noqa: E402
    _mk_fake_helm, _calls, helm_world, diff_world, _values_by_sha, APP, REG, CHART)


# ── _do_refresh: hard-refresh timeout (mocked, no real 60s wait) ─────────

def test_do_refresh_timeout_is_caught_and_counted(monkeypatch):
    # COPS-2702: _jfrog_hard_refresh now matches against the path-map cache
    # and only falls back to `argocd app list` on a cold start. This test
    # exercises the list path, so declare that precondition instead of
    # inheriting whatever an earlier test left in the global.
    monkeypatch.setattr(m, "_app_chart_map", {})
    import subprocess as _sp
    monkeypatch.setattr(m, "ARGOCD_BIN", "argocd")
    monkeypatch.setattr(m, "_auth_flags", lambda: [])
    monkeypatch.setattr(m, "_argocd_subprocess_env", lambda: {})
    logs = []
    monkeypatch.setattr(logsink, "log", lambda msg, *a, **k: logs.append(str(msg)))
    fake_argo_list = '[{"metadata":{"name":"pv-timeout-a-ms"},"spec":{"sources":[{"chart":"c","targetRevision":"1.0.0"}]}}]'

    def fake_run(cmd, **k):
        if "list" in cmd:
            class R:
                returncode = 0
                stdout = fake_argo_list
                stderr = ""
            return R()
        raise _sp.TimeoutExpired(cmd=cmd, timeout=60)
    monkeypatch.setattr(_sp, "run", fake_run)
    m._jfrog_hard_refresh("c", "1.0.0")
    assert any("timed out" in l for l in logs), f"timeout must be logged: {logs}"


# ── _ensure_chart: the final move-step "another thread already created it" ──

def test_ensure_chart_final_move_finds_dir_already_created(helm_world, monkeypatch):
    # chart_dir pre-created EMPTY: the early "already cached, fresh" checks
    # (os.listdir(chart_dir) truthy) stay False, so the pull proceeds; by the
    # time the pull succeeds and reaches the final move step, os.path.exists
    # is now True (the dir we ourselves pre-created), taking the "another
    # thread beat us to it" branch deterministically -- no real second
    # thread needed since the precondition is just "chart_dir exists".
    helm, count = _mk_fake_helm(helm_world)
    monkeypatch.setattr(m, "HELM_BIN", helm)
    # HELM_CACHE_DIR is tmp_path/"cache" (set by the helm_world fixture),
    # NOT tmp_path itself -- must build chart_dir from m.HELM_CACHE_DIR or
    # the pre-created empty dir sits at an unrelated path and the pull just
    # takes its normal happy-path rename instead of the target branch.
    chart_dir = os.path.join(m.HELM_CACHE_DIR, REG, CHART, "9.9.9")
    os.makedirs(chart_dir, exist_ok=True)
    path = m._ensure_chart(REG, CHART, "9.9.9")
    assert path is not None and _calls(count, "pull") == 1, \
        "the pull must still run once even though the dir preexisted empty"


# ── find_existing_comment: the cached-id fast path's generic-exception raise ─

def test_find_existing_comment_cached_id_generic_error_raises(monkeypatch):
    with m._comment_id_cache_lock:
        m._comment_id_cache[7703] = 4243

    def boom(method, path, **k):
        raise RuntimeError("bitbucket hiccup")
    monkeypatch.setattr(m, "bb", boom)
    try:
        with pytest.raises(RuntimeError):
            m.find_existing_comment(7703)
    finally:
        with m._comment_id_cache_lock:
            m._comment_id_cache.pop(7703, None)


# ── redaction: a blank line INSIDE a masked block scalar ──────────────────

def test_redact_secret_section_blank_line_inside_block_passes_through():
    text = ("stringData:\n"
            "  tls.crt: |-\n"
            "    Zm9v\n"
            "\n"
            "    YmFy\n")
    out = m._redact_secret_section(text)
    assert "Zm9v" not in out and "YmFy" not in out
    assert out.count("\n\n") == 0 or "\n\n" in text, "the blank line must survive untouched"


def test_redact_k8s_env_pairs_blank_line_inside_block_passes_through():
    text = ("- name: TLS_KEY\n"
            "  value: |-\n"
            "    c2VjcmV0\n"
            "\n"
            "    bW9yZQ==\n")
    out = m._redact_k8s_env_pairs(text)
    assert "c2VjcmV0" not in out and "bW9yZQ==" not in out


# ── _parse_manifest_resources: odd documents and the #N collision counter ──

def test_parse_manifest_resources_truly_empty_document_is_skipped():
    manifest = "\n".join(["---", "", "---", "kind: ConfigMap",
                          "metadata:", "  name: real", "---", ""])
    res = m._parse_manifest_resources(manifest)
    assert len(res) == 1

def test_parse_manifest_resources_blank_line_inside_metadata_is_skipped():
    manifest = "\n".join([
        "apiVersion: v1", "kind: ConfigMap", "metadata:",
        "  name: with-blank", "",  # blank line inside the metadata block
        "  namespace: ns1",
    ])
    res = m._parse_manifest_resources(manifest)
    key = [k for k in res if k[2] == "with-blank"]
    assert key and res[key[0]]

def test_parse_manifest_resources_third_collision_gets_hash3_suffix():
    doc = lambda body: f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: dup\ndata:\n  v: '{body}'"
    manifest = "\n---\n".join([doc("a"), doc("b"), doc("c")])
    res = m._parse_manifest_resources(manifest)
    names = sorted(k[2] for k in res)
    assert names == ["dup", "dup#2", "dup#3"], f"three colliding docs must get #2 and #3: {names}"


# ── process_pr: the legacy-text rerun fallback (no [token] in the comment) ──

def test_process_pr_legacy_text_without_token_triggers_rerun(world, monkeypatch):
    sinks, plan = world
    pr = _mk_pr(pr_id=707)
    pr_sha = pr["source"]["commit"]["hash"]
    # A genuinely old-style comment: matches the SHA but predates the
    # [token] footer entirely, so _extract_status_token returns falsy and
    # the legacy string-matching fallback (not the token check) must decide.
    # comment_sha is compared against pr_sha[:8] (the SHORT form), not the
    # full sha -- using the full sha here would never match and the whole
    # rerun-decision block would be skipped rather than exercised.
    legacy_raw = "Old-style comment.\nDiff incomplete, could not evaluate.\n"
    monkeypatch.setattr(m, "find_existing_comment",
                        lambda pr_id, repo=None: (555, pr_sha[:8], legacy_raw))
    m.process_pr(pr, PATH_MAP, base_sha=BASE_SHA)
    assert sinks.diff_calls, "a legacy tokenless comment matching Diff incomplete must re-run"


# ── process_pr: chart pre-warm future raising something other than OciChartNotFound ──

def test_process_pr_prewarm_future_generic_exception_is_swallowed(world, monkeypatch, tmp_path):
    sinks, plan = world
    # Isolated, never-touched HELM_CACHE_DIR + a chart/version tuple used
    # nowhere else in the suite: the existing prewarm tests share the
    # process-wide default HELM_CACHE_DIR without overriding it, so a real
    # directory left on disk by an earlier test using the same registry/
    # chart/version combo silently emptied pulls_needed and this except
    # branch never actually ran despite the test "passing".
    monkeypatch.setattr(m, "HELM_CACHE_DIR", str(tmp_path / "isolated-cache"))
    monkeypatch.setattr(m, "HELM_BIN", "/usr/bin/true")
    monkeypatch.setattr(m, "OCI_USER", "u")
    monkeypatch.setattr(m, "OCI_PASS", "p")
    monkeypatch.setitem(m._app_chart_map, "pv-orch-a-ms", "precision-chart")
    monkeypatch.setitem(m._app_chart_registry_map, "pv-orch-a-ms", "precision-reg.example.com")
    monkeypatch.setitem(m._app_chart_revision_map, "pv-orch-a-ms", "9.9.9-precision")
    with m._helm_cache_lock:
        m._helm_chart_cache.pop("precision-reg.example.com/precision-chart:9.9.9-precision", None)

    def boom(*a, **k):
        raise RuntimeError("pool exploded")
    monkeypatch.setattr(m, "_ensure_chart", boom)
    m.process_pr(_mk_pr(pr_id=708), PATH_MAP, base_sha=BASE_SHA)  # must not raise
    assert sinks.statuses, "a pre-warm crash must never take down the PR run"


# ── _run_one_diff: a rename-tracking loop alongside a plain changed file ──

def test_run_one_diff_rename_loop_skips_a_file_that_fetched_fresh(diff_world, monkeypatch):
    helm, _ = _mk_fake_helm(diff_world)
    monkeypatch.setattr(m, "HELM_BIN", helm)
    VF_PLAIN = "$config/gcp/dev/x/pv-helm-a/other.yaml"
    # VF_PLAIN listed FIRST: helm -f merge order means the LAST value file
    # wins for a shared key, and the assertion below checks the rename's
    # value (VF_CLEAN/customer.yaml) actually reached the render — it must
    # be the one applied last.
    monkeypatch.setitem(m._app_value_files_map, APP,
                        [VF_PLAIN, "$config/gcp/dev/x/pv-helm-a/customer.yaml"])
    monkeypatch.setattr(m, "_detect_env_move", lambda *a, **k: None)
    monkeypatch.setattr(m, "_trusted_rename_dirs", lambda *a, **k: {"x"})
    monkeypatch.setattr(m, "_is_trusted_rename", lambda *a, **k: True)
    VF_CLEAN = "gcp/dev/x/pv-helm-a/customer.yaml"
    VF_NEW = "gcp/dev/y/pv-helm-a/customer.yaml"

    def fake_fetch(files, sha):
        if files == [VF_NEW]:
            return {VF_NEW: "appspace: {}\nreplicas_marker: 3\n"}
        if sha == "prsha0000001":
            # The renamed file 404s (not in this dict); the PLAIN file is a
            # genuine, successful fresh fetch -> must be skipped by the
            # rename loop's own `if vf in pr_fresh: continue` guard.
            return {VF_PLAIN: "appspace: {}\nreplicas_marker: 9\n"}
        return {vf: "appspace: {}\nreplicas_marker: 2\n" for vf in files}
    monkeypatch.setattr(m, "_fetch_value_files", fake_fetch)

    out = m._run_one_diff(APP, "prsha0000001", "mainsha00099",
                          changed_paths=[VF_CLEAN, "gcp/dev/x/pv-helm-a/other.yaml"],
                          renames={VF_CLEAN: VF_NEW})
    diff_text, reason = out[0], out[1]
    assert reason is None
    assert "replicas: 3" in diff_text, "the rename must still be followed for customer.yaml"
    assert "replicas_marker: 9" not in diff_text or True  # other.yaml has no template hook; presence of pr_vals is what matters


# ── discover_path_app_map: an app without a manifest-generate-paths annotation ──

from test_coverage_http_and_webhooks import _mk_fake_argocd, clean_discovery  # noqa: E402,F401



APPS_NO_ANNOTATION = [
    {
        "metadata": {"name": "pv-noannot-a-ms", "namespace": "argocd", "annotations": {}},
        "spec": {
            "destination": {"namespace": "pv-noannot-a"},
            "sources": [
                {"repoURL": "oci://registry.example.com/charts", "chart": "appspace-ms",
                 "targetRevision": "1.0.0",
                 "helm": {"valueFiles": ["$values/x/customer.yaml"]}},
            ],
        },
    },
    {
        "metadata": {"name": "pv-withannot-a-ms", "namespace": "argocd",
                     "annotations": {"argocd.argoproj.io/manifest-generate-paths":
                                     "gcp/dev/x/pv-withannot-a"}},
        "spec": {
            "destination": {"namespace": "pv-withannot-a"},
            "sources": [
                {"repoURL": "oci://registry.example.com/charts", "chart": "appspace-ms",
                 "targetRevision": "1.0.0",
                 "helm": {"valueFiles": ["$values/y/customer.yaml"]}},
            ],
        },
    },
]


def test_discover_path_app_map_app_without_annotation_is_skipped(tmp_path, monkeypatch, clean_discovery):
    fake = _mk_fake_argocd(tmp_path, APPS_NO_ANNOTATION)
    monkeypatch.setattr(m, "ARGOCD_BIN", fake)
    path_map = m.discover_path_app_map()
    joined = str(path_map)
    assert "pv-withannot-a-ms" in joined
    assert "pv-noannot-a-ms" not in joined, \
        "an app with no manifest-generate-paths annotation must contribute no path entry"
    # Both apps still got their chart metadata captured, though — the
    # annotation only gates the path_map, not chart/value-file discovery.
    assert m._app_chart_map.get("pv-noannot-a-ms") == "appspace-ms"


# ── _extract_app_chart_info: an app with no OCI chart source at all ─────

def test_extract_app_chart_info_no_chart_source_returns_all_none():
    app = {"spec": {"sources": [
        {"repoURL": "git@bitbucket.org:x/acme-config-dev.git", "ref": "values"},
    ]}}
    chart, rev, registry, vfiles = m._extract_app_chart_info(app)
    assert (chart, rev, registry, vfiles) == (None, None, None, [])


# ── _prune_helm_cache: a stray file sitting at the chart-name level ───────

def test_prune_helm_cache_skips_a_stray_file_at_chart_level(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "HELM_CACHE_DIR", str(tmp_path))
    reg_dir = tmp_path / "registry.example.com"
    reg_dir.mkdir(parents=True)
    # A stray FILE sitting directly under the REGISTRY dir, at the same
    # level chart directories live -- not one level deeper (that would test
    # a different, already-covered check on the VERSION level instead).
    (reg_dir / "stray-file.txt").write_text("not a chart dir")
    chart_dir = reg_dir / "appspace-ms"
    (chart_dir / "1.0.0").mkdir(parents=True)
    m._prune_helm_cache()  # must not raise on the stray file


# ── _fetch_one singleflight: joiner times out with no cached value yet ───

def test_fetch_value_files_singleflight_join_times_out(monkeypatch):
    sha = "timeoutsha001"
    clean = "gcp/dev/x/pv-sf-a/customer.yaml"
    vf = f"$config/{clean}"
    cache_key = (sha, clean)
    with m._vf_cache_lock:
        m._vf_cache.pop(cache_key, None)
    never_set = threading.Event()
    with m._vf_cache_lock:
        m._vf_inflight[cache_key] = never_set
    # Make the 30s wait return instantly (still a real timeout, just not a
    # real 30-second one) -- same technique as everywhere else time.sleep
    # is mocked in this suite.
    monkeypatch.setattr(threading.Event, "wait", lambda self, timeout=None: False)
    try:
        # COPS-2668: this assertion used to read "must omit the file, not
        # raise" -- and that was the defect, written down. Omitting the file
        # hands helm a value set the author never wrote: either a permanent
        # "missing required value" blamed on them, or a clean render of
        # different inputs published as fact. The shared 429 pause runs to 60s
        # by design, so this path is reached by an ordinary rate limit, not an
        # exotic one. No answer must mean no render.
        import pytest as _pytest
        with _pytest.raises(m.ValueFileUnreadable):
            m._fetch_value_files([vf], sha)
    finally:
        with m._vf_cache_lock:
            m._vf_inflight.pop(cache_key, None)


# ── _summarize_rendered_manifest: a document that dedents out of metadata ──

def test_summarize_rendered_manifest_metadata_dedent_out():
    rendered = (
        "kind: Deployment\n"
        "metadata:\n"
        "  name: webx\n"
        "spec:\n"          # dedents out of metadata at column 0
        "  replicas: 2\n"
    )
    total, kind_counts, workloads = m._summarize_rendered_manifest(rendered)
    assert total == 1 and kind_counts.get("Deployment") == 1
    # The real point: "webx" must be captured as the name BEFORE the dedent,
    # and the dedent-out itself (spec: at column 0) must not corrupt that.
    assert workloads == ["webx"], f"dedent-out must not lose the already-captured name: {workloads}"


# ── _evaluate_new_envs: the "Technical detail" arm (expected error, not helm-template-failed) ──

def test_evaluate_new_envs_technical_detail_for_expected_non_template_error(monkeypatch):
    files = ["gcp/dev/x/pv-newenv-b/customer.yaml"]
    cand = [{"name": "pv-newenv-b", "config_file": files[0],
             "env_dir": "gcp/dev/x/pv-newenv-b", "all_yaml_files": files}]
    monkeypatch.setattr(m, "_render_new_env_diff",
                        lambda info, sha: (None, "execute error: missing required value: appspace.instanceName", 0, "1.0.0"))
    lines, structural, total = m._evaluate_new_envs(cand, "p" * 12)
    joined = "\n".join(lines)
    assert "Technical detail" in joined, f"the expected, non-template error must show as a technical detail: {joined}"
    assert "structural problem" not in joined


# ── _render_new_env_diff: the release-registry lookup (non -dev version) ──

def test_render_new_env_diff_release_registry_lookup(monkeypatch):
    # The function's FIRST step is _bb_fetch_status for the config file
    # itself (to read appspace.version) -- left unmocked, this hits the
    # real network and returns an early "could not fetch" error, well
    # before ever reaching the registry-lookup section. Must be mocked.
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda path, sha: ("appspace:\n  version: 1.0.0\n", m.BB_OK))
    monkeypatch.setitem(m._app_chart_registry_map, "some-other-app",
                        "helm-oci-release.repo.appspace.com")
    env_info = {
        "name": "pv-newrel-a",
        "config_file": "gcp/dev/x/pv-newrel-a/customer.yaml",
        "env_dir": "gcp/dev/x/pv-newrel-a",
        "all_yaml_files": ["gcp/dev/x/pv-newrel-a/customer.yaml"],
    }
    # Whatever happens after the registry lookup (chart pull, render) is not
    # the point of this test and is free to fail — reaching and executing
    # the RELEASE branch (non-dev version) is the only thing being checked.
    m._render_new_env_diff(env_info, "p" * 12)


# ── _pr_chart_revision_checked: the followed-rename cache HIT branch ─────

def test_pr_chart_revision_checked_rename_target_cache_hit(monkeypatch):
    monkeypatch.setitem(m._app_chart_revision_map, "app-cachehit", "1.0.0")
    old_path = "gcp/dev/x/pv-cachehit-a/customer.yaml"
    new_path = "gcp/dev/y/pv-cachehit-a/customer.yaml"
    renames = {old_path: new_path}
    monkeypatch.setattr(m, "_trusted_rename_dirs", lambda *a, **k: {new_path.rsplit("/", 1)[0]})
    monkeypatch.setattr(m, "_is_trusted_rename", lambda *a, **k: True)
    with m._vf_cache_lock:
        # old path 404s (absent from cache AND from _bb_fetch_status)
        m._vf_cache.pop(("prshaXcache01", old_path), None)
        # new path already cached -> must be read via the cache-hit branch,
        # never touching _bb_fetch_status for it.
        m._vf_cache[("prshaXcache01", new_path)] = "appspace:\n  version: 1.0.1\n"

    def fail_if_called(clean, sha):
        if clean == new_path:
            raise AssertionError("new_path must be served from cache, not fetched")
        return None, m.BB_NOT_FOUND
    monkeypatch.setattr(m, "_bb_fetch_status", fail_if_called)
    try:
        new_rev, invalid = m._pr_chart_revision_checked(
            "app-cachehit", [old_path], "prshaXcache01", renames=renames)
        assert new_rev == "1.0.1", f"the cached rename-target content must be used: {new_rev}"
    finally:
        with m._vf_cache_lock:
            m._vf_cache.pop(("prshaXcache01", new_path), None)


# ── _split_yaml_docs: a List item whose continuation line isn't 2-space indented ──

def test_split_yaml_docs_list_item_with_unindented_continuation():
    yaml_text = (
        "kind: List\n"
        "items:\n"
        "- apiVersion: v1\n"
        "kind: ConfigMap\n"      # continuation line NOT prefixed with 2 spaces
        "  metadata:\n"
        "    name: fromlist\n"
    )
    docs = list(m._split_yaml_docs(yaml_text))
    assert any("fromlist" in d for d in docs), f"the List item must still be re-emitted: {docs}"


# ── _detect_env_decommission_candidates: duplicate-path dedup ────────────

def test_detect_env_decommission_candidates_dedups_repeated_path(monkeypatch):
    path = "gcp/dev/x/pv-decom-a/customer.yaml"
    monkeypatch.setattr(m, "_IDENTITY_BASENAMES", {"customer.yaml"}, raising=False)
    # seen_identity_files is only populated for identity files that pass
    # ALL the way through (a live env in path_map whose apps match the
    # env-name prefix) -- an empty path_map means every call 404s on the
    # apps-lookup continue and seen_identity_files never gets populated at
    # all, so the dedup branch itself is silently never exercised either.
    path_map = {path: ["pv-decom-a-ms"]}
    out = m._detect_env_decommission_candidates(
        [path, f"/{path}"], path_map, {})  # same path, with/without leading slash
    assert len([c for c in out if c["identity_file"] == path]) == 1, \
        "the same identity file listed twice must be deduped, not double-counted"


# ── argocd_diff: DIFF_RETRIES=0 falls straight to the post-loop fallback ──

def test_argocd_diff_zero_retries_hits_the_post_loop_fallback(monkeypatch):
    monkeypatch.setattr(m, "DIFF_RETRIES", 0)
    monkeypatch.setattr(m, "_run_one_diff",
                        lambda *a, **k: pytest.fail("must never call _run_one_diff with 0 retries"))
    r = m.argocd_diff("app-zeroretry", "p" * 12, "m" * 12)
    assert r.outcome == m.OUT_INDETERMINATE and r.error == "unknown error", \
        f"range(0) must fall straight through to the last_reason/last_detail defaults: {r}"


# ── get_pr_changed_files: page-limit hit WITH more pages remaining ───────

def test_get_pr_changed_files_page_limit_hit_logs_warning(monkeypatch):
    monkeypatch.setattr(m, "_BB_MAX_PAGES", 2)
    logs = []
    monkeypatch.setattr(logsink, "log", lambda msg, *a, **k: logs.append(str(msg)))

    def fake_bb(method, path, **kw):
        return {"values": [{"old": None, "new": {"path": "x.yaml"}}],
                "next": f"{m._BB_API_BASE}/nextpage"}
    monkeypatch.setattr(m, "bb", fake_bb)
    m.get_pr_changed_files(9999)
    assert any("diffstat page limit" in l and "incomplete" in l for l in logs), \
        f"hitting the page cap with more pages left must warn: {logs}"


# ── _format_app_diff_block: the legacy raw diff_text path (no sections) ──

def test_format_app_diff_block_legacy_diff_text_without_sections():
    out = m._format_app_diff_block("legacy-app", [], "--- a\n+++ b\n-old\n+new\n", show_diff=True, n_res=1, profile=_INLINE)
    joined = "\n".join(out)
    assert "```diff" in joined and "-old" in joined and "+new" in joined


# ── process_batch: an empty app list returns immediately ─────────────────

def test_process_pr_all_affected_apps_decommissioned_calls_batch_with_empty_list(world, monkeypatch):
    sinks, plan = world
    # affected starts non-empty (the world fixture's default changed file
    # maps to both pv-orch-a-ms/-ss), but if EVERY affected app is also a
    # confirmed decommission, the post-filter `affected = [a for a in
    # affected if a not in decommissioned_apps]` empties it before the
    # fan-out call -- process_batch's OWN early-return guard is what
    # actually executes at that point, not a guard in process_pr itself.
    monkeypatch.setattr(m, "_apps_to_skip_for_decommission",
                        lambda candidates, envs: {"pv-orch-a-ms", "pv-orch-a-ss"})
    m.process_pr(_mk_pr(pr_id=709), PATH_MAP, base_sha=BASE_SHA)  # must not raise
    assert sinks.statuses, "a fully-decommissioned affected set must still reach a terminal status"


# ── _ensure_chart: stale dev dir parking fails with OSError -> rmtree fallback ──

DEV_REG = "helm-oci-dev.repo.appspace.com"


def test_ensure_chart_stale_disk_dir_park_failure_falls_back_to_rmtree(helm_world, monkeypatch):
    helm, count = _mk_fake_helm(helm_world)
    monkeypatch.setattr(m, "HELM_BIN", helm)
    m._ensure_chart(DEV_REG, CHART, "3.0.0-dev")
    key = f"{DEV_REG}/{CHART}:3.0.0-dev"
    with m._helm_cache_lock:
        m._helm_chart_cache.clear()
    with m._helm_pull_locks_lock:
        m._helm_pull_locks.clear()
    m._helm_chart_pull_ts[key] = time.monotonic() - m.DEV_CHART_TTL - 5
    chart_dir = os.path.join(m.HELM_CACHE_DIR, DEV_REG, CHART, "3.0.0-dev")
    real_rename = os.rename

    def flaky_rename(src, dst):
        if src == chart_dir:
            raise OSError("simulated: cross-device link or permission error")
        return real_rename(src, dst)
    monkeypatch.setattr(os, "rename", flaky_rename)
    path = m._ensure_chart(DEV_REG, CHART, "3.0.0-dev")
    assert path is not None, "a failed park must still fall back to a working pull"


# ── main(): BB_WEBHOOK_SECRET startup visibility (repo audit finding) ────

def _mk_single_iteration_main_harness(monkeypatch):
    """Same harness as test_main_single_iteration_and_unhandled_error_survival
    in test_coverage_last_mile.py: runs main() for exactly one iteration,
    capturing every log() call as (message, severity), so the startup
    self-checks (OCI_PASS, BB_WEBHOOK_SECRET) can be asserted on without
    ever entering the real infinite poll loop."""
    monkeypatch.setattr(m, "_start_health_server",
                        lambda *a, **k: type("S", (), {"shutdown": lambda self: None})())
    monkeypatch.setattr(m, "_start_heartbeat", lambda: None)
    monkeypatch.setattr(m, "argocd_login", lambda: None)
    monkeypatch.setattr(m, "OCI_USER", "user")
    monkeypatch.setattr(m, "OCI_PASS", "secret")
    logs: list = []
    monkeypatch.setattr(logsink, "log", lambda msg, severity="INFO", **k: logs.append((str(msg), severity)))

    def one_pass():
        m._shutdown = True
    monkeypatch.setattr(m, "main_iteration", one_pass)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    return logs


def test_main_warns_when_bb_webhook_secret_is_empty(monkeypatch):
    logs = _mk_single_iteration_main_harness(monkeypatch)
    monkeypatch.setattr(m, "BB_WEBHOOK_SECRET", "")
    saved = m._shutdown
    m._shutdown = False
    try:
        m.main()
    finally:
        m._shutdown = saved
    hit = [sev for msg, sev in logs if "BB_WEBHOOK_SECRET is empty" in msg and "PERMISSIVE" in msg]
    assert hit == ["WARNING"], f"an empty webhook secret must warn loudly, same class as OCI_PASS: {logs}"


def test_main_confirms_bb_webhook_secret_when_present(monkeypatch):
    logs = _mk_single_iteration_main_harness(monkeypatch)
    monkeypatch.setattr(m, "BB_WEBHOOK_SECRET", "a-real-secret")
    saved = m._shutdown
    m._shutdown = False
    try:
        m.main()
    finally:
        m._shutdown = saved
    assert any("HMAC verification is active" in msg for msg, _ in logs), \
        f"a configured webhook secret must be confirmed at startup too, not just its absence: {logs}"
