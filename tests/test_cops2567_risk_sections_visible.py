"""COPS-2567 - a named deletion must also be VISIBLE in the diff body.

Field report, acme-config-prod PR 3845 ("resizing DKV since migrations are
complete"). The comment shouted "5 RESOURCE(S) DELETED" and listed five
HorizontalPodAutoscalers, then printed ten Deployment sections and not one
of the five deletions. A reviewer told to "verify each deletion is
intentional" was given zero evidence, so the honest reading was "false
positive". The deletions were real.

Two correct behaviours combined badly:
  1. _diff_resources sorts sections by resource key, so /apps/Deployment
     always sorts before /autoscaling/HorizontalPodAutoscaler.
  2. _package_sections took a flat prefix of that sorted list.
With 38 changed resources, the ten display slots were eaten by Deployments
and every HPA fell off the cut.

Detection was never the problem (COPS-2563, PR-6773). Display priority was.
Fix: _package_sections reserves part of the display budget for the risk
sections it already detects, and reorders instead of dropping.

COPS-2579 update: the storage cap itself grew from AI_MAX_SECTIONS_PER_APP
(10) to the much more generous FULL_SECTIONS_MAX_PER_APP, since the old
cap turned out to be the root cause of a bigger bug (a large-PR comment
showing 60 of 16616 real diff sections). Every test below that pinned the
OLD cap value is updated to exercise the same reordering property at the
NEW boundary, so the reserve mechanism (still needed for the rare
Real app with more than FULL_SECTIONS_MAX_PER_APP resources) stays covered.
"""
import os
import sys

os.environ.setdefault("BB_USER", "test-user")
os.environ.setdefault("BB_TOKEN", "test-token")
os.environ.setdefault("ARGOCD_PASS", "test-pass")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m

DEL_BODY = ("--- \n+++ \n@@ -1,6 +0,0 @@\n"
            "-apiVersion: autoscaling/v2\n-kind: HorizontalPodAutoscaler\n"
            "-metadata:\n-  name: userstory\n-spec:\n-  minReplicas: 5\n")
MOD_BODY = "--- \n+++ \n@@ -3,7 +3,7 @@\n metadata:\n-  foo: old\n+  foo: new\n"
ZERO_BODY = ("--- \n+++ \n@@ -5,7 +5,7 @@\n spec:\n-  replicas: 3\n+  replicas: 0\n"
             "   selector: {}\n")


def _pr3845_sections():
    """The shape of the real PR: many Deployments, then the HPAs.

    Section order is the sorted resource key, so every Deployment comes
    first and the deletions sit at the very end of the list.
    """
    names = ["accesscontrol", "account", "apigateway-webhook", "channeldirectory",
             "channeldirectory-background", "channelplaylist",
             "channelplaylist-background", "communityfeed", "contentfeed",
             "feedsubscription", "library", "mediatransform", "publishingdirectory",
             "reservation", "screenshots", "universalsearch", "user", "userpost",
             "userstory", "workspace"]
    deployments = [(f"/apps/Deployment {n}", MOD_BODY) for n in names]
    changed_hpas = [(f"/autoscaling/HorizontalPodAutoscaler {n}", MOD_BODY)
                    for n in ("accesscontrol", "account", "channeldirectory")]
    deleted_hpas = [(f"/autoscaling/HorizontalPodAutoscaler {n}", DEL_BODY)
                    for n in ("channeldirectory-background", "channelplaylist-background",
                              "screenshots", "universalsearch", "userstory")]
    return deployments + changed_hpas + deleted_hpas


# -- the bug itself ---------------------------------------------------

def test_every_deletion_is_visible_in_the_displayed_sections():
    """The PR-3845 bug: named in the block, absent from the body."""
    full = _pr3845_sections()
    _, capped, deleted, _, _, _, _, _ = m._package_sections(full)

    assert len(deleted) == 5, "precondition: the fixture must contain 5 deletions"
    shown = {hdr for hdr, _ in capped}
    missing = [h for h in deleted if h not in shown]
    assert not missing, (
        "deletions named in the shouty block but never shown in the diff "
        f"body: {missing}")


def test_reordering_preserves_every_section_up_to_the_storage_cap():
    """COPS-2579: the old assertion here was `len(capped) ==
    AI_MAX_SECTIONS_PER_APP` (10) -- exactly the bug this ticket fixes.
    Priority reordering must never drop or duplicate anything below the
    (much larger) storage cap: all 28 sections in this fixture survive."""
    full = _pr3845_sections()
    _, capped, _, _, _, _, _, _ = m._package_sections(full)
    assert len(capped) == len(full)
    assert sorted(capped) == sorted(full)


def test_ordinary_changes_keep_part_of_the_budget():
    """A deletion-heavy PR must not hide every ordinary change."""
    _, capped, deleted, _, _, _, _, _ = m._package_sections(_pr3845_sections())
    ordinary = [hdr for hdr, _ in capped if hdr not in set(deleted)]
    assert ordinary, "all display slots went to deletions"


def test_reserve_caps_how_many_slots_deletions_can_take_at_the_storage_cap():
    """COPS-2579: this property only bites once the section count exceeds
    FULL_SECTIONS_MAX_PER_APP -- exercise it at that (much larger)
    boundary instead of the old AI_MAX_SECTIONS_PER_APP=10 one, so the
    reserve mechanism stays covered for the rare app that really does
    have more than the storage cap's worth of changed resources."""
    cap = m.FULL_SECTIONS_MAX_PER_APP
    dels = [(f"/v1/Secret gone-{i:04d}", DEL_BODY) for i in range(cap + 20)]
    mods = [(f"/apps/Deployment app-{i:04d}", MOD_BODY) for i in range(20)]
    _, capped, deleted, _, _, _, _, _ = m._package_sections(mods + dels)
    assert len(capped) == cap
    ordinary_shown = sum(1 for hdr, _ in capped if hdr not in set(deleted))
    assert ordinary_shown > 0, "ordinary changes were entirely pushed out by deletions"


def test_replicas_zeroed_gets_the_same_priority():
    """Same class of bug, same fix: a zeroing that sorts last stays visible."""
    mods = [(f"/apps/Deployment app-{i:02d}", MOD_BODY) for i in range(15)]
    zero = [("/apps/StatefulSet zzz-last", ZERO_BODY)]
    _, capped, _, zeroed, _, _, _, _ = m._package_sections(mods + zero)
    assert zeroed == ["/apps/StatefulSet zzz-last"]
    assert "/apps/StatefulSet zzz-last" in {hdr for hdr, _ in capped}


# -- do not break what worked -----------------------------------------

def test_no_risk_sections_means_untouched_order():
    """The common case must stay byte for byte identical to before."""
    plain = [(f"/apps/Deployment app-{i:02d}", MOD_BODY) for i in range(15)]
    clean_diff, capped, deleted, zeroed, _, _, _, _ = m._package_sections(plain)
    assert deleted == [] and zeroed == []
    assert capped == plain
    assert clean_diff.startswith("===== /apps/Deployment app-00 =====")


def test_nothing_is_dropped_by_the_reordering():
    """Reorder, never discard: the caps stay the only thing that removes."""
    full = _pr3845_sections()
    ordered = m._prioritise_risk_sections(
        full,
        m._detect_deleted_resources(full),
        m._detect_replicas_zeroed(full),
        m.RISK_SECTION_RESERVE,
    )
    assert sorted(ordered) == sorted(full)


def test_detection_still_runs_on_the_full_list():
    """Guard the PR-6773 lesson: detect before any cap, not after."""
    fillers = [(f"/apps/Deployment filler-{i}", MOD_BODY) for i in range(30)]
    full = fillers + [("/v1/Secret buried-last", DEL_BODY)]
    _, _, deleted, _, _, _, _, _ = m._package_sections(full)
    assert deleted == ["/v1/Secret buried-last"]


# -- what the reviewer reads ------------------------------------------

def test_truncation_note_says_risk_first_when_it_reorders():
    sections = [("/v1/Secret gone", DEL_BODY), ("/apps/Deployment app", MOD_BODY)]
    out = "\n".join(m._format_app_diff_block(
        "pv-dkv-a-ms", sections, "", n_res=38,
        risk_headers={"/v1/Secret gone"}))
    assert "Showing first" not in out, "wording still claims a plain prefix"
    assert "2 of 38" in out


def test_truncation_note_is_unchanged_without_risk_sections():
    sections = [("/apps/Deployment app", MOD_BODY)]
    out = "\n".join(m._format_app_diff_block(
        "pv-dkv-a-ms", sections, "", n_res=38))
    assert "Showing first 1 of 38 changed resources" in out
