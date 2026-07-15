"""Coverage campaign, pass F: the Bitbucket API layer and remaining clean edges.

Everything here is deterministic and infra-free, same discipline as the
earlier passes. Deliberately NOT covered: the helm render internals
(_ensure_chart, _helm_template, _run_one_diff, ...) — faking the helm binary
end to end would test the fake, not the service.
"""
import os
import sys
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m  # noqa: E402


def _http_error(code, url="https://api.bitbucket.org/x"):
    return urllib.error.HTTPError(url, code, "err", hdrs=None, fp=None)


# ── get_open_prs ─────────────────────────────────────────────────────────

def test_get_open_prs_concatenates_pages(monkeypatch):
    pages = [
        {"values": [{"id": 1}, {"id": 2}], "next": "https://api.bitbucket.org/2.0/x?page=2"},
        {"values": [{"id": 3}]},
    ]
    calls = []
    monkeypatch.setattr(m, "http", lambda method, url, **kw: calls.append(url) or pages[len(calls) - 1])
    prs = m.get_open_prs()
    assert [p["id"] for p in prs] == [1, 2, 3]
    assert len(calls) == 2 and "page=2" in calls[1]


def test_get_open_prs_stops_at_page_limit_with_warning(monkeypatch):
    monkeypatch.setattr(m, "_BB_MAX_PAGES", 2)
    monkeypatch.setattr(m, "http",
                        lambda method, url, **kw: {"values": [{"id": 9}], "next": url})
    logs = []
    monkeypatch.setattr(m, "log", lambda msg, *a, **k: logs.append(str(msg)))
    prs = m.get_open_prs()
    assert len(prs) == 2
    assert any("page limit" in l for l in logs)


# ── find_existing_comment ────────────────────────────────────────────────

@pytest.fixture()
def clean_comment_cache():
    with m._comment_id_cache_lock:
        m._comment_id_cache.clear()
    yield
    with m._comment_id_cache_lock:
        m._comment_id_cache.clear()


def test_find_existing_comment_full_scan_then_fast_path(monkeypatch, clean_comment_cache):
    raw = f"header\n{m.COMMENT_MARKER} [clean]"
    calls = []

    def fake_bb(method, path, **kw):
        calls.append(path)
        if path.endswith("/comments/77"):
            return {"id": 77, "content": {"raw": raw}}
        return {"values": [
            {"id": 5, "content": {"raw": "someone else's comment"}},
            {"id": 77, "content": {"raw": raw}},
        ]}

    monkeypatch.setattr(m, "bb", fake_bb)
    cid, _sha, got = m.find_existing_comment(4242)
    assert cid == 77 and m.COMMENT_MARKER in got
    # Second lookup: the cached id is fetched DIRECTLY (1 call, no pagination).
    calls.clear()
    cid2, _sha2, _ = m.find_existing_comment(4242)
    assert cid2 == 77
    assert calls == [f"pullrequests/4242/comments/77"]


def test_find_existing_comment_cached_404_falls_back_to_scan(monkeypatch, clean_comment_cache):
    with m._comment_id_cache_lock:
        m._comment_id_cache[4243] = 99  # stale: comment was deleted

    def fake_bb(method, path, **kw):
        if path.endswith("/comments/99"):
            raise _http_error(404)
        return {"values": []}

    monkeypatch.setattr(m, "bb", fake_bb)
    cid, sha, raw = m.find_existing_comment(4243)
    assert cid is None and raw == ""


# ── upsert_comment ───────────────────────────────────────────────────────

def test_upsert_comment_posts_new_and_updates_existing(monkeypatch):
    recorded = []
    monkeypatch.setattr(m, "bb",
                        lambda method, path, **kw: recorded.append((method, path, kw)) or {"id": 11})
    m.upsert_comment(10, "hello world")
    m.upsert_comment(10, "hello again", existing_id=55)
    assert recorded[0][0] == "POST" and recorded[0][1].endswith("/comments")
    assert recorded[1][0] == "PUT" and recorded[1][1].endswith("/comments/55")


def test_upsert_comment_truncates_oversized_bodies(monkeypatch):
    recorded = []
    monkeypatch.setattr(m, "bb",
                        lambda method, path, **kw: recorded.append(kw) or {"id": 11})
    monkeypatch.setattr(m, "MAX_COMMENT_BYTES", 500)
    m.upsert_comment(10, "x" * 2000)
    sent = recorded[0]["body"]["content"]["raw"]
    assert len(sent.encode()) < 900
    assert "truncated" in sent and m.COMMENT_MARKER in sent


# ── fix_stuck_inprogress ─────────────────────────────────────────────────

def test_fix_stuck_inprogress_completes_a_clean_stuck_status(monkeypatch):
    monkeypatch.setattr(m, "http", lambda *a, **kw: {"state": "INPROGRESS"})
    posted = []
    monkeypatch.setattr(m, "post_build_status",
                        lambda pr_sha, state, description, pr_id=None, repo=None:
                        posted.append(state))
    m.fix_stuck_inprogress("a" * 12, 10, f"body {m.COMMENT_MARKER} [clean]")
    assert posted and posted[-1] == "SUCCESSFUL"


def test_fix_stuck_inprogress_permanent_token_finalizes_failed(monkeypatch):
    monkeypatch.setattr(m, "http", lambda *a, **kw: {"state": "INPROGRESS"})
    posted = []
    monkeypatch.setattr(m, "post_build_status",
                        lambda pr_sha, state, description, pr_id=None, repo=None:
                        posted.append(state))
    m.fix_stuck_inprogress("a" * 12, 10, f"body {m.COMMENT_MARKER} [permanent]")
    assert posted and posted[-1] == "FAILED"


def test_fix_stuck_inprogress_leaves_finished_statuses_alone(monkeypatch):
    monkeypatch.setattr(m, "http", lambda *a, **kw: {"state": "SUCCESSFUL"})
    posted = []
    monkeypatch.setattr(m, "post_build_status",
                        lambda *a, **kw: posted.append(a))
    m.fix_stuck_inprogress("a" * 12, 10, f"body {m.COMMENT_MARKER} [clean]")
    assert posted == []


def test_fix_stuck_inprogress_swallows_api_errors(monkeypatch):
    def boom(*a, **kw):
        raise _http_error(500)
    monkeypatch.setattr(m, "http", boom)
    m.fix_stuck_inprogress("a" * 12, 10, "raw")  # must not raise


# ── _bb_fetch_status (raw file fetch, direct urllib) ─────────────────────

class _Resp:
    def __init__(self, data: bytes):
        self._d = data

    def read(self):
        return self._d

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_bb_fetch_status_ok(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, **kw: _Resp(b"appspace:\n  version: 1.0.0\n"))
    content, status = m._bb_fetch_status("gcp/dev/x/customer.yaml", "a" * 12)
    assert status == m.BB_OK and "version" in content


def test_bb_fetch_status_404_is_a_stable_not_found(monkeypatch):
    def raise404(req, **kw):
        raise _http_error(404)
    monkeypatch.setattr(urllib.request, "urlopen", raise404)
    content, status = m._bb_fetch_status("gone.yaml", "a" * 12)
    assert content is None and status == m.BB_NOT_FOUND


def test_bb_fetch_status_transient_error_is_not_cacheable_missing(monkeypatch):
    def raise_net(req, **kw):
        raise urllib.error.URLError("connection reset")
    monkeypatch.setattr(urllib.request, "urlopen", raise_net)
    monkeypatch.setattr(m.time, "sleep", lambda w: None)
    content, status = m._bb_fetch_status("x.yaml", "a" * 12)
    assert content is None and status == m.BB_ERROR


# ── argocd_login ─────────────────────────────────────────────────────────

@pytest.fixture()
def login_state():
    saved = (m._ready, m._consecutive_login_fails, m._argocd_token, m._argocd_token_ts)
    yield
    m._ready, m._consecutive_login_fails, m._argocd_token, m._argocd_token_ts = saved


def test_argocd_login_success_resets_failure_counter(monkeypatch, login_state):
    m._consecutive_login_fails = 2
    monkeypatch.setattr(m, "_argocd_fetch_token", lambda: "tok-abc")
    m.argocd_login()
    assert m._argocd_token == "tok-abc"
    assert m._consecutive_login_fails == 0


def test_argocd_login_clears_readiness_after_threshold(monkeypatch, login_state):
    def boom():
        raise RuntimeError("session api down")
    monkeypatch.setattr(m, "_argocd_fetch_token", boom)
    m._consecutive_login_fails = 0
    m._ready = True
    for _ in range(m.LOGIN_FAIL_THRESHOLD):
        with pytest.raises(RuntimeError):
            m.argocd_login()
    assert m._ready is False, "readiness must flip so the probe can restart the pod"


# ── _prune_helm_cache (filesystem only — no helm involved) ───────────────

def test_prune_helm_cache_keeps_newest_version_dirs(tmp_path, monkeypatch):
    # Cache layout is HELM_CACHE_DIR/<registry>/<chart>/<version>/ — three
    # levels deep, never flat dirs.
    import time as _t
    chart_dir = tmp_path / "registry.example.com" / "appspace-ms"
    chart_dir.mkdir(parents=True)
    for i in range(5):
        d = chart_dir / f"260{i}.0.0"
        d.mkdir()
        ts = _t.time() - (5 - i) * 3600
        os.utime(d, (ts, ts))
    parked = chart_dir / "2699.0.0.stale-123"
    parked.mkdir()
    (tmp_path / "stray-file.txt").write_text("not a dir")
    monkeypatch.setattr(m, "HELM_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(m, "HELM_CACHE_MAX_CHARTS", 3)
    m._helm_chart_pull_ts.clear()
    m._prune_helm_cache()
    left = sorted(p.name for p in chart_dir.iterdir())
    assert "2604.0.0" in left and "2603.0.0" in left and "2602.0.0" in left
    assert "2600.0.0" not in left and "2601.0.0" not in left
    assert "2699.0.0.stale-123" not in left, "parked stale dirs are removed unconditionally"
    assert (tmp_path / "stray-file.txt").exists()
    m._helm_chart_pull_ts.clear()


def test_prune_helm_cache_missing_dir_is_a_noop(monkeypatch):
    monkeypatch.setattr(m, "HELM_CACHE_DIR", "/nonexistent/helm-cache-xyz")
    m._prune_helm_cache()  # must not raise


# ── format_comment: large-changeset summary mode ─────────────────────────

def test_format_comment_many_apps_renders_summary_table():
    results = {
        f"pv-many-{i:02d}-a-ms": m.DiffResult(
            "--- main\n+++ pr", [("Deployment/webx", "-a\n+b")], 3, True, "",
            m.OUT_DIFF, "")
        for i in range(30)
    }
    body = m.format_comment("a" * 12, results, base_sha="b" * 12)
    for name in results:
        assert name in body
    assert body.count("|") > 60, "expected a compact per-app summary table"


# ── main_iteration: Bitbucket outage branch ──────────────────────────────

def test_main_iteration_survives_bitbucket_outage(monkeypatch):
    monkeypatch.setattr(m, "argocd_login", lambda: None)
    monkeypatch.setattr(m, "discover_path_app_map", lambda: {})
    monkeypatch.setattr(m, "_prune_helm_cache", lambda *a, **k: None)
    monkeypatch.setattr(m, "_touch_progress", lambda: None)

    def boom(*a, **kw):
        raise _http_error(503)
    monkeypatch.setattr(m, "http", boom)
    logs = []
    monkeypatch.setattr(m, "log", lambda msg, *a, **k: logs.append(str(msg)))
    m.main_iteration()  # must not raise
    assert any("poll_fails" in l for l in logs)
