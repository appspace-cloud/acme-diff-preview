"""COPS-2766 block 4: the docs say what the YAML hygiene checks do.

The guards table names the three new checks, and only the legacy Helm one has
a trailer, the one the parser reads. The wipe guard section is now the YAML
slip section, and its old anchor stays, so old links still work.
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import comment_render as cr  # noqa: E402
import identity  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = ("README.md", "docs/reference.md", "docs/internals.md",
        "docs/microservices-definitions-guard.md")
TRAILER = cr.GATES["legacy_helm"][1]
SECTIONS = ("### Why a YAML slip is blocked",
            "### Why a key in a file the ApplicationSet does not read is blocked",
            "### Why turning the legacy Helm writer back on is blocked")
ROWS = ("**YAML slip**", "**Key the ApplicationSet does not read**",
        "**Legacy Helm writer back on**")


def _read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


def _guards():
    """The rows of the merge-blocking guards table, by their first cell."""
    part = _read("docs/reference.md").split("## Merge-blocking guards", 1)[1]
    return {r.split("|")[1].strip(): r for r in part.split("\n\n**", 1)[0].splitlines()
            if r.startswith("|")}


def test_the_guards_table_names_the_new_checks():
    rows = _guards()
    assert not [r for r in rows if "microservices.definitions" in r], "it is a YAML slip now"
    assert set(ROWS) <= set(rows)
    assert "Confirm-" not in rows[ROWS[0]] + rows[ROWS[1]], "no line lifts these two"
    assert f"`{TRAILER}: <env>`" in rows[ROWS[2]]


def test_the_docs_name_only_trailers_the_parser_reads():
    named = {n for d in DOCS for n in re.findall(r"\bConfirm-[A-Za-z]+(?:-[A-Za-z]+)*", _read(d))}
    assert TRAILER in named and named <= {g[1] for g in cr.GATES.values() if g[1]}, named
    assert identity._confirmations([f"{TRAILER}: pv-x-a"]) == {f"{TRAILER.lower()}: pv-x-a"}
    for doc in ("docs/reference.md", "docs/internals.md"):
        assert f"`{TRAILER}: " in _read(doc), doc


def test_every_internals_link_resolves_and_the_old_anchor_stays():
    text = _read("docs/internals.md")
    anchors = {re.sub(r"[^\w\- ]", "", h.lstrip("#").strip().lower()).replace(" ", "-")
               for h in text.splitlines() if h.startswith("#")}
    assert "why-an-empty-microservicesdefinitions-is-blocked" not in anchors
    anchors |= set(re.findall(r'<a id="([\w-]+)"></a>', text))
    links = {a for d in DOCS for a in re.findall(r"internals\.md#([\w-]+)", _read(d))}
    links |= set(re.findall(r"\]\(#([\w-]+)\)", text))
    assert "why-an-empty-microservicesdefinitions-is-blocked" in anchors
    assert {"why-a-yaml-slip-is-blocked",
            "why-a-key-in-a-file-the-applicationset-does-not-read-is-blocked",
            "why-turning-the-legacy-helm-writer-back-on-is-blocked"} <= links
    assert links <= anchors, links - anchors


@pytest.mark.parametrize("heading", SECTIONS)
def test_the_new_sections_have_no_long_dash(heading):
    part = _read("docs/internals.md").split(heading + "\n", 1)[1].split("\n### ", 1)[0]
    assert "—" not in part and "–" not in part


def test_the_new_rows_have_no_long_dash():
    ref = _read("docs/reference.md")
    rows = [_guards()[r] for r in ROWS]
    rows += [r for r in ref.splitlines() if r.startswith("| \U0001f4a4 ")]
    assert len(rows) == 4
    assert not [r for r in rows if "—" in r or "–" in r]
