"""Full-diff artifact store + web UI (Atlantis-style).

The PR comment is the summary; the complete, untruncated diff body is
persisted per (repo, pr_id, sha) and served by the existing health server
at /diff/<repo>/<pr>/<sha> (HTML) and /diff/<repo>/<pr>/<sha>/raw (text).
DIFF_UI_ENABLED defaults to on (verified safe: no ingress path exposes
/diff/* externally today, see README), but every test below still sets it
explicitly so the test's intent never depends on that default.
"""
import json
import os
import subprocess
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
    # metadata is optional: not passed here, so it must default sanely
    # rather than raise or store None-ish garbage.
    assert art["base_sha"] == ""
    assert art["outcome_counts"] == {}
    assert art["app_count"] is None
    assert diff_ui.has_artifact(str(tmp_path), "acme-config-dev", 42, "ab12cd3")


def test_save_and_load_with_pr_metadata(tmp_path):
    p = diff_ui.save_artifact(str(tmp_path), "acme-config-dev", 42, "ab12cd3",
                              BODY, base_sha="deadbee",
                              outcome_counts={"diff": 3, "no_diff": 12},
                              app_count=15)
    art = diff_ui.load_artifact(str(tmp_path), "acme-config-dev", 42, "ab12cd3")
    assert art["base_sha"] == "deadbee"
    assert art["outcome_counts"] == {"diff": 3, "no_diff": 12}
    assert art["app_count"] == 15


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


# ── branding: every page must say who it is ────────────────────────────────

def test_render_html_names_the_service():
    # Every page states "ACME Diff Preview" explicitly, both in the browser
    # tab (<title>) and on the page itself, so a reviewer arriving from a
    # build-status link never has to guess which tool posted it.
    art = {"repo": "repo-x", "pr_id": 1, "sha": "abcdef1",
           "created_utc": "2026-01-01 00:00:00 UTC", "body": "x"}
    out = diff_ui.render_html(art)
    assert "<title>ACME Diff Preview - repo-x #1 @ abcdef1</title>" in out
    assert "ACME Diff Preview" in out
    assert "acme-diff-preview" in out  # the repo/service slug, lowercase form


def test_ui_url_contains_acme_diff_preview_path():
    # The permalink path itself is /diff/..., served by acme-diff-preview;
    # the branding lives in DIFF_UI_BASE_URL (the operator-chosen hostname,
    # e.g. acme-diff-preview.appspace.com per the chart's own values.yaml
    # comment) plus the page content asserted above.
    url = diff_ui.ui_url("https://acme-diff-preview.appspace.com",
                         "repo-x", 7, "abcdef1")
    assert url == "https://acme-diff-preview.appspace.com/diff/repo-x/7/abcdef1"
    assert "acme-diff-preview" in url


def test_render_html_no_summary_line_without_metadata():
    # An artifact saved without PR metadata (or by an older version of this
    # module) must render cleanly with no dangling "None" text and no
    # summary div at all.
    art = {"repo": "repo-x", "pr_id": 1, "sha": "abcdef1",
           "created_utc": "2026-01-01 00:00:00 UTC", "body": "x"}
    out = diff_ui.render_html(art)
    assert "None" not in out
    assert '<div class="summary">' not in out


def test_render_html_shows_pr_metadata_summary():
    art = {"repo": "acme-config-dev", "pr_id": 42, "sha": "abcdef1",
           "base_sha": "deadbee", "created_utc": "2026-01-01 00:00:00 UTC",
           "body": "x", "app_count": 15,
           "outcome_counts": {"diff": 3, "no_diff": 12}}
    out = diff_ui.render_html(art)
    assert "vs base <code>deadbee</code>" in out
    assert "15 apps evaluated" in out
    assert "3 changed" in out
    assert "12 no changes" in out


def test_render_html_summary_includes_unknown_outcome_key():
    # A future outcome kind this module does not have a friendly label for
    # must still show up (labeled with its raw key) instead of being dropped.
    art = {"repo": "repo-x", "pr_id": 1, "sha": "abcdef1",
           "created_utc": "2026-01-01 00:00:00 UTC", "body": "x",
           "app_count": 1, "outcome_counts": {"something_new": 2}}
    out = diff_ui.render_html(art)
    assert "2 something_new" in out


# ── diff highlighting (server-side, per line, everything still escaped) ───

def _art(body):
    return {"repo": "repo-x", "pr_id": 1, "sha": "abcdef1",
            "created_utc": "2026-01-01 00:00:00 UTC", "body": body}


def test_render_html_colors_diff_lines():
    body = ("### `app-one`\n"
            "```diff\n"
            "@@ -1,2 +1,2 @@\n"
            "     image: repo/x:1\n"
            "-    replicas: 2\n"
            "+    replicas: 3\n"
            "```\n")
    out = diff_ui.render_html(_art(body))
    assert '<span class="l del">-    replicas: 2</span>' in out
    assert '<span class="l add">+    replicas: 3</span>' in out
    assert '<span class="l hunk">@@ -1,2 +1,2 @@</span>' in out
    assert '<span class="l ctx">     image: repo/x:1</span>' in out


def test_render_html_escapes_inside_colored_lines():
    # PR-controlled content inside a colored diff line must stay escaped:
    # the highlighting must never open an injection hole the plain <pre>
    # did not have.
    body = "```diff\n- <script>alert(1)</script>\n+ <b>bold</b>\n```\n"
    out = diff_ui.render_html(_art(body))
    assert "<script>" not in out and "<b>" not in out
    assert '<span class="l del">- &lt;script&gt;alert(1)&lt;/script&gt;</span>' in out
    assert '<span class="l add">+ &lt;b&gt;bold&lt;/b&gt;</span>' in out


def test_render_html_markdown_headers_highlighted():
    out = diff_ui.render_html(_art("## Title\nplain text\n### `sub`\n"))
    assert '<span class="l mdh">## Title</span>' in out
    assert '<span class="l mdh">### `sub`</span>' in out
    assert '<span class="l">plain text</span>' in out


def test_render_html_non_diff_fence_not_colored():
    # A yaml fence contains lines starting with "-" (list items) that must
    # NOT be painted as deletions; only ```diff fences get diff colors.
    body = "```yaml\n- item-one\n+ not-an-addition\n```\n"
    out = diff_ui.render_html(_art(body))
    assert '<span class="l del">' not in out
    assert '<span class="l add">' not in out
    assert '<span class="l ctx">- item-one</span>' in out


def test_render_html_fence_markers_present_and_dimmed():
    out = diff_ui.render_html(_art("```diff\n+ x\n```\n"))
    assert '<span class="l fence">```diff</span>' in out
    assert '<span class="l fence">```</span>' in out


def test_render_html_empty_lines_survive():
    # Blank lines must produce their own line span (CSS gives it height),
    # so vertical rhythm matches the raw text.
    out = diff_ui.render_html(_art("a\n\nb\n"))
    assert '<span class="l"></span>' in out


def test_render_html_has_dark_mode_and_wraps_line_spans_in_pre():
    out = diff_ui.render_html(_art("x\n"))
    assert "prefers-color-scheme: dark" in out
    assert "<pre>" in out and "</pre>" in out


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


def test_save_hook_forwards_pr_metadata(tmp_path, monkeypatch):
    # The orchestrator's call site passes base_sha/outcome_counts/app_count
    # through unchanged; this is what makes them show up in the rendered page.
    monkeypatch.setattr(m, "DIFF_UI_ENABLED", True)
    monkeypatch.setattr(m, "DIFF_UI_DIR", str(tmp_path))
    m._save_diff_ui_artifact("repo-x", 1, "abcdef1", BODY, base_sha="deadbee",
                             outcome_counts={"diff": 2, "no_diff": 5},
                             app_count=7)
    art = diff_ui.load_artifact(str(tmp_path), "repo-x", 1, "abcdef1")
    assert art["base_sha"] == "deadbee"
    assert art["outcome_counts"] == {"diff": 2, "no_diff": 5}
    assert art["app_count"] == 7


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


def _req_with_headers(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.read(), dict(r.headers)


def test_http_diff_route_sets_content_length(health, tmp_path, monkeypatch):
    # Every other route on this handler sets Content-Length explicitly;
    # this one serves the largest bodies of anything here (a full
    # multi-app diff), so it is exactly the one that must not be the odd
    # route out relying on connection-close to mark the body end.
    monkeypatch.setattr(m, "DIFF_UI_ENABLED", True)
    monkeypatch.setattr(m, "DIFF_UI_DIR", str(tmp_path))
    diff_ui.save_artifact(str(tmp_path), "repo-x", 3, "abcdef1", BODY)
    code, payload, headers = _req_with_headers(f"{health}/diff/repo-x/3/abcdef1")
    assert code == 200
    assert int(headers["Content-Length"]) == len(payload)


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


# ── module-level default (a genuinely fresh process, env var unset) ───────
# Every test above sets DIFF_UI_ENABLED explicitly by monkeypatching the
# module attribute, which proves the FEATURE works either way but says
# nothing about what a real pod does with no override at all. These two
# spawn a clean subprocess (only the required BB_USER/BB_TOKEN/ARGOCD_PASS
# set) to check the actual default a fresh container would boot with.

def _diff_ui_enabled_in_fresh_process(env_overrides):
    src_dir = os.path.join(os.path.dirname(__file__), "..", "src")
    env = {"PATH": os.environ.get("PATH", ""), "BB_USER": "t",
           "BB_TOKEN": "t", "ARGOCD_PASS": "t"}
    env.update(env_overrides)
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, %r); import diff_preview as m; "
         "print(m.DIFF_UI_ENABLED)" % src_dir],
        capture_output=True, text=True, env=env, timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip() == "True"


def test_default_is_enabled_with_no_override():
    assert _diff_ui_enabled_in_fresh_process({}) is True


def test_explicit_false_env_var_disables_it():
    assert _diff_ui_enabled_in_fresh_process({"DIFF_UI_ENABLED": "false"}) is False
