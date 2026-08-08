"""One page link, and per-app pointers that actually point at the app.

COPS-2622, raised by Marcos on reading a phase E comment. Counted on the
regenerated goldens before this change:

| golden                    | apps | copies of the same URL |
|---------------------------|------|------------------------|
| platform_bump_single_env  |  1   |  3                     |
| merge_summary_mixed       |  2   |  4                     |
| readable_budget_collapse  |  6   |  8                     |

A 40-app fleet bump would carry ~42 copies. On a comment phase E had just
shrunk to a decision summary, that was most of what remained.

Two causes, each reasonable alone:

- COPS-2609 rendered the fixed pointer TWICE, because "on a long comment
  the header has scrolled away". Phase E is the change that made the
  comment short, so that reason expired.
- COPS-2612 gave every app block its own pointer, correctly, but pointed
  each at the bare page URL instead of at that app.

Phase D already emits a stable per-application anchor. The fix is to use
it, which turns N identical links into N useful ones.

The trap, documented in the ticket and defended below: `diff_ui` owns the
anchor shape. If the comment grows its own copy of that logic the two
drift and every deep link 404s SILENTLY, which is worse than the
repetition it replaces.
"""
import os
import re
import sys

import pytest

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m
import diff_ui as ui

SHA = "a" * 40
URL = "https://argocd.appspace.com/diff/acme-config-dev/1/abc123"
BT = chr(96)


def _app(n=2):
    secs = [("/apps/Deployment web", "+ a: 1\n"), ("/Service web", "+ b: 2\n")]
    return m.DiffResult("+ a: 1\n", secs[:n], n, True, None, m.OUT_DIFF, "diff")


def _comment(names, **kw):
    kw.setdefault("artifact_url", URL)
    return m.format_comment(SHA, {n: _app() for n in names}, **kw)


# --- 1. one page-level link, not two --------------------------------------

def test_the_fixed_pointer_renders_once():
    out = _comment(["pv-a-a-ms"])
    assert out.count("Full rendered diff (every hunk)") == 1


def test_the_surviving_pointer_stays_above_the_status_footer():
    """_truncate_comment finds the footer with rfind('\\n---\\n**Status:**').
    Anything inserted between them loses the [clean]/[base:] tokens the
    poll loop parses for SHA dedup."""
    out = _comment(["pv-a-a-ms"])
    i_link = out.rfind("Full rendered diff (every hunk)")
    i_foot = out.rfind("\n---\n**Status:**")
    assert i_link != -1 and i_foot != -1
    assert i_link < i_foot, "the pointer must sit above the footer sequence"
    assert "[clean]" in out


# --- 2. per-app pointers are deep links -----------------------------------

def test_each_app_pointer_targets_that_app():
    names = ["pv-alpha-a-ms", "pv-beta-b-ss", "pv-gamma-c-glb"]
    out = _comment(names)
    for n in names:
        anchor = ui.app_anchor(n)
        assert f"{URL}#{anchor}" in out, f"no deep link for {n}"


def test_no_bare_url_repeats_once_apps_are_deep_linked():
    names = ["pv-alpha-a-ms", "pv-beta-b-ss", "pv-gamma-c-glb"]
    out = _comment(names)
    bare = len(re.findall(re.escape(URL) + r"(?![#\w])", out))
    assert bare == 1, f"expected one bare page link, found {bare}"


# --- 3. the anchor shape has exactly one owner ----------------------------

def test_the_comment_and_the_page_agree_on_every_anchor():
    """The whole point. If these two ever disagree the links 404 in
    silence, which is why the shape lives in diff_ui and the comment asks
    for it rather than rebuilding it."""
    names = ["pv-alpha-a-ms", "pv-beta-b-ss", "pv-gamma-c-glb"]
    page = m.format_comment(SHA, {n: _app() for n in names},
                            artifact_url=URL, profile=m.FULL_PROFILE)
    outline = ui.build_outline(page)
    assert [a["id"] for a in outline] == [ui.app_anchor(n) for n in names]


def test_real_fleet_names_never_collide():
    """Anchors are order-independent by construction, so they must be
    collision-free for real application names. These are the shapes the
    fleet actually uses."""
    names = ["pv-alpha-a-ms", "pv-alpha-a-ss", "pv-alpha-a-glb",
             "pv-alpha-b-ms", "cl-prod-b-ms", "pv-ford--aec1-a-ms",
             "pv-uksha-b-ms", "pv-ukhsa-b-ms"]
    anchors = [ui.app_anchor(n) for n in names]
    assert len(set(anchors)) == len(names)


def test_a_hostile_app_name_still_yields_a_safe_anchor():
    a = ui.app_anchor('pv-evil"><script>alert(1)</script>-ms')
    assert re.fullmatch(r"[a-z0-9-]+", a)


# --- 4. nothing that phase E guaranteed may regress -----------------------

def test_without_a_page_there_are_no_pointers_and_the_yaml_stays():
    out = _comment(["pv-a-a-ms"], artifact_url="")
    assert "could not be produced" in out
    assert (BT * 3 + "diff") in out
    assert "#app-" not in out, "no anchors when there is no page to anchor to"


def test_every_app_is_still_named_with_its_real_count():
    names = ["pv-alpha-a-ms", "pv-beta-b-ss"]
    out = _comment(names)
    for n in names:
        assert n in out
    assert out.count("resource(s) changed") == len(names)

