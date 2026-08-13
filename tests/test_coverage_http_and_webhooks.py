"""Coverage campaign (post v2.5.15), pass C: the outer I/O layer.

Previously live-only paths, now exercised deterministically:
- http(): the REAL retry/backoff engine against a local HTTP stub server
  (success, 5xx retry, 429 Retry-After honoring + cap, network error).
- bb(): URL composition and credential wiring.
- Webhook HMAC verifiers (JFrog + Bitbucket), including the v2.5.3 CRIT-2
  non-ASCII header regression and Bitbucket's permissive empty-secret mode.
- discover_path_app_map() + _extract_app_chart_info(): a fake `argocd`
  binary returns a realistic multi-source app list JSON.
- _jfrog_hard_refresh(): matching apps by chart:revision and refreshing them.
"""
import hashlib
import hmac as hmac_mod
import json
import os
import stat
import sys
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m  # noqa: E402
import logsink


# ── local HTTP stub ──────────────────────────────────────────────────────

class _Stub(BaseHTTPRequestHandler):
    hits = []
    plan = []          # list of (status, payload_dict_or_None, extra_headers)

    def log_message(self, *a):  # silence
        pass

    def _serve(self):
        body = b""
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            body = self.rfile.read(n)
        _Stub.hits.append({
            "method": self.command, "path": self.path,
            "auth": self.headers.get("Authorization", ""),
            "body": body.decode() if body else "",
        })
        status, payload, extra = (_Stub.plan.pop(0) if _Stub.plan else (200, {"ok": True}, {}))
        self.send_response(status)
        for k, v in extra.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if payload is not None:
            self.wfile.write(json.dumps(payload).encode())

    do_GET = do_POST = do_PUT = do_DELETE = _serve


@pytest.fixture()
def stub():
    _Stub.hits = []
    _Stub.plan = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", _Stub
    srv.shutdown()


# ── http() ───────────────────────────────────────────────────────────────

def test_http_get_success_parses_json(stub):
    url, s = stub
    s.plan = [(200, {"hello": "world"}, {})]
    assert m.http("GET", f"{url}/x") == {"hello": "world"}


def test_http_post_sends_json_body_and_basic_auth(stub):
    url, s = stub
    s.plan = [(200, {"ok": 1}, {})]
    m.http("POST", f"{url}/y", body={"a": 1}, auth=("user", "pass"))
    hit = s.hits[0]
    assert hit["method"] == "POST"
    assert json.loads(hit["body"]) == {"a": 1}
    assert hit["auth"].startswith("Basic ")


def test_http_retries_5xx_then_succeeds(stub, monkeypatch):
    url, s = stub
    s.plan = [(503, None, {}), (200, {"recovered": True}, {})]
    sleeps = []
    monkeypatch.setattr(m.time, "sleep", lambda w: sleeps.append(w))
    assert m.http("GET", f"{url}/z") == {"recovered": True}
    assert len(s.hits) == 2 and sleeps == [1]


def test_http_429_honors_retry_after_capped_at_60(stub, monkeypatch):
    url, s = stub
    s.plan = [(429, None, {"Retry-After": "45"}),
              (429, None, {"Retry-After": "9999"}),
              (200, {"ok": True}, {})]
    sleeps = []
    monkeypatch.setattr(m.time, "sleep", lambda w: sleeps.append(w))
    assert m.http("GET", f"{url}/rl") == {"ok": True}
    # First wait: server-mandated 45s (>> exponential 1s). Second: capped at 60.
    assert sleeps == [45, 60]


def test_http_gives_up_after_three_5xx(stub, monkeypatch):
    url, s = stub
    s.plan = [(500, None, {}), (500, None, {}), (500, None, {})]
    monkeypatch.setattr(m.time, "sleep", lambda w: None)
    with pytest.raises(urllib.error.HTTPError):
        m.http("GET", f"{url}/dead")
    assert len(s.hits) == 3


def test_http_4xx_raises_immediately_no_retry(stub, monkeypatch):
    url, s = stub
    s.plan = [(404, None, {})]
    monkeypatch.setattr(m.time, "sleep", lambda w: None)
    with pytest.raises(urllib.error.HTTPError):
        m.http("GET", f"{url}/missing")
    assert len(s.hits) == 1


def test_http_network_error_retries_then_raises(monkeypatch):
    # Nothing listens on this port: pure connection-refused path.
    monkeypatch.setattr(m.time, "sleep", lambda w: None)
    with pytest.raises((urllib.error.URLError, OSError)):
        m.http("GET", "http://127.0.0.1:1/nothing")


# ── bb() ─────────────────────────────────────────────────────────────────

def test_bb_composes_repo_url_and_credentials(monkeypatch):
    captured = {}

    def fake_http(method, url, **kw):
        captured.update(method=method, url=url, **kw)
        return {"ok": True}

    monkeypatch.setattr(m, "http", fake_http)
    m.bb("GET", "pullrequests/42")
    assert captured["method"] == "GET"
    assert captured["url"] == (
        f"https://api.bitbucket.org/2.0/repositories/{m.BB_WORKSPACE}/{m.BB_REPO}/pullrequests/42")
    assert captured["auth"] == (m.BB_USER, m.BB_TOKEN)


# ── webhook HMAC verifiers ───────────────────────────────────────────────

def test_jfrog_hmac_accepts_correct_signature(monkeypatch):
    monkeypatch.setattr(m, "JFROG_WEBHOOK_SECRET", "topsecret")
    body = b'{"event":"pushed"}'
    good = hmac_mod.new(b"topsecret", body, hashlib.sha256).hexdigest()
    assert m._verify_jfrog_hmac(body, good) is True
    assert m._verify_jfrog_hmac(body, good[:-1] + "0") is False


def test_jfrog_hmac_rejects_missing_secret_or_header(monkeypatch):
    monkeypatch.setattr(m, "JFROG_WEBHOOK_SECRET", "")
    assert m._verify_jfrog_hmac(b"x", "anything") is False
    monkeypatch.setattr(m, "JFROG_WEBHOOK_SECRET", "s")
    assert m._verify_jfrog_hmac(b"x", "") is False


def test_jfrog_hmac_non_ascii_header_is_a_mismatch_not_a_crash(monkeypatch):
    # v2.5.3 CRIT-2 regression: a single unauthenticated request with a
    # non-ASCII signature header used to raise TypeError inside
    # hmac.compare_digest and crash the request thread.
    monkeypatch.setattr(m, "JFROG_WEBHOOK_SECRET", "s")
    assert m._verify_jfrog_hmac(b"x", "ñÿ\u2603") is False


def test_bb_hmac_permissive_when_secret_unset(monkeypatch):
    monkeypatch.setattr(m, "BB_WEBHOOK_SECRET", "")
    assert m._verify_bb_hmac(b"payload", "") is True


def test_bb_hmac_strict_with_secret_and_sha256_prefix(monkeypatch):
    monkeypatch.setattr(m, "BB_WEBHOOK_SECRET", "bbsecret")
    body = b'{"pullrequest":{}}'
    good = hmac_mod.new(b"bbsecret", body, hashlib.sha256).hexdigest()
    assert m._verify_bb_hmac(body, f"sha256={good}") is True
    assert m._verify_bb_hmac(body, f"sha256={good[:-2]}ff") is False
    assert m._verify_bb_hmac(body, "") is False
    assert m._verify_bb_hmac(body, "sha256=ñ") is False


# ── fake argocd world: discovery + JFrog hard refresh ────────────────────

APPS_JSON = [
    {
        "metadata": {"name": "pv-synth-a-ms", "namespace": "argocd",
                     "annotations": {"argocd.argoproj.io/manifest-generate-paths":
                                     "gcp/dev/private-cloud/ap1/custom/pv-synth-a"}},
        "spec": {
            "destination": {"namespace": "pv-synth-a"},
            "sources": [
                {"repoURL": "oci://registry.example.com/charts", "chart": "appspace-ms",
                 "targetRevision": "2603.0.1-dev",
                 "helm": {"valueFiles": ["$values/gcp/dev/private-cloud/ap1/custom/pv-synth-a/customer.yaml"]}},
                {"repoURL": "git@bitbucket.org:x/acme-config-dev.git", "ref": "values"},
            ],
        },
    },
    {
        "metadata": {"name": "pv-other-a-ms", "namespace": "argocd",
                     "annotations": {"argocd.argoproj.io/manifest-generate-paths":
                                     "gcp/dev/private-cloud/ap1/custom/pv-other-a"}},
        "spec": {
            "destination": {"namespace": "pv-other-a"},
            "sources": [
                {"repoURL": "oci://registry.example.com/charts", "chart": "appspace-ms",
                 "targetRevision": "2602.9.9-dev",
                 "helm": {"valueFiles": ["$values/gcp/dev/private-cloud/ap1/custom/pv-other-a/customer.yaml"]}},
            ],
        },
    },
]


def _mk_fake_argocd(tmp_path, apps_json, refresh_rc=0):
    payload = json.dumps(apps_json).replace("'", "'\\''")
    p = tmp_path / "argocd"
    p.write_text(f"""#!/bin/bash
case "$*" in
  *"app list"*) printf '%s' '{payload}'; exit 0;;
  *"--hard-refresh"*) exit {refresh_rc};;
  *) exit 0;;
esac
""")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


@pytest.fixture()
def clean_discovery(monkeypatch):
    monkeypatch.setattr(m, "_path_map_cache", {}, raising=False)
    monkeypatch.setattr(m, "_path_map_ts", 0.0, raising=False)
    yield


def test_discover_path_app_map_builds_maps_from_annotations(tmp_path, monkeypatch, clean_discovery):
    fake = _mk_fake_argocd(tmp_path, APPS_JSON)
    monkeypatch.setattr(m, "ARGOCD_BIN", fake)
    path_map = m.discover_path_app_map()
    joined = json.dumps(path_map)
    assert "pv-synth-a-ms" in joined and "pv-other-a-ms" in joined
    # Chart metadata captured for the republish-invalidation machinery.
    assert m._app_chart_map.get("pv-synth-a-ms") == "appspace-ms"
    assert m._app_chart_revision_map.get("pv-synth-a-ms") == "2603.0.1-dev"
    # Second call within TTL: served from cache (no subprocess).
    monkeypatch.setattr(m, "ARGOCD_BIN", "/nonexistent/argocd")
    assert m.discover_path_app_map() == path_map


def test_discover_path_app_map_raises_on_cli_failure(tmp_path, monkeypatch, clean_discovery):
    p = tmp_path / "argocd"
    p.write_text("#!/bin/bash\necho nope >&2; exit 1\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(m, "ARGOCD_BIN", str(p))
    with pytest.raises(RuntimeError, match="app list failed"):
        m.discover_path_app_map()


def test_discover_path_app_map_raises_on_bad_json(tmp_path, monkeypatch, clean_discovery):
    p = tmp_path / "argocd"
    p.write_text("#!/bin/bash\nprintf 'not-json'; exit 0\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(m, "ARGOCD_BIN", str(p))
    with pytest.raises(RuntimeError, match="invalid JSON"):
        m.discover_path_app_map()


def test_jfrog_hard_refresh_matches_by_chart_and_revision(tmp_path, monkeypatch, capsys):
    fake = _mk_fake_argocd(tmp_path, APPS_JSON)
    monkeypatch.setattr(m, "ARGOCD_BIN", fake)
    logs = []
    monkeypatch.setattr(logsink, "log", lambda msg, *a, **k: logs.append(str(msg)))
    m._jfrog_hard_refresh("appspace-ms", "2603.0.1-dev")
    joined = "\n".join(logs)
    assert "pv-synth-a-ms" in joined
    assert "pv-other-a-ms" not in joined.replace("looking for apps", "")


def test_jfrog_hard_refresh_no_match_logs_and_returns(tmp_path, monkeypatch):
    fake = _mk_fake_argocd(tmp_path, APPS_JSON)
    monkeypatch.setattr(m, "ARGOCD_BIN", fake)
    logs = []
    monkeypatch.setattr(logsink, "log", lambda msg, *a, **k: logs.append(str(msg)))
    m._jfrog_hard_refresh("appspace-ms", "9.9.9-nope")
    assert any("no apps found" in l for l in logs)


def test_jfrog_hard_refresh_survives_cli_failure(tmp_path, monkeypatch):
    p = tmp_path / "argocd"
    p.write_text("#!/bin/bash\necho denied >&2; exit 1\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(m, "ARGOCD_BIN", str(p))
    logs = []
    monkeypatch.setattr(logsink, "log", lambda msg, *a, **k: logs.append(str(msg)))
    m._jfrog_hard_refresh("appspace-ms", "1.0.0")  # must not raise
    assert any("app list failed" in l for l in logs)
