"""The full-diff page must never truncate or expire (COPS-2610, phase C).

Phase B made the link to this page unconditional; this phase makes the page
worthy of it. Measured before writing a line of code:

- acme-config-prod #3887's stored artifact carries **981** occurrences of
  "diff truncated for display" -- 981 places where the page that calls
  itself "full, untruncated output" tells the reader to go somewhere else.
- The same artifact is 25.7MB / 786,150 lines and renders to **113MB of
  HTML in 1.14s** with everything shipped (visible rows are a paint
  default, not a truncation). That number is why the visible-line cap is
  raised, not removed: 786K visible <tr> on first paint is a browser
  killer, and nothing is lost behind the button.
- The GCS lifecycle deletes artifacts at 90 days. After phase E that means
  a merged PR has no retrievable record; retention moves to 365 days in
  the bucket's terragrunt module (the durable layer -- _prune only walks
  the local cache).
- The local cache prunes by COUNT (500) while living in a 1Gi emptyDir.
  500 x the observed 26.7MB worst case is 25x the eviction limit, so the
  prune gains a byte budget.

Non-negotiable, tested here: redaction is not truncation. Uncapping must
not move one byte out of _redact_for_display.
"""
import json
import os
import sys

import pytest

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m
import diff_ui as ui

SHA = "a2e6383e7c1d4f5a6b7c8d9e0f1a2b3c4d5e6f70"
OTHER_SHA = "beef00d1c2b3a49586778695a4b3c2d1e0f90807"
TRUNC_MARK = "diff truncated for display"


def _one_app(body, header="/apps/ConfigMap pv-big-a/data", n_res=1,
             sections=None):
    secs = sections if sections is not None else [(header, body)]
    return {"pv-big-a-ss": m.DiffResult(
        body[:2000], secs, n_res, True, None, m.OUT_DIFF, "diff")}


# --- 1. no resource body is ever cut on the page ------------------------

def test_a_20k_body_is_whole_on_the_page_and_capped_on_the_comment():
    """The 6,000-char body cap is a COMMENT protection (one giant ConfigMap
    rewrite must not push the comment past MAX_COMMENT_BYTES and chop the
    status token off). On the page it was simply a lie: measured 981 cuts
    in one production artifact."""
    needle_top = "replica-first-marker: 111"
    needle_end = "replica-last-marker: 999"
    body = (f"+    {needle_top}\n"
            + "".join(f"+    filler-{i}: x\n" for i in range(1200))
            + f"+    {needle_end}\n")
    assert len(body) > 3 * m.DISPLAY_BODY_MAX_CHARS, "fixture must dwarf the cap"
    results = _one_app(body)
    page = m.format_comment(SHA, results, profile=m.FULL_PROFILE)
    assert needle_end in page, "the tail of the body must survive on the page"
    assert TRUNC_MARK not in page
    comment = m.format_comment(SHA, results)
    assert needle_top in comment
    assert needle_end not in comment, "the comment cap must keep protecting"
    assert TRUNC_MARK in comment


# --- 2. every section of every app is on the page -----------------------

def test_450_sections_are_all_stored_and_all_rendered():
    """FULL_SECTIONS_MAX_PER_APP=400 trimmed at STORAGE time, before either
    render, so the remainder was gone from the artifact too -- and the
    comment's note claimed those resources were 'only in the full diff
    view', which was exactly where they were not."""
    secs = [(f"/apps/ConfigMap pv-many-a/cm-{i:03d}", f"+  v-{i:03d}: 1\n")
            for i in range(450)]
    packaged = m._package_sections(secs)
    stored = packaged[1]
    assert len(stored) == 450, \
        f"storage kept {len(stored)} of 450; the artifact is already lossy"
    results = _one_app("", n_res=450, sections=stored)
    page = m.format_comment(SHA, results, profile=m.FULL_PROFILE)
    for i in (0, 199, 399, 449):
        assert f"cm-{i:03d}" in page


def test_the_storage_cap_still_exists_and_is_loud_when_hit():
    """The cap stays as a memory-safety bound (raised, env-tunable), but
    hitting it may never again be silent: the stats must say so, because a
    silent trim after phase E is information that ceased to exist."""
    n = m.FULL_SECTIONS_MAX_PER_APP + 7
    secs = [(f"/apps/ConfigMap pv-huge-a/cm-{i}", "+  a: 1\n")
            for i in range(n)]
    with m._diff_stats_lock:
        before = m._diff_stats.get("section_cap_trims", 0)
    stored = m._package_sections(secs)[1]
    assert len(stored) == m.FULL_SECTIONS_MAX_PER_APP
    with m._diff_stats_lock:
        assert m._diff_stats["section_cap_trims"] == before + 1


# --- 3. redaction is not truncation --------------------------------------

def test_a_secret_far_past_the_old_cap_is_fully_redacted_on_the_page():
    """The one non-negotiable. The body cap accidentally bounded how much
    secret material could ever reach the page; removing the cap must not
    widen that by a byte. Every value in a Secret body must be [REDACTED]
    on the FULL render, including the ones past position 6,000."""
    lines = [f"+  secret-value-{i:04d}: hunter2-{i:04d}" for i in range(400)]
    body = "kind: Secret\n" + "\n".join(lines) + "\n"
    assert len(body) > 2 * m.DISPLAY_BODY_MAX_CHARS
    page = m.format_comment(
        SHA, _one_app(body, header="/apps/Secret pv-big-a/credentials"),
        profile=m.FULL_PROFILE)
    assert "hunter2-0001" not in page, "a value before the old cap leaked"
    assert "hunter2-0399" not in page, "a value AFTER the old cap leaked"
    assert "[REDACTED]" in page


# --- 4. the escape hatch restores today exactly --------------------------

def test_uncapped_false_restores_the_old_page(monkeypatch):
    monkeypatch.setattr(m, "FULL_PAGE_UNCAPPED", False)
    body = "".join(f"+    filler-{i}: x\n" for i in range(700))
    page = m.format_comment(SHA, _one_app(body),
                            profile=m.FULL_PROFILE)
    assert TRUNC_MARK in page, \
        "with the hatch off, the page must cap bodies exactly as before"


# --- 5. visible-line default ---------------------------------------------

def test_a_typical_page_renders_fully_visible_no_button():
    """Old default: 1,500 visible lines, so a routine 3,000-line PR opened
    mostly folded. New default is high enough that typical PRs render whole;
    the button remains for the measured monster (786K lines / 113MB HTML),
    where 'all visible' is a browser killer, not a feature."""
    body = "```diff\n" + "\n".join(f"+ line-{i}" for i in range(5000)) + "\n```"
    html_page = ui.render_html({"repo": "acme-config-dev", "pr_id": 1,
                                "sha": SHA, "body": body})
    assert "show full output" not in html_page
    assert "line-4999" in html_page


def test_a_monster_page_keeps_the_button_and_every_line():
    n = ui.MAX_VISIBLE_LINES + 500
    body = "```diff\n" + "\n".join(f"+ line-{i}" for i in range(n)) + "\n```"
    html_page = ui.render_html({"repo": "acme-config-dev", "pr_id": 1,
                                "sha": SHA, "body": body})
    assert "show full output" in html_page
    assert f"line-{n - 1}" in html_page, "hidden is not dropped"


# --- 6. the page says which commit it shows -------------------------------

def _saved(tmp_path):
    ui.save_artifact(str(tmp_path), "acme-config-dev", 42, SHA,
                     "```diff\n+ a: 1\n```", pr_url="")
    return str(tmp_path)


def test_requesting_an_older_sha_states_the_mismatch(tmp_path):
    """The artifact is keyed by (repo, pr) on purpose: one live page per PR,
    like the one comment. Opening the build status of an OLDER commit
    therefore serves the current tip -- fine, as long as the page says so
    instead of letting a reviewer read commit A's page as commit B's."""
    base = _saved(tmp_path)
    code, ctype, payload = ui.respond(
        f"/diff/acme-config-dev/42/{OTHER_SHA}", base, True)
    assert code == 200
    text = payload.decode()
    assert OTHER_SHA[:12] in text, "the requested sha must be named"
    assert SHA[:12] in text, "the stored sha must be named"
    assert "you requested" in text


def test_requesting_the_stored_sha_shows_no_mismatch_banner(tmp_path):
    base = _saved(tmp_path)
    code, _, payload = ui.respond(
        f"/diff/acme-config-dev/42/{SHA}", base, True)
    assert code == 200
    assert "you requested" not in payload.decode()


# --- 7. a pruned artifact fails loudly ------------------------------------

def test_a_missing_artifact_renders_an_explanation_not_a_dead_end(tmp_path):
    """Today a pruned page is two words of text/plain. After phase E that
    moment is a reviewer discovering the only record is gone; the least the
    page owes them is what happened and where else to look."""
    code, ctype, payload = ui.respond(
        f"/diff/acme-config-dev/9999/{SHA}", str(tmp_path), True)
    assert code == 404
    assert "text/html" in ctype
    text = payload.decode()
    assert "no longer retained" in text
    assert "acme-config-dev" in text and "9999" in text


# --- 8. /raw stays byte-exact ---------------------------------------------

def test_raw_is_byte_exact_against_the_stored_body(tmp_path):
    body = "```diff\n+ exact: \u2713 bytes\n```"
    ui.save_artifact(str(tmp_path), "acme-config-dev", 43, SHA, body)
    code, ctype, payload = ui.respond(
        f"/diff/acme-config-dev/43/{SHA}/raw", str(tmp_path), True)
    assert code == 200
    assert payload == body.encode("utf-8")


# --- 9. the local cache prunes by bytes, not only by count ----------------

def test_prune_enforces_a_byte_budget(tmp_path):
    """DIFF_UI_DIR is a 1Gi emptyDir and the kubelet EVICTS the pod past the
    limit. A count-only prune (500) is measured in the wrong unit: 500 of
    the observed 26.7MB worst case is 13GB. Oldest goes first; GCS remains
    the durable copy, so a local prune costs one re-download.

    Bodies are near-incompressible so zstd (COPS-2631 stage 4) does not
    collapse five files under the budget the way a repeated `"x"` would.
    Assert the budget invariant and FIFO order, not a magic keep-count
    (compressed size still varies a little with the compressor).
    """
    max_bytes = 35_000
    rnd = __import__("random").Random(0)
    for i in range(5):
        body = "".join(chr(rnd.randint(32, 126)) for _ in range(10_000))
        ui.save_artifact(str(tmp_path), "acme-config-dev", 100 + i, SHA,
                         body, max_artifacts=500,
                         max_bytes=max_bytes)
    kept = sorted(os.listdir(str(tmp_path)))
    total = sum(os.path.getsize(tmp_path / n) for n in kept)
    assert total <= max_bytes, f"byte budget must hold, kept={kept} total={total}"
    assert len(kept) < 5, f"at least one artifact must be pruned, kept: {kept}"
    assert any(n.startswith("acme-config-dev__104.") for n in kept), \
        "newest must survive"
    assert not any(n.startswith("acme-config-dev__100.") for n in kept), \
        "oldest must go first"
