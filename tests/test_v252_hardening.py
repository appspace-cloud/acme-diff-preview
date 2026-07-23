"""Regression tests for the v2.5.2 critical-review round.

C1: a negative Content-Length bypasses the webhook body-size cap and makes
    self.rfile.read(length) read until EOF (unbounded) instead of being
    rejected. Both /diff-preview/webhook and /jfrog-webhook share the bug.
C2: the liveness heartbeat ticked _last_ok every 30s completely independent
    of whether the main loop was actually making progress, so a wedged main
    loop would report /healthz healthy forever and never get restarted.

Each test encodes one finding from CRITICAL_REVIEW_ROUND3.md and must FAIL
against the pre-fix code, then PASS once the fix lands.
"""
import hashlib
import hmac as hmac_mod
import importlib
import json
import os
import socket
import sys
import time

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


def _raw_post(port, path, headers_extra, body=b""):
    """Send a raw, hand-crafted HTTP POST — urllib will not let us set an
    invalid/negative Content-Length itself, and that is exactly the input
    we need to send to reproduce C1."""
    lines = [f"POST {path} HTTP/1.1", "Host: 127.0.0.1", "Connection: close"]
    for k, v in headers_extra.items():
        lines.append(f"{k}: {v}")
    request = ("\r\n".join(lines) + "\r\n\r\n").encode() + body
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(("127.0.0.1", port))
        s.sendall(request)
        try:
            data = s.recv(4096)
        except socket.timeout:
            pytest.fail("C1: server did not respond within 5s — "
                        "read(-1) is hanging on the unbounded body read")
    return data


# ── C1: negative Content-Length must not bypass the body-size cap ─────────
def test_c1_negative_content_length_rejected_bitbucket_webhook():
    mod = _import_module()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = mod._start_health_server(port)
    try:
        resp = _raw_post(port, "/diff-preview/webhook", {"Content-Length": "-1"})
        status_line = resp.split(b"\r\n", 1)[0].decode()
        assert " 413 " in status_line, (
            f"C1: negative Content-Length must be rejected 413, got: {status_line!r}")
    finally:
        srv.shutdown()


def test_c1_negative_content_length_rejected_jfrog_webhook():
    mod = _import_module()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = mod._start_health_server(port)
    try:
        resp = _raw_post(port, "/jfrog-webhook", {"Content-Length": "-1"})
        status_line = resp.split(b"\r\n", 1)[0].decode()
        assert " 413 " in status_line, (
            f"C1: negative Content-Length must be rejected 413, got: {status_line!r}")
    finally:
        srv.shutdown()


def test_c1_normal_oversized_length_still_rejected():
    """Guard the happy path: a plain oversized POSITIVE Content-Length must
    still be rejected exactly as before — the C1 fix must not loosen this."""
    mod = _import_module()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = mod._start_health_server(port)
    try:
        big = str(mod.JFROG_MAX_BODY_BYTES + 1)
        resp = _raw_post(port, "/diff-preview/webhook", {"Content-Length": big})
        status_line = resp.split(b"\r\n", 1)[0].decode()
        assert " 413 " in status_line
    finally:
        srv.shutdown()


def test_c1_valid_small_request_still_works():
    """Guard the happy path: a normal, correctly-signed small webhook call
    must still be accepted (200) after the fix."""
    mod = _import_module()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = mod._start_health_server(port)
    try:
        secret = os.environ["JFROG_WEBHOOK_SECRET"].encode()
        body = json.dumps({"event_type": "pushed",
                            "data": {"image_name": "x", "tag": "1.0.0"}}).encode()
        sig = hmac_mod.new(secret, body, hashlib.sha256).hexdigest()
        resp = _raw_post(port, "/jfrog-webhook",
                          {"Content-Length": str(len(body)),
                           "X-JFrog-Event-Auth": sig,
                           "Content-Type": "application/json"}, body=body)
        status_line = resp.split(b"\r\n", 1)[0].decode()
        assert " 202 " in status_line or " 200 " in status_line, status_line
    finally:
        srv.shutdown()


# ── C2: liveness heartbeat must reflect real main-loop progress ───────────
def test_c2_liveness_refreshes_when_idle():
    mod = _import_module()
    assert mod._liveness_should_refresh(token=5, last_seen_token=5, idle=True) is True


def test_c2_liveness_refreshes_when_progressed():
    mod = _import_module()
    assert mod._liveness_should_refresh(token=6, last_seen_token=5, idle=False) is True


def test_c2_liveness_does_not_refresh_when_wedged():
    """The core of C2: not idle AND no progress since the last tick must NOT
    vouch for liveness — this is exactly the case a hung main loop produces,
    and /healthz must be allowed to go stale so Kubernetes restarts the pod."""
    mod = _import_module()
    assert mod._liveness_should_refresh(token=5, last_seen_token=5, idle=False) is False


def test_c2_touch_progress_advances_token():
    mod = _import_module()
    before = mod._loop_progress_token
    mod._touch_progress()
    after = mod._loop_progress_token
    assert after != before, "_touch_progress() must advance the progress token"


def test_c2_touch_progress_thread_safe_under_concurrency():
    """Many threads touching progress concurrently must not raise and must
    leave the counter having advanced by some amount (no torn writes)."""
    import threading as _th
    mod = _import_module()
    before = mod._loop_progress_token
    threads = [_th.Thread(target=mod._touch_progress) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert mod._loop_progress_token != before


def test_c2_heartbeat_no_longer_ticks_unconditionally():
    """Structural guard against regressing back to the old unconditional
    `_last_ok = time.monotonic()` every 30s inside _beat(), which is exactly
    what made /healthz blind to a wedged main loop."""
    src = open(os.path.join(SRC, "diff_preview.py")).read()
    start = src.index("def _start_heartbeat")
    end = src.index("\ndef ", start + 10)
    beat_src = src[start:end]
    assert "_liveness_should_refresh" in beat_src, (
        "C2: _beat() must consult _liveness_should_refresh before bumping "
        "_last_ok, not tick it unconditionally every 30s"
    )


def test_c2_main_iteration_touches_progress():
    """Structural guard: main_iteration must checkpoint progress at coarse
    points (start, after discovery, after fetching PRs) so a genuinely long
    but healthy iteration is not mistaken for a wedged one."""
    src = open(os.path.join(SRC, "diff_preview.py")).read()
    start = src.index("def main_iteration")
    end = src.index("\ndef ", start + 10)
    body = src[start:end]
    assert body.count("_touch_progress()") >= 3, (
        "C2: main_iteration should checkpoint progress at several coarse "
        "points, not just once"
    )


def test_c2_per_app_diff_touches_progress():
    """Structural guard: the per-app diff completion loop (as_completed over
    run_diff futures) must checkpoint progress so a single very large PR
    (hundreds of apps) keeps refreshing liveness while genuinely working."""
    src = open(os.path.join(SRC, "diff_preview.py")).read()
    start = src.index("def process_batch")
    end = src.index("\n        def ", start + 10) if "\n        def " in src[start:start+4000] else start + 4000
    body = src[start:end]
    assert "_touch_progress()" in body, (
        "C2: process_batch's per-app as_completed loop must call "
        "_touch_progress() so long single-PR iterations stay live"
    )


def test_c2_main_loop_marks_idle_around_wake_wait():
    """Structural guard: the outer poll loop must mark the loop as idle
    right before blocking on _wake.wait, and clear it right after — this is
    the known-safe state _liveness_should_refresh relies on. The wait
    timeout became variable with HA (leader: 60s safety net, standby: 5s
    reactive poll for a handoff), so the guard matches the call shape
    rather than a literal 60.
    """
    src = open(os.path.join(SRC, "diff_preview.py")).read()
    start = src.index("def main()")
    body = src[start:]
    idx = body.index("_wake.wait(timeout=_idle_timeout)")
    before = body[max(0, idx - 300):idx]
    after = body[idx:idx + 300]
    assert "_loop_idle = True" in before, "must mark idle before waiting"
    assert "_loop_idle = False" in after, "must clear idle after waking"
