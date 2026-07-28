"""Coverage campaign (post v2.5.15), pass B: src/dev_hard_refresh.py (was 0%).

Same philosophy as the acme-mcp fake-infrastructure layer: point the module
at throwaway fake binaries and a patched urlopen so every real code path
(REST session auth, app listing, per-app refresh, the timeout guard, the
summary accounting) runs deterministically with zero infrastructure.
"""
import io
import json
import os
import stat
import sys
import tempfile
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import dev_hard_refresh as dhr  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────

def _mk_fake_argocd(tmp_path, script_body: str) -> str:
    """Write an executable fake `argocd` whose behavior is a shell case on $*."""
    p = tmp_path / "argocd"
    p.write_text(f"#!/bin/bash\n{script_body}\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


class _FakeResp:
    def __init__(self, payload: dict):
        self._data = json.dumps(payload).encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ── _fetch_argocd_token ──────────────────────────────────────────────────

def test_fetch_token_posts_credentials_and_returns_token(monkeypatch):
    captured = {}

    def fake_urlopen(req, context=None, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = json.loads(req.data)
        return _FakeResp({"token": "tok-123"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("ARGOCD_PASS", "s3cret")
    monkeypatch.setenv("ARGOCD_USER", "diff-preview")

    tok = dhr._fetch_argocd_token()

    assert tok == "tok-123"
    assert captured["url"] == f"https://{dhr.SERVER}/api/v1/session"
    assert captured["method"] == "POST"
    assert captured["body"] == {"username": "diff-preview", "password": "s3cret"}


def test_fetch_token_requires_password(monkeypatch):
    monkeypatch.delenv("ARGOCD_PASS", raising=False)
    with pytest.raises(KeyError):
        dhr._fetch_argocd_token()


# ── list_apps ────────────────────────────────────────────────────────────

def test_list_apps_strips_project_prefix_and_blank_lines(tmp_path, monkeypatch):
    fake = _mk_fake_argocd(tmp_path, 'printf "argocd/app-one\\napp-two\\n\\n"')
    monkeypatch.setattr(dhr, "ARGOCD", fake)
    assert dhr.list_apps() == ["app-one", "app-two"]


def test_list_apps_exits_on_cli_failure(tmp_path, monkeypatch, capsys):
    fake = _mk_fake_argocd(tmp_path, 'echo "permission denied" >&2; exit 1')
    monkeypatch.setattr(dhr, "ARGOCD", fake)
    with pytest.raises(SystemExit) as exc:
        dhr.list_apps()
    assert exc.value.code == 1
    assert "app list failed" in capsys.readouterr().out


# ── hard_refresh ─────────────────────────────────────────────────────────

def test_hard_refresh_success_returns_ok_and_elapsed(tmp_path, monkeypatch):
    fake = _mk_fake_argocd(tmp_path, "exit 0")
    monkeypatch.setattr(dhr, "ARGOCD", fake)
    app, ok, elapsed = dhr.hard_refresh("pv-x-a-ms")
    assert app == "pv-x-a-ms" and ok is True and elapsed >= 0


def test_hard_refresh_failure_warns_and_returns_false(tmp_path, monkeypatch, capsys):
    fake = _mk_fake_argocd(tmp_path, 'echo "rpc unavailable" >&2; exit 1')
    monkeypatch.setattr(dhr, "ARGOCD", fake)
    app, ok, _ = dhr.hard_refresh("pv-x-a-ms")
    assert ok is False
    assert "WARN: pv-x-a-ms: failed" in capsys.readouterr().out


def test_hard_refresh_timeout_is_caught_not_raised(tmp_path, monkeypatch, capsys):
    # A single slow app must never crash the ThreadPoolExecutor pool: the
    # TimeoutExpired is converted into a (app, False, elapsed) result.
    fake = _mk_fake_argocd(tmp_path, "sleep 5")
    monkeypatch.setattr(dhr, "ARGOCD", fake)
    monkeypatch.setattr(dhr, "TIMEOUT", 1)
    app, ok, elapsed = dhr.hard_refresh("pv-slow-a-ms")
    assert ok is False and elapsed >= 1
    assert "timed out" in capsys.readouterr().out


# ── main ─────────────────────────────────────────────────────────────────

def test_main_happy_path_counts_ok_and_failed(tmp_path, monkeypatch, capsys):
    fake = _mk_fake_argocd(tmp_path, '''
case "$*" in
  *"app list"*) printf "argocd/app-good\\nargocd/app-bad\\n"; exit 0;;
  *"app get app-bad"*) echo "boom" >&2; exit 1;;
  *"app get "*) exit 0;;
  *) exit 0;;
esac''')
    monkeypatch.setattr(dhr, "ARGOCD", fake)
    monkeypatch.setattr(dhr, "_fetch_argocd_token", lambda: "tok-xyz")
    try:
        dhr.main()
        out = capsys.readouterr().out
        assert "ArgoCD authentication OK." in out
        # v2.13.0: the log names the configured projects instead of saying
        # "dev/qa", which stopped being true when stage was added.
        assert "Hard-refreshing 2 apps in appspace-dev" in out
        assert "OK: app-good" in out
        assert "Done: 1/2 refreshed" in out
        # The REST token must be exported for the CLI subprocesses.
        assert os.environ.get("ARGOCD_AUTH_TOKEN") == "tok-xyz"
    finally:
        os.environ.pop("ARGOCD_AUTH_TOKEN", None)


def test_main_exits_1_when_auth_fails(monkeypatch, capsys):
    def boom():
        raise RuntimeError("session api down")
    monkeypatch.setattr(dhr, "_fetch_argocd_token", boom)
    with pytest.raises(SystemExit) as exc:
        dhr.main()
    assert exc.value.code == 1
    assert "authentication failed" in capsys.readouterr().out


def test_main_counts_a_real_timeout_toward_the_timeouts_tally(tmp_path, monkeypatch, capsys):
    # hard_refresh's own timeout unit test already covers the function in
    # isolation; this closes main()'s own `if elapsed >= TIMEOUT: timeouts
    # += 1` line, which the earlier "app-bad" happy-path test never touched
    # because that failure returned in milliseconds, well under TIMEOUT.
    import subprocess as _sp
    fake = tmp_path / "argocd"
    fake.write_text('#!/bin/bash\ncase "$*" in *"app list"*) printf "argocd/app-slow\\n"; exit 0;; *) exit 0;; esac\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(dhr, "ARGOCD", str(fake))
    monkeypatch.setattr(dhr, "_fetch_argocd_token", lambda: "tok-xyz")

    ticks = {"n": 0}
    def fake_monotonic():
        ticks["n"] += 1
        return ticks["n"] * 100.0   # every call lands 100s after the previous
    monkeypatch.setattr(dhr.time, "monotonic", fake_monotonic)

    def fake_run(cmd, **k):
        if "list" in cmd:
            class R:
                returncode = 0
                stdout = "argocd/app-slow\n"
                stderr = ""
            return R()
        raise _sp.TimeoutExpired(cmd=cmd, timeout=dhr.TIMEOUT)
    monkeypatch.setattr(_sp, "run", fake_run)
    try:
        dhr.main()
        out = capsys.readouterr().out
        assert "1 timed out" in out, f"a >=TIMEOUT elapsed failure must be tallied: {out}"
    finally:
        os.environ.pop("ARGOCD_AUTH_TOKEN", None)
