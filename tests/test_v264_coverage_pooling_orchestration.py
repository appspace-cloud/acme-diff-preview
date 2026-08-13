"""v2.6.4 coverage pass 2 — pooling edges, retry/raise contracts, input-panel
branches, OCI self-check loop, and main_iteration state self-healing.

Every test here targets a specific line identified as reachable-but-untested
by a fresh coverage.py run (as opposed to pass 1's pure helpers, or the
handful of lines confirmed genuinely dead and marked `pragma: no cover` in
src/diff_preview.py directly).
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
import logsink


# ── _proxy_for_host: NO_PROXY="*" wildcard disables proxying entirely ────

def test_proxy_for_host_no_proxy_wildcard_disables_all_proxying(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    monkeypatch.setenv("NO_PROXY", "*")
    assert m._proxy_for_host("api.bitbucket.org") is None


# ── _pooled_urlopen: query string, reused-socket rearm, close-failure ────

@pytest.fixture(autouse=True)
def _pooling_on(monkeypatch):
    monkeypatch.setattr(m, "HTTP_POOLING_ENABLED", True)
    for k in list(os.environ):
        if k.lower().endswith("_proxy") or k.lower() == "no_proxy":
            monkeypatch.delenv(k, raising=False)
    if hasattr(m._http_conn_local, "conns"):
        del m._http_conn_local.conns


class _FakeResp:
    status = 200
    import email.message
    headers = email.message.Message()

    def read(self):
        return b"{}"


def test_pooled_urlopen_includes_query_string_in_path(monkeypatch):
    # L1075: a request with a query string must have it appended to path.
    captured = {}

    class _Conn:
        def __init__(self, *a, **k):
            pass

        def request(self, method, path, body=None, headers=None):
            captured["path"] = path

        def getresponse(self):
            return _FakeResp()

        def close(self):
            pass

    monkeypatch.setattr(m._http_client, "HTTPSConnection", _Conn)
    req = urllib.request.Request(
        "https://api.bitbucket.org/2.0/x?pagelen=50&page=2")
    with m._pooled_urlopen(req, timeout=10) as r:
        r.read()
    assert captured["path"] == "/2.0/x?pagelen=50&page=2"


def test_pooled_urlopen_reused_connection_rearms_live_socket_timeout(monkeypatch):
    # L1093-1095: a REUSED connection with a live .sock must have that
    # socket's timeout re-armed to THIS call's timeout, not the original's.
    sock_calls = []

    class _FakeSock:
        def settimeout(self, t):
            sock_calls.append(t)

    class _Conn:
        def __init__(self, *a, **k):
            self.sock = _FakeSock()

        def request(self, *a, **k):
            pass

        def getresponse(self):
            return _FakeResp()

        def close(self):
            pass

    monkeypatch.setattr(m._http_client, "HTTPSConnection", _Conn)
    req = urllib.request.Request("https://api.bitbucket.org/2.0/x")
    with m._pooled_urlopen(req, timeout=15) as r1:
        r1.read()
    with m._pooled_urlopen(req, timeout=42) as r2:
        r2.read()
    assert 42 in sock_calls, \
        "second (reused) call must rearm the live socket's timeout"


def test_pooled_urlopen_cleanup_close_failure_is_swallowed(monkeypatch):
    # L1112-1115: when a reused connection's request fails AND the cleanup
    # conn.close() itself raises, that failure must be swallowed — the
    # retry-on-fresh-connection path must still succeed.
    class _Conn:
        def __init__(self, *a, **k):
            self.reqs = 0

        def request(self, *a, **k):
            self.reqs += 1
            if self.reqs > 1:
                raise ConnectionResetError("stale keep-alive")

        def getresponse(self):
            return _FakeResp()

        def close(self):
            raise OSError("close failed too")

    monkeypatch.setattr(m._http_client, "HTTPSConnection", _Conn)
    req = urllib.request.Request("https://api.bitbucket.org/2.0/x")
    with m._pooled_urlopen(req, timeout=10) as r1:
        r1.read()  # fresh conn, succeeds, stays pooled
    # Second call reuses the pooled conn; its request() raises, its close()
    # ALSO raises during cleanup — must not propagate, must retry on fresh.
    with m._pooled_urlopen(req, timeout=10) as r2:
        assert r2.read() == b"{}"


# ── find_existing_comment: fast-path raise contracts (correct cache key) ──

def test_find_existing_comment_fastpath_non_404_http_error_raises(monkeypatch):
    # L4337-4338: cached comment id present, bb() raises a non-404
    # HTTPError -> must propagate (transient, same contract as full scan).
    ck = (m.BB_REPO, 88801)
    with m._comment_id_cache_lock:
        m._comment_id_cache[ck] = 5555

    def boom(method, path, **k):
        raise urllib.error.HTTPError("u", 500, "bb down", None, None)

    monkeypatch.setattr(m, "bb", boom)
    try:
        with pytest.raises(urllib.error.HTTPError):
            m.find_existing_comment(88801)
    finally:
        with m._comment_id_cache_lock:
            m._comment_id_cache.pop(ck, None)


def test_find_existing_comment_fastpath_generic_exception_raises(monkeypatch):
    # L4339-4340: any other exception on the fast path must also propagate.
    ck = (m.BB_REPO, 88802)
    with m._comment_id_cache_lock:
        m._comment_id_cache[ck] = 5556

    def boom(method, path, **k):
        raise RuntimeError("weird transient")

    monkeypatch.setattr(m, "bb", boom)
    try:
        with pytest.raises(RuntimeError):
            m.find_existing_comment(88802)
    finally:
        with m._comment_id_cache_lock:
            m._comment_id_cache.pop(ck, None)


# ── _ensure_chart: login failure at the ERROR escalation threshold ───────

def test_ensure_chart_login_failure_logs_error_at_threshold(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "HELM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(m, "_helm_login", lambda registry: False)
    with m._helm_cache_lock:
        m._helm_chart_cache.clear()
    m._helm_chart_pull_ts.clear()
    with m._oci_health_lock:
        prior = m._diff_stats["oci_consecutive_pull_failures"]
        m._diff_stats["oci_consecutive_pull_failures"] = m.OCI_FAIL_ERROR_THRESHOLD - 1
    logged = []
    monkeypatch.setattr(logsink, "log",
                        lambda msg, severity="INFO", **k: logged.append((severity, msg)))
    try:
        assert m._ensure_chart("registry.example.com", "appspace-ms", "9.0.0") is None
        assert any(sev == "ERROR" and "persistently failing" in msg
                   for sev, msg in logged), logged
    finally:
        with m._oci_health_lock:
            m._diff_stats["oci_consecutive_pull_failures"] = prior


# ── OCI self-check loop: disabled path + exception/shutdown in the loop ──

def test_oci_selfcheck_loop_disabled_when_interval_non_positive(monkeypatch):
    # L1973-1974: interval <= 0 must return before even building the thread.
    monkeypatch.setattr(m, "OCI_SELFCHECK_INTERVAL", 0)
    constructed = []
    monkeypatch.setattr(m.threading, "Thread",
                        lambda *a, **k: constructed.append(k))
    assert m._start_oci_selfcheck_loop() is None
    assert constructed == []


@pytest.mark.no_thread_stub
def test_oci_selfcheck_loop_survives_exception_and_honors_shutdown(monkeypatch):
    # L1979-1981 (exception swallowed) and L1983-1984 (shutdown observed
    # mid-wait) inside the daemon loop body, run synchronously (no real
    # thread/sleep) by capturing the target function ourselves.
    monkeypatch.setattr(m, "OCI_SELFCHECK_INTERVAL", 30)

    def _boom():
        raise RuntimeError("selfcheck boom")

    monkeypatch.setattr(m, "_oci_selfcheck", _boom)

    sleep_calls = {"n": 0}

    def fake_sleep(secs):
        sleep_calls["n"] += 1
        if sleep_calls["n"] == 2:
            m._shutdown = True

    monkeypatch.setattr(m.time, "sleep", fake_sleep)

    captured = {}

    class _FakeThread:
        def __init__(self, target=None, daemon=None, name=None):
            captured["target"] = target

        def start(self):
            captured["target"]()

    monkeypatch.setattr(m.threading, "Thread", _FakeThread)
    m._shutdown = False
    try:
        m._start_oci_selfcheck_loop()
    finally:
        m._shutdown = False
    assert sleep_calls["n"] >= 2


# ── _summarize_input_changes: every branch of the input-diff panel ───────

def _fetch_factory(files):
    def fake_fetch(path, sha, repo=None):
        if path in files:
            return files[path], m.BB_OK
        return None, m.BB_NOT_FOUND
    return fake_fetch


def test_summarize_input_changes_skips_non_yaml_files(monkeypatch):
    # L5347-5348: a changed file that isn't .yaml/.yml is skipped outright.
    files = {
        "a/customer.yaml__pr": "appspace:\n  version: 2.0.0\n",
        "a/customer.yaml__base": "appspace:\n  version: 1.0.0\n",
    }

    def fake_fetch(path, sha, repo=None):
        return files[f"{path}__{sha}"], m.BB_OK

    monkeypatch.setattr(m, "_bb_fetch_status", fake_fetch)
    out = m._summarize_input_changes(["a/README.md", "a/customer.yaml"],
                                     "pr", "base")
    joined = "\n".join(out)
    assert "README.md" not in joined
    assert "customer.yaml" in joined


def test_summarize_input_changes_notes_unparseable_yaml(monkeypatch):
    # L5355-5358: YAMLError on either side is reported, not fatal.
    monkeypatch.setattr(m, "_bb_fetch_status",
                        _fetch_factory({"x/customer.yaml": "a: [1, 2\n"}))
    out = m._summarize_input_changes(["x/customer.yaml"], "s", "s")
    joined = "\n".join(out)
    assert "not parseable as YAML" in joined


def test_summarize_input_changes_notes_no_scannable_keys(monkeypatch):
    # L5359-5361: both sides parse to a non-mapping (no dotted keys at all).
    monkeypatch.setattr(m, "_bb_fetch_status",
                        _fetch_factory({"x/customer.yaml": "just plain text\n"}))
    out = m._summarize_input_changes(["x/customer.yaml"], "s", "s")
    joined = "\n".join(out)
    assert "no scannable keys" in joined


def test_summarize_input_changes_per_file_overflow_adds_more_line(monkeypatch):
    # L5377-5380: a single file with more changes than the line budget gets
    # truncated with an explicit "+N more change(s)" marker.
    old_lines = "\n".join(f"key{i}: old{i}" for i in range(30))
    new_lines = ""  # every key removed -> 30 "removed" entries in one file
    files = {("x/customer.yaml", "pr"): new_lines,
             ("x/customer.yaml", "base"): old_lines}

    def fake_fetch(path, sha, repo=None):
        return files.get((path, sha), ""), m.BB_OK

    monkeypatch.setattr(m, "_bb_fetch_status", fake_fetch)
    out = m._summarize_input_changes(["x/customer.yaml"], "pr", "base")
    joined = "\n".join(out)
    assert "more change(s) in this file" in joined


def test_summarize_input_changes_stops_once_budget_exhausted(monkeypatch):
    # L5382-5384: once the running budget hits zero, the outer loop breaks
    # and later files are never even considered.
    old_lines = "\n".join(f"key{i}: old{i}" for i in range(30))
    files = {("a/customer.yaml", "base"): old_lines,
             ("a/customer.yaml", "pr"): "",
             ("b/customer.yaml", "base"): "z: 1\n",
             ("b/customer.yaml", "pr"): "z: 2\n"}

    def fake_fetch(path, sha, repo=None):
        return files.get((path, sha), ""), m.BB_OK

    monkeypatch.setattr(m, "_bb_fetch_status", fake_fetch)
    out = m._summarize_input_changes(
        ["a/customer.yaml", "b/customer.yaml"], "pr", "base")
    joined = "\n".join(out)
    assert "a/customer.yaml" in joined
    assert "b/customer.yaml" not in joined, \
        "budget exhausted on the first file must skip the second entirely"


# ── main_iteration: _main_render_sha self-heals, legacy _seen keys evict ──

def _quiet_iteration_edges(monkeypatch, prs, base_sha="deadbeefcafe"):
    monkeypatch.setattr(m, "argocd_login", lambda: None)
    monkeypatch.setattr(m, "discover_path_app_map", lambda: {})
    monkeypatch.setattr(m, "get_open_prs", lambda repo=None: prs)
    monkeypatch.setattr(m, "_prune_helm_cache", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(m, "_touch_progress", lambda: None)
    monkeypatch.setattr(m, "process_pr", lambda *a, **k: None)
    monkeypatch.setattr(m, "http", lambda method, url, **kw: {"target": {"hash": base_sha}})


def test_main_iteration_heals_non_dict_main_render_sha(monkeypatch):
    # L6433-6434: a corrupted/legacy _main_render_sha (not a dict) must be
    # replaced with a fresh dict rather than crash the whole iteration.
    _quiet_iteration_edges(monkeypatch, [])
    backup = m._main_render_sha
    m._main_render_sha = "not-a-dict-anymore"
    try:
        m.main_iteration()
        assert isinstance(m._main_render_sha, dict)
    finally:
        m._main_render_sha = backup


def test_main_iteration_evicts_legacy_non_tuple_seen_keys(monkeypatch):
    # L6465-6466: a _seen key that isn't a (repo, pr_id) tuple (pre-multirepo
    # shape, or any foreign key) must be evicted unconditionally.
    _quiet_iteration_edges(monkeypatch, [])
    with m._seen_lock:
        m._seen[12345] = ("somesha", "othersha")
    try:
        m.main_iteration()
        with m._seen_lock:
            assert 12345 not in m._seen
    finally:
        with m._seen_lock:
            m._seen.pop(12345, None)
