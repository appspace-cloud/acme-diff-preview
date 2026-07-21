"""Full-diff artifact store and minimal web UI (Atlantis-style).

The PR comment stays the human summary (and is truncated over
MAX_COMMENT_BYTES); the COMPLETE, untruncated diff body is persisted here per
(repo, pr_id, sha) and served by the existing health server at:

    /diff/<repo>/<pr_id>/<sha>        rendered HTML (everything escaped)
    /diff/<repo>/<pr_id>/<sha>/raw    exact plain text

This mirrors Atlantis: the Bitbucket build status "Details" link opens the
full output page instead of the truncated comment.

Standalone module on purpose (stdlib only, never imports diff_preview), same
pattern as the provider split: independently testable, no circular imports.
diff_preview passes configuration as arguments.

Storage v1 is a bounded flat directory (one JSON file per artifact, atomic
write, oldest-by-mtime pruned past max_artifacts). The caller passes the SAME
body it posts to Bitbucket, so the store only ever holds already-redacted
content. A durable GCS backend is a planned follow-up in the tracking ticket;
the function surface here is the seam where it plugs in.
"""
from __future__ import annotations

import html
import json
import os
import re
import tempfile
import time

# Bitbucket repo slugs are lowercase alphanumerics plus ._- ; PR ids are
# positive integers; shas are abbreviated-to-full lowercase hex. Anything
# else is rejected before it can touch the filesystem (no separators, no
# traversal, no case games on case-insensitive filesystems).
_REPO_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
_PR_RE = re.compile(r"^[1-9][0-9]{0,8}$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


def _validate(repo, pr_id, sha):
    """Return (repo, pr_id_str, sha) or raise ValueError."""
    pr_s = str(pr_id)
    if not _REPO_RE.match(str(repo)):
        raise ValueError(f"bad repo slug: {repo!r}")
    if not _PR_RE.match(pr_s):
        raise ValueError(f"bad pr id: {pr_id!r}")
    if not _SHA_RE.match(str(sha)):
        raise ValueError(f"bad sha: {sha!r}")
    return str(repo), pr_s, str(sha)


def _artifact_path(base_dir, repo, pr_id, sha):
    repo, pr_s, sha = _validate(repo, pr_id, sha)
    return os.path.join(base_dir, f"{repo}__{pr_s}__{sha}.json")


def save_artifact(base_dir, repo, pr_id, sha, body, pr_url="",
                  max_artifacts=500):
    """Persist the full (already redacted) diff body. Atomic; then prune.

    Atomic tmp+rename so a concurrent reader can never see a half-written
    file; pruning is best-effort (a locked/vanished file must never break
    the diff run that triggered the save).
    """
    path = _artifact_path(base_dir, repo, pr_id, sha)
    os.makedirs(base_dir, exist_ok=True)
    artifact = {
        "repo": str(repo),
        "pr_id": int(pr_id),
        "sha": str(sha),
        "pr_url": pr_url,
        "created_utc": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "body": body,
    }
    fd, tmp = tempfile.mkstemp(dir=base_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(artifact, f, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):  # pragma: no cover - only on a failed replace
            os.remove(tmp)
    _prune(base_dir, max_artifacts)
    return path


def _prune(base_dir, max_artifacts):
    """Remove oldest artifacts (by mtime) beyond max_artifacts. Best-effort."""
    try:
        entries = [os.path.join(base_dir, n) for n in os.listdir(base_dir)
                   if n.endswith(".json")]
        entries.sort(key=lambda p: os.path.getmtime(p))
    except OSError:  # pragma: no cover - directory vanished mid-run
        return
    for path in entries[:max(0, len(entries) - max_artifacts)]:
        try:
            os.remove(path)
        except OSError:
            pass  # locked or already gone: never fail the save over pruning


def load_artifact(base_dir, repo, pr_id, sha):
    """Return the artifact dict, or None if missing/corrupt/bad key."""
    try:
        path = _artifact_path(base_dir, repo, pr_id, sha)
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def has_artifact(base_dir, repo, pr_id, sha):
    return load_artifact(base_dir, repo, pr_id, sha) is not None


def ui_url(base_url, repo, pr_id, sha):
    """Permalink for the build status URL."""
    return f"{base_url}/diff/{repo}/{pr_id}/{sha}"


def parse_request_path(path):
    """Parse /diff/<repo>/<pr>/<sha>[/raw]. Return tuple or None.

    Strict by construction: exact segment count, each segment re-validated
    with the same regexes used for filenames, query strings rejected. A None
    here becomes a 400, so nothing unvalidated ever reaches the filesystem.
    """
    if "?" in path or "#" in path:
        return None
    parts = path.split("/")
    # ["", "diff", repo, pr, sha] or ["", "diff", repo, pr, sha, "raw"]
    if len(parts) == 6 and parts[5] == "raw":
        raw = True
    elif len(parts) == 5:
        raw = False
    else:
        return None
    if parts[0] != "" or parts[1] != "diff":
        return None
    repo, pr_s, sha = parts[2], parts[3], parts[4]
    if not (_REPO_RE.match(repo) and _PR_RE.match(pr_s) and _SHA_RE.match(sha)):
        return None
    return repo, int(pr_s), sha, raw


def render_html(artifact):
    """Minimal server-rendered page. EVERY dynamic value is escaped: the body
    is PR-controlled content, so the same comment-injection hardening the
    Bitbucket comment gets applies here (no raw HTML can survive)."""
    repo = html.escape(str(artifact.get("repo", "")))
    pr_id = html.escape(str(artifact.get("pr_id", "")))
    sha = html.escape(str(artifact.get("sha", "")))
    created = html.escape(str(artifact.get("created_utc", "")))
    body = html.escape(str(artifact.get("body", "")))
    pr_url = str(artifact.get("pr_url", ""))
    pr_link = (f'<a href="{html.escape(pr_url, quote=True)}">PR #{pr_id}</a>'
               if pr_url else f"PR #{pr_id}")
    raw_href = html.escape(f"/diff/{artifact.get('repo','')}"
                           f"/{artifact.get('pr_id','')}"
                           f"/{artifact.get('sha','')}/raw", quote=True)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>diff {repo} #{pr_id} @ {sha}</title>
<style>
body {{ font-family: -apple-system, Segoe UI, sans-serif; margin: 2rem; }}
pre {{ background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 6px;
      padding: 1rem; overflow-x: auto; font-size: 12px; line-height: 1.45; }}
.meta {{ color: #57606a; margin-bottom: 1rem; }}
</style>
</head>
<body>
<h2>acme-diff-preview: full diff</h2>
<div class="meta">{repo} &middot; {pr_link} &middot; commit <code>{sha}</code>
 &middot; generated {created} &middot; <a href="{raw_href}">raw</a></div>
<pre>{body}</pre>
</body>
</html>
"""


def respond(path, base_dir, enabled):
    """Pure request handler: (status, content_type, payload bytes).

    Pure on purpose so the HTTP layer in diff_preview stays a 5-line shim
    and everything here is unit-testable without a socket.
    """
    text = "text/plain; charset=utf-8"
    if not enabled:
        return 404, text, b"diff UI disabled"
    parsed = parse_request_path(path)
    if parsed is None:
        return 400, text, b"bad request"
    repo, pr_id, sha, raw = parsed
    artifact = load_artifact(base_dir, repo, pr_id, sha)
    if artifact is None:
        return 404, text, b"not found"
    if raw:
        return 200, text, str(artifact.get("body", "")).encode("utf-8")
    return 200, "text/html; charset=utf-8", render_html(artifact).encode("utf-8")
