"""The destructive panels must not contradict themselves (COPS-2668).

Found while preparing goldens for the five BLOCK-severity comment shapes. A
golden freezes whatever the renderer produces, defects included, so each panel
had to be read before being pinned. Four of them were lying, in the same class
the rest of this ticket has been closing: a confident statement about a
destructive change that the comment's own body contradicts.

1. The merge-summary verdict ALWAYS claimed the data purge was armed.
   `_build_merge_summary` decided it with `"PURGE" in txt.upper()` over the
   joined panel text — and EVERY variant contains the literal
   `decommissionPurgeData`, including the prose that exists specifically to
   say the purge is NOT armed. Uppercased, that contains "PURGE". So the
   verdict read "data purge is ARMED: buckets/datasets are destroyed" two
   lines above a panel reading "✅ Data is not purged". Broken since the
   matcher was written.

   Fixed the way COPS-2660 fixed its equivalent: one constant, owned by the
   renderer, written by the producer and matched by the consumer, so the two
   halves cannot drift apart again.

2. The COPS-2660 VM-strip warning was swallowed on two branches. It is
   computed for every branch, but `elif was_armed and not is_armed` (disarm)
   and `elif is_armed and was_purge and not is_purge` (purge removed) never
   appended it — and by matching, they made the `elif _vm_broken` fallback
   unreachable. A PR that disarms the cascade AND strips the VM block still
   makes helm stop rendering the VM CRs while `allowDeletion: true` survives.
   That is the exact scenario COPS-2660 shipped to catch, silently reopened
   on two paths.

3. "(resource preview unavailable)" printed underneath a complete inventory.
   The `else` binds to `if cascade and retained_counts`, not to the
   `if any_rendered and total` that decides whether a preview exists — so any
   decommission with a rendered inventory and nothing retained (every orphan
   case) claimed its own preview was missing.

4. The PURGE ARMED phase table said THIS PR arms the cascade, contradicting
   both the branch's precondition (the cascade was already armed at base) and
   its own inline comment saying "Phase 2 reads done".
"""
import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import comment_render
import diff_preview as m


# ── 1. the purge verdict must follow the panel, not a substring ──────────

PURGE_ARMED_PANEL = [
    "# 🗑️⚠️ ENVIRONMENT DECOMMISSION ⚠️🗑️",
    "",
    "🚨 **DATA WILL BE PERMANENTLY DESTROYED.** This environment also has "
    "`appspace.decommissionPurgeData: true`, so Config Connector empties and "
    "deletes the BigQuery dataset and the user content bucket.",
]
NO_PURGE_PANEL = [
    "# 🗑️⚠️ ENVIRONMENT DECOMMISSION ⚠️🗑️",
    "",
    "✅ **Data is not purged.** The BigQuery dataset and the content bucket "
    "are abandoned rather than deleted, so they survive in GCP and stay "
    "recoverable. Destroying them needs `appspace.decommissionPurgeData: "
    "true` as a separate, reviewed change (COPS-2572).",
]


def _summary(decommission_lines):
    return "\n".join(comment_render._build_merge_summary(
        {}, {}, None, decommission_lines, None, None, False))


def test_no_purge_panel_does_not_claim_a_purge():
    """The bug: the word decommissionPurgeData in the DENIAL matched "PURGE"."""
    out = _summary(NO_PURGE_PANEL)
    assert "data purge is ARMED" not in out, (
        "the verdict claimed a purge while the panel below it says the data "
        "is NOT purged:\n" + out)
    assert "abandoned" in out or "not purged" in out


ORPHAN_PANEL = [
    "# 🗑️⚠️ ENVIRONMENT DECOMMISSION ⚠️🗑️",
    "",
    "⚠️ " + comment_render._DECOM_ORPHAN_HDR + " — they keep running.** This "
    "environment has not opted into cascade deletion, and the ApplicationSet "
    "sets `preserveResourcesOnDeletion: true`.",
]


def test_orphan_panel_verdict_does_not_claim_deletion():
    """Found by reading the rendered orphan comment while preparing its
    golden. There are THREE states, and the summary knew two: an environment
    with no cascade armed was announced as "resources are deleted" directly
    above a panel saying they are NOT deleted and keep running."""
    out = _summary(ORPHAN_PANEL)
    assert "resources are deleted" not in out, (
        "no cascade is armed here — the workloads survive:\n" + out)
    assert "orphaned" in out or "keep running" in out


def test_orphan_is_still_a_block():
    """Leaving a fleet of unmanaged workloads behind is a different outcome,
    not a safer one."""
    out = _summary(ORPHAN_PANEL)
    assert "DO NOT MERGE" in out


def test_purge_armed_panel_still_reports_the_purge():
    """The genuine case must keep shouting — this is the whole point."""
    out = _summary(PURGE_ARMED_PANEL)
    assert "data purge is ARMED" in out, (
        "a real purge must be named in the verdict:\n" + out)


def test_producer_and_consumer_share_one_constant():
    """Two halves matching on prose drift apart; that is how this broke."""
    assert hasattr(comment_render, "_DECOM_PURGE_HDR"), (
        "the purge marker must be a shared constant, not a substring guess")
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "src", "diff_preview.py")).read()
    assert "_DECOM_PURGE_HDR" in src, (
        "the panel that WRITES the purge prose must use the same constant "
        "the summary reads")


def _code_only(path):
    """Source with comments and docstrings stripped.

    These guards look for CODE, and this file's own comments quote the very
    patterns they forbid. Scanning raw text would make the explanation of a
    bug indistinguishable from the bug.
    """
    import io
    import tokenize
    with open(path) as f:
        src = f.read()
    out, prev_end = [], (1, 0)
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return " ".join(out)


def test_purge_detection_is_not_a_bare_substring():
    """Scoped to the purge decision on purpose.

    Two other `txt.upper()` scans live in this file, over
    appspace_state_lines rather than decommission_lines, and they are
    order-guarded (DISARMED is tested before ARMED, which is a substring of
    it). Those are a different question; forbidding the method outright would
    be a guard nobody could satisfy.
    """
    import ast
    path = os.path.join(os.path.dirname(__file__), "..",
                        "src", "comment_render.py")
    tree = ast.parse(open(path).read())

    purge_assigns = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(getattr(t, "id", "") == "purge" for t in n.targets)
    ]
    assert purge_assigns, "the purge decision must exist"
    for a in purge_assigns:
        dumped = ast.dump(a.value)
        assert "upper" not in dumped, (
            "the purge verdict must not be a case-folded substring over the "
            "whole panel — the denial contains the same word as the warning")
        assert "_DECOM_PURGE_HDR" in dumped, (
            "it must match the constant the panel writes: %s" % dumped[:160])


# ── 2. the VM-strip warning must survive every branch ────────────────────

def test_strip_warning_is_appended_on_every_branch_that_computes_it():
    """COPS-2660's warning was dropped on the disarm and purge-removed
    branches, which also made the standalone fallback unreachable."""
    import ast
    import inspect
    src = inspect.getsource(m._appspace_state_lines) \
        if hasattr(m, "_appspace_state_lines") else None
    if src is None:
        # The panel is built inline; read the region around the elif chain.
        whole = open(os.path.join(os.path.dirname(__file__), "..",
                                  "src", "diff_preview.py")).read()
        start = whole.find("elif was_armed and not is_armed:")
        end = whole.find("elif _vm_broken:", start)
        src = whole[start:end]
    assert "_strip_warning" in src, (
        "the disarm branch must carry the VM-strip warning: stripping the VM "
        "block while allowDeletion survives is dangerous whether or not the "
        "cascade is being backed out")


def test_purge_removed_branch_carries_the_strip_warning():
    whole = open(os.path.join(os.path.dirname(__file__), "..",
                              "src", "diff_preview.py")).read()
    start = whole.find("elif is_armed and was_purge and not is_purge:")
    end = whole.find("elif _vm_broken:", start)
    assert start > 0 and end > start
    assert "_strip_warning" in whole[start:end], (
        "the purge-removed branch must carry the VM-strip warning too")


# ── 3. a rendered inventory is not an unavailable preview ────────────────

def test_unavailable_notice_binds_to_the_inventory_not_the_retained_list():
    """The else hung off `if cascade and retained_counts`, so every orphan
    decommission printed 'preview unavailable' under its own full inventory."""
    import ast
    path = os.path.join(os.path.dirname(__file__), "..", "src", "diff_preview.py")
    tree = ast.parse(open(path).read())

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        body_src = ast.dump(ast.Module(body=node.body, type_ignores=[]))
        if "resource preview unavailable" not in body_src:
            continue
        found.append(ast.dump(node.test))

    assert found, "the 'preview unavailable' notice must sit under some guard"
    for test in found:
        assert "retained_counts" not in test, (
            "the notice must be guarded by whether a preview was produced, "
            "not by whether anything was retained: %s" % test[:160])
        assert "any_rendered" in test or "total" in test, (
            "the guard must ask the inventory's own question: %s" % test[:160])


# ── 4. the purge phase table must not contradict its own precondition ────

import pytest


@pytest.mark.skip(reason=(
    "COPS-2668: deliberately NOT fixed. The cascade really was armed at base, "
    "so _PH_THIS_PR is inaccurate — but this branch passes removal_state=None, "
    "so marking Phase 2 done leaves no row marked 'this PR' at all, and "
    "locating the reader in the sequence is the table's entire purpose. "
    "Trading a small inaccuracy for a table that locates nothing needs a "
    "product call, not a unilateral one. Kept as executable documentation of "
    "the open question; test_cops2616 pins the current behaviour."))
def test_purge_armed_phase_table_reports_cascade_already_done():
    """The branch only fires when the cascade was armed at base, and its own
    comment says so — the table said THIS PR arms it."""
    import ast
    path = os.path.join(os.path.dirname(__file__), "..", "src", "diff_preview.py")
    tree = ast.parse(open(path).read())

    # The purge branch is the one whose test mentions is_purge and not
    # was_purge; find its phase-table call and read cascade_state off the AST
    # rather than off text, so comments cannot answer for the code.
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = ast.dump(node.test)
        if "is_purge" not in test or "was_purge" not in test:
            continue
        if "'not'" in test.replace('"', "'") and "was_purge" in test:
            pass
        for sub in ast.walk(ast.Module(body=node.body, type_ignores=[])):
            if (isinstance(sub, ast.Call)
                    and getattr(sub.func, "id", "") == "_decommission_phase_table"):
                for kw in sub.keywords:
                    if kw.arg == "cascade_state":
                        calls.append((test, ast.dump(kw.value)))

    purge_armed = [v for t, v in calls if "PURGE" not in t]
    assert calls, "the purge panel must render a phase table"
    assert any("_PH_DONE" in v for _, v in calls), (
        "on a branch whose precondition is that the cascade was already armed "
        "at base, Phase 2 must read done, not this-PR: %r" % [v for _, v in calls])
