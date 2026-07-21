"""Full-diff artifact store + web UI (Atlantis-style).

The PR comment is the summary; the complete, untruncated diff body is
persisted per (repo, pr_id, sha) and served by the existing health server
at /diff/<repo>/<pr>/<sha> (HTML) and /diff/<repo>/<pr>/<sha>/raw (text).
Everything is gated on DIFF_UI_ENABLED (default off): with the flag off,
behavior must be byte-for-byte identical to before this feature.
"""
import json
import os
import sys
import urllib.request
import urllib.error

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_ui  # noqa: E402
import diff_preview as m  # noqa: E402


BODY = "## diff\n```diff\n- a: 1\n+ a: 2\n```\n<script>alert(1)</script>\n"


# ── artifact store ─────────────────────────────────────────────────────────

def test_save_and_load_roundtrip(tmp_path):
    p = diff_ui.save_artifact(str(tmp_path), "acme-config-dev", 42, "ab12cd3",
                              BODY, pr_url="https://bb/pr/42")
    assert os.path.isfile(p)
    art = diff_ui.load_artifact(str(tmp_path), "acme-config-dev", 42, "ab12cd3")
    assert art["body"] == BODY
    assert art["repo"] == "acme-config-dev"
    assert art["pr_id"] == 42
    assert art["sha"] == "ab12cd3"
    assert art["pr_url"] == "https://bb/pr/42"
    assert "created_utc" in art
    assert diff_ui.has_artifact(str(tmp_path), "acme-config-dev", 42, "ab12cd3")


def test_load_missing_returns_none(tmp_path):
    assert diff_ui.load_artifact(str(tmp_path), "repo-x", 1, "abcdef1") is None
    assert not diff_ui.has_artifact(str(tmp_path), "repo-x", 1, "abcdef1")


def test_load_corrupt_returns_none(tmp_path):
    p = diff_ui.save_artifact(str(tmp_path), "repo-x", 1, "abcdef1", BODY)
    with open(p, "w") as f:
        f.write("{not json")
    assert diff_ui.load_artifact(str(tmp_path), "repo-x", 1, "abcdef1") is None


@pytest.mark.parametrize("repo,pr,sha", [
    ("../etc", 1, "abcdef1"),          # traversal in repo
    ("Repo", 1, "abcdef1"),            # uppercase repo
    ("repo/x", 1, "abcdef1"),          # separator in repo
    ("repo", 1, "ABCDEF1"),            # uppercase sha
    ("repo", 1, "abc"),                # sha too short
    ("repo", 1, "zzzzzzz"),            # non-hex sha
    ("repo", "x", "abcdef1"),          # non-numeric pr
    ("repo", -1, "abcdef1"),           # negative pr
])
def test_save_rejects_bad_keys(tmp_path, repo, pr, sha):
    with pytest.raises(ValueError):
        diff_ui.save_artifact(str(tmp_path), repo, pr, sha, BODY)


def test_prune_keeps_newest(tmp_path):
    for i in range(5):
        p = diff_ui.save_artifact(str(tmp_path), "repo-x", i + 1, "abcdef1",
                                  BODY, max_artifacts=3)
        os.utime(p, (i + 1, i + 1))  # deterministic mtimes, oldest = pr 1
    diff_ui.save_artifact(str(tmp_path), "repo-x", 99, "abcdef1",
                          BODY, max_artifacts=3)
    files = sorted(os.listdir(str(tmp_path)))
    assert len(files) == 3
    assert any("__99__" in f for f in files)
    assert not any("__1__" in f for f in files)


def test_prune_ignores_remove_errors(tmp_path, monkeypatch):
    for i in range(3):
        diff_ui.save_artifact(str(tmp_path), "repo-x", i + 1, "abcdef1", BODY)
    monkeypatch.setattr(diff_ui.os, "remove",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("ro")))
    # must not raise even though every removal fails
    diff_ui.save_artifact(str(tmp_path), "repo-x", 50, "abcdef1",
                          BODY, max_artifacts=1)


# ── request path parsing / responses ───────────────────────────────────────

@pytest.mark.parametrize("path,expect", [
    ("/diff/repo-x/42/abcdef1", ("repo-x", 42, "abcdef1", False)),
    ("/diff/repo-x/42/abcdef1/raw", ("repo-x", 42, "abcdef1", True)),
    ("/diff/repo-x/42", None),                    # missing sha
    ("/diff/repo-x/42/abcdef1/extra", None),      # extra segment
    ("/diff/../x/42/abcdef1", None),              # traversal
    ("/diff/repo-x/42/abcdef1?x=1", None),        # query string rejected
    ("/diff/repo-x/4x2/abcdef1", None),           # bad pr
    ("/other/repo-x/42/abcdef1", None),           # wrong prefix
])
def test_parse_request_path(path, expect):
    assert diff_ui.parse_request_path(path) == expect


def test_respond_disabled_is_404(tmp_path):
    code, ctype, body = diff_ui.respond("/diff/repo-x/1/abcdef1",
                                        str(tmp_path), enabled=False)
    assert code == 404
    assert b"disabled" in body


def test_respond_bad_path_is_400(tmp_path):
    code, _, _ = diff_ui.respond("/diff/BAD//x", str(tmp_path), enabled=True)
    assert code == 400


def test_respond_missing_artifact_is_404(tmp_path):
    code, _, body = diff_ui.respond("/diff/repo-x/1/abcdef1",
                                    str(tmp_path), enabled=True)
    assert code == 404 and b"not found" in body


def test_respond_html_escapes_content(tmp_path):
    diff_ui.save_artifact(str(tmp_path), "repo-x", 1, "abcdef1", BODY,
                          pr_url="https://bb/pr/1")
    code, ctype, payload = diff_ui.respond("/diff/repo-x/1/abcdef1",
                                           str(tmp_path), enabled=True)
    assert code == 200 and ctype.startswith("text/html")
    text = payload.decode()
    assert "<script>" not in text            # injected script must be escaped
    assert "&lt;script&gt;" in text
    assert "repo-x" in text and "abcdef1" in text
    assert "https://bb/pr/1" in text         # link back to the PR
    assert "/raw" in text                    # link to the raw view


def test_respond_raw_returns_exact_body(tmp_path):
    diff_ui.save_artifact(str(tmp_path), "repo-x", 1, "abcdef1", BODY)
    code, ctype, payload = diff_ui.respond("/diff/repo-x/1/abcdef1/raw",
                                           str(tmp_path), enabled=True)
    assert code == 200 and ctype.startswith("text/plain")
    assert payload.decode() == BODY


def test_ui_url():
    assert (diff_ui.ui_url("https://d.example.com", "repo-x", 7, "abcdef1")
            == "https://d.example.com/diff/repo-x/7/abcdef1")


# ── diff_preview wiring ────────────────────────────────────────────────────

def test_save_hook_disabled_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "DIFF_UI_ENABLED", False)
    monkeypatch.setattr(m, "DIFF_UI_DIR", str(tmp_path))
    m._save_diff_ui_artifact("repo-x", 1, "abcdef1", BODY)
    assert os.listdir(str(tmp_path)) == []


def test_save_hook_enabled_writes_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "DIFF_UI_ENABLED", True)
    monkeypatch.setattr(m, "DIFF_UI_DIR", str(tmp_path))
    m._save_diff_ui_artifact("repo-x", 1, "abcdef1", BODY)
    art = diff_ui.load_artifact(str(tmp_path), "repo-x", 1, "abcdef1")
    assert art and art["body"] == BODY
    assert "pull-requests/1" in art["pr_url"]


def test_save_hook_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "DIFF_UI_ENABLED", True)
    monkeypatch.setattr(m, "DIFF_UI_DIR", str(tmp_path))
    monkeypatch.setattr(m.diff_ui, "save_artifact",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    m._save_diff_ui_artifact("repo-x", 1, "abcdef1", BODY)  # must swallow


def test_build_status_links_ui_when_artifact_exists(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(m, "bb",
                        lambda meth, path, repo=None, body=None: calls.append(body))
    monkeypatch.setattr(m, "DIFF_UI_ENABLED", True)
    monkeypatch.setattr(m, "DIFF_UI_DIR", str(tmp_path))
    monkeypatch.setattr(m, "DIFF_UI_BASE_URL", "https://diff.example.com")
    sha = "a" * 40
    diff_ui.save_artifact(str(tmp_path), m.BB_REPO, 7, sha, BODY)
    m.post_build_status(sha, "SUCCESSFUL", "desc", pr_id=7)
    assert calls and calls[0]["url"] == f"https://diff.example.com/diff/{m.BB_REPO}/7/{sha}"


def test_build_status_falls_back_without_artifact(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(m, "bb",
                        lambda meth, path, repo=None, body=None: calls.append(body))
    monkeypatch.setattr(m, "DIFF_UI_ENABLED", True)
    monkeypatch.setattr(m, "DIFF_UI_DIR", str(tmp_path))
    monkeypatch.setattr(m, "DIFF_UI_BASE_URL", "https://diff.example.com")
    sha = "b" * 40
    m.post_build_status(sha, "SUCCESSFUL", "desc", pr_id=8)
    assert calls and "bitbucket.org" in calls[0]["url"]  # unchanged fallback


def test_build_status_unchanged_when_disabled(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(m, "bb",
                        lambda meth, path, repo=None, body=None: calls.append(body))
    monkeypatch.setattr(m, "DIFF_UI_ENABLED", False)
    monkeypatch.setattr(m, "DIFF_UI_DIR", str(tmp_path))
    monkeypatch.setattr(m, "DIFF_UI_BASE_URL", "https://diff.example.com")
    sha = "c" * 40
    diff_ui.save_artifact(str(tmp_path), m.BB_REPO, 9, sha, BODY)
    m.post_build_status(sha, "SUCCESSFUL", "desc", pr_id=9)
    assert calls and "bitbucket.org" in calls[0]["url"]


# ── HTTP surface (real server, same pattern as test_coverage_edges) ───────

@pytest.fixture()
def health(monkeypatch):
    monkeypatch.setattr(m, "_jfrog_hard_refresh", lambda name, ver: None)
    srv = m._start_health_server(0)
    port = srv.server_address[1]
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


def _req(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_http_diff_route_serves_artifact(health, tmp_path, monkeypatch):
    monkeypatch.setattr(m, "DIFF_UI_ENABLED", True)
    monkeypatch.setattr(m, "DIFF_UI_DIR", str(tmp_path))
    diff_ui.save_artifact(str(tmp_path), "repo-x", 3, "abcdef1", BODY)
    code, payload = _req(f"{health}/diff/repo-x/3/abcdef1")
    assert code == 200 and b"&lt;script&gt;" in payload
    code, payload = _req(f"{health}/diff/repo-x/3/abcdef1/raw")
    assert code == 200 and payload.decode() == BODY


def test_http_diff_route_404_when_disabled(health, tmp_path, monkeypatch):
    monkeypatch.setattr(m, "DIFF_UI_ENABLED", False)
    monkeypatch.setattr(m, "DIFF_UI_DIR", str(tmp_path))
    diff_ui.save_artifact(str(tmp_path), "repo-x", 3, "abcdef1", BODY)
    code, _ = _req(f"{health}/diff/repo-x/3/abcdef1")
    assert code == 404
