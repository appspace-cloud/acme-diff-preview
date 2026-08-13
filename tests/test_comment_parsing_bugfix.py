"""Regression tests for the comment-parsing bugs found in the v2.4.5
autonomous test-battery round: two regexes that read back the bot's own
comment could NEVER match what the bot actually writes.

CRITICAL TESTING PRINCIPLE embodied here: every mock comment in this file
is built via the REAL writer functions (format_comment) or the exact real
literal formats, never hand-typed approximations. Hand-typed mocks are
exactly how these two bugs went undetected since v1.9.1 - a test's mock
comment coincidentally didn't need to match the real format to "pass".

Bug 1 - sha extractor: real header is "**Commit** `{sha}`" (bold, space
before backtick); the old regex was r'Commit `([0-9a-f]{8})`' (no **, no
space) and never matched. Consequence: comment_sha was always "", so the
cross-pod dedup (`comment_sha == pr_sha[:8]`) never fired -> every pod
restart caused a full unnecessary re-diff of every open PR.

Bug 2 - status token extractor: real footer is "{MARKER} [{token}]" (em
dash + space before the marker, never a literal '['); the old regex was
the pattern r bracket + MARKER + ... (required a literal bracket immediately before the
marker) and never matched. Consequence: _extract_status_token always fell
back to legacy substring matching, which happened to reproduce intended
behavior for clean/transient/error but not for permanent errors (their
status text also contains "Diff incomplete", the transient-detection
substring) - so permanent errors were retried forever, and in the
fix_stuck_inprogress crash-recovery path could resolve to a false
"SUCCESSFUL" Bitbucket status instead of "FAILED".
"""
import importlib
import os
import re
import sys

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


def _import_module():
    os.environ.setdefault("BB_USER", "test")
    os.environ.setdefault("BB_TOKEN", "test")
    os.environ.setdefault("ARGOCD_PASS", "test")
    os.environ.setdefault("JFROG_WEBHOOK_SECRET", "testsecret")
    if SRC not in sys.path:
        sys.path.insert(0, SRC)
    mod = importlib.import_module("diff_preview")
    return importlib.reload(mod)


def _source():
    """Every module the service ships, concatenated.

    This used to read diff_preview.py alone, which was the whole service.
    COPS-2658 moved `_extract_status_token` and `_extract_comment_sha` into
    app_meta.py, and a guard that counts occurrences in one file would then
    have found zero and failed for a reason that has nothing to do with the
    bug it protects.

    Scanning the tree is also what the guard actually meant. The v2.4.5 bug
    survived because the marker pattern was DUPLICATED at a call site instead
    of shared, so "exactly one occurrence" has to hold across every module a
    call site could live in, not just the one the helper happens to sit in.
    """
    out = []
    for fn in sorted(os.listdir(SRC)):
        if fn.endswith(".py"):
            with open(os.path.join(SRC, fn)) as f:
                out.append(f.read())
    return "\n".join(out)


# ── The dead-code sentinel: both broken patterns must be gone entirely ──────
def test_broken_patterns_fully_retired():
    src = _source()
    # Check for LIVE call sites specifically (re.search(r'...')), not the
    # bug's own description quoted in this fix's explanatory docstrings.
    assert "re.search(r'Commit `([0-9a-f]{8})`'" not in src, (
        "the sha extractor that can never match '**Commit** `sha`' is still a live call site"
    )
    # re.escape(COMMENT_MARKER) must appear EXACTLY once in the whole file:
    # inside _extract_status_token's own definition. More than once means an
    # inline copy has crept back in at some call site (how the bug survived
    # originally - it was duplicated instead of shared).
    assert src.count("re.escape(COMMENT_MARKER)") == 1, (
        f"expected exactly 1 occurrence (inside the shared helper), found {src.count('re.escape(COMMENT_MARKER)')}"
    )
    assert src.count("def _extract_comment_sha(") == 1
    assert src.count("def _extract_status_token(") == 1
    assert src.count("_extract_comment_sha(") >= 3
    assert src.count("_extract_status_token(") >= 3

    assert src.count("def _extract_status_token(") == 1


# ── Bug 1: sha extraction against REAL format_comment() output ─────────────
def test_sha_extraction_matches_real_comment_header(monkeypatch):
    mod = _import_module()
    monkeypatch.setattr(mod, "generate_ai_summary", lambda *a, **k: None)
    results = {"app-a": mod.DiffResult("", [], 0, False, None, mod.OUT_NO_DIFF, "")}
    real_comment = mod.format_comment("deadbeef" + "0" * 32, results, base_sha="b" * 40)
    sha = mod._extract_comment_sha(real_comment)
    assert sha == "deadbeef", f"expected 'deadbeef', got {sha!r} from real comment output"


def test_sha_extraction_empty_on_no_match():
    mod = _import_module()
    assert mod._extract_comment_sha("no commit info here") == ""


# ── Bug 2: status-token extraction against REAL format_comment() output,
# across every outcome branch that sets a different token ──────────────────
def test_status_token_clean_matches_real_output(monkeypatch):
    mod = _import_module()
    monkeypatch.setattr(mod, "generate_ai_summary", lambda *a, **k: None)
    results = {"app-a": mod.DiffResult("", [], 0, False, None, mod.OUT_NO_DIFF, "")}
    comment = mod.format_comment("a" * 40, results, base_sha="b" * 40)
    assert mod._extract_status_token(comment) == "clean"


def test_status_token_permanent_matches_real_output(monkeypatch):
    mod = _import_module()
    monkeypatch.setattr(mod, "generate_ai_summary", lambda *a, **k: None)
    results = {"app-a": mod.DiffResult("", [], 0, False, "chart not found",
                                       mod.OUT_INDETERMINATE, mod.REASON_OCI_NOT_FOUND)}
    comment = mod.format_comment("a" * 40, results, base_sha="b" * 40)
    assert mod._extract_status_token(comment) == "permanent", (
        "an oci_not_found (permanent) error must round-trip as 'permanent', "
        "not silently fail to match and fall back to legacy text-matching "
        "(which mis-derives 'transient' behavior for this case)"
    )


def test_status_token_transient_matches_real_output(monkeypatch):
    mod = _import_module()
    monkeypatch.setattr(mod, "generate_ai_summary", lambda *a, **k: None)
    results = {"app-a": mod.DiffResult("", [], 0, False, "timeout",
                                       mod.OUT_INDETERMINATE, mod.REASON_TIMEOUT)}
    comment = mod.format_comment("a" * 40, results, base_sha="b" * 40)
    assert mod._extract_status_token(comment) == "transient"


def test_status_token_empty_on_legacy_comment_without_token():
    mod = _import_module()
    assert mod._extract_status_token("some old comment with no token at all") == ""


# ── Consequence #1 (cross-pod dedup): a pod restart must NOT re-diff a PR
# whose posted comment already covers the exact current sha ────────────────
def test_cross_pod_dedup_actually_works_after_pod_restart(monkeypatch):
    """Simulates the scenario the bug broke: pod A posts a clean comment for
    sha X, pod A dies, pod B starts fresh (_seen empty) and sees the same
    PR still at sha X. Pod B must SKIP, not re-run the whole diff."""
    mod = _import_module()
    monkeypatch.setattr(mod, "generate_ai_summary", lambda *a, **k: None)
    pr_sha = "deadbeef" + "0" * 32

    results = {"app-a": mod.DiffResult("", [], 0, False, None, mod.OUT_NO_DIFF, "")}
    posted_comment = mod.format_comment(pr_sha, results, base_sha="b" * 40)

    monkeypatch.setattr(mod, "find_existing_comment",
                        lambda pid, repo=None: (55, mod._extract_comment_sha(posted_comment), posted_comment))
    monkeypatch.setattr(mod, "fix_stuck_inprogress", lambda *a, **k: None)

    diff_ran = []
    monkeypatch.setattr(mod, "get_pr_changed_files", lambda pid, repo=None: diff_ran.append(1) or [])

    # _seen is EMPTY, as it would be right after a pod restart.
    assert mod._seen.get(77) is None
    mod.process_pr(
        {"id": 77, "title": "t", "source": {"commit": {"hash": pr_sha}},
         "destination": {"branch": {"name": "main"}}},
        {}, base_sha="b" * 40)

    assert not diff_ran, (
        "cross-pod dedup failed: a freshly-restarted pod re-ran the full diff "
        "even though the existing comment already covers this exact sha+base"
    )


# ── Consequence #2 (crash recovery): a permanent error must resolve to
# FAILED, not SUCCESSFUL, when recovering a stuck INPROGRESS status ────────
def test_stuck_inprogress_recovery_marks_permanent_error_as_failed(monkeypatch):
    mod = _import_module()
    monkeypatch.setattr(mod, "generate_ai_summary", lambda *a, **k: None)
    results = {"app-a": mod.DiffResult("", [], 0, False, "chart not found",
                                       mod.OUT_INDETERMINATE, mod.REASON_OCI_NOT_FOUND)}
    comment = mod.format_comment("a" * 40, results, base_sha="b" * 40)

    monkeypatch.setattr(mod, "http", lambda *a, **k: {"state": "INPROGRESS"})
    posted = {}
    def fake_http2(method, url, **kw):
        if "statuses/build" in url and method == "GET":
            return {"state": "INPROGRESS"}
        posted["call"] = (method, url, kw)
        return {}
    monkeypatch.setattr(mod, "http", fake_http2)
    monkeypatch.setattr(mod, "post_build_status",
                        lambda sha, state, desc, pr_id=None, repo=None: posted.update(state=state, desc=desc))

    mod.fix_stuck_inprogress("a" * 40, 88, comment)

    assert posted.get("state") == "FAILED", (
        f"a permanent (oci_not_found) error recovered from a stuck INPROGRESS "
        f"status must resolve to FAILED, got {posted.get('state')!r} -- a false "
        f"'SUCCESSFUL' would let a hard-blocked PR look mergeable"
    )
