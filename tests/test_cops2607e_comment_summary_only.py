"""The PR comment becomes a decision summary (COPS-2612, phase E).

The comment keeps every verdict and every name; the YAML evidence moves to
the full-diff page, which phases C and D made complete, durable and
navigable. Measured target (umbrella corpus): median comment from 9,874
bytes to under 3KB, zero fences.

Written before the implementation; the ticket's ten required cases.
"""
import os
import sys

import pytest

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m

SHA = "a2e6383e7c1d4f5a6b7c8d9e0f1a2b3c4d5e6f70"
URL = "https://argocd.appspace.com/diff/acme-config-dev/1/a2e6383e"
FENCE = chr(96) * 3


def _changed(name="pv-a-a-ms", n=2, deleted=None):
    return m.DiffResult(
        "+ a: 1\n", [("/apps/Deployment web", "+ a: 1\n"),
                     ("/Service web", "+ b: 2\n")][:n], n, True, None,
        m.OUT_DIFF, "diff", deleted_resources=deleted)


def _clean():
    return m.DiffResult("", [], 0, False, None, m.OUT_NO_DIFF, "no_diff")


def _comment(results, **kw):
    kw.setdefault("artifact_url", URL)
    return m.format_comment(SHA, results, **kw)


def _page(results, **kw):
    return m.format_comment(SHA, results, profile=m.FULL_PROFILE, **kw)


# 1 + 2: the comment carries no fences and no config panel ----------------

def test_the_comment_has_no_fenced_diff_blocks():
    out = _comment({"pv-a-a-ms": _changed()})
    assert (FENCE + "diff") not in out
    assert out.count(FENCE) == 0


PANEL = ["### \U0001f4dd Config changes in this PR", "",
         "`a.yaml`:", "- `k`: `1` -> `2`", ""]


def test_the_comment_has_no_config_changes_panel():
    out = _comment({"pv-a-a-ms": _changed()}, input_change_lines=PANEL)
    assert "Config changes in this PR" not in out


# 3: the page keeps both ---------------------------------------------------

def test_the_page_keeps_fences_and_the_config_panel():
    out = _page({"pv-a-a-ms": _changed()}, input_change_lines=PANEL)
    assert (FENCE + "diff") in out
    assert "Config changes in this PR" in out


# 4: the changed-applications index ----------------------------------------

def test_every_changed_app_is_indexed_with_its_real_count():
    out = _comment({"pv-a-a-ms": _changed(n=2),
                    "pv-b-b-ss": _changed(name="pv-b-b-ss", n=1)})
    assert "pv-a-a-ms" in out and "pv-b-b-ss" in out
    assert "2" in out and URL in out


# 5: a deletion still names the resource in the comment ---------------------

def test_a_deletion_is_still_named_in_the_comment():
    out = _comment({"pv-a-a-ms": _changed(
        deleted=["/apps/Deployment doomed-svc"])})
    assert "doomed-svc" in out


# 6: clean apps collapse to a count -----------------------------------------

def test_200_clean_apps_collapse_to_one_line_in_the_comment():
    results = {f"pv-c{i:03d}-a-ms": _clean() for i in range(200)}
    results["pv-hot-a-ms"] = _changed(name="pv-hot-a-ms")
    out = _comment(results)
    assert out.count("no manifest changes") == 0
    assert "200" in out and "unchanged" in out
    page = _page(results)
    assert page.count("no manifest changes") == 200, \
        "the page remains the complete record"


# 7: the escape hatch restores today's shape --------------------------------

def test_inline_diffs_true_restores_the_old_comment(monkeypatch):
    monkeypatch.setattr(m, "COMMENT_INLINE_DIFFS", True)
    out = _comment({"pv-a-a-ms": _changed()})
    assert (FENCE + "diff") in out


# 8: the narrow evidence hatch ----------------------------------------------

def test_evidence_lines_show_for_a_risk_app_only(monkeypatch):
    monkeypatch.setattr(m, "COMMENT_INLINE_EVIDENCE_LINES", 8)
    # the deleted resource must BE one of the sections, or there is no
    # evidence body to excerpt
    risky = {"pv-a-a-ms": m.DiffResult(
        "- gone\n", [("/apps/Deployment doomed-svc", "- gone: yes\n")], 1,
        True, None, m.OUT_DIFF, "diff",
        deleted_resources=["/apps/Deployment doomed-svc"])}
    routine = {"pv-b-b-ms": _changed(name="pv-b-b-ms")}
    assert (FENCE + "diff") in _comment(risky)
    assert (FENCE + "diff") not in _comment(routine)


# 9: no page means the comment inlines again and says so --------------------

def test_page_unavailable_forces_inline_and_says_so():
    out = _comment({"pv-a-a-ms": _changed()}, artifact_url="")
    assert "could not be produced" in out
    assert (FENCE + "diff") in out, \
        "removing YAML is only ever safe when the page exists"


# 10: the AI summary lives on the page only ---------------------------------

def test_ai_summary_is_absent_from_the_comment_and_kept_on_the_page(
        monkeypatch):
    """Decision (recorded in the ticket, as it required): page only. It is
    model output that partly restates the deterministic merge summary; in a
    comment whose whole purpose is now "the verdict, fast", the
    deterministic narrative is the one that belongs. _sanitize_ai_summary
    keeps running unchanged on the page path."""
    monkeypatch.setattr(m, "generate_ai_summary",
                        lambda _r: "AI says something changed somewhere")
    results = {"pv-a-a-ms": _changed()}
    assert "AI Analysis" not in _comment(results)
    assert "something changed somewhere" not in _comment(results)
    page = _page(results)
    assert "AI Analysis" in page
    assert "something changed somewhere" in page
