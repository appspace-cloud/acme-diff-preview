"""COPS-2507 multi-repo support tests: DIFF_REPOS parsing, path-map
partitioning by git-source repo, scope filtering (Finding A), hierarchy
version resolution (Finding B), (repo, pr_id) state keying, and sha->repo
resolution — plus the default-config guarantee: with no DIFF_REPOS set,
behavior is byte-identical to the historical single-repo service."""
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m  # noqa: E402

DEV = "acme-config-dev"
STG = "acme-config-stage"


# ── DIFF_REPOS parsing ───────────────────────────────────────────────────

def test_parse_default_is_dev_only_no_scopes():
    assert m._parse_diff_repos("acme-config-dev") == {DEV: {"scopes": []}}


def test_parse_empty_falls_back_to_dev():
    assert m._parse_diff_repos("") == {DEV: {"scopes": []}}
    assert m._parse_diff_repos(" ; ;") == {DEV: {"scopes": []}}


def test_parse_multi_repo_with_scopes():
    repos = m._parse_diff_repos("acme-config-dev;acme-config-stage:gcp/")
    assert list(repos) == [DEV, STG], "order preserved: first repo is default"
    assert repos[DEV]["scopes"] == []
    assert repos[STG]["scopes"] == ["gcp/"]


def test_parse_multiple_scopes_pipe_separated():
    repos = m._parse_diff_repos("acme-config-prod:gcp/|extra/")
    assert repos["acme-config-prod"]["scopes"] == ["gcp/", "extra/"]


def test_default_bb_repo_alias_is_first_repo():
    # The transitional alias must equal the first configured repo so every
    # untouched single-repo call site keeps its historical behavior.
    assert m.BB_REPO == next(iter(m.REPOS))


# ── app -> git repo extraction ───────────────────────────────────────────

def _app(name, git_url, chart_url="helm-oci-dev.repo.appspace.com",
         paths="gcp/dev/x/y"):
    return {
        "metadata": {"name": name, "annotations": {
            "argocd.argoproj.io/manifest-generate-paths": paths}},
        "spec": {"sources": [
            {"repoURL": git_url, "ref": "config"},
            {"repoURL": chart_url, "chart": "appspace-ms",
             "targetRevision": "1.0.0",
             "helm": {"valueFiles": [f"$config/{paths}/customer.yaml"]}},
        ], "destination": {"namespace": "ns"}},
    }


def test_extract_git_repo_ssh_and_https_and_dotgit():
    assert m._extract_app_git_repo(
        _app("a", "git@bitbucket.org:appspace-cloud/acme-config-stage")) == STG
    assert m._extract_app_git_repo(
        _app("a", "https://bitbucket.org/appspace-cloud/acme-config-dev.git")) == DEV
    assert m._extract_app_git_repo(
        _app("a", "https://bitbucket.org/appspace-cloud/acme-config-dev/")) == DEV


def test_extract_git_repo_skips_chart_source_and_handles_missing():
    app = _app("a", "git@bitbucket.org:appspace-cloud/acme-config-dev")
    assert m._extract_app_git_repo(app) == DEV  # never the OCI source
    assert m._extract_app_git_repo({"spec": {"sources": []}}) is None


# ── path map partitioning ────────────────────────────────────────────────

def test_path_map_partition_by_repo(monkeypatch):
    apps = [
        _app("pv-dev-01-a-ms", "git@bitbucket.org:appspace-cloud/acme-config-dev",
             paths="gcp/dev/pc/ap1/custom/pv-dev-01-a"),
        _app("pv-stage1-a-ms", "git@bitbucket.org:appspace-cloud/acme-config-stage",
             paths="gcp/stage/pc/na1/custom/pv-stage1-a"),
        _app("pv-other-x-ms", "git@bitbucket.org:appspace-cloud/acme-config-unknown",
             paths="gcp/other/x"),
    ]
    import json, subprocess
    fake = type("R", (), {"returncode": 0, "stdout": json.dumps(apps), "stderr": ""})()
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: fake)
    monkeypatch.setattr(m, "REPOS", {DEV: {"scopes": []}, STG: {"scopes": ["gcp/"]}})
    monkeypatch.setattr(m, "_path_map_cache", {})
    monkeypatch.setattr(m, "_path_map_ts", 0.0)

    global_map = m.discover_path_app_map()
    # Global map keeps EVERY app (historical shape preserved).
    assert any("pv-dev-01-a-ms" in v for v in global_map.values())
    assert any("pv-stage1-a-ms" in v for v in global_map.values())
    # Partitions are strict: a repo only sees its own apps.
    dev_map = m.path_map_for_repo(DEV)
    stg_map = m.path_map_for_repo(STG)
    assert all("pv-stage1-a-ms" not in v for v in dev_map.values())
    assert all("pv-dev-01-a-ms" not in v for v in stg_map.values())
    assert any("pv-dev-01-a-ms" in v for v in dev_map.values())
    assert any("pv-stage1-a-ms" in v for v in stg_map.values())
    # Unconfigured repo: app ignored in partitions, unknown repo empty map.
    assert m.path_map_for_repo("acme-config-unknown") == {}
    assert m._app_repo_map["pv-stage1-a-ms"] == STG


# ── sha -> repo resolution ───────────────────────────────────────────────

def test_sha_repo_registration_and_fallback():
    m._sha_repo_map.clear()
    m._register_sha_repo("a" * 12, STG)
    assert m._repo_for_sha("a" * 12) == STG
    assert m._repo_for_sha("f" * 12) is None  # unregistered -> caller falls back
    m._register_sha_repo("", STG)             # no-ops, never raises
    m._register_sha_repo("b" * 12, None)
    assert m._repo_for_sha("b" * 12) is None
    m._sha_repo_map.clear()


def test_sha_repo_map_is_bounded():
    m._sha_repo_map.clear()
    for i in range(m._SHA_REPO_MAX + 5):
        m._register_sha_repo(f"sha{i}", DEV)
    assert len(m._sha_repo_map) <= m._SHA_REPO_MAX + 1
    m._sha_repo_map.clear()


# ── scope filter (Finding A): azure-only stage PR is a silent no-op ─────

STAGE_ENV  = "gcp/stage/private-cloud/na1/custom/pv-stage1-a"
STAGE_MAP  = {f"{STAGE_ENV}/customer.yaml": ["pv-stage1-a-ms"]}


def _mk_pr(pr_id, sha="e" * 12):
    return {"id": pr_id, "title": f"pr {pr_id}",
            "source": {"commit": {"hash": sha}, "branch": {"name": "f"}},
            "destination": {"branch": {"name": "main"}}}


@pytest.fixture()
def stage_world(monkeypatch):
    sinks = {"upserts": [], "statuses": [], "changed": []}
    monkeypatch.setattr(m, "REPOS",
                        {DEV: {"scopes": []}, STG: {"scopes": ["gcp/"]}})
    monkeypatch.setattr(m, "find_existing_comment",
                        lambda pr_id, repo=None: (None, "", ""))
    monkeypatch.setattr(m, "upsert_comment",
                        lambda pr_id, body, existing_id=None, repo=None:
                        sinks["upserts"].append((repo, body)))
    monkeypatch.setattr(m, "post_build_status",
                        lambda sha, st, d, pr_id=None, repo=None:
                        sinks["statuses"].append((repo, st)))
    monkeypatch.setattr(m, "fix_stuck_inprogress", lambda *a, **k: None)
    monkeypatch.setattr(m, "_touch_progress", lambda: None)
    m._seen.clear(); m._force_recompute.clear()
    yield sinks
    m._seen.clear(); m._force_recompute.clear()


def test_scope_filter_azure_only_pr_is_silent_noop(stage_world, monkeypatch):
    azure_files = ["azure/stage/pc/na1/pv-corp-x/customer.yaml",
                   "azure/stage/config.yaml"]
    monkeypatch.setattr(m, "get_pr_changed_files",
                        lambda pr_id, repo=None: (list(azure_files), {}))
    m.process_pr(_mk_pr(101), STAGE_MAP, base_sha="b" * 12, repo=STG)
    assert stage_world["upserts"] == [], "no comment for out-of-scope PR"
    assert stage_world["statuses"] == [], "no build status for out-of-scope PR"


def test_scope_filter_mixed_pr_sees_only_gcp_files(stage_world, monkeypatch):
    files = ["azure/stage/pc/na1/pv-corp-x/customer.yaml",
             f"{STAGE_ENV}/customer.yaml"]
    seen_by_matcher = {}
    real_match = m._match_files_to_apps
    def spy(changed, pmap):
        seen_by_matcher["files"] = list(changed)
        return real_match(changed, pmap)
    monkeypatch.setattr(m, "_match_files_to_apps", spy)
    monkeypatch.setattr(m, "get_pr_changed_files",
                        lambda pr_id, repo=None: (list(files), {}))
    # Diff engine short-circuit: report clean no-diff for the matched app.
    monkeypatch.setattr(m, "argocd_diff",
                        lambda *a, **k: m.DiffResult("", [], 0, False, "",
                                                     m.OUT_NO_DIFF, ""))
    m.process_pr(_mk_pr(102), STAGE_MAP, base_sha="b" * 12, repo=STG)
    assert seen_by_matcher["files"] == [f"{STAGE_ENV}/customer.yaml"], \
        "azure/ file must be filtered out before app matching"
    assert stage_world["statuses"], "in-scope file must still produce a run"


# ── (repo, pr_id) keying: same id in two repos is independent state ─────

def test_same_pr_id_in_two_repos_has_independent_dedup(stage_world, monkeypatch):
    calls = []
    monkeypatch.setattr(m, "get_pr_changed_files",
                        lambda pr_id, repo=None: (calls.append(repo) or [], {}))
    # Same pr_id (7), same sha, two different repos: BOTH must be processed
    # (no cross-repo dedup), and _seen must hold two distinct keys.
    m.process_pr(_mk_pr(7), {}, base_sha="b" * 12, repo=DEV)
    m.process_pr(_mk_pr(7), {}, base_sha="b" * 12, repo=STG)
    assert calls == [DEV, STG]
    with m._seen_lock:
        assert (DEV, 7) in m._seen and (STG, 7) in m._seen


# ── hierarchy version resolution (Finding B) ─────────────────────────────

ENV_DIR = "gcp/prod/public-cloud/na1/cl-prod-b/customer-x"

def _fetch_factory(files):
    def fake_fetch(path, sha, repo=None):
        if path in files:
            return files[path], m.BB_OK
        return None, m.BB_NOT_FOUND
    return fake_fetch


def _run_version_probe(monkeypatch, files):
    """Run _render_new_env_diff far enough to see which version it picked;
    abort at the chart-pull step by returning a sentinel error."""
    monkeypatch.setattr(m, "_bb_fetch_status", _fetch_factory(files))
    picked = {}
    def fake_ensure(registry, chart, version):
        picked["version"] = version
        raise RuntimeError("stop-here")
    monkeypatch.setattr(m, "_ensure_chart", fake_ensure)
    env = {"name": "customer-x", "config_file": f"{ENV_DIR}/customer.yaml",
           "env_dir": ENV_DIR, "all_yaml_files": [f"{ENV_DIR}/customer.yaml"]}
    try:
        res = m._render_new_env_diff(env, "c" * 12)
    except RuntimeError:
        return picked.get("version"), None
    return picked.get("version"), res


def _y(version):
    return f"appspace:\n  version: {version}\n"


def test_version_from_own_customer_yaml_wins(monkeypatch):
    v, _ = _run_version_probe(monkeypatch, {
        f"{ENV_DIR}/customer.yaml": _y("1.1.1"),
        "gcp/prod/public-cloud/na1/cl-prod-b/config.yaml": _y("9.9.9"),
    })
    assert v == "1.1.1", "own file must win over every ancestor"


def test_version_inherited_from_cohort_config(monkeypatch):
    # The COPS-2508 pattern: 95% of prod customer.yaml have no version.
    v, _ = _run_version_probe(monkeypatch, {
        f"{ENV_DIR}/customer.yaml": "appspace:\n  customerName: x\n",
        "gcp/prod/public-cloud/na1/cl-prod-b/config.yaml": _y("2601.4.16"),
        "gcp/prod/config.yaml": _y("0.0.1"),
    })
    assert v == "2601.4.16", "most specific ancestor level must win"


def test_version_inherited_from_top_level_config(monkeypatch):
    v, _ = _run_version_probe(monkeypatch, {
        f"{ENV_DIR}/customer.yaml": "appspace:\n  customerName: x\n",
        "gcp/config.yaml": _y("2600.0.1"),
    })
    assert v == "2600.0.1"


def test_no_version_anywhere_is_structural_failure(monkeypatch):
    v, res = _run_version_probe(monkeypatch, {
        f"{ENV_DIR}/customer.yaml": "appspace:\n  customerName: x\n",
    })
    assert v is None and res is not None
    diff_text, err = res[0], res[1]
    assert diff_text is None
    assert "no appspace.version" in err, \
        "structural classification depends on this error prefix"


# ── v2.6.1: build-status url deep-links to the review comment ────────────

def test_build_status_anchors_to_comment_when_id_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "bb", lambda method, path, repo=None, body=None:
                        calls.append(body) or {})
    with m._comment_id_cache_lock:
        m._comment_id_cache[(DEV, 321)] = 777888
    m._register_sha_repo("9" * 12, DEV)
    try:
        m.post_build_status("9" * 12, "SUCCESSFUL", "d", pr_id=321)
        assert calls and calls[0]["url"].endswith("/pull-requests/321#comment-777888")
    finally:
        with m._comment_id_cache_lock:
            m._comment_id_cache.pop((DEV, 321), None)
        m._sha_repo_map.clear()


def test_build_status_plain_pr_link_without_cached_comment(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "bb", lambda method, path, repo=None, body=None:
                        calls.append(body) or {})
    m.post_build_status("8" * 12, "INPROGRESS", "d", pr_id=999)
    assert calls and calls[0]["url"].endswith("/pull-requests/999"), \
        "no cached comment id -> plain PR link (never an empty/broken anchor)"


def test_upsert_comment_caches_new_comment_id(monkeypatch):
    monkeypatch.setattr(m, "bb", lambda method, path, repo=None, body=None:
                        {"id": 424242})
    with m._comment_id_cache_lock:
        m._comment_id_cache.pop((DEV, 55), None)
    m.upsert_comment(55, "body", existing_id=None, repo=DEV)
    with m._comment_id_cache_lock:
        assert m._comment_id_cache.get((DEV, 55)) == 424242, \
            "POSTed comment id must be cached for the same-run final status"
        m._comment_id_cache.pop((DEV, 55), None)
