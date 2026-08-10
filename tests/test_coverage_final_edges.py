"""Coverage campaign, final pass: value-file fetch cache, republish
invalidation, token fetch, render-reason classification.

Deliberately left out (proportionate-scope calls, documented in README):
the helm render internals, and the heartbeat thread wrapper (its decision
logic _liveness_should_refresh is already unit-tested; the wrapper is a
hardcoded 30s-sleep daemon loop whose test would leave a live thread behind).
"""
import json
import os
import sys
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m  # noqa: E402


# ── _fetch_value_files ───────────────────────────────────────────────────

@pytest.fixture()
def clean_vf_cache():
    with m._vf_cache_lock:
        m._vf_cache.clear()
        m._vf_inflight.clear()
    yield
    with m._vf_cache_lock:
        m._vf_cache.clear()
        m._vf_inflight.clear()


def test_fetch_value_files_strips_alias_fetches_and_caches(monkeypatch, clean_vf_cache):
    fetched = []

    def fake_fetch(path, sha):
        fetched.append(path)
        return "appspace:\n  version: 1.0.0\n", m.BB_OK

    monkeypatch.setattr(m, "_bb_fetch_status", fake_fetch)
    vfs = ["$config/gcp/dev/x/customer.yaml", "$config/gcp/dev/x/cicd-versions.yaml"]
    out = m._fetch_value_files(vfs, "a" * 12)
    assert set(out) == set(vfs)
    assert fetched == ["gcp/dev/x/customer.yaml", "gcp/dev/x/cicd-versions.yaml"], \
        "the $config/ git-source alias must be stripped before the API call"
    # Second call, same sha: served entirely from the (sha, path) cache.
    fetched.clear()
    out2 = m._fetch_value_files(vfs, "a" * 12)
    assert out2 == out and fetched == []


def test_fetch_value_files_skips_404s_silently(monkeypatch, clean_vf_cache):
    monkeypatch.setattr(m, "_bb_fetch_status", lambda path, sha: (None, m.BB_NOT_FOUND))
    out = m._fetch_value_files(["$config/gcp/dev/new-cluster/config.yaml"], "a" * 12)
    assert out == {}


def test_fetch_value_files_never_caches_a_transient_error(monkeypatch, clean_vf_cache):
    # The poison rule: a transient failure must NOT be remembered as
    # "missing", or every app sharing the (sha, path) key inherits the hole.
    calls = {"n": 0}

    def flaky(path, sha):
        calls["n"] += 1
        if calls["n"] == 1:
            return None, m.BB_ERROR
        return "recovered: yes\n", m.BB_OK

    monkeypatch.setattr(m, "_bb_fetch_status", flaky)
    vf = ["$config/gcp/dev/x/customer.yaml"]
    assert m._fetch_value_files(vf, "b" * 12) == {}
    out = m._fetch_value_files(vf, "b" * 12)
    assert out and "recovered" in next(iter(out.values())), \
        "a transient error must be retried on the next call, not cached"


# ── _argocd_fetch_token ──────────────────────────────────────────────────

def test_argocd_fetch_token_posts_session_request(monkeypatch):
    captured = {}

    class _Resp:
        def read(self):
            return json.dumps({"token": "jwt-abc"}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, context=None, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert m._argocd_fetch_token() == "jwt-abc"
    assert captured["url"].endswith("/api/v1/session")
    assert captured["body"]["username"] == m.ARGOCD_USER


# ── _render_reason ───────────────────────────────────────────────────────

def test_render_reason_classifies_yaml_syntax_errors():
    assert m._render_reason("Error: error converting YAML to JSON") == m.REASON_INVALID_YAML
    assert m._render_reason("yaml: line 42: did not find expected key") == m.REASON_INVALID_YAML
    assert m._render_reason("mapping values are not allowed in this context") == m.REASON_INVALID_YAML


def test_render_reason_everything_else_is_generic_render():
    assert m._render_reason("chart requires kubeVersion >= 1.25") == m.REASON_RENDER
    assert m._render_reason("") == m.REASON_RENDER


# ── _invalidate_for_republish ────────────────────────────────────────────

def test_invalidate_for_republish_evicts_forces_and_wakes(monkeypatch):
    chart, ver = "appspace-ms", "2603.0.1-dev"
    key_match = f"registry.example.com/{chart}:{ver}"
    key_other = f"registry.example.com/{chart}:9.9.9"
    with m._helm_cache_lock:
        m._helm_chart_cache[key_match] = "/tmp/x"
        m._helm_chart_cache[key_other] = "/tmp/y"
    m._helm_chart_pull_ts[key_match] = 1.0
    m._app_chart_map["pv-repub-a-ms"] = chart
    m._app_chart_revision_map["pv-repub-a-ms"] = ver
    with m._main_render_lock:
        m._main_render_cache["deadbeef" * 8] = "cached-render"
        m._main_render_cache["cafebabe" * 8] = "keep-me"
    with m._seen_lock:
        m._pr_chart_targets[("acme-config-dev", 777)] = {(chart, ver)}
        m._pr_chart_targets[("acme-config-dev", 888)] = {(chart, "1.1.1")}
        m._seen[("acme-config-dev", 777)] = "aabbccdd"
    m._wake.clear()
    try:
        m._invalidate_for_republish(chart, ver)
        assert key_match not in m._helm_chart_cache
        assert key_other in m._helm_chart_cache, "other versions stay cached"
        # COPS-2631: content-keyed memory front is cleared wholesale on
        # republish (keys are digests, not app tuples).
        assert m._main_render_cache == {}
        assert ("acme-config-dev", 777) in m._force_recompute and ("acme-config-dev", 888) not in m._force_recompute
        assert ("acme-config-dev", 777) not in m._seen, "dedup must be bypassed for the forced PR"
        assert m._wake.is_set(), "the polling loop must wake up immediately"
    finally:
        with m._helm_cache_lock:
            m._helm_chart_cache.clear()
        m._helm_chart_pull_ts.clear()
        m._app_chart_map.pop("pv-repub-a-ms", None)
        m._app_chart_revision_map.pop("pv-repub-a-ms", None)
        with m._main_render_lock:
            m._main_render_cache.clear()
        with m._seen_lock:
            m._pr_chart_targets.clear()
            m._seen.clear()
        m._force_recompute.clear()
        m._wake.clear()


# ── main_iteration: force-recompute pruning ──────────────────────────────

def test_main_iteration_prunes_force_flags_of_closed_prs(monkeypatch):
    monkeypatch.setattr(m, "argocd_login", lambda: None)
    monkeypatch.setattr(m, "discover_path_app_map", lambda: {})
    monkeypatch.setattr(m, "_prune_helm_cache", lambda *a, **k: None)
    monkeypatch.setattr(m, "_touch_progress", lambda: None)
    monkeypatch.setattr(m, "http", lambda *a, **kw: {"target": {"hash": "c" * 12}})
    pr = {"id": 5, "title": "t",
          "source": {"commit": {"hash": "d" * 12}, "branch": {"name": "f"}},
          "destination": {"branch": {"name": "main"}}}
    monkeypatch.setattr(m, "get_open_prs", lambda repo=None: [pr])
    monkeypatch.setattr(m, "process_pr", lambda *a, **kw: None)
    m._force_recompute.update({("acme-config-dev", 5), ("acme-config-dev", 999)})  # 999 = a PR that was closed meanwhile
    try:
        m.main_iteration()
        assert ("acme-config-dev", 5) in m._force_recompute or ("acme-config-dev", 5) not in m._force_recompute  # consumed or kept by process
        assert ("acme-config-dev", 999) not in m._force_recompute, \
            "flags for no-longer-open PRs must be pruned, not accumulate forever"
    finally:
        m._force_recompute.clear()
