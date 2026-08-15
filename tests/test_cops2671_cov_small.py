"""The dark corners of the five small leaf modules (COPS-2671).

COPS-2658 sliced `comment_render`, `grouping`, `render_profile`,
`chart_identity` and `schema_errors` out of diff_preview.py verbatim. Their
tests stayed behind on whatever path the extraction happened to exercise,
and what was left uncovered has a shape: it is almost always the branch that
fires when the *usual* input is missing -- no page to link to, no chart
revision to name, no readable file, no room on the line.

That is the dangerous half. A pointer branch that degrades wrong does not
crash; it renders a link to nowhere. A conservative grouping guard that stops
guarding does not crash; it folds two unrelated changes into one line that
claims to describe both. Nothing here goes red in production, which is
exactly why it needs a test.

What each case pins:

`comment_render`
  * `_full_hunks_link` on both of its non-deep-link branches: the bare page
    link (no app in hand) and the no-page fallback sentence. The second is
    live production behaviour -- when the artifact save fails, format_comment
    forces the hunks back inline and every fold line still has to say where
    the complete record is.
  * `_name_list` refusing to name anything, when the very first resource
    header is longer than the whole line budget. The consequence is
    negative and easy to regress: the repeat-group note must NOT emit a
    dangling "Same change:" line with nothing after it.
  * `_routine_bump_label`'s two unexercised arms: a signature carrying no
    transition at all (the fallback phrase, instead of "chart `` -> ``"),
    and the "+N more field(s)" suffix -- which is the ordinary fleet-bump
    shape, a chart revision move whose rendered diff also moves an image
    tag.
  * the merge summary's VM verdict when EVERY dangerous bullet is a
    provision group (COPS-2635): the headline counts the environments and
    says what happens to them, instead of the generic danger flag.
  * the merge summary's routine VM finding.
  * the auto-sync RESUMED verdict. See the note above that test: the
    shipped panel cannot currently reach it, and that is a finding.

`grouping`
  * `_shape_signature` meeting a section that is not a (header, body) pair
    -- the shape a legacy/coerced result can carry. It must answer None,
    and `_group_changed_apps_by_shape` must then leave those apps alone.
    A shared placeholder here would make every unreadable app match every
    other one, which is the one outcome the module's docstring forbids.

`render_profile`
  * the per-app pointer the block emits when it owns the pointer itself
    (`row_pointer`), which is the default for every caller that has no
    Changeset overview row to hang the deep link on.
  * the two storage-cap notes nobody drove: the complete record owning its
    own shortfall, and the comment's fold-aware wording (with the fold
    active, "showing first N of M" would be false).

`chart_identity`
  * a chart tree that cannot be stat-ed -- a dangling symlink is the
    realistic way, an OCI tree pulled with a link to a file the package
    does not carry. The stat fingerprint must answer None so the memo is
    bypassed entirely, and the digest must then refuse to exist rather
    than serve the previous tree's entry.

`schema_errors`
  * the quoted-stderr overflow marker. helm can emit a hundred lines; the
    comment quotes six and must say how many it dropped, or the author
    reads a truncated error as the whole error.

Everything asserts the rendered consequence -- a comment line, a returned
verdict, a raised type -- never the source text.
"""
import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import pytest                       # noqa: E402

import chart_identity as ci         # noqa: E402
import comment_render as cr         # noqa: E402
import diff_preview as m            # noqa: E402
import grouping as g                # noqa: E402
import render_profile as rp         # noqa: E402
import version_fold as vfmod        # noqa: E402
import vm_analysis as vma           # noqa: E402
import vocabulary                   # noqa: E402


URL = "https://argocd.appspace.com/diff/acme-config-prod/4321/ffba80a2"

# The comment profile with the hunks put back inline. Phase E (COPS-2612)
# ships with COMMENT_INLINE_DIFFS=false, so a block-level test that wants to
# see YAML has to ask for it explicitly, exactly like the existing suite does.
_INLINE = rp.COMMENT_PROFILE.replace(inline_diffs=True)


@pytest.fixture(autouse=True)
def _no_vertex(monkeypatch):
    """Never call the AI summariser from a rendering test."""
    monkeypatch.setattr(m, "generate_ai_summary", lambda *a, **k: None)


def _res(sections, n_res=None, version_change=None, fingerprint=None,
         version_fold=None):
    """An OUT_DIFF DiffResult over (header, body) sections."""
    secs = list(sections)
    return m.DiffResult("\n".join(h for h, _ in secs), secs,
                        n_res if n_res is not None else len(secs),
                        True, None, m.OUT_DIFF, "changes",
                        version_change, None, None, fingerprint, None, None,
                        version_fold)


def _odd_res(sections):
    """An OUT_DIFF DiffResult whose sections are NOT (header, body) pairs.

    Its own builder because `_res` reads the headers out to make the diff
    text, which is the very thing these sections cannot answer.
    """
    return m.DiffResult("", list(sections), len(sections), True, None,
                        m.OUT_DIFF, "changes")


def _comment(results, **kw):
    return m.format_comment("a" * 40, results, base_sha="b" * 40, **kw)


def _summary_block(comment: str) -> str:
    """Only the merge-summary section.

    The panels below it quote the same facts in their own words, so a naive
    substring search over the whole comment would let a panel answer for the
    verdict -- which is the confusion COPS-2668 spent a ticket unpicking.
    """
    lines = comment.splitlines()
    start = lines.index("## ℹ️ Merge summary")
    rest = lines[start + 1:]
    end = next((i for i, l in enumerate(rest)
                if l == "---" or l.startswith("## ")), len(rest))
    return "\n".join(rest[:end])


# ══ 1 ── the merge summary's VM verdicts ═════════════════════════════════
# COPS-2635: when every dangerous bullet in the VM panel is a provision
# group, the headline speaks the operator's language ("N environments
# provision a NEW linux VM") instead of raising the generic danger flag.
# The panel is built by its own producer here rather than hand-typed: the
# summary recognises its panels by the header constants precisely so the two
# halves cannot drift, and a test that types the header itself would not
# notice if they did.

PROV_RABBIT = ("- \U0001f6a8 **3 environments provision a new linux VM "
               "· rabbit** — `machineType n2-standard-4`, new boot disk")
PROV_REDIS = ("- \U0001f6a8 **2 environments provision a new linux VM "
              "· redis** — `machineType n2-standard-2`")
RESIZE = ("- \U0001f6a8 **`pv-x-a`** · `ComputeInstance vm-1`: "
          "`machineType` `n1-standard-1` → `n1-standard-8`")


def _vm_panel(dangerous=(), routine=()):
    return vma._vm_panel_lines([], set(), list(routine), list(dangerous))


def test_an_all_provision_vm_panel_gets_the_counted_headline():
    """Two provision groups, five environments: the verdict adds them up and
    names the event, and the generic wording stands down."""
    out = _summary_block(_comment(
        {}, vm_change_lines=_vm_panel(dangerous=[PROV_RABBIT, PROV_REDIS])))
    assert "**5 environment(s) provision a NEW linux VM**" in out, out
    assert "flagged dangerous" not in out, (
        "every dangerous bullet is a provision, so the generic danger flag "
        "must not also fire:\n" + out)
    assert "DO NOT MERGE" in out, "a new machine in GCP is still a blocker"


def test_one_non_provision_danger_keeps_the_generic_wording():
    """The control that makes the test above mean something. A resize mixed
    in means 'see the VM section' must not sound like it is only about new
    machines."""
    out = _summary_block(_comment(
        {}, vm_change_lines=_vm_panel(dangerous=[PROV_RABBIT, RESIZE])))
    assert "flagged dangerous" in out, out
    assert "provision a NEW linux VM" not in out, out


def test_a_routine_vm_panel_is_reported_as_routine():
    """The routine panel carries a different header constant, and the
    summary must read it as ROUTINE -- not silently say nothing about VMs,
    and not escalate."""
    out = _summary_block(_comment({}, vm_change_lines=_vm_panel(
        routine=[("pv-x-a", "- `pv-x-a` · `ComputeInstance vm-1`: "
                            "`labels.owner` `a` → `b`")])))
    assert "VM infrastructure changed (routine)" in out, out
    assert "DO NOT MERGE" not in out
    assert "Review before merging" not in out


# ══ 2 ── the routine-bump label ══════════════════════════════════════════
# _routine_bump_label turns a rollup signature into the one line that
# describes what a whole fleet of environments is doing. Three arms; the
# suite only ever drove one.

def _bump_fleet(sections, version_change, n=3, tag="b"):
    """n same-signature apps, each its own fingerprint group.

    fingerprint=None keeps them from collapsing into ONE byte-identical
    group (which would never reach INPUT_ROLLUP_MIN_SERVICES), so the
    routine-bump rollup is what folds them -- the PR #3891 shape.
    """
    return {f"pv-{tag}{i}-a-ss": _res(sections, version_change=version_change)
            for i in range(n)}


def test_a_chart_bump_that_also_moves_a_field_says_so():
    """The ordinary fleet bump: the chart revision moves AND the rendered
    diff moves an image tag with it. The extra field must be counted, or the
    line claims the transition was the only change."""
    secs = [("/apps/Deployment api",
             "-  image: acme/api:2602.4.9\n+  image: acme/api:2603.1.2\n")]
    out = _comment(_bump_fleet(secs, ("2602.4.9", "2603.1.2")),
                   artifact_url=URL)
    assert "chart `2602.4.9` → `2603.1.2` (+1 more field(s))" in out, out
    assert "**3 environment(s) jumping**" in _summary_block(out)


def test_a_signature_with_no_transition_falls_back_to_a_true_phrase():
    """A rollup whose signature names neither a revision nor a changed field:
    every changed line was cascade checksum noise, which the signature skips,
    and the app's version_change carries nothing quotable.

    `_routine_bump_signature` still forms a signature there -- it only
    refuses when there is no version_change at all -- and the label must
    then degrade to a phrase that is still true, instead of rendering an
    empty transition like "chart `` -> ``" that reads as a rendering bug and
    tells a reviewer nothing about what the fleet is doing. Today's diff
    path always resolves both chart revisions before comparing them, so this
    is the arm that catches a caller (or a future source of version_change)
    that does not.
    """
    secs = [("/apps/Deployment api",
             "-checksum/config: 8f14e45f\n+checksum/config: c4ca4238\n")]
    results = _bump_fleet(secs, (None, ""), tag="r")
    # Precondition: this really is the empty signature, not a near miss.
    assert m._routine_bump_signature(next(iter(results.values()))) == \
        ("", "", ())
    out = _comment(results, artifact_url=URL)
    assert "version-only change" in out, out
    assert "chart `` → ``" not in out
    assert "**3 environment(s) jumping** version-only change" in \
        _summary_block(out)


# ══ 3 ── where the hunks are, when there is no deep link ═════════════════
# Every place the comment folds content away has to point somewhere. Two of
# the three answers were dark.

def test_the_bare_page_link_carries_no_anchor():
    """With no application in hand there is nothing to deep-link to, so the
    pointer is the page itself -- and must not invent an anchor, which would
    404 in silence (the failure COPS-2622 wired app_anchor to prevent)."""
    bare = cr._full_hunks_link(URL)
    assert bare == f"[Full hunks in the full diff view]({URL})"
    deep = cr._full_hunks_link(URL, app="pv-x-a-ss")
    assert deep != bare and deep.endswith(f"#{m.diff_ui.app_anchor('pv-x-a-ss')})")


def test_with_no_page_the_fold_line_still_says_where_the_hunks_are():
    """Live shape: the artifact save failed, so format_comment puts every
    hunk back inline -- and the fold line, which is a conclusion drawn ABOUT
    hunks the reader can no longer be sent to, must still name a place. A
    markdown link with an empty target here would render as broken text."""
    secs = [(f"/apps/Deployment svc-{i}",
             f"-  image: acme/svc-{i}:2602.4.9\n"
             f"+  image: acme/svc-{i}:2603.1.2\n") for i in range(4)]
    secs.append(("/apps/ConfigMap needle",
                 "-  featureFlag: \"off\"\n+  featureFlag: \"on\"\n"))
    fold = vfmod._classify_version_fold(secs, ("2602.4.9", "2603.1.2"))
    assert fold and fold["n_foldable"] == 4      # precondition
    out = _comment({"pv-x-a-ss": _res(secs, n_res=5,
                                      version_change=("2602.4.9", "2603.1.2"),
                                      version_fold=fold)},
                   artifact_url="")
    fold_line = next(l for l in out.splitlines()
                     if "changed resource(s)** are the version transition" in l)
    assert fold_line.endswith("Full hunks are in the diff-preview full-diff "
                              "view, linked from the build status."), fold_line
    assert "]()" not in out, "an empty link target reached the comment"


def test_a_block_that_owns_its_pointer_deep_links_to_the_app():
    """row_pointer is the default: a surface with no Changeset overview row
    to carry the deep link needs the block to carry it, or a comment with
    the hunks moved to the page names the app and then abandons the reader.
    """
    secs = [("/apps/Deployment api", "-  replicas: 1\n+  replicas: 2\n")]
    out = "\n".join(rp._format_app_diff_block(
        "pv-x-a-ss", secs, "", n_res=1, artifact_url=URL,
        profile=rp.COMMENT_PROFILE.replace(inline_diffs=False)))
    assert f"{URL}#{m.diff_ui.app_anchor('pv-x-a-ss')}" in out, out
    assert "```" not in out, "the summary surface must ship no fenced YAML"
    assert "1 resource(s) changed" in out, "the app and its count still stand"


def test_the_same_block_without_a_page_names_the_build_status():
    secs = [("/apps/Deployment api", "-  replicas: 1\n+  replicas: 2\n")]
    out = "\n".join(rp._format_app_diff_block(
        "pv-x-a-ss", secs, "", n_res=1, artifact_url="",
        profile=rp.COMMENT_PROFILE.replace(inline_diffs=False)))
    assert "linked from the build status" in out, out
    assert "](" not in out, "there is no URL, so there must be no link syntax"


# ══ 4 ── naming the members of a repeat group ════════════════════════════

def test_a_repeat_group_of_unnameable_resources_prints_no_empty_list():
    """`_name_list` names as many resources as fit on a readable line. When
    the FIRST header is already longer than the whole budget it can name
    none -- and the note must then stop, not emit "> Same change:" followed
    by nothing. The count sentence is the part that carries information and
    it has to survive."""
    long_hdr = ("/pv-prod-corporate-westeurope-b/ConfigMap "
                + "appspace-generated-runtime-configuration-" * 6)
    assert len(long_hdr) > 240                      # precondition
    hdrs = [f"{long_hdr}-{i}" for i in range(3)]
    secs = [(h, "-  a: 1\n+  a: 2\n") for h in hdrs]
    out = "\n".join(rp._format_app_diff_block(
        "pv-x-a-ss", secs, "", n_res=3, artifact_url=URL,
        group_repeats=True, profile=_INLINE))
    assert "2 more resource(s) change exactly the same lines" in out, out
    assert "Same change:" not in out, (
        "no name fitted, so the label must not be printed with an empty "
        "list after it:\n" + out)
    # A short header in the same position still gets named -- otherwise the
    # assertion above would pass on a renderer that never names anything.
    short = [(f"/apps/Deployment svc-{i}", "-  a: 1\n+  a: 2\n")
             for i in range(3)]
    out2 = "\n".join(rp._format_app_diff_block(
        "pv-x-a-ss", short, "", n_res=3, artifact_url=URL,
        group_repeats=True, profile=_INLINE))
    assert "Same change: `/apps/Deployment svc-1`" in out2, out2


# ══ 5 ── the auto-sync resume verdict ════════════════════════════════════

def test_a_resume_panel_is_reported_as_a_resume_not_a_pause():
    """Resuming applies whatever drift accumulated while the environment was
    frozen, so it is a REVIEW item -- but of the opposite kind to a pause,
    and a verdict that names the wrong direction is worse than none.

    NOTE for the reader, not for this assertion: the panel
    `_summarize_appspace_state_changes` actually emits for a resume also
    contains the sentence "If this environment drifted while paused", and
    the pause test above this branch is `"PAUSED" in txt.upper()` -- so
    today every real resume is announced as a pause. Same class of defect as
    COPS-2668's purge verdict (a denial matching the warning's keyword), and
    it belongs to whoever fixes that ordering, not to a coverage test that
    would have to pin the wrong behaviour to reach the line.
    """
    panel = [
        "### ▶️ Auto-sync RESUMED for `pv-qa88-a`",
        "",
        # One panel line, split for width -- parenthesised so it cannot be
        # read (by a human or by CodeQL) as a list entry missing its comma.
        ("`appspace.autosync: false` was removed from this environment's "
         "`customer.yaml`. Automated sync resumes for `pv-qa88-a-ss`."),
        "",
    ]
    out = "\n".join(cr._build_merge_summary({}, {}, None, None, panel,
                                            None, False))
    assert "**ArgoCD auto-sync resumed**" in out, out
    assert "pending drift will be applied" in out
    assert "auto-sync paused" not in out, (
        "a resume must never be announced as its opposite:\n" + out)
    assert "Review before merging" in out


# ══ 6 ── the storage-cap notes ═══════════════════════════════════════════
# total > shown means _package_sections dropped resources at STORAGE time:
# they exist on neither surface. Which sentence is honest depends on who is
# speaking.

_CAP_SECS = [("/apps/Deployment api", "-  replicas: 1\n+  replicas: 2\n")]


def test_the_complete_record_owns_its_own_shortfall():
    """The full-diff page describes itself as the complete record, so it
    must not point the reader at itself for the missing resources -- it says
    they were not retained at all."""
    out = "\n".join(rp._format_app_diff_block(
        "pv-x-a-ss", _CAP_SECS, "", n_res=9, artifact_url=URL,
        profile=rp.FULL_PROFILE))
    assert "Storage cap reached: showing 1 of 9 changed resources" in out, out
    assert "FULL_SECTIONS_MAX_PER_APP" in out, "name the knob that did it"
    assert "only in the full diff view" not in out, (
        "the page must not send the reader to the page:\n" + out)


def test_with_the_fold_active_the_note_does_not_claim_a_prefix():
    """"Showing first N of M" is false once the fold has removed the
    version-only sections from the inline list, so the fold-aware branch
    reports the storage shortfall instead."""
    fold = {"n_foldable": 1, "n_total": 9, "label": "2602.4.9 → 2603.1.2",
            "headers": ("/apps/Deployment api",), "classes": ("image tags",)}
    out = "\n".join(rp._format_app_diff_block(
        "pv-x-a-ss", _CAP_SECS, "", n_res=9, artifact_url=URL,
        version_fold=fold, profile=_INLINE))
    assert "8 more changed resource(s) beyond the storage cap are only in " \
           "the full diff view" in out, out
    assert "Showing first" not in out, (
        "the shown sections are not a prefix any more:\n" + out)
    # Without the fold the same numbers take the plain-prefix wording.
    plain = "\n".join(rp._format_app_diff_block(
        "pv-x-a-ss", _CAP_SECS, "", n_res=9, artifact_url=URL,
        profile=_INLINE))
    assert "Showing first 1 of 9 changed resources" in plain, plain


# ══ 7 ── a section that is not a (header, body) pair ═════════════════════
# _shape_signature reads sections positionally, the same way
# _format_app_diff_block does. A legacy or hand-coerced result can carry
# something else; the answer must be "never group this", never a shared
# placeholder that makes every unreadable app match every other one.

_GOOD_SECS = [("/apps/Deployment api", "-  a: 1\n+  a: 2\n"),
              ("/apps/Service api", "-  b: 1\n+  b: 2\n")]


@pytest.mark.parametrize("bad,why", [
    ([("/apps/Deployment api", "body"), ()], "IndexError: empty pair"),
    ([{"header": "/apps/Deployment api"}], "KeyError: dict-shaped section"),
    ([7], "TypeError: not subscriptable at all"),
])
def test_an_unreadable_section_shape_has_no_signature(bad, why):
    assert g._shape_signature(_odd_res(bad)) is None, why
    # And the well-formed neighbour still answers, so "None" is a verdict
    # about THIS result and not about the function.
    assert g._shape_signature(_res(_GOOD_SECS)) == \
        (2, ("/apps/Deployment api", "/apps/Service api"))


def test_unreadable_apps_are_never_grouped_with_each_other():
    """The placeholder trap: three apps whose sections are equally
    unreadable would all share one fake signature and collapse into a line
    claiming they take the same change. They must each stay on their own,
    while three genuinely same-shape apps still collapse."""
    good = [(f"pv-g{i}-a-ss", _res(_GOOD_SECS)) for i in range(3)]
    bad = [(f"pv-b{i}-a-ss", _odd_res([{"header": "/apps/Deployment api"}]))
           for i in range(3)]
    grouped = g._group_changed_apps_by_shape(good + bad)
    assert sorted(grouped) == ["pv-g0-a-ss", "pv-g1-a-ss", "pv-g2-a-ss"], \
        grouped
    assert grouped["pv-g0-a-ss"][1] == ["pv-g0-a-ss", "pv-g1-a-ss",
                                        "pv-g2-a-ss"]


# ══ 8 ── a chart tree that cannot be stat-ed ═════════════════════════════

def _chart(tmp_path, name="chart"):
    d = tmp_path / name
    d.mkdir()
    (d / "Chart.yaml").write_text("name: demo\nversion: 1.0.0\n")
    (d / "templates").mkdir()
    (d / "templates" / "d.yaml").write_text("kind: ConfigMap\n")
    return d


def test_an_unstattable_tree_has_no_identity_and_no_key(tmp_path):
    """A dangling symlink inside the pulled chart -- a link to a file the
    package does not carry. The stat fingerprint must answer None (there is
    no honest identity to memo against), and the digest must then refuse to
    exist rather than fall back on the entry the healthy tree left behind: a
    content key is a claim about bytes, and these bytes were never read.
    """
    chart = _chart(tmp_path)
    healthy = ci._chart_tree_identity(str(chart))
    assert healthy is not None
    good_digest = ci._hash_chart_tree(str(chart))
    assert ci._chart_tree_digest_memo.get(healthy) == good_digest, \
        "precondition: the healthy tree is memoized"

    (chart / "values.yaml").symlink_to(chart / "not-shipped.yaml")
    assert ci._chart_tree_identity(str(chart)) is None, (
        "a tree with a file it cannot stat has no stat fingerprint")
    with pytest.raises(ci.ChartTreeUnreadable):
        ci._hash_chart_tree(str(chart))
    with pytest.raises(ci.ChartTreeUnreadable):
        m._main_render_content_key(str(chart), "rel", "ns", {"v.yaml": "a: 1\n"})
    assert ci._chart_tree_digest_memo.get(healthy) == good_digest, (
        "the failed hash must not have disturbed the healthy entry")


# ══ 9 ── quoting a long helm stderr ══════════════════════════════════════

def test_a_long_stderr_is_capped_and_says_how_much_it_dropped():
    """helm can emit a hundred lines and the comment quotes six. Without the
    marker the author reads a truncated error as the whole error -- and the
    line that names the real cause is usually not in the first six."""
    err = "\n".join(f"line {i} of helm stderr" for i in range(1, 10))
    out = _comment({"pv-heb-a-ss": m.DiffResult(
        "", [], 0, False, err, m.OUT_INDETERMINATE,
        vocabulary.REASON_TEMPLATE)}, artifact_url=URL)
    assert "line 6 of helm stderr" in out
    assert "line 7 of helm stderr" not in out, "the cap must actually cap"
    assert "... and 3 more line(s) — full stderr in the pod logs" in out, out


def test_a_short_stderr_gets_no_overflow_marker():
    """The control: a stderr that fits is quoted whole and says nothing
    about lines it did not drop."""
    err = "\n".join(f"line {i} of helm stderr" for i in range(1, 4))
    out = _comment({"pv-heb-a-ss": m.DiffResult(
        "", [], 0, False, err, m.OUT_INDETERMINATE,
        vocabulary.REASON_TEMPLATE)}, artifact_url=URL)
    assert "line 3 of helm stderr" in out
    assert "more line(s)" not in out, out
