"""COPS-2625 (Phase H of COPS-2607): the full-diff page must RENDER its
prose instead of printing the markup.

Phases C and D made the page complete and navigable. Both work. What is
left is that the page shows its own prose unrendered. Measured on the live
2.35.0 page for acme-config-prod #4006 (artifact 296ae9064e90): of 440
rows, 94 prose rows carried literal markup -- 4 heading prefixes, 37
`**bold**`, 63 inline backticks and 12 `---` rules. The surface that
exists to be read is the harder of the two to read.

The contract these tests pin comes from the ticket:

  1. Transform prose ONLY outside fences. Inside a fence, alignment and the
     leading +/- carry meaning, so nothing there may change.
  2. Escape first, transform the ALREADY-ESCAPED string, closed whitelist.
  3. Defensive by construction: a line a transform cannot handle
     confidently renders exactly as it does today. An unrenderable line is
     a cosmetic miss; a swallowed line is lost information.
  4. One row per source line for everything except tables.

_render_body_rows is the XSS sink of this service: the body is chosen in
part by whoever opens the pull request. Every test below that feeds it
attacker-shaped input asserts on the RAW row html and never on a stripped
or normalised copy of it. A test that strips tags before asserting deletes
its own evidence.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_ui  # noqa: E402


_CODE_CELL = re.compile(r'<td class="code">(.*)</td></tr>$', re.S)


def cells(rows):
    """The rendered content of every code cell, in document order."""
    out = []
    for r in rows:
        m = _CODE_CELL.search(r)
        assert m, "row does not end in a code cell: %r" % r
        out.append(m.group(1))
    return out


def classes(rows):
    return [re.search(r'<tr class="([^"]*)"', r).group(1) for r in rows]


# -- the whitelist, one transform per test ---------------------------------

def test_heading_hashes_are_removed_and_the_level_is_styled():
    """Today the mdh class adds weight but the hashes stay on screen, so
    every section title on the page reads '## Merge summary'."""
    rows = diff_ui._render_body_rows("## Merge summary\n")
    assert "mdh2" in classes(rows)[0]
    assert cells(rows)[0] == "Merge summary"


def test_every_heading_level_one_to_six_is_recognised():
    body = "\n".join("%s h%d" % ("#" * n, n) for n in range(1, 7))
    rows = diff_ui._render_body_rows(body)
    assert cells(rows) == ["h1", "h2", "h3", "h4", "h5", "h6"]
    assert ["mdh%d" % n in c for n, c in enumerate(classes(rows), 1)] == [True] * 6


def test_seven_hashes_is_not_a_heading_and_renders_untouched():
    """Rule 3: what the whitelist does not recognise stays exactly as it is
    today rather than being guessed at."""
    rows = diff_ui._render_body_rows("####### seven\n")
    assert cells(rows)[0] == "####### seven"


def test_bold_becomes_strong_and_no_asterisks_remain():
    rows = diff_ui._render_body_rows("**Routine** nothing dangerous\n")
    assert cells(rows)[0] == "<strong>Routine</strong> nothing dangerous"


def test_inline_code_becomes_a_code_element():
    rows = diff_ui._render_body_rows("instance `pv-hsbc-svc-a` is fine\n")
    assert cells(rows)[0] == "instance <code>pv-hsbc-svc-a</code> is fine"


def test_markup_inside_backticks_stays_literal():
    """Inline code is opaque. A resource path that happens to contain
    asterisks or brackets must not be re-interpreted after it is fenced."""
    rows = diff_ui._render_body_rows("see `a **b** [c](d)` here\n")
    assert cells(rows)[0] == "see <code>a **b** [c](d)</code> here"


def test_horizontal_rule_becomes_a_rule_row():
    rows = diff_ui._render_body_rows("---\n")
    assert "mdrule" in classes(rows)[0]
    assert "---" not in cells(rows)[0]


def test_list_marker_is_rendered_not_printed():
    rows = diff_ui._render_body_rows("- VM infrastructure changed\n")
    assert "mdli" in classes(rows)[0]
    assert cells(rows)[0] == "&bull; VM infrastructure changed"


def test_a_nested_list_keeps_its_indent():
    """The code cell is white-space: pre, so indentation is the only thing
    carrying nesting. Replacing the marker must not eat it."""
    rows = diff_ui._render_body_rows("  - nested item\n")
    assert cells(rows)[0] == "  &bull; nested item"


def test_http_and_https_links_are_rendered():
    rows = diff_ui._render_body_rows("[PR #4006](https://bitbucket.org/x/4006)\n")
    assert cells(rows)[0] == \
        '<a href="https://bitbucket.org/x/4006" rel="noopener noreferrer">' \
        'PR #4006</a>'


def test_pipe_table_becomes_a_real_table():
    body = "| Env | Result |\n|---|---|\n| pv-hsbc-c | OK |"
    rows = diff_ui._render_body_rows(body)
    assert len(rows) == 1, "a table collapses to one row, by design"
    cell = cells(rows)[0]
    assert '<table class="mdt">' in cell
    assert "<th>Env</th>" in cell and "<td>OK</td>" in cell
    assert "|---|" not in cell


def test_a_pipe_line_without_a_separator_row_is_not_a_table():
    """Rule 3 again: a values line that merely contains pipes is not a
    table, and guessing would swallow it into one."""
    body = "command: a | b | c"
    rows = diff_ui._render_body_rows(body)
    assert len(rows) == 1
    assert cells(rows)[0] == "command: a | b | c"


# -- fences are untouched --------------------------------------------------

def test_inside_a_diff_fence_prose_markup_stays_literal():
    body = "```diff\n@@ -1,2 +1,2 @@\n-  name: **old**\n+  name: `new`\n```"
    rows = diff_ui._render_body_rows(body)
    assert classes(rows) == ["row fence", "row hunk", "row del", "row add",
                             "row fence"]
    assert cells(rows)[2] == "-  name: **old**"
    assert cells(rows)[3] == "+  name: `new`"


def test_a_fence_lookalike_inside_a_code_fence_still_closes_only_once():
    body = "```\n``` not a real marker\n```\n**after**\n"
    rows = diff_ui._render_body_rows(body)
    assert classes(rows)[:3] == ["row fence", "row ctx", "row fence"]
    assert cells(rows)[1] == "``` not a real marker"
    assert cells(rows)[3] == "<strong>after</strong>"


def test_diff_row_counts_and_classes_are_unchanged_by_this_ticket():
    """Acceptance: for the 4006 shape the diff rows keep their count and
    class. Only prose rows may move."""
    body = ("## title\n```diff\n@@ -1,1 +1,1 @@\n-a\n+b\n c\n```\n"
            "**prose**\n")
    got = [c for c in classes(diff_ui._render_body_rows(body))]
    assert got.count("row add") == 1
    assert got.count("row del") == 1
    assert got.count("row hunk") == 1
    assert got.count("row fence") == 2
    assert got.count("row ctx") == 1


# -- security: the body is chosen by whoever opens the PR -------------------

def test_a_script_tag_in_a_values_line_stays_inert():
    rows = diff_ui._render_body_rows("value: <script>alert(1)</script>\n")
    raw = rows[0]
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in raw
    assert "<script>" not in raw


def test_an_img_onerror_payload_stays_inert():
    rows = diff_ui._render_body_rows("**x** <img src=x onerror=alert(1)>\n")
    raw = rows[0]
    assert "&lt;img src=x onerror=alert(1)&gt;" in raw
    assert "<img" not in raw


def test_javascript_and_data_links_are_never_rendered_as_links():
    for payload in ("[click](javascript:alert(1))",
                    "[click](data:text/html,<script>alert(1)</script>)",
                    "[click](JaVaScRiPt:alert(1))",
                    "[click](vbscript:msgbox)"):
        rows = diff_ui._render_body_rows(payload + "\n")
        raw = rows[0]
        assert "<a " not in raw, "rendered a link for %r" % payload
        assert "href" not in raw, "leaked an href for %r" % payload
        assert "<script>" not in raw


def test_an_unbalanced_bold_marker_does_not_leak_a_tag():
    """Markup opened on one line and never closed must not emit an
    unclosed <strong> that swallows the rest of the document."""
    rows = diff_ui._render_body_rows("**opened but never closed\nnext line\n")
    assert "<strong>" not in rows[0]
    assert cells(rows)[0] == "**opened but never closed"
    assert cells(rows)[1] == "next line"


def test_an_unbalanced_backtick_does_not_leak_a_code_element():
    rows = diff_ui._render_body_rows("`opened but never closed\n")
    assert "<code>" not in rows[0]
    assert cells(rows)[0] == "`opened but never closed"


def test_a_link_label_cannot_smuggle_markup_into_the_anchor():
    rows = diff_ui._render_body_rows(
        '[<img src=x onerror=alert(1)>](https://ok.example)\n')
    raw = rows[0]
    assert "<img" not in raw
    assert "&lt;img src=x onerror=alert(1)&gt;" in raw


def test_a_link_url_cannot_break_out_of_the_href_attribute():
    rows = diff_ui._render_body_rows(
        '[x](https://ok.example/" onmouseover="alert(1))\n')
    raw = rows[0]
    assert 'onmouseover="alert(1)"' not in raw
    assert "&quot;" in raw or "<a " not in raw


# -- invariants the umbrella already relies on ------------------------------

def test_one_row_per_source_line_outside_tables():
    """MAX_VISIBLE_LINES and the 'show full output' overflow are counted in
    rows, so rows and source lines must stay aligned."""
    body = ("## h\n\n- a\n**b**\n`c`\n---\n[d](https://e.example)\n"
            "```diff\n+x\n```\nplain\n")
    lines = body.split("\n")
    assert len(diff_ui._render_body_rows(body)) == len(lines)


def test_the_outline_anchor_still_lands_on_its_own_row():
    """Phase D sends every index click at a prose row. The match runs
    against the RAW line, so rendering that line must not move the id."""
    body = "**`pv-hsbc-c`**\nbody\n"
    outline = [{"name": "pv-hsbc-c", "id": "app-pv-hsbc-c", "resources": []}]
    rows = diff_ui._render_body_rows(body, outline=outline)
    assert 'id="app-pv-hsbc-c"' in rows[0]
    assert cells(rows)[0] == "<strong><code>pv-hsbc-c</code></strong>"


def test_rendering_the_page_never_mutates_the_stored_body(tmp_path):
    """/raw serves the stored body and must stay byte-exact."""
    body = "## h\n**b**\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    diff_ui.save_artifact(str(tmp_path), "acme-config-prod", 4006, "ab12cd3",
                          body, pr_url="https://bb/pr/4006")
    art = diff_ui.load_artifact(str(tmp_path), "acme-config-prod", 4006,
                                "ab12cd3")
    diff_ui.render_html(art)
    after = diff_ui.load_artifact(str(tmp_path), "acme-config-prod", 4006,
                                  "ab12cd3")
    assert after["body"] == body


def test_the_page_drops_the_body_header_the_chrome_already_states():
    """Scope item 6: the chrome already carries repo, PR, commit and base,
    and the body then repeats them. Page only -- the comment keeps it."""
    body = ("## \U0001f52d ACME Diff Preview\n\n"
            "**Commit** `296ae906` \u2192 `main` | `acme-config-prod`\n\n"
            "### \U0001f9ed Merge summary\n")
    kept = cells(diff_ui._render_body_rows(body, drop_header=True))
    assert not any("ACME Diff Preview" in c for c in kept)
    assert not any("296ae906" in c for c in kept)
    assert any("Merge summary" in c for c in kept)
    # default is off, so nothing else that calls this function changes
    all_rows = cells(diff_ui._render_body_rows(body))
    assert any("ACME Diff Preview" in c for c in all_rows)


def test_the_header_drop_only_fires_on_the_real_header():
    """A body that does not start with the generated header keeps every
    line, so an unexpected shape can never lose its first rows."""
    body = "### \U0001f9ed Merge summary\n**Commit** `abc` | `repo`\n"
    kept = cells(diff_ui._render_body_rows(body, drop_header=True))
    assert len(kept) == len(body.split("\n"))
