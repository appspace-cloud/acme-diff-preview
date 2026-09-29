"""COPS-2766 block 3, item 7: the docs say what the block 3 gates do.

A reviewer copies the override from the guards table, so each block 3 gate
has a row there: `Confirm-Clone-Sanitized: <env>` for a clone that starts
running, and 'none' for the four that no commit line lifts. The docs name no
Confirm-* line the parser does not read. The internals sections the rows link
exist, the old structural row no longer names the one new-env failure that
stays green, and the two goldens of this block have a row.
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import comment_render as cr  # noqa: E402
import decommission  # noqa: E402
import identity  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = ("README.md", "docs/reference.md", "docs/internals.md", "tests/golden/README.md")
GUARDS = {
    "clone_wake": "AEC clone starts running",
    "dup_identity": "New environment name already in use",
    "ashn_copy": "Clone ashn already in use",
    "vm_disk": "n4 or c4 machine with a pd- disk",
    "appset_miss": "No ApplicationSet reads the folder",
}
SECTIONS = ("why-waking-an-aec-clone-is-blocked", "why-a-copied-clone-ashn-is-blocked",
            "why-a-new-env-needs-an-applicationset-glob")


def _read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


def _slug(heading):
    """The anchor GitHub gives a heading."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def _guard_rows():
    part = _read("docs/reference.md").split("## Merge-blocking guards", 1)[1]
    return [l for l in part.split("\n\n**", 1)[0].splitlines() if l.startswith("|")]


def _cells(row):
    return [c.strip() for c in row.split("|")[1:-1]]


@pytest.mark.parametrize("kind", sorted(GUARDS))
def test_every_block3_gate_has_a_guards_row_with_its_override(kind):
    rows = [r for r in _guard_rows() if f"**{GUARDS[kind]}**" == _cells(r)[0]]
    assert len(rows) == 1, (kind, rows)
    cells = _cells(rows[0])
    assert len(cells) == 4, cells          # Guard, catches, why, Override
    trailer = cr.GATES[kind][1]
    if trailer:
        assert cells[3].startswith(f"`{trailer}: <env>`"), cells[3]
    else:
        assert cells[3] == "none", cells[3]


def test_the_clone_trailer_reads_as_the_docs_write_it():
    for doc in ("docs/reference.md", "docs/internals.md"):
        assert "`Confirm-Clone-Sanitized: " in _read(doc), doc
    assert identity._confirmations(["Confirm-Clone-Sanitized: pv-x--aec1-a"]) == {
        "confirm-clone-sanitized: pv-x--aec1-a"}


def test_the_docs_name_no_trailer_the_parser_does_not_read():
    named = {n for doc in DOCS
             for n in re.findall(r"\bConfirm-[A-Za-z]+(?:-[A-Za-z]+)*", _read(doc))}
    trailers = {g[1] for g in cr.GATES.values() if g[1]}
    assert "Confirm-Clone-Sanitized" in named
    assert named <= trailers, named - trailers


def test_the_internals_sections_the_docs_link_exist():
    internals = _read("docs/internals.md")
    anchors = {_slug(l.lstrip("#")) for l in internals.splitlines() if l.startswith("#")}
    ref = _read("docs/reference.md")
    for a in SECTIONS:
        assert a in anchors, a
        assert f"internals.md#{a}" in ref, a
        assert f"](#{a})" in internals, a    # the contents list


def test_the_structural_row_does_not_name_the_shape_that_stays_green():
    row = next(r for r in _guard_rows() if _cells(r)[0] == "Structural new-env failure")
    assert decommission._new_env_status("Missing required value: x")[1] is True
    assert "missing a required value" not in row
    assert "`required`" in row and _cells(row)[3] == "none", row


def test_the_block3_goldens_have_a_row():
    text = _read("tests/golden/README.md")
    for name in ("new_env_only.md", "clone_wake.md"):
        assert os.path.exists(os.path.join(REPO, "tests", "golden", name)), name
        assert f"| `{name}` |" in text, name
