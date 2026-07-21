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

# Mirrors diff_preview.STATUS_NAME. Duplicated on purpose: this module stays
# standalone stdlib-only (see module docstring) and never imports
# diff_preview just to reuse one string. Every page this module renders
# names the service explicitly, so a reviewer landing here from a build
# status link never has to guess which tool they are looking at.
SERVICE_NAME = "ACME Diff Preview"

# Outcome keys mirror diff_preview.OUT_* (diff/no_diff/indeterminate/error/
# decommissioned), passed in as plain strings so this module stays decoupled
# from those constants. Unknown keys still render, just unlabeled.
_OUTCOME_LABELS = {
    "diff": "changed",
    "no_diff": "no changes",
    "indeterminate": "unavailable",
    "error": "errors",
    "decommissioned": "decommissioned",
}

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
    # Keyed by (repo, pr) only, NOT by sha: one live artifact per PR, exactly
    # like the single PR comment that gets updated in place on every commit.
    # A new commit's save_artifact overwrites the previous one (atomic
    # os.replace), and load-by-sha resolves to whatever the PR's current diff
    # is, so the build-status link never 404s just because the tip moved. The
    # sha is still validated (below) and stored inside the artifact.
    repo, pr_s, sha = _validate(repo, pr_id, sha)
    return os.path.join(base_dir, f"{repo}__{pr_s}.json")


def save_artifact(base_dir, repo, pr_id, sha, body, pr_url="",
                  max_artifacts=500, base_sha="", outcome_counts=None,
                  app_count=None):
    """Persist the full (already redacted) diff body. Atomic; then prune.

    base_sha/outcome_counts/app_count are optional PR-level context (the diff
    base commit, the per-outcome breakdown, and how many apps were
    evaluated) so the page shows more than the raw comment text: the same
    at-a-glance summary a reviewer gets from the comment header, kept even
    after the comment itself gets truncated.

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
        "base_sha": str(base_sha) if base_sha else "",
        "outcome_counts": dict(outcome_counts) if outcome_counts else {},
        "app_count": int(app_count) if app_count is not None else None,
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


def _format_outcome_summary(app_count, outcome_counts):
    """Chip per fact: '15 apps evaluated', '3 changed', '12 no changes'.
    Empty string if there is no metadata (e.g. an artifact saved before
    these fields existed), so the page renders no summary row at all."""
    if app_count is None and not outcome_counts:
        return ""
    parts = []
    if app_count is not None:
        parts.append(f"{app_count} app{'s' if app_count != 1 else ''} evaluated")
    for key, label in _OUTCOME_LABELS.items():
        n = outcome_counts.get(key, 0)
        if n:
            parts.append(f"{n} {label}")
    for key, n in outcome_counts.items():
        if key not in _OUTCOME_LABELS and n:
            parts.append(f"{n} {key}")
    return "".join(f"<span>{html.escape(str(p))}</span>" for p in parts)


# Fence markers as the comment renderer emits them: ``` optionally followed
# by a language tag. Only ```diff fences get diff coloring; any other fence
# is rendered as neutral code so yaml list items ("- item") are never
# painted as deletions.
_FENCE_RE = re.compile(r"^```([A-Za-z0-9_-]*)\s*$")

# Hunk headers look like "@@ -18,6 +18,8 @@": pull the old/new start lines so
# the gutters can count from the right place.
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

# Default cap on rows rendered outright. A huge multi-app diff can be many
# thousands of lines; past this the overflow is still emitted (nothing is
# dropped, /raw stays byte-exact) but hidden behind a "show full output"
# button so first paint and scrolling stay snappy. Module-level so tests and
# operators can tune it.
MAX_VISIBLE_LINES = 1500


def _diff_row(cls, old_no, new_no, marker, esc_text):
    """One table row: two line-number gutters, a +/- marker cell, and the
    escaped code cell. esc_text is ALREADY html-escaped by the caller."""
    o = str(old_no) if old_no is not None else ""
    n = str(new_no) if new_no is not None else ""
    row_cls = f"row {cls}" if cls else "row"
    return (f'<tr class="{row_cls}">'
            f'<td class="ln-old">{o}</td>'
            f'<td class="ln-new">{n}</td>'
            f'<td class="mk">{marker}</td>'
            f'<td class="code">{esc_text}</td></tr>')


def _render_body_rows(body):
    """Render the comment body as diff table rows. Same information as the
    raw text (the /raw endpoint stays byte-exact), just readable: inside
    ```diff fences, +/-/@@ lines get GitHub-palette colors and old/new line
    numbers in the gutters; non-diff fences render as neutral code (so a yaml
    "- item" is never painted as a deletion); markdown headers outside fences
    get weight; fence markers are dimmed. Every line goes through html.escape
    BEFORE being placed in a cell, so highlighting can never open an
    injection hole. Returns a list of row strings (one per source line)."""
    rows = []
    fence = None      # None | "diff" | "code"
    old_no = new_no = 0
    for line in str(body).split("\n"):
        esc = html.escape(line)
        m = _FENCE_RE.match(line)
        if m:
            fence = (None if fence is not None
                     else ("diff" if m.group(1) == "diff" else "code"))
            rows.append(_diff_row("fence", None, None, "", esc))
            continue
        if fence == "diff":
            hm = _HUNK_RE.match(line)
            if hm:
                old_no = int(hm.group(1))
                new_no = int(hm.group(2))
                rows.append(_diff_row("hunk", None, None, "", esc))
            elif line.startswith("+"):
                rows.append(_diff_row("add", None, new_no, "+", esc))
                new_no += 1
            elif line.startswith("-"):
                rows.append(_diff_row("del", old_no, None, "-", esc))
                old_no += 1
            else:
                rows.append(_diff_row("ctx", old_no, new_no, "", esc))
                old_no += 1
                new_no += 1
        elif fence == "code":
            rows.append(_diff_row("ctx", None, None, "", esc))
        elif line.startswith("# ") or line.startswith("## ") \
                or line.startswith("### "):
            rows.append(_diff_row("mdh", None, None, "", esc))
        else:
            rows.append(_diff_row("", None, None, "", esc))
    return rows


def render_html(artifact):
    """Server-rendered Azure DevOps-style diff page. No external assets; the
    only script is a tiny theme switcher and a show-all toggle. EVERY dynamic
    value is escaped: the body is PR-controlled content, so the same
    comment-injection hardening the Bitbucket comment gets applies here.
    Colors follow the GitHub diff palette (more legible than Monaco's own),
    with Azure DevOps blue chrome. Light / Auto / Dark via a segmented
    control, persisted in localStorage; Auto follows prefers-color-scheme."""
    repo = html.escape(str(artifact.get("repo", "")))
    pr_id = html.escape(str(artifact.get("pr_id", "")))
    sha = html.escape(str(artifact.get("sha", "")))
    base_sha = html.escape(str(artifact.get("base_sha", "") or ""))
    created = html.escape(str(artifact.get("created_utc", "")))
    pr_url = str(artifact.get("pr_url", ""))
    pr_link = (f'<a href="{html.escape(pr_url, quote=True)}">PR #{pr_id}</a>'
               if pr_url else f"PR #{pr_id}")
    raw_href = html.escape(f"/diff/{artifact.get('repo','')}"
                           f"/{artifact.get('pr_id','')}"
                           f"/{artifact.get('sha','')}/raw", quote=True)
    base_bit = f" vs base <code>{base_sha}</code>" if base_sha else ""
    summary = _format_outcome_summary(artifact.get("app_count"),
                                      artifact.get("outcome_counts") or {})
    summary_html = f'<div class="summary">{summary}</div>' if summary else ""

    rows = _render_body_rows(artifact.get("body", ""))
    visible = "".join(rows[:MAX_VISIBLE_LINES])
    overflow = rows[MAX_VISIBLE_LINES:]
    if overflow:
        rest = "".join(overflow)
        n_more = len(overflow)
        rest_html = (
            f'<tbody class="rest" hidden>{rest}</tbody>'
            f'<tbody class="show-all-row"><tr><td colspan="4">'
            f'<button type="button" class="show-all" onclick="'
            f"this.closest('table').querySelector('.rest').hidden=false;"
            f"this.closest('tbody').remove();"
            f'">show full output ({n_more} more lines)</button>'
            f'</td></tr></tbody>')
    else:
        rest_html = ""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{SERVICE_NAME} - {repo} #{pr_id} @ {sha}</title>
<style>
:root {{
  --bg: #ffffff; --fg: #1f2328; --muted: #57606a; --border: #d0d7de;
  --panel: #f6f8fa; --link: #0969da; --accent: #0078d4;
  --gutter-bg: #fafbfc; --gutter-fg: #8b949e;
  --add-bg: #e6ffec; --add-mk: #1a7f37;
  --del-bg: #ffebe9; --del-mk: #cf222e;
  --hunk-bg: #f6f8fa; --hunk-fg: #57606a;
  --seg-bg: #eceef1; --seg-thumb: #ffffff; --seg-active: #0078d4;
}}
:root[data-theme="dark"] {{
  --bg: #0d1117; --fg: #e6edf3; --muted: #8d96a0; --border: #30363d;
  --panel: #161b22; --link: #4493f8; --accent: #4493f8;
  --gutter-bg: #0d1117; --gutter-fg: #6e7681;
  --add-bg: #2ea04326; --add-mk: #3fb950;
  --del-bg: #f8514926; --del-mk: #f85149;
  --hunk-bg: #161b22; --hunk-fg: #8d96a0;
  --seg-bg: #161b22; --seg-thumb: #30363d; --seg-active: #4493f8;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --bg: #0d1117; --fg: #e6edf3; --muted: #8d96a0; --border: #30363d;
    --panel: #161b22; --link: #4493f8; --accent: #4493f8;
    --gutter-bg: #0d1117; --gutter-fg: #6e7681;
    --add-bg: #2ea04326; --add-mk: #3fb950;
    --del-bg: #f8514926; --del-mk: #f85149;
    --hunk-bg: #161b22; --hunk-fg: #8d96a0;
    --seg-bg: #161b22; --seg-thumb: #30363d; --seg-active: #4493f8;
  }}
}}
* {{ box-sizing: border-box; }}
body {{ background: var(--bg); color: var(--fg); margin: 0;
       font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }}
.topbar {{ position: sticky; top: 0; z-index: 5; display: flex;
          align-items: center; justify-content: space-between;
          padding: 9px 18px; background: var(--bg);
          border-bottom: 1px solid var(--border); }}
.wordmark {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
            font-size: 13px; font-weight: 700; letter-spacing: .02em; }}
.seg {{ display: inline-flex; gap: 2px; padding: 2px; border-radius: 7px;
       background: var(--seg-bg); }}
.seg button {{ border: none; background: transparent; width: 30px; height: 24px;
              border-radius: 5px; cursor: pointer; color: var(--muted);
              font-size: 13px; line-height: 1; }}
.seg button[aria-pressed="true"] {{ background: var(--seg-thumb);
              color: var(--seg-active); }}
main {{ max-width: 980px; margin: 0 auto; padding: 1.5rem 1.25rem 3rem; }}
.brand {{ color: var(--muted); font-size: 12px; font-weight: 600;
         text-transform: uppercase; letter-spacing: .08em; }}
h1 {{ margin: .25rem 0 .35rem; font-size: 21px; font-weight: 600; }}
h1 .pr {{ color: var(--muted); font-weight: 400; }}
.meta {{ color: var(--muted); font-size: 13px; margin-bottom: .6rem; }}
.meta a {{ color: var(--link); text-decoration: none; }}
.meta a:hover {{ text-decoration: underline; }}
code {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
       font-size: 12px; background: var(--panel);
       border: 1px solid var(--border); border-radius: 4px; padding: 0 4px; }}
.summary {{ margin: 0 0 1rem; }}
.summary span {{ display: inline-block; background: var(--panel);
                border: 1px solid var(--border); border-radius: 999px;
                color: var(--muted); font-size: 12px;
                padding: 2px 10px; margin: 0 6px 6px 0; }}
.diffwrap {{ border: 1px solid var(--border); border-radius: 8px;
            overflow: auto; max-height: 78vh; }}
table.diff {{ width: 100%; border-collapse: collapse;
             font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
             font-size: 12px; line-height: 20px; }}
table.diff td {{ padding: 0 8px; vertical-align: top; }}
table.diff td.code {{ white-space: pre; width: 100%; }}
td.ln-old, td.ln-new {{ width: 1%; text-align: right; padding: 0 8px;
             color: var(--gutter-fg); background: var(--gutter-bg);
             user-select: none; border-right: 1px solid var(--border); }}
td.mk {{ width: 14px; text-align: center; user-select: none; }}
tr.add td.code {{ background: var(--add-bg); }}
tr.add td.mk {{ color: var(--add-mk); }}
tr.del td.code {{ background: var(--del-bg); }}
tr.del td.mk {{ color: var(--del-mk); }}
tr.hunk td {{ background: var(--hunk-bg); color: var(--hunk-fg); }}
tr.fence td.code {{ color: var(--muted); opacity: .55; }}
tr.mdh td.code {{ font-weight: 700; }}
.show-all {{ width: 100%; border: none; background: var(--panel);
            color: var(--link); font: inherit; font-size: 12px;
            padding: 8px; cursor: pointer; }}
.show-all:hover {{ text-decoration: underline; }}
footer {{ color: var(--muted); font-size: 12px; margin-top: 1rem; }}
</style>
</head>
<body>
<div class="topbar">
  <span class="wordmark">acme-diff-preview</span>
  <div class="seg" role="group" aria-label="Appearance">
    <button type="button" data-set-theme="light" aria-label="Light" title="Light">&#9728;</button>
    <button type="button" data-set-theme="auto" aria-label="Auto" title="Auto">&#9673;</button>
    <button type="button" data-set-theme="dark" aria-label="Dark" title="Dark">&#9789;</button>
  </div>
</div>
<main>
<div class="brand">{SERVICE_NAME}</div>
<h1>{repo} <span class="pr">{pr_link}</span></h1>
<div class="meta">commit <code>{sha}</code>{base_bit}
 &middot; generated {created} &middot; <a href="{raw_href}">raw</a></div>
{summary_html}
<div class="diffwrap">
<table class="diff"><tbody>{visible}</tbody>{rest_html}</table>
</div>
<footer>served by acme-diff-preview &middot; full, untruncated output for this exact commit</footer>
</main>
<script>
(function(){{
  var root=document.documentElement;
  function apply(t){{
    if(t==="auto"){{root.removeAttribute("data-theme");}}
    else{{root.setAttribute("data-theme",t);}}
    var b=document.querySelectorAll("[data-set-theme]");
    for(var i=0;i<b.length;i++){{
      b[i].setAttribute("aria-pressed", b[i].getAttribute("data-set-theme")===t ? "true":"false");
    }}
  }}
  var saved="auto";
  try{{ saved=localStorage.getItem("adp-theme")||"auto"; }}catch(e){{}}
  apply(saved);
  var btns=document.querySelectorAll("[data-set-theme]");
  for(var i=0;i<btns.length;i++){{
    btns[i].addEventListener("click",function(){{
      var t=this.getAttribute("data-set-theme");
      try{{ localStorage.setItem("adp-theme",t); }}catch(e){{}}
      apply(t);
    }});
  }}
}})();
</script>
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
