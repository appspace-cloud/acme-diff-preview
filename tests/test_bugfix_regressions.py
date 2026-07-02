"""Regression tests for the v2.4.4 bug-hunt fixes (F1-F5).

Born as bughunt/test_findings.py: each test asserted the CORRECT behavior
and FAILED against v2.4.3, proving the bug empirically. The v2.4.4 fixes
turned them green; they now guard against regressions. Full analysis with
proposed (now implemented) solutions: bughunt/FINDINGS.md.
"""
import hashlib
import hmac as hmac_mod
import importlib
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

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


# ── F1: PR diff must be recomputed when main advances ─────────────────────────
def test_f1_recompute_when_main_advances(monkeypatch):
    """The published diff is rendered against main at computation time.
    When main moves (another PR merges changes for the same apps), the old
    comment is stale, but both dedups only compare the PR source sha.
    process_pr already RECEIVES base_sha and ignores it in the decision."""
    mod = _import_module()
    ran = []
    pr_sha = "abcd1234" + "0" * 32
    marker_comment = f"diff ok [{mod.COMMENT_MARKER} [clean]] sha"
    monkeypatch.setattr(mod, "find_existing_comment",
                        lambda pid: (99, pr_sha[:8], marker_comment))
    monkeypatch.setattr(mod, "fix_stuck_inprogress", lambda *a, **k: None)
    monkeypatch.setattr(mod, "post_build_status", lambda *a, **k: None)
    monkeypatch.setattr(mod, "upsert_comment", lambda *a, **k: None)
    monkeypatch.setattr(mod, "get_pr_changed_files",
                        lambda pid: ran.append("diff-ran") or [])
    pr = {"id": 1, "title": "t",
          "source": {"commit": {"hash": pr_sha}},
          "destination": {"branch": {"name": "main"}}}

    mod.process_pr(pr, {}, base_sha="main-sha-1")   # comment covers pr_sha
    with mod._seen_lock:                            # isolate cross-pod check
        mod._seen.clear()
    mod.process_pr(pr, {}, base_sha="main-sha-2")   # MAIN ADVANCED

    assert "diff-ran" in ran, (
        "BUG F1: PR skipped although main advanced. The comment now shows a "
        "diff against an old main; dedup must include base_sha (it is already "
        "a parameter of process_pr and is ignored)."
    )


# ── F2: transient PUT failure must not create a duplicate comment ────────────
def test_f2_no_duplicate_comment_on_transient_put_failure(monkeypatch):
    """upsert_comment falls back to POST on ANY exception. That fallback is
    only correct when the comment was deleted (404). For 5xx/429 the comment
    still exists, so the POST creates a duplicate that is never cleaned up."""
    mod = _import_module()
    ops = []

    def fake_bb(method, path, **kw):
        ops.append(method)
        if method == "PUT":
            raise urllib.error.HTTPError("u", 502, "Bad Gateway", None, None)
        return {}

    monkeypatch.setattr(mod, "bb", fake_bb)
    mod.upsert_comment(7, "body", existing_id=123)

    assert ops.count("POST") == 0, (
        "BUG F2: transient PUT failure (502) fell back to POST -> duplicate "
        "comment. Fallback must be restricted to HTTP 404 (comment deleted)."
    )


# ── F3: webhook burst must not spawn unbounded refresh threads ────────────────
def test_f3_webhook_burst_bounded_concurrency(monkeypatch):
    """One daemon thread is spawned per distinct chart:version push. A CI
    mass-republish (dozens of charts in a minute - observed in production
    during the rev1 burst) creates dozens of concurrent hard-refresh threads
    hammering the ArgoCD API with no cap."""
    mod = _import_module()
    monkeypatch.setattr(mod, "_invalidate_for_republish", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_jfrog_hard_refresh", lambda *a, **k: time.sleep(2.5))

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = mod._start_health_server(port)
    try:
        secret = os.environ["JFROG_WEBHOOK_SECRET"].encode()
        n = 24
        for i in range(n):
            body = json.dumps({"event_type": "pushed",
                               "data": {"image_name": "bughunt-chart",
                                        "tag": f"9.9.{i}"}}).encode()
            sig = hmac_mod.new(secret, body, hashlib.sha256).hexdigest()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/jfrog-webhook", data=body,
                headers={"X-JFrog-Event-Auth": sig,
                         "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).read()

        peak = 0
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            alive = [t for t in threading.enumerate()
                     if t.name.startswith("jfrog-refresh-")]
            peak = max(peak, len(alive))
            time.sleep(0.05)
    finally:
        srv.shutdown()

    assert peak <= 8, (
        f"BUG F3: {peak} concurrent jfrog-refresh threads for {n} distinct "
        "pushes - unbounded. Use a small worker pool / queue (e.g. 4 workers) "
        "so a CI republish burst cannot hammer the ArgoCD API."
    )


# ── F4: http() must honor Retry-After on 429 ─────────────────────────────────
def test_f4_http_honors_retry_after(monkeypatch):
    """Bitbucket rate-limit windows last ~60s and send Retry-After. http()
    retries at 1s+2s and gives up (~3s total), so during a rate-limit storm
    every call in the iteration fails through - and via F2 each failed PUT
    also duplicates a comment."""
    mod = _import_module()
    sleeps = []
    import email.message
    hdrs = email.message.Message()
    hdrs["Retry-After"] = "30"

    def fake_urlopen(req, **kw):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests",
                                     hdrs, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(mod.time, "sleep", lambda s: sleeps.append(s))

    with pytest.raises(urllib.error.HTTPError):
        mod.http("GET", "https://api.bitbucket.org/2.0/x")

    assert sum(sleeps) >= 30, (
        f"BUG F4: server mandated Retry-After: 30s but http() only backed off "
        f"{sum(sleeps)}s in total before failing through."
    )


# ── F5: the manifest parser must not silently drop resources ─────────────────
def test_f5a_flow_style_metadata_not_dropped():
    """helm templates can emit flow-style metadata. The line-based parser
    only matches block style ('  name:'), so the whole resource is skipped:
    a real change reports as NO-DIFF."""
    mod = _import_module()
    main = ('apiVersion: v1\nkind: ConfigMap\n'
            'metadata: {name: cm1, namespace: x}\ndata:\n  a: "1"\n')
    pr = main.replace('"1"', '"2"')
    assert mod._diff_manifests(main, pr) != "", (
        "BUG F5a: resource with flow-style metadata is invisible to the "
        "diff - a real change reports as no-diff."
    )


def test_f5b_duplicate_resource_key_not_silently_overwritten():
    """If a render emits the same (kind, ns, name) twice (umbrella charts
    merging subchart output), dict insertion keeps only the LAST one. A PR
    change in the FIRST copy is invisible."""
    mod = _import_module()
    doc_v1 = 'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: dup\ndata:\n  a: "1"\n'
    doc_v2 = doc_v1.replace('"1"', '"2"')
    other = 'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: dup\ndata:\n  b: "9"\n'
    main = doc_v1 + "---\n" + other
    pr = doc_v2 + "---\n" + other
    assert mod._diff_manifests(main, pr) != "", (
        "BUG F5b: duplicate (kind, ns, name) documents - the last one "
        "overwrites the first, so the change is invisible."
    )
