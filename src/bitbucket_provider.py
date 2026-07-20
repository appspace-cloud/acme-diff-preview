"""BitbucketProvider: the Bitbucket implementation of VCSProvider (COPS-2520).

Extracted from diff_preview.py with NO behavior change — every line of logic
here is byte-for-byte what diff_preview.py ran before this extraction. The
module-level functions in diff_preview.py (bb, _bb_api_base, get_open_prs,
get_pr_changed_files, _verify_bb_hmac) now delegate to a single module-level
instance of this class, so every existing test (which monkeypatches those
exact module-level names) continues to work unchanged.

IMPORTANT: the HTTP transport (`http_fn`) is passed as an argument on every
call, NOT captured once in __init__. diff_preview.py's tests monkeypatch the
module-level `http` name at call time (`monkeypatch.setattr(m, "http", ...)`);
if this class captured a reference to `http` at construction time instead, a
test's monkeypatch would have no effect on calls routed through this class,
and the ORIGINAL (unmocked) http would run and try a real network call. Every
public method here therefore takes `http_fn` as a parameter, so callers in
diff_preview.py can pass whatever the module-level `http` name currently
resolves to at the moment of the call, honoring any active monkeypatch.

Constructor takes its configuration as plain arguments (no import of
diff_preview.py, to avoid a circular import, and so this class is
independently constructible/testable — see tests/test_bitbucket_provider.py).
"""
from __future__ import annotations

import hashlib
import hmac
import posixpath


class BitbucketProvider:
    def __init__(self, workspace: str, default_repo: str,
                 user: str, token: str):
        # Not captured here: webhook_secret and max_pages. Both are read
        # fresh on every call from diff_preview.py's module-level globals
        # (BB_WEBHOOK_SECRET, _BB_MAX_PAGES), because tests monkeypatch
        # both of those at the module level to exercise specific branches
        # (permissive-vs-verified webhook mode; page-limit safety guard).
        # workspace/default_repo/user/token are NOT monkeypatched by any
        # test (they identify which provider instance this is), so capturing
        # them once here is safe.
        self._workspace = workspace
        self._default_repo = default_repo
        self._user = user
        self._token = token

    # ── low-level primitives ─────────────────────────────────────────────

    def api_base(self, repo: str | None = None) -> str:
        """Per-repo Bitbucket API base URL (COPS-2507 multi-repo)."""
        return f"https://api.bitbucket.org/2.0/repositories/{self._workspace}/{repo or self._default_repo}"

    def call(self, method, path, repo, http_fn, **kw):
        url = f"https://api.bitbucket.org/2.0/repositories/{self._workspace}/{repo or self._default_repo}/{path}"
        return http_fn(method, url, auth=(self._user, self._token), **kw)

    # ── VCSProvider surface ──────────────────────────────────────────────

    def list_open_prs(self, repo, http_fn, max_pages):
        # max_pages passed per-call (not stored in __init__): tests
        # monkeypatch diff_preview's module-level _BB_MAX_PAGES to a small
        # value to exercise the page-limit safety guard, and that only
        # takes effect if this method re-reads it on every call.
        base = self.api_base(repo)
        url = f"{base}/pullrequests?state=OPEN&pagelen=50"
        prs, nxt, pages = [], url, 0
        while nxt and pages < max_pages:
            data = http_fn("GET", nxt, auth=(self._user, self._token))
            prs += data.get("values", [])
            nxt = data.get("next")
            pages += 1
        hit_page_limit = pages >= max_pages
        return prs, hit_page_limit

    def get_pr_diffstat(self, pr_id, repo, bb_fn, max_pages):
        """Return (changed_file_paths, renames, hit_page_limit) for a PR's diffstat.

        Rename detection here follows Bitbucket's own diffstat semantics: a
        rename appears as one diffstat entry with both an `old` and a `new`
        path (content-similarity paired by Bitbucket itself, not by us).
        Both paths are kept in `files` because the OLD path is what a live
        ArgoCD Application's valueFiles still reference (v2.4.9 fix), and
        the old->new pairing is recorded in `renames` so the value-fetch
        layer follows the move instead of treating the old path's 404 as a
        deletion (v2.5.4 Finding 6; confirmed live on PRs #6647/#6648/#6649/#6654).

        GitHub's compare/diff API detects renames with a different
        algorithm/threshold and exposes them differently in its response
        shape — this method's logic is Bitbucket-specific and is NOT
        expected to be reusable as-is for a future GitHubProvider (this is
        one of the two hard porting points flagged in COPS-2520).

        `bb_fn` (not http_fn): the ORIGINAL get_pr_changed_files called the
        module-level `bb()` directly (not `http()`), and several tests
        monkeypatch `m.bb` itself to fake diffstat pages. So this method
        takes the current `bb` callable as a parameter and calls it exactly
        as the original code did (`bb_fn("GET", path, repo=repo)`), rather
        than going through self.call() — which would bypass a test's
        monkeypatch of the module-level `bb` name entirely.
        """
        files, renames, path, pages = [], {}, f"pullrequests/{pr_id}/diffstat?pagelen=100", 0
        base = self.api_base(repo)
        while path and pages < max_pages:
            data = bb_fn("GET", path, repo=repo)
            for item in data.get("values", []):
                old_p = (item.get("old") or {}).get("path", "")
                new_p = (item.get("new") or {}).get("path", "")
                for p in (old_p, new_p):
                    if p and p not in files:
                        files.append(p)
                if old_p and new_p and old_p != new_p:
                    renames[posixpath.normpath(old_p.lstrip("/"))] = \
                        posixpath.normpath(new_p.lstrip("/"))
            nxt = data.get("next", "")
            path = nxt.replace(f"{base}/", "") if nxt else ""
            pages += 1
        hit_page_limit = pages >= max_pages and bool(path)
        return files, renames, hit_page_limit

    def verify_webhook_signature(self, body: bytes, header: str, webhook_secret: str) -> bool:
        """Verify Bitbucket X-Hub-Signature HMAC-SHA256 against the shared secret.

        Bitbucket signs the payload as: X-Hub-Signature: sha256=<hex-digest>
        If no secret is configured, the webhook is accepted without
        verification (permissive mode for backward compatibility during
        rollout). Comparison is done in bytes, not str (v2.5.3 CRIT-2):
        hmac.compare_digest on two str values raises TypeError for any
        non-ASCII character, and that exception was uncaught in do_POST.

        webhook_secret is passed per-call (not stored in __init__): tests
        monkeypatch diff_preview's module-level BB_WEBHOOK_SECRET to
        exercise both permissive and verified modes, and that only takes
        effect if this method re-reads it on every call.
        """
        if not webhook_secret:
            return True
        if not header:
            return False
        sig = header.removeprefix("sha256=")
        expected = hmac.new(webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig.encode("utf-8", errors="replace"),
                                    expected.encode("ascii"))


    # ═════════════════════════════════════════════════════════════════════
    # COPS-2520 (part 2): the rest of the VCSProvider surface, so the core in
    # diff_preview.py can drive Bitbucket and GitHub through one contract.
    # Every method below reproduces EXACTLY what diff_preview.py did inline
    # before this extraction — same URLs, same auth, same request bodies — so
    # the existing suite stays green. GitHubProvider mirrors this surface.
    # ═════════════════════════════════════════════════════════════════════

    def _basic_auth_header(self) -> str:
        import base64
        return "Basic " + base64.b64encode(
            f"{self._user}:{self._token}".encode()).decode()

    # ── PR-shape accessors ───────────────────────────────────────────────
    # A Bitbucket PR object is
    # {id, title, source:{commit:{hash}}, destination:{branch:{name}}}.

    @staticmethod
    def pr_id(pr):
        return pr["id"]

    @staticmethod
    def pr_source_sha(pr):
        return pr["source"]["commit"]["hash"]

    @staticmethod
    def pr_dest_branch(pr):
        return pr["destination"]["branch"]["name"]

    @staticmethod
    def pr_title(pr):
        return pr["title"]

    # ── branch head / raw file ───────────────────────────────────────────

    def get_branch_head_sha(self, branch, repo, http_fn):
        """Head commit SHA of `branch`. Bitbucket: GET /refs/branches/{branch}
        -> {target:{hash}}. Uses http_fn with Basic auth, exactly as the
        inline call in main_iteration did."""
        data = http_fn("GET", f"{self.api_base(repo)}/refs/branches/{branch}",
                       auth=(self._user, self._token))
        return data["target"]["hash"]

    def raw_file_url_and_headers(self, filepath, sha, repo):
        """(url, headers) to fetch a raw file at a commit SHA. Byte-for-byte
        the Bitbucket /src/{sha}/{path} URL and Basic-auth header that
        _bb_fetch_status built inline before COPS-2520."""
        url = f"{self.api_base(repo)}/src/{sha}/{filepath}"
        return url, {"Authorization": self._basic_auth_header()}

    # ── comments (normalized {"id","body"} shape) ────────────────────────
    # bb_fn (not http_fn) is passed on every comment call: the inline code in
    # diff_preview.py called the module-level bb() directly, and several tests
    # monkeypatch m.bb. Calling bb_fn exactly as before keeps that seam.

    def get_comment(self, pr_id, comment_id, repo, bb_fn):
        c = bb_fn("GET", f"pullrequests/{pr_id}/comments/{comment_id}", repo=repo)
        return {"id": c.get("id"), "body": c.get("content", {}).get("raw", "")}

    def iter_comments(self, pr_id, repo, bb_fn, max_pages):
        """Yield {"id","body"} for every comment on a PR, paginating exactly
        as find_existing_comment did (relative next-link, base-stripped)."""
        base = self.api_base(repo)
        nxt, pages = f"pullrequests/{pr_id}/comments?pagelen=100", 0
        while nxt and pages < max_pages:
            data = bb_fn("GET", nxt, repo=repo)
            for c in data.get("values", []):
                yield {"id": c["id"], "body": c.get("content", {}).get("raw", "")}
            next_url = data.get("next", "")
            nxt = next_url.replace(f"{base}/", "") if next_url else ""
            pages += 1

    def create_comment(self, pr_id, body, repo, bb_fn):
        c = bb_fn("POST", f"pullrequests/{pr_id}/comments", repo=repo,
                  body={"content": {"raw": body}})
        return {"id": c.get("id") if isinstance(c, dict) else None, "body": body}

    def update_comment(self, pr_id, comment_id, body, repo, bb_fn):
        bb_fn("PUT", f"pullrequests/{pr_id}/comments/{comment_id}", repo=repo,
              body={"content": {"raw": body}})

    # ── build status ─────────────────────────────────────────────────────

    def post_build_status(self, pr_sha, state, description, url, repo, bb_fn,
                          *, key, context):
        """Post a Bitbucket build status. state is already the native
        INPROGRESS/SUCCESSFUL/FAILED value. `key` is the stable status key,
        `context` the display name."""
        bb_fn("POST", f"commit/{pr_sha}/statuses/build", repo=repo, body={
            "state": state, "key": key, "name": context,
            "url": url, "description": (description or "")[:255],
        })

    def get_build_status(self, pr_sha, repo, http_fn, *, key, context):
        """Return our status row for a commit, {"state": ...} in native
        vocabulary. `context` is unused by Bitbucket (the row is keyed by
        `key`); accepted only for a uniform signature across providers."""
        return http_fn("GET",
                       f"{self.api_base(repo)}/commit/{pr_sha}/statuses/build/{key}",
                       auth=(self._user, self._token))

    # ── URLs / limits ────────────────────────────────────────────────────

    def pr_web_url(self, pr_id, repo):
        return (f"https://bitbucket.org/{self._workspace}/"
                f"{repo or self._default_repo}/pull-requests/{pr_id}")

    def comment_anchor(self, comment_id):
        return f"#comment-{comment_id}"

    @property
    def max_comment_bytes(self):
        return 245_000

    # ── webhook ──────────────────────────────────────────────────────────

    signature_header = "X-Hub-Signature"
    event_header = "X-Event-Key"

    @staticmethod
    def is_pr_event(event_value: str) -> bool:
        return bool(event_value) and event_value.startswith("pullrequest:")
