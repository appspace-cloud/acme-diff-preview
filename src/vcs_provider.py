"""VCSProvider: the interface acme-diff-preview uses to talk to whichever
git host a repo lives on. Two concrete providers implement it today:
BitbucketProvider (bitbucket_provider.py) and GitHubProvider
(github_provider.py) — COPS-2520. Everything the diffing/rendering logic
needs from a PR's host goes through this contract, so that logic never has
to know which host it is talking to.

Status (COPS-2520):
  - Part 1 extracted the read-only, stateless subset (list_open_prs,
    get_pr_diffstat, verify_webhook_signature, api_base/call).
  - Part 2 (this change) completes the surface so the core can drive either
    host end to end: PR-shape accessors (pr_id/pr_source_sha/pr_dest_branch/
    pr_title), the base/main SHA fetch (get_branch_head_sha), the hot raw
    file read (raw_file_url_and_headers), comment CRUD in a normalized
    {"id","body"} shape (get_comment/iter_comments/create_comment/
    update_comment), commit build-status post/read (post_build_status/
    get_build_status, translating the pending/success/failure vocabulary),
    PR/comment URL construction (pr_web_url/comment_anchor), the comment-size
    limit (max_comment_bytes), and the webhook header names + event test
    (signature_header/event_header/is_pr_event).
  - Business logic (comment marker matching, the comment-id cache, dedup,
    truncation, the retry/pool/concurrency loop around the raw fetch) stays
    in diff_preview.py: providers only supply transport + native shape.

Method signatures below are the CORE-FACING contract. Concrete providers take
extra transport arguments (an http_fn / bb_fn, page limits, status key/context)
so tests can inject fakes and monkeypatch the module-level transport; those are
implementation parameters, intentionally omitted from the Protocol stubs.
runtime_checkable structural typing verifies method NAMES, not signatures, so
this stays a light contract with no ABC/mypy machinery.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class VCSProvider(Protocol):
    """What acme-diff-preview needs from a git-hosting provider.

    A repo slug (`repo`) is always the provider-native identifier for that
    repo (a Bitbucket repo slug; a GitHub short repo name whose owner the
    provider prepends) — never assumed to be host-specific by callers.
    """

    # ── read: PRs and diffs ──────────────────────────────────────────────
    def list_open_prs(self, repo=None): ...            # pragma: no cover
    def get_pr_diffstat(self, pr_id, repo=None): ...   # pragma: no cover

    # ── PR-shape accessors (host-native JSON -> core fields) ─────────────
    def pr_id(self, pr): ...                            # pragma: no cover
    def pr_source_sha(self, pr): ...                    # pragma: no cover
    def pr_dest_branch(self, pr): ...                   # pragma: no cover
    def pr_title(self, pr): ...                         # pragma: no cover

    # ── read: branch head + raw file ─────────────────────────────────────
    def get_branch_head_sha(self, branch, repo=None): ...              # pragma: no cover
    def raw_file_url_and_headers(self, filepath, sha, repo=None): ...  # pragma: no cover

    # ── comments (normalized {"id","body"}) ──────────────────────────────
    def get_comment(self, pr_id, comment_id, repo=None): ...           # pragma: no cover
    def iter_comments(self, pr_id, repo=None): ...                     # pragma: no cover
    def create_comment(self, pr_id, body, repo=None): ...              # pragma: no cover
    def update_comment(self, pr_id, comment_id, body, repo=None): ...  # pragma: no cover

    # ── build status (core INPROGRESS/SUCCESSFUL/FAILED vocabulary) ───────
    def post_build_status(self, pr_sha, state, description, url, repo=None): ...  # pragma: no cover
    def get_build_status(self, pr_sha, repo=None): ...                 # pragma: no cover

    # ── URLs / limits ────────────────────────────────────────────────────
    def pr_web_url(self, pr_id, repo=None): ...                        # pragma: no cover
    def comment_anchor(self, comment_id): ...                          # pragma: no cover

    # ── webhook ──────────────────────────────────────────────────────────
    def verify_webhook_signature(self, body: bytes, header: str) -> bool: ...  # pragma: no cover
    def is_pr_event(self, event_value: str) -> bool: ...               # pragma: no cover
