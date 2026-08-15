"""COPS-2673 security hardening — one behavioural test per pentest fix.

Each test asserts the exploit is now closed AND, where a regression could
silently reopen it, that the benign case still works. These pin the fixes so a
future edit that reverts the guard turns the suite red.

Fixes covered:
  SL-1/SL-3  redact.py     credentials in a value under a benign key
  DOS-1      diff_ui /
             render_cache  zstd decompression-bomb cap
  CAI-1      diff_preview  `--` terminator on `helm template`
  SSRF-1     diff_preview  off-host pagination link refused
  PT-1       diff_preview  unsafe OCI registry refused
  XSS-01     diff_ui       PR href limited to http/https
  XSS-02     diff_preview  security headers on the /diff response
"""
import json
import os
import sys
import urllib.request

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import diff_preview as m  # noqa: E402
import diff_ui  # noqa: E402
import redact  # noqa: E402
import render_cache  # noqa: E402


def _redact(text):
    """The real pipeline applied before diff text leaves for Vertex/comment."""
    return redact._redact_k8s_env_pairs(redact._redact_sensitive(text))


# ── SL-1 / SL-3 : secrets hiding in a value under a benign key ────────────

def test_url_userinfo_password_is_masked_under_a_benign_key():
    """DATABASE_URL / SERVICE_URL carry the credential in the value, and the
    key is not in the sensitive list. The password must not survive to Vertex,
    the comment or the cache."""
    for key in ("DATABASE_URL", "SERVICE_URL", "SOME_ENDPOINT"):
        out = _redact(f"        - name: {key}\n"
                      f"          value: postgres://admin:hunter2pass@db:5432/x\n")
        assert "hunter2pass" not in out, f"{key}: password leaked:\n{out}"
        assert "[REDACTED]" in out


def test_url_username_and_host_are_kept_only_the_password_is_masked():
    """The mask must be surgical: diff context (which user, which host) stays,
    so the reviewer still sees what changed."""
    out = _redact("        value: mysql://appuser:s3cr3t@db.prod:3306/main\n")
    assert "appuser" in out and "db.prod:3306" in out
    assert "s3cr3t" not in out
    assert "appuser:[REDACTED]@" in out


def test_sensitive_key_in_a_flow_mapping_is_masked():
    """A flow-style mapping on a non-Secret resource hides the sensitive key
    inside {...}, past the block/env passes."""
    out = _redact("data: {password: hunter2secret, user: bob}\n")
    assert "hunter2secret" not in out
    assert "user: bob" in out           # the benign neighbour is untouched


def test_plain_urls_without_credentials_are_not_touched():
    """No false positives: a URL with no userinfo, and an ssh remote, are
    ordinary diff content and must be preserved verbatim."""
    for line in ("image: http://registry.example.com/app:1.2.3\n",
                 "url: git@github.com:org/repo.git\n",
                 "homepage: https://example.com/docs\n"):
        assert _redact(line).strip() == line.strip(), line


def test_named_sensitive_keys_still_redact():
    """The value-shape pass must not have displaced the existing key-name
    redaction."""
    assert "topsecret" not in _redact("password: topsecret\n")
    assert "topsecret" not in _redact(
        "        - name: PASSWORD\n          value: topsecret\n")


# ── DOS-1 : zstd decompression bomb ───────────────────────────────────────

def _zstd(raw):
    import zstandard as zstd
    return zstd.ZstdCompressor().compress(raw)


def test_zstd_bomb_is_capped_not_expanded(monkeypatch):
    monkeypatch.setattr(diff_ui, "_ZSTD_MAX_DECOMPRESS_BYTES", 1 << 20)  # 1 MiB
    import zstandard as zstd
    bomb = _zstd(b"\0" * (16 << 20))    # 16 MiB from a tiny frame
    try:
        diff_ui._zstd_decompress_capped(zstd, bomb)
        assert False, "a 16 MiB expansion slipped past the 1 MiB cap"
    except ValueError as e:
        assert "cap" in str(e)


def test_legit_payload_below_the_cap_still_decodes(monkeypatch):
    monkeypatch.setattr(diff_ui, "_ZSTD_MAX_DECOMPRESS_BYTES", 1 << 20)
    art = _zstd(json.dumps({"ok": 1}).encode())
    assert diff_ui._decode_artifact_bytes(art) == {"ok": 1}


def test_render_cache_decode_shares_the_cap(monkeypatch):
    monkeypatch.setattr(diff_ui, "_ZSTD_MAX_DECOMPRESS_BYTES", 1 << 20)
    import zstandard as zstd
    bomb = _zstd(b"\0" * (16 << 20))
    try:
        render_cache._main_render_gcs_decode(bomb)
        assert False, "render decode did not cap the bomb"
    except ValueError as e:
        assert "cap" in str(e)
    assert render_cache._main_render_gcs_decode(_zstd(b"hello")) == "hello"


# ── CAI-1 : helm argument injection ───────────────────────────────────────

def test_helm_template_places_positionals_after_a_dash_terminator(monkeypatch):
    """A leading-dash release name must be parsed by helm as the NAME, not a
    flag. The `--` terminator is what guarantees it; assert it is present and
    that the release lands after it."""
    seen = {}

    class _R:
        returncode = 0
        stdout = "rendered: true\n"
        stderr = ""

    def _fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return _R()

    monkeypatch.setattr(m.subprocess, "run", _fake_run)
    out, err = m._helm_template("/charts/app", "--post-renderer=sh", "ns", {})
    assert err is None and out == "rendered: true\n"
    cmd = seen["cmd"]
    assert "--" in cmd, f"no -- terminator in helm cmd: {cmd}"
    dash = cmd.index("--")
    assert cmd[dash + 1] == "--post-renderer=sh", (
        "the injected release name is not the first positional after --")
    assert cmd[dash + 2] == "/charts/app"
    # and the dangerous token never appears before the terminator, where cobra
    # would parse it as a flag
    assert "--post-renderer=sh" not in cmd[:dash]


# ── SSRF-1 : pagination link must stay on the Bitbucket API host ───────────

def test_get_open_prs_refuses_an_off_host_pagination_link(monkeypatch):
    calls = []

    def _fake_http(method, url, **kw):
        calls.append(url)
        if len(calls) == 1:
            # A compromised upstream points `next` at the cloud metadata server.
            return {"values": [{"id": 1}],
                    "next": "http://169.254.169.254/latest/meta-data/"}
        return {"values": [{"id": 2}]}    # must never be reached

    warned = []
    monkeypatch.setattr(m, "http", _fake_http)
    monkeypatch.setattr(m.logsink, "log",
                        lambda msg, lvl="INFO", **k: warned.append((lvl, msg)))
    prs = m.get_open_prs("acme-config-dev")
    assert len(calls) == 1, (
        "followed the off-host link instead of refusing it: %s" % calls)
    assert prs == [{"id": 1}]
    assert any("off-host" in msg for _lvl, msg in warned)


def test_get_open_prs_follows_a_same_host_next(monkeypatch):
    """The control: an on-host `next` is still followed, so the refusal is a
    host check, not a blanket stop-after-one."""
    base = m._bb_api_base("acme-config-dev")
    calls = []

    def _fake_http(method, url, **kw):
        calls.append(url)
        if len(calls) == 1:
            return {"values": [{"id": 1}],
                    "next": f"{base}/pullrequests?page=2"}
        return {"values": [{"id": 2}]}

    monkeypatch.setattr(m, "http", _fake_http)
    prs = m.get_open_prs("acme-config-dev")
    assert len(calls) == 2 and prs == [{"id": 1}, {"id": 2}]


# ── PT-1 : unsafe OCI registry rejected at the choke point ─────────────────

def test_ensure_chart_refuses_a_traversing_registry_before_any_pull(
        tmp_path, monkeypatch):
    """`oci://..` yields registry `..`, which was joined into the on-disk cache
    path. It must be refused at the choke point -- BEFORE the pull. Asserting
    only `is None` would pass for the wrong reason (an unsafe registry also
    makes the pull fail), so this proves the guard short-circuits: the pull is
    never reached."""
    monkeypatch.setattr(m, "HELM_CACHE_DIR", str(tmp_path))   # empty: no cache hit
    login_calls = []
    # _helm_login is the first thing the pull path does, and it sits AFTER the
    # guard. If the guard short-circuits, login is never reached.
    monkeypatch.setattr(m, "_helm_login",
                        lambda reg: login_calls.append(reg) or False)
    for reg in ("..", "reg/../../etc", "-flag", "reg name with space"):
        assert m._ensure_chart(reg, "app", "1.2.3") is None, reg
        assert login_calls == [], f"{reg}: reached the pull path despite the guard"


def test_ensure_chart_allows_a_normal_registry_through_to_the_pull(
        tmp_path, monkeypatch):
    """The control: a well-formed registry is NOT rejected by the guard -- it
    reaches the pull path (login). Without this, the guard could reject
    everything and the test above would still pass."""
    monkeypatch.setattr(m, "HELM_CACHE_DIR", str(tmp_path))
    login_calls = []
    monkeypatch.setattr(m, "_helm_login",
                        lambda reg: login_calls.append(reg) or False)
    assert m._ensure_chart("helm-oci-dev.repo.appspace.com", "app", "1.2.3") is None
    assert login_calls == ["helm-oci-dev.repo.appspace.com"], (
        "a valid registry was rejected before the pull path")


# ── XSS-01 : PR href limited to http/https ─────────────────────────────────

def test_pr_link_drops_a_non_http_scheme():
    """A javascript: (or data:) pr_url must never become a clickable href."""
    art = {"repo": "acme-config-dev", "pr_id": 7, "sha": "a" * 12,
           "pr_url": "javascript:alert(document.domain)"}
    html = diff_ui.render_html(art)
    assert "javascript:" not in html
    assert 'href="javascript' not in html
    assert "PR #7" in html               # the text is still shown, un-linked


def test_pr_link_keeps_a_real_https_url():
    art = {"repo": "acme-config-dev", "pr_id": 7, "sha": "a" * 12,
           "pr_url": "https://bitbucket.org/appspace-cloud/acme-config-dev/pull-requests/7"}
    html = diff_ui.render_html(art)
    assert ('href="https://bitbucket.org/appspace-cloud/'
            'acme-config-dev/pull-requests/7"') in html


# ── XSS-02 : security headers on the /diff response ───────────────────────

def test_diff_response_carries_security_headers(tmp_path, monkeypatch):
    """The escaped HTML is the only XSS layer; these headers are the second.
    Driven over real HTTP through the health server, so the do_GET path that
    sets them is exercised end to end."""
    diff_ui.save_artifact(str(tmp_path), "acme-config-dev", 42, "abcdef1",
                          "--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y\n")
    monkeypatch.setattr(m, "DIFF_UI_DIR", str(tmp_path))
    monkeypatch.setattr(m, "DIFF_UI_ENABLED", True)
    # DIFF_UI_GCS_BUCKET is left as-is (patching it would not reach envcfg/
    # render_cache, per the seam audit): respond() finds the artifact locally
    # in DIFF_UI_DIR, so the GCS fallback is never taken.
    monkeypatch.setattr(m, "_jfrog_hard_refresh", lambda name, ver: None)
    srv = m._start_health_server(0)
    try:
        port = srv.server_address[1]
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/diff/acme-config-dev/42/abcdef1")
        with urllib.request.urlopen(req, timeout=10) as r:
            hdrs = {k.lower(): v for k, v in r.headers.items()}
            body = r.read()
        assert hdrs.get("x-content-type-options") == "nosniff"
        assert hdrs.get("x-frame-options") == "DENY"
        csp = hdrs.get("content-security-policy", "")
        assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
        assert b"<html" in body.lower() or b"<!doctype" in body.lower()
    finally:
        srv.shutdown()
