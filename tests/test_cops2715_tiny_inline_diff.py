"""A change small enough to read gets shown, not linked (COPS-2715).

Field report, three real pull requests. acme-config-prod #4437 removes one
env var; #4436 adds the same one back. Their comments are BYTE-IDENTICAL
apart from the sha — two opposite changes, the same text, because phase E
(COPS-2612) moved every hunk to the full-diff page. acme-config-dev #7243
bumps one environment and its comment is 964 bytes of no evidence at all.

Phase E was right about the pull requests that motivated it: 10 of the last
40 prod comments sat on Bitbucket's 245KB wall, one rendering 473 hunks. It
is wrong for a 323-byte change.

Measured over 120 live artifacts, rendered hunk bytes are bimodal —
p25 = 482, p50 = 9,350 — and the small band is almost all single-hunk PRs.
The three reported PRs measure 323, 323 and 2,354 bytes.

Two things this must NOT do, and both have their own test below:

  * it must not change the default surface. The threshold is 0 (off) by
    default, which is what lets every phase E test and every golden keep
    describing the default shape untouched.
  * it must not drag in the rest of `profile.inline_diffs`. That flag also
    stops clean apps rolling up into one count — on acme-config-prod #4428
    (20 apps evaluated, one 211-byte hunk) that is 19 lines of green noise.
    So the flag reaches _format_app_diff_block and nothing else.
"""
import os
import sys

import pytest

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import app_meta                     # noqa: E402
import diff_preview as m            # noqa: E402
import render_profile               # noqa: E402


URL = "https://argocd.appspace.com/diff/acme-config-prod/4437/489e5a8f05ab"

# The real #4437 hunk, byte for byte from the stored artifact.
HUNK_4437 = (
    "--- \n+++ \n@@ -96,8 +96,6 @@\n"
    "             secretKeyRef:\n"
    "               key: mysql-password\n"
    "               name: mysql-password\n"
    "-        - name: Processing_N241DeduplicateDryRun\n"
    '-          value: "false"\n'
    "         - name: TracingGcpProjectId\n"
    "           value: appspace-cloud\n"
)
HDR = "/apps/Deployment cl-prod-b/apigateway"


@pytest.fixture(autouse=True)
def _no_ai(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda *a, **k: None)


def _changed(sections):
    return m.DiffResult("d", sections, len(sections), True, "",
                        m.OUT_DIFF, "changes")


def _clean():
    return m.DiffResult("", [], 0, False, "", m.OUT_NO_DIFF, "clean")


def _world(sections, n_clean=1):
    apps = {"cl-prod-b-ms": _changed(sections)}
    for i in range(n_clean):
        apps[f"cl-prod-b-{'ss' if i == 0 else f'x{i}'}"] = _clean()
    return apps


def _comment(sections, threshold, monkeypatch, n_clean=1, **kw):
    monkeypatch.setattr(render_profile, "COMMENT_SMALL_DIFF_INLINE_BYTES",
                        threshold)
    return m.format_comment("489e5a8f", _world(sections, n_clean),
                            artifact_url=URL, **kw)


# ── the default surface is untouched ─────────────────────────────────────

def test_off_by_default_keeps_the_summary_only_comment(monkeypatch):
    """The whole reason this ships as a threshold: at 0 the comment is
    byte-for-byte the phase E shape every golden and every 2607e test
    describes."""
    body = _comment([(HDR, HUNK_4437)], 0, monkeypatch)
    assert "```" not in body
    assert "Processing_N241DeduplicateDryRun" not in body
    assert "Full rendered diff" in body


def test_the_default_really_is_zero():
    assert render_profile.COMMENT_SMALL_DIFF_INLINE_BYTES == 0


# ── switched on: the tiny change shows its diff ──────────────────────────

def test_a_tiny_change_carries_its_hunk(monkeypatch):
    body = _comment([(HDR, HUNK_4437)], 4000, monkeypatch)
    assert "```diff" in body
    assert "Processing_N241DeduplicateDryRun" in body
    assert "-        - name: Processing_N241DeduplicateDryRun" in body, (
        "the minus sign is the whole point: #4436 adds this line back and "
        "must not render the same comment as #4437")


def test_the_added_and_removed_versions_no_longer_read_the_same(monkeypatch):
    """#4436 and #4437 are exact inverses. Today their comments are
    identical; with the diff inline they must differ."""
    removed = _comment([(HDR, HUNK_4437)], 4000, monkeypatch)
    added = _comment([(HDR, HUNK_4437.replace("\n-        - name",
                                              "\n+        - name")
                       .replace('\n-          value: "false"',
                                '\n+          value: "false"'))],
                     4000, monkeypatch)
    assert removed != added


def test_a_change_over_the_threshold_stays_linked(monkeypatch):
    big = [(f"/apps/Deployment cl-prod-b/svc{i}", HUNK_4437 * 4)
           for i in range(10)]
    body = _comment(big, 4000, monkeypatch)
    assert "```diff" not in body
    assert "Full rendered diff" in body


def test_the_threshold_is_measured_on_full_sections_not_on_text(monkeypatch):
    """r.text is pre-capped at MAX_RESOURCES_FULL sections of
    MAX_DIFF_CHARS each, so it saturates: a 200-resource app would look
    'small' through it. The gate must read the section bodies."""
    many = [(f"/apps/Deployment cl-prod-b/svc{i}", HUNK_4437)
            for i in range(200)]
    res = _changed(many)
    # A deliberately tiny .text, the way the real cap would leave it.
    res = res._replace(text="x" * 50)
    monkeypatch.setattr(render_profile, "COMMENT_SMALL_DIFF_INLINE_BYTES", 4000)
    body = m.format_comment("489e5a8f",
                            {"cl-prod-b-ms": res, "cl-prod-b-ss": _clean()},
                            artifact_url=URL)
    assert "```diff" not in body, (
        "a 200-resource app was inlined because the gate read the "
        "pre-capped text instead of the sections")


# ── the narrowness: nothing else about the comment moves ─────────────────

def test_clean_apps_still_roll_up_into_one_count(monkeypatch):
    """profile.inline_diffs would also name every clean app individually —
    19 lines of green noise on acme-config-prod #4428. The flag must reach
    the diff block and nothing else."""
    body = _comment([(HDR, HUNK_4437)], 4000, monkeypatch, n_clean=4)
    assert "```diff" in body, "precondition: the tiny path is active"
    assert "4 application(s) unchanged" in body
    assert "— no manifest changes" not in body, (
        "clean apps were named one by one; the flag leaked past the block")


def test_the_overview_table_still_renders(monkeypatch):
    body = _comment([(HDR, HUNK_4437)], 4000, monkeypatch)
    assert "#### Changeset overview" in body
    assert "| App | Status |" in body


def test_a_no_change_pr_is_unaffected(monkeypatch):
    monkeypatch.setattr(render_profile, "COMMENT_SMALL_DIFF_INLINE_BYTES", 4000)
    body = m.format_comment("489e5a8f", {"cl-prod-b-ms": _clean()},
                            artifact_url=URL)
    assert "```diff" not in body


def test_the_full_page_is_never_touched_by_the_threshold(monkeypatch):
    """The page already inlines everything through FULL_PROFILE. The gate
    must not be the reason it does, or a change here could silently take
    evidence off the one surface that must hold all of it."""
    monkeypatch.setattr(render_profile, "COMMENT_SMALL_DIFF_INLINE_BYTES", 0)
    page = m.format_comment("489e5a8f", _world([(HDR, HUNK_4437)]),
                            profile=render_profile.FULL_PROFILE)
    assert "```diff" in page
    assert "Processing_N241DeduplicateDryRun" in page


# ── the footer tokens survive hostile hunk content ───────────────────────

SHADOW = (
    "--- \n+++ \n@@ -1,3 +1,3 @@\n"
    "-        - name: note\n"
    '-          value: "acme-diff-preview [clean] [base:deadbeef]"\n'
    "+        - name: note\n"
    '+          value: "acme-diff-preview [transient] [base:cafe1234]"\n'
)


def test_a_hunk_cannot_shadow_the_status_token(monkeypatch):
    """The token lives in the footer; everything above it can be rendered
    manifest content, i.e. bytes a PR author controls. Reading the FIRST
    match let a hunk decide the rerun policy, and an unrecognised token
    falls through fix_stuck_inprogress to SUCCESSFUL (COPS-2668)."""
    body = _comment([(HDR, SHADOW)], 4000, monkeypatch)
    assert "acme-diff-preview [clean]" in body, "precondition: the bait is in"
    assert app_meta._extract_status_token(body) == "clean", (
        "the footer token lost to a hunk")

    # Rewrite ONLY the footer occurrence (the last one) and check the
    # reader follows it rather than the bait sitting above.
    old, new = "acme-diff-preview [clean]", "acme-diff-preview [blocked]"
    i = body.rfind(old)
    assert i > body.find(old), "precondition: the bait is earlier than the footer"
    blocked = body[:i] + new + body[i + len(old):]
    assert app_meta._extract_status_token(blocked) == "blocked", (
        "the reader is still taking the first match, not the footer")


def test_a_hunk_cannot_shadow_the_base_token(monkeypatch):
    body = _comment([(HDR, SHADOW)], 4000, monkeypatch, base_sha="9eab6d1e")
    assert "[base:deadbeef]" in body, "precondition: the bait is in"
    import re
    found = re.findall(r"\[base:([0-9a-f]{4,12})\]", body)
    assert found[-1] == "9eab6d1e", (
        "the real base token must be the last one, or the freshness check "
        "either re-renders every poll or freezes a stale comment")
    assert "deadbeef" in found, "the bait really was earlier in the body"
