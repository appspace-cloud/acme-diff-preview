"""COPS-2636: calmer VM header, promoted Merge summary, overview table on
small changesets.

Operator feedback on the 2.49.0 output, reviewed on acme-config-dev #7066
(one env provisioning a new svc VM):

1. The VM danger header carried two sirens around the title. The 🚨 marks
   on the dangerous bullets are where the signal belongs; the wrapping
   pair added alarm without information.
2. The Merge summary — the single most important block in the comment —
   rendered one heading level SMALLER than the VM panel.
3. Small changesets still painted the pre-COPS-2635 per-app shape
   ("⚠️ app — N resource(s) changed" + "Full hunks for app"), because the
   Changeset overview table only rendered in large mode. The table now
   renders whenever there is at least one table-worthy app, and the
   redundant blocks drop under the same conditions as large mode.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_preview as dp  # noqa: E402

URL = "https://argocd.appspace.com/diff/acme-config-dev/7066/71c5af1e6041"


def _changed(name="x", n=3):
    hdrs = ["/apps/Deployment d%d" % i for i in range(n)]
    secs = [(h, "  image: acme/%s:1" % name) for h in hdrs]
    return dp.DiffResult("\n".join("--- %s" % h for h in hdrs), secs,
                         n, True, None, dp.OUT_DIFF, None)


def _unchanged():
    return dp.DiffResult("", [], 0, False, None, dp.OUT_NO_DIFF, None)


def _small(changed=1, unchanged=5):
    r = {"pv-t%02d-a-ss" % i: _changed("t%02d" % i, n=3 + i)
         for i in range(changed)}
    r.update({"pv-u%02d-a-ss" % i: _unchanged() for i in range(unchanged)})
    return r


def _comment(results, **kw):
    return dp.format_comment("a" * 40, results, base_sha="b" * 40, **kw)


# -- 1. the sirens ----------------------------------------------------------

def test_vm_danger_header_has_no_sirens():
    assert "\U0001f6a8" not in dp._VM_PANEL_DANGER_HDR
    assert dp._VM_PANEL_DANGER_HDR.startswith("## ")
    assert "VM INFRASTRUCTURE CHANGES" in dp._VM_PANEL_DANGER_HDR


def test_dangerous_bullets_keep_their_mark():
    """Removing the sirens from the TITLE must not touch the 🚨 on the
    bullets themselves — that is where the signal lives."""
    panel = dp._vm_panel_lines(
        [], set(), [], ["- \U0001f6a8 `pv-a` \u00b7 **linux VM (KCC) \u00b7 "
                        "svc**: `zone`: `a` \u2192 `b` \u2014 zone is "
                        "immutable"])
    txt = "\n".join(panel)
    assert txt.count("\U0001f6a8") == 1
    assert txt.splitlines()[0] == dp._VM_PANEL_DANGER_HDR


# -- 2. the merge summary header --------------------------------------------

def test_merge_summary_is_h2_with_info_icon(monkeypatch):
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    out = _comment(_small())
    assert "## \u2139\ufe0f Merge summary" in out
    assert "### \U0001f9ed Merge summary" not in out


# -- 3. the table on small changesets ----------------------------------------

def test_small_pr_renders_the_table_with_linked_cells(monkeypatch):
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    out = _comment(_small(), artifact_url=URL)
    assert "#### Changeset overview" in out
    assert "| [pv-t00-a-ss](%s#app-pv-t00-a-ss) |" % URL in out


def test_small_pr_drops_the_redundant_blocks(monkeypatch):
    """The #7066 shape: '⚠️ app — 3 resource(s) changed' + 'Full hunks
    for app' restated what the table row now says."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    out = _comment(_small(), artifact_url=URL)
    assert "[Full hunks for" not in out
    assert "resource(s) changed" not in out


def test_all_unchanged_pr_renders_no_table(monkeypatch):
    """A grid whose only row would be 'no changes' is scroll without
    information."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    out = _comment(_small(changed=0, unchanged=4), artifact_url=URL)
    assert "#### Changeset overview" not in out


def test_without_artifact_url_blocks_stay(monkeypatch):
    """No page means no anchors: the table renders with plain cells and
    the per-app blocks stay, because they are the only pointer left."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    out = _comment(_small())
    assert "| `pv-t00-a-ss` |" in out
    assert "resource(s) changed" in out


def test_risky_app_keeps_its_block_in_small_mode(monkeypatch):
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    results = _small()
    results["pv-risky-a-ss"] = _changed("risky", n=4)._replace(
        deleted_resources=["/apps/Deployment d0"])
    out = _comment(results, artifact_url=URL)
    tail = out.split("Changeset overview")[1]
    assert "`pv-risky-a-ss`" in tail
    assert "deleted" in out.lower()


def test_the_page_profile_keeps_its_blocks(monkeypatch):
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    out = _comment(_small(), artifact_url=URL,
                   profile=dp.RenderProfile("page", is_complete_record=True,
                                            inline_diffs=True))
    assert "resource(s) changed" in out
