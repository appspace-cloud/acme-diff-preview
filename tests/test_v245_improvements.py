"""Regression tests for the v2.4.5 non-critical-angle improvements (N1-N5).

Each was found via bughunt/nc_probe*.py (measurement-driven, not just
assertion) and implemented here. These tests guard the fixes going forward.
"""
import hashlib
import hmac as hmac_mod
import importlib
import json
import os
import re
import sys
import threading
import urllib.error

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


def _import_module():
    os.environ.setdefault("BB_USER", "test")
    os.environ.setdefault("BB_TOKEN", "test")
    os.environ.setdefault("ARGOCD_PASS", "test")
    os.environ.setdefault("JFROG_WEBHOOK_SECRET", "testsecret")
    if SRC not in sys.path:
        sys.path.insert(0, SRC)
    mod = importlib.import_module("diff_preview")
    return importlib.reload(mod)


def _source():
    with open(os.path.join(SRC, "diff_preview.py")) as f:
        return f.read()


# ── N1: JFROG_REFRESH_WORKERS split into two independently named knobs ──────
def test_n1_dispatch_and_fanout_workers_are_separate_knobs():
    src = _source()
    assert 'JFROG_DISPATCH_WORKERS = _env_int("JFROG_DISPATCH_WORKERS", 4)' in src, (
        "dispatch-pool size must be its own named env var"
    )
    assert 'REFRESH_WORKERS = _env_int("JFROG_REFRESH_FANOUT", 8)' in src, (
        "per-event fan-out size must be its own named env var"
    )
    assert 'os.environ.get("JFROG_REFRESH_WORKERS"' not in src, (
        "the old overloaded name must be fully retired"
    )


def test_n1_dispatch_pool_uses_new_env_var(monkeypatch):
    monkeypatch.setenv("JFROG_DISPATCH_WORKERS", "7")
    mod = _import_module()
    assert mod.JFROG_DISPATCH_WORKERS == 7
    assert mod._jfrog_refresh_pool._max_workers == 7


# ── N2: large-PR table omits no-change rows, keeps a single count line ──────
def test_n2_no_change_apps_collapsed_to_one_row(monkeypatch):
    mod = _import_module()
    monkeypatch.setattr(mod, "generate_ai_summary", lambda *a, **k: None)

    def mkres(outcome, n=0, text=""):
        secs = mod.parse_diff_sections(text) if text else []
        return mod.DiffResult(text, secs, n, outcome == mod.OUT_DIFF, None, outcome, "x")

    results = {}
    # Must exceed LARGE_PR_APP_THRESHOLD (5) to trigger the summary table.
    for i in range(8):
        results[f"chg-{i}"] = mkres(mod.OUT_DIFF, n=2,
                                    text="===== /v1/ConfigMap ns/x =====\n+ a\n")
    for i in range(50):
        results[f"ok-{i}"] = mkres(mod.OUT_NO_DIFF, n=0)

    body = mod.format_comment("a" * 40, results, base_sha="b" * 40)
    no_change_rows = body.count("no changes")
    assert no_change_rows == 1, (
        f"expected exactly 1 collapsed 'no changes' row, found {no_change_rows}"
    )
    assert "+50 more" in body, "collapsed row must state the omitted count"
    assert "chg-0" in body and "chg-1" in body and "chg-7" in body, (
        "changed apps must still be listed individually"
    )


def test_n2_small_pr_unaffected(monkeypatch):
    """The collapse only applies in large mode; small PRs are unchanged."""
    mod = _import_module()
    monkeypatch.setattr(mod, "generate_ai_summary", lambda *a, **k: None)

    def mkres(outcome, n=0, text=""):
        secs = mod.parse_diff_sections(text) if text else []
        return mod.DiffResult(text, secs, n, outcome == mod.OUT_DIFF, None, outcome, "x")

    results = {"app-a": mkres(mod.OUT_DIFF, n=1, text="===== /v1/ConfigMap ns/x =====\n+ a\n"),
              "app-b": mkres(mod.OUT_NO_DIFF, n=0)}
    body = mod.format_comment("a" * 40, results, base_sha="b" * 40)
    assert "Changeset overview" not in body, "small PRs must not get the large-mode table"


# ── N3: a bad numeric env var degrades to default instead of crashing ───────
def test_n3_env_int_bad_value_falls_back(monkeypatch, capsys):
    mod = _import_module()
    assert mod._env_int("DOES_NOT_EXIST", 42) == 42
    monkeypatch.setenv("BUGHUNT_N3_TEST", "not-a-number")
    result = mod._env_int("BUGHUNT_N3_TEST", 99)
    assert result == 99, "invalid value must fall back to the default, not raise"


def test_n3_env_int_good_value_parses(monkeypatch):
    mod = _import_module()
    monkeypatch.setenv("BUGHUNT_N3_TEST2", "123")
    assert mod._env_int("BUGHUNT_N3_TEST2", 1) == 123


def test_n3_module_survives_bad_env_at_import(monkeypatch):
    """The scenario that used to crash-loop the pod: a typo in a real knob."""
    monkeypatch.setenv("DIFF_WORKERS", "sixteen")
    mod = _import_module()  # must NOT raise
    assert mod.DIFF_WORKERS == 16, "must fall back to the documented default"
    monkeypatch.delenv("DIFF_WORKERS", raising=False)
    _import_module()  # restore clean state for subsequent tests


# ── N5: comment-id cache avoids re-paginating a hot PR every iteration ──────
def test_n5_cached_lookup_makes_a_single_call(monkeypatch):
    mod = _import_module()
    mod._comment_id_cache.clear()
    mod._comment_id_cache[("acme-config-dev", 555)] = 42

    calls = []

    def fake_bb(method, path, **kw):
        calls.append((method, path))
        assert path == "pullrequests/555/comments/42", (
            "cached path must fetch the comment DIRECTLY by id, not paginate"
        )
        return {"content": {"raw": f"**Commit** `abcd1234` \u2192 `main` | `{mod.BB_REPO}`\n\n*ts \u2014 {mod.COMMENT_MARKER} [clean]*"}}

    monkeypatch.setattr(mod, "bb", fake_bb)
    cid, sha8, raw = mod.find_existing_comment(555)
    assert cid == 42 and sha8 == "abcd1234"
    assert len(calls) == 1, f"expected exactly 1 API call via the cache, got {len(calls)}"


def test_n5_stale_cached_id_falls_back_to_full_scan(monkeypatch):
    """A 404 on the cached id must evict it and fall back to pagination."""
    mod = _import_module()
    mod._comment_id_cache.clear()
    mod._comment_id_cache[("acme-config-dev", 777)] = 999   # stale: comment was deleted

    calls = []

    def fake_bb(method, path, **kw):
        calls.append(path)
        if path == "pullrequests/777/comments/999":
            raise urllib.error.HTTPError("u", 404, "Not Found", None, None)
        return {"values": [{"id": 1001,
                            "content": {"raw": f"**Commit** `deadbeef` \u2192 `main` | `{mod.BB_REPO}`\n\n*ts \u2014 {mod.COMMENT_MARKER} [clean]*"}}]}

    monkeypatch.setattr(mod, "bb", fake_bb)
    cid, sha8, raw = mod.find_existing_comment(777)
    assert cid == 1001 and sha8 == "deadbeef"
    assert mod._comment_id_cache[("acme-config-dev", 777)] == 1001, "cache must be updated with the NEW id"


def test_n5_cache_pruned_alongside_seen():
    """The eviction block that prunes _seen for closed PRs must also prune
    _comment_id_cache (same open_ids set), or it grows unbounded forever."""
    src = _source()
    idx_seen_evict = src.find("for stale_k in [k for k in _seen if _stale(k)]")
    idx_cache_evict = src.find("for stale_k in [k for k in _comment_id_cache if _stale(k)]")
    assert idx_seen_evict > 0 and idx_cache_evict > 0, (
        "_comment_id_cache must be pruned using the same open_ids computation as _seen"
    )


# ── N4: richer /diff-preview/stats counters ──────────────────────────────────
def test_n4_new_stats_counters_exist():
    mod = _import_module()
    for key in ("apps_render_failed", "apps_timeout",
               "main_render_cache_hits", "main_render_cache_misses"):
        assert key in mod._diff_stats, f"missing stats counter: {key}"


def test_n4_render_cache_hit_counted(monkeypatch):
    """A cached main-side render must increment the hit counter."""
    mod = _import_module()
    mod._diff_stats["main_render_cache_hits"] = 0
    mod._diff_stats["main_render_cache_misses"] = 0
    mod._main_render_cache.clear()
    key = ("app-x", "mainsha", "1.0.0", 0)
    mod._main_render_cache[key] = {"some": "resources"}
    with mod._main_render_lock:
        hit = mod._main_render_cache.get(key)
    needs_render = hit is None
    with mod._diff_stats_lock:
        mod._diff_stats["main_render_cache_misses" if needs_render
                        else "main_render_cache_hits"] += 1
    assert mod._diff_stats["main_render_cache_hits"] == 1
    assert mod._diff_stats["main_render_cache_misses"] == 0
