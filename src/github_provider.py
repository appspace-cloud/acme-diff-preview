"""GitHubProvider: the GitHub implementation of VCSProvider (COPS-2520).

Sibling of bitbucket_provider.py. It speaks GitHub's REST API v3 but exposes
the SAME surface BitbucketProvider does, so the diff/render core in
diff_preview.py can drive either host without knowing which one it is.

Design mirrors BitbucketProvider on purpose:

  - The HTTP transport (`http_fn`) is passed on every call, never captured in
    __init__, so a test that monkeypatches diff_preview's module-level `http`
    still takes effect on calls routed through this class (same seam contract
    as BitbucketProvider — see its module docstring).
  - Configuration comes in as plain constructor arguments; this module never
    imports diff_preview (no circular import, independently testable).

Where GitHub genuinely differs from Bitbucket, the difference is contained
entirely inside this class:

  - Auth: a bearer token header (`Authorization: Bearer <token>`), not Basic.
  - Pagination: page-number (`?per_page=N&page=K`) until a short/empty page,
    because diff_preview's `http()` returns only the parsed JSON body and
    discards the `Link` header GitHub would otherwise use for pagination.
  - PR JSON shape: number/head.sha/base.ref instead of
    id/source.commit.hash/destination.branch.name.
  - Rename detection: GitHub's own `files` API marks a rename with
    status == "renamed" and a `previous_filename`, so no content-similarity
    pairing is done here (that is Bitbucket-specific).
  - Comments live on the issues endpoint; a normalized {"id","body"} shape is
    returned so the core's marker/dedup logic is unchanged.
  - Commit-status state vocabulary is pending/success/failure/error; the
    provider translates to/from the core's Bitbucket-style
    INPROGRESS/SUCCESSFUL/FAILED so no caller has to special-case it.
"""
from __future__ import annotations

import hashlib
import hmac
import posixpath
import urllib.parse
import urllib.request


# Commit-status state translation. The core speaks Bitbucket's vocabulary
# (INPROGRESS/SUCCESSFUL/FAILED) as its internal canonical set; GitHub uses
# pending/success/failure/error. Kept as two small maps so the round-trip is
# obvious and symmetric.
_STATE_TO_GH = {"INPROGRESS": "pending", "SUCCESSFUL": "success", "FAILED": "failure"}
_STATE_FROM_GH = {"pending": "INPROGRESS", "success": "SUCCESSFUL",
                  "failure": "FAILED", "error": "FAILED"}

# GitHub caps a commit-status description at 140 characters (Bitbucket allows
# 255). The core already trims to 255 before calling; this is the tighter
# provider-native ceiling applied here so a long description is never rejected.
_GH_STATUS_DESC_MAX = 140


class GitHubProvider:
    def __init__(self, owner: str, default_repo: str, token: str,
                 api_base_url: str = "https://api.github.com",
                 web_base_url: str = "https://github.com"):
        # owner/default_repo/token identify this provider instance and are not
        # monkeypatched by any test, so capturing them once here is safe (same
        # reasoning as BitbucketProvider). api_base_url/web_base_url are
        # constructor arguments purely so tests can point them at a fake host.
        self._owner = owner
        self._default_repo = default_repo
        self._token = token
        self._api = api_base_url.rstrip("/")
        self._web = web_base_url.rstrip("/")

    # ── auth / low-level primitives ──────────────────────────────────────

    def _headers(self, accept: str = "application/vnd.github+json") -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def api_base(self, repo: str | None = None) -> str:
        """Per-repo GitHub API base URL. `repo` is the short slug; the owner
        is prepended so callers pass the same slug they use for Bitbucket."""
        return f"{self._api}/repos/{self._owner}/{repo or self._default_repo}"

    def call(self, method, path, repo, http_fn, **kw):
        url = f"{self.api_base(repo)}/{path}"
        return http_fn(method, url, headers=self._headers(), **kw)

    # ── PR-shape accessors ───────────────────────────────────────────────
    # A GitHub PR object is {number, title, head:{sha, ref}, base:{ref, sha}}.
    # These read the fields the core needs so process_pr/main_iteration never
    # touch the provider-native JSON shape directly (COPS-2520 gaps 2 & 3).

    @staticmethod
    def pr_id(pr):
        return pr["number"]

    @staticmethod
    def pr_source_sha(pr):
        return pr["head"]["sha"]

    @staticmethod
    def pr_dest_branch(pr):
        return pr["base"]["ref"]

    @staticmethod
    def pr_title(pr):
        return pr["title"]

    # ── VCSProvider surface ──────────────────────────────────────────────

    def list_open_prs(self, repo, http_fn, max_pages):
        """Return (open_prs, hit_page_limit). Page-number pagination: GitHub
        returns a bare JSON array and signals "last page" by a short/empty
        result, so we stop when a page is not full."""
        base = self.api_base(repo)
        per_page = 50
        prs, page = [], 1
        while page <= max_pages:
            url = f"{base}/pulls?state=open&per_page={per_page}&page={page}"
            data = http_fn("GET", url, headers=self._headers())
            batch = data if isinstance(data, list) else []
            prs += batch
            if len(batch) < per_page:
                return prs, False
            page += 1
        return prs, True

    def get_pr_diffstat(self, pr_id, repo, http_fn, max_pages):
        """Return (changed_file_paths, renames, hit_page_limit) for a PR.

        GitHub's own diff marks a rename as one `files` entry with
        status == "renamed" and a `previous_filename`. Both the old and the
        new path are kept in `files` (the old path is what a live ArgoCD
        Application's valueFiles may still reference — the same v2.4.9/v2.5.4
        reason as the Bitbucket path) and the old->new pairing is recorded in
        `renames`. No content-similarity pairing is done here; GitHub already
        did it. A "copied" file also carries a previous_filename but is NOT a
        move, so its old path is kept as a changed file without a rename edge.
        """
        base = self.api_base(repo)
        per_page = 100
        files, renames, page = [], {}, 1
        while page <= max_pages:
            url = f"{base}/pulls/{pr_id}/files?per_page={per_page}&page={page}"
            data = http_fn("GET", url, headers=self._headers())
            batch = data if isinstance(data, list) else []
            for item in batch:
                new_p = item.get("filename", "") or ""
                prev_p = item.get("previous_filename", "") or ""
                status = item.get("status", "")
                for p in (prev_p, new_p):
                    if p and p not in files:
                        files.append(p)
                if status == "renamed" and prev_p and new_p and prev_p != new_p:
                    renames[posixpath.normpath(prev_p.lstrip("/"))] = \
                        posixpath.normpath(new_p.lstrip("/"))
            if len(batch) < per_page:
                return files, renames, False
            page += 1
        return files, renames, True

    def get_branch_head_sha(self, branch, repo, http_fn):
        """Return the head commit SHA of `branch` (used for the base/main SHA
        the PR is diffed against). GitHub: GET /branches/{branch} ->
        {commit: {sha}}."""
        data = http_fn("GET", f"{self.api_base(repo)}/branches/{branch}",
                       headers=self._headers())
        return data["commit"]["sha"]

    def raw_file_url_and_headers(self, filepath, sha, repo):
        """Return (url, headers) to fetch a raw file at a commit SHA.

        The core owns the retry / connection-pool / concurrency-limiter loop
        (it is shared infrastructure and the single hottest path on a mass
        PR); this only supplies the provider-specific URL and auth headers.

        GitHub's Contents API returns the raw bytes when asked with the
        `application/vnd.github.raw` media type, and a 404 for a path absent
        at that SHA — the exact (content, 404) distinction the caller's
        BB_OK/BB_NOT_FOUND/BB_ERROR classification needs, identical to
        Bitbucket's /src/{sha}/{path} endpoint.
        """
        # Percent-encode each path segment but keep the slashes as separators.
        safe_path = urllib.parse.quote(filepath.lstrip("/"), safe="/")
        ref = urllib.parse.quote(str(sha), safe="")
        url = f"{self.api_base(repo)}/contents/{safe_path}?ref={ref}"
        return url, self._headers("application/vnd.github.raw")

    # ── comments (normalized {"id","body"} shape) ────────────────────────

    def get_comment(self, pr_id, comment_id, repo, http_fn):
        """Fetch one comment by id, normalized. Raises urllib HTTPError(404)
        if it was deleted — same contract the core relies on for cache
        invalidation."""
        c = http_fn("GET", f"{self.api_base(repo)}/issues/comments/{comment_id}",
                    headers=self._headers())
        return {"id": c.get("id"), "body": c.get("body", "")}

    def iter_comments(self, pr_id, repo, http_fn, max_pages):
        """Yield every comment on a PR as {"id","body"}, oldest API order.
        PR conversation comments are issue comments on GitHub."""
        base = self.api_base(repo)
        per_page = 100
        page = 1
        while page <= max_pages:
            url = f"{base}/issues/{pr_id}/comments?per_page={per_page}&page={page}"
            data = http_fn("GET", url, headers=self._headers())
            batch = data if isinstance(data, list) else []
            for c in batch:
                yield {"id": c.get("id"), "body": c.get("body", "")}
            if len(batch) < per_page:
                return
            page += 1

    def create_comment(self, pr_id, body, repo, http_fn):
        c = http_fn("POST", f"{self.api_base(repo)}/issues/{pr_id}/comments",
                    headers=self._headers(), body={"body": body})
        return {"id": c.get("id") if isinstance(c, dict) else None, "body": body}

    def update_comment(self, pr_id, comment_id, body, repo, http_fn):
        http_fn("PATCH", f"{self.api_base(repo)}/issues/comments/{comment_id}",
                headers=self._headers(), body={"body": body})

    # ── build status ─────────────────────────────────────────────────────

    def post_build_status(self, pr_sha, state, description, url, repo, http_fn,
                          *, key, context):
        """Post a commit status. `state` arrives in the core's Bitbucket-style
        vocabulary and is translated to GitHub's here. `context` is the
        status name shown on the PR; `key` is unused by GitHub (Bitbucket's
        stable status key) and accepted only to keep one call signature across
        providers."""
        gh_state = _STATE_TO_GH.get(state, "error")
        http_fn("POST", f"{self.api_base(repo)}/statuses/{pr_sha}",
                headers=self._headers(), body={
                    "state": gh_state,
                    "target_url": url,
                    "description": (description or "")[:_GH_STATUS_DESC_MAX],
                    "context": context,
                })

    def get_build_status(self, pr_sha, repo, http_fn, *, key, context):
        """Return {"state": <core vocabulary>} for our own status on a commit,
        translated back from GitHub's vocabulary. Returns {} when we have no
        status on that commit yet (mirrors Bitbucket 'no such status')."""
        data = http_fn("GET", f"{self.api_base(repo)}/commits/{pr_sha}/statuses",
                       headers=self._headers())
        rows = data if isinstance(data, list) else []
        for row in rows:
            # /commits/{sha}/statuses is newest-first, so the first row that
            # matches our context is the current one.
            if row.get("context") == context:
                return {"state": _STATE_FROM_GH.get(row.get("state"), "FAILED")}
        return {}

    # ── URLs / limits ────────────────────────────────────────────────────

    def pr_web_url(self, pr_id, repo):
        return f"{self._web}/{self._owner}/{repo or self._default_repo}/pull/{pr_id}"

    def comment_anchor(self, comment_id):
        return f"#issuecomment-{comment_id}"

    @property
    def max_comment_bytes(self):
        # GitHub's hard limit is 262144 bytes; keep the same headroom margin
        # the Bitbucket path uses so truncation behaves identically for both.
        return 245_000

    # ── webhook ──────────────────────────────────────────────────────────

    # Header names GitHub uses on an inbound webhook. Exposed as attributes so
    # the HTTP handler in diff_preview can read the right headers per provider
    # without hard-coding either host's names.
    signature_header = "X-Hub-Signature-256"
    event_header = "X-GitHub-Event"

    @staticmethod
    def is_pr_event(event_value: str) -> bool:
        """True if the webhook event header denotes a pull-request event."""
        return event_value == "pull_request"

    def verify_webhook_signature(self, body: bytes, header: str,
                                 webhook_secret: str) -> bool:
        """Verify GitHub's X-Hub-Signature-256 HMAC-SHA256 over the raw body.

        Same algorithm and permissive-when-unset convention as
        BitbucketProvider (GitHub also prefixes the digest with 'sha256=').
        Comparison is done in bytes, not str, so a non-ASCII header value can
        never raise TypeError on this pre-auth path (v2.5.3 CRIT-2 rationale).
        """
        if not webhook_secret:
            return True
        if not header:
            return False
        sig = header.removeprefix("sha256=")
        expected = hmac.new(webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig.encode("utf-8", errors="replace"),
                                   expected.encode("ascii"))
