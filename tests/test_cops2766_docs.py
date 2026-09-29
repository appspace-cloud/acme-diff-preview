"""COPS-2766 block 2, item 9: the docs say what the code does.

A reviewer copies the Confirm-* line from the docs into a commit message, so
every trailer the docs name must be one the parser reads and a gate uses.
The docs must not promise an override that does not exist (there is no
Confirm-Purge and no Confirm-Downgrade). Every merge gate has a row in the
guards table, with its override or 'none'. The green status lead is always
the warning sign now, so the docs must not show it red.
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import comment_render as cr  # noqa: E402
import identity  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = ("README.md", "docs/reference.md", "docs/internals.md", "tests/golden/README.md")
TRAILERS = {g[1] for g in cr.GATES.values() if g[1]}


def _read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


def _slug(heading):
    """The anchor GitHub gives a heading."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def _guards_table():
    ref = _read("docs/reference.md")
    part = ref.split("## Merge-blocking guards", 1)[1]
    return [l for l in part.split("\n\n**", 1)[0].splitlines() if l.startswith("|")]


@pytest.mark.parametrize("doc", ["docs/reference.md", "docs/internals.md"])
def test_every_trailer_is_documented(doc):
    text = _read(doc)
    assert not [t for t in TRAILERS if f"`{t}: " not in text], doc


def test_the_docs_name_only_trailers_the_parser_reads():
    named = {n for doc in DOCS
             for n in re.findall(r"\bConfirm-[A-Za-z]+(?:-[A-Za-z]+)*", _read(doc))}
    assert named == TRAILERS, named ^ TRAILERS
    for t in TRAILERS:
        assert identity._confirmations([f"{t}: pv-x-a"]) == {f"{t.lower()}: pv-x-a"}


def test_every_gate_has_a_row_in_the_guards_table():
    rows = _guards_table()
    assert rows[0].split("|")[1:-1][-1].strip() == "Override", rows[0]
    body = rows[2:]
    assert all(len(r.split("|")[1:-1]) == 4 for r in body), body
    overrides = [r.split("|")[1:-1][-1].strip() for r in body]
    named = {n for o in overrides for n in re.findall(r"`(Confirm-[A-Za-z-]+):", o)}
    assert named == TRAILERS, named ^ TRAILERS
    assert all(o.startswith(("`Confirm-", "none")) for o in overrides), overrides
    guards = " ".join(r.split("|")[1] for r in body)
    for name in ("Teardown hold", "Cascade not live",
                 "Disk shrink", "Released static IP or DNS record"):
        assert name in guards, name
    assert "paused" not in guards, "a pause is a warning, not a guard"


def test_the_green_status_lead_is_never_red_in_the_docs():
    ref = _read("docs/reference.md")
    assert "\U0001f6a8 means the comment verdict is" not in ref
    part = ref.split("**The green status leads with the top finding**", 1)[1]
    example = part.split("```", 2)[1].strip().splitlines()
    assert example and all(l.startswith("⚠️ ") for l in example), example


def test_every_internals_anchor_the_docs_link_exists():
    anchors = {_slug(l.lstrip("#")) for l in _read("docs/internals.md").splitlines()
               if l.startswith("#")}
    links = {a for doc in DOCS for a in re.findall(r"internals\.md#([\w-]+)", _read(doc))}
    assert "why-a-teardown-is-blocked" in links
    assert links <= anchors, links - anchors
