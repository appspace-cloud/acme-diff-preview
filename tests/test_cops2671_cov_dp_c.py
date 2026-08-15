"""COPS-2671 (slice C): dark branches in the bump classifier, the VM panel
and the standalone decommission-strip fallback.

Every line covered here belongs to a rule that was written for a named
incident and then never exercised by a test. Grouped by the question each
one answers:

1. `_routine_bump_key_ok` / `_routine_bump_signature` (the PR #3891 fleet
   bump). Two rules were dark:
     * an env-var `value:` line only folds when BOTH sides look like a
       version string, so a feature flag can never disappear into a
       one-line rollup;
     * a key whose removed and added values are the SAME SET is reorder
       noise and is dropped from the signature -- which is what lets an
       environment whose rendered labels moved fold together with its
       siblings taking the identical bump.
   Both are only observable through the rollup: a signature is either
   shared (one "Routine version bump" line, no diff blocks) or it is not
   (every app renders in full). That is what these tests assert.

2. `_summarize_appspace_state_changes`'s `elif _vm_broken` fallback
   (COPS-2660). It fires when the cascade was armed by an EARLIER PR and
   this one only strips the VM block, so no arm/disarm/purge transition
   fires above it. It was dark because the existing COPS-2660 test for
   this shape moves from HAPPY_YAML to STRIPPED_YAML, and STRIPPED_YAML
   also adds `decommissionPurgeData` -- so that case is answered two
   branches earlier, by PURGE ARMED. With the purge flag held constant the
   fallback is the only branch left, and it is the difference between the
   panel explaining the orphaning and the panel saying nothing at all on
   the PR that causes it.

3. `_summarize_vm_changes`, four rules on the values level:
     * the per-file skip (already-seen path, non-YAML file) -- a PR that
       lists the same file twice must not fetch or report it twice;
     * the YAMLError skip -- one unparseable value file must not take the
       whole panel down with it (the input panel already flags it);
     * the disk-shrink danger (GCP cannot shrink a disk in place);
     * the COPS-2635 TAINTED provision replay: a brand-new VM domain whose
       keys carry a real danger (allowDeletion armed from birth) must NOT
       collapse into the "N environments provision a new VM" group line,
       because folding a flagged line into a roster is the outcome this
       service exists to prevent;
     * and the rendered-level routine line, the COPS-2618 contract that a
       VM object which changed is never reported as "no changes".
"""
import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import pytest  # noqa: E402

import comment_render  # noqa: E402
import diff_preview as m  # noqa: E402

PR_SHA = "c671aaaa" * 5
BASE_SHA = "c671bbbb" * 5


@pytest.fixture(autouse=True)
def _quiet_ai(monkeypatch):
    """The rollup assertions read the whole body; a model paragraph in the
    middle of it would make them non-deterministic."""
    monkeypatch.setattr(m, "generate_ai_summary", lambda *a, **k: None)


# ══ 1. the routine-bump classifier ═════════════════════════════════════
#
# Bodies carry the environment name so that no two apps are byte-identical:
# that is the PR #3891 shape the rollup exists for (fingerprint grouping
# sees N distinct diffs, the classifier sees one transition). Headers carry
# it too, so same-shape grouping (COPS-2629) cannot answer instead of the
# rollup and hide which mechanism actually fired.

def _sections(env, body):
    return [("/apps/Deployment %s-broadcast" % env, body)]


def _diff_result(env, body):
    secs = _sections(env, body)
    return m.DiffResult(body, secs, 1, True, None, m.OUT_DIFF, None, None,
                        None, None, m._fingerprint_sections(secs))


def _image_bump(env):
    return ("--- \n+++ \n@@ -10,6 +10,6 @@\n"
            "       env: %s\n"
            "-        image: appspace-ms:2603.0.0\n"
            "+        image: appspace-ms:2603.1.0\n"
            "         ports:\n" % env)


def _envvar_bump(env):
    """The version arrives as a plain env-var value, not as an image tag."""
    return ("--- \n+++ \n@@ -10,7 +10,7 @@\n"
            "       env: %s\n"
            "         - name: APPSPACE_VERSION\n"
            "-          value: 2603.0.0\n"
            "+          value: 2603.1.0\n" % env)


def _feature_flag(env):
    """Same `value:` key, a value that is not a version at all."""
    return ("--- \n+++ \n@@ -10,7 +10,7 @@\n"
            "       env: %s\n"
            "         - name: FEATURE_SELF_SERVE\n"
            "-          value: false\n"
            "+          value: true\n" % env)


def _bump_with_reordered_labels(env):
    """The bump, plus two label lines that swapped places in the render.

    Both labels are removed AND added with the same value, so per key the
    removed and added sets are equal: nothing changed, the lines only
    moved.
    """
    return ("--- \n+++ \n@@ -4,10 +4,10 @@\n"
            "       env: %s\n"
            "   labels:\n"
            "-    tier: backend\n"
            "-    zone: europe-west1\n"
            "+    zone: europe-west1\n"
            "+    tier: backend\n"
            "-        image: appspace-ms:2603.0.0\n"
            "+        image: appspace-ms:2603.1.0\n" % env)


def _comment(bodies):
    results = {"pv-c671-%s-ms" % env: _diff_result(env, body(env))
               for env, body in bodies}
    return m.format_comment(PR_SHA, results, base_sha=BASE_SHA)


# -- 1a. `value:` folds only when both sides are version-shaped -----------

def test_a_versionish_env_var_value_folds_like_an_image_tag():
    body = _comment([("01", _envvar_bump), ("02", _envvar_bump),
                     ("03", _envvar_bump)])
    assert "Routine version bump" in body, (
        "three environments taking the same APPSPACE_VERSION bump are one "
        "fact:\n" + body)
    assert "`value`: `2603.0.0` \u2192 `2603.1.0`" in body
    assert "3 environments" in body
    assert "```diff" not in body, (
        "the folded apps must not also render their diff blocks")


def test_a_non_version_value_change_never_folds():
    """The rule's whole point: `value:` is the most generic key in a
    rendered manifest, so folding it on the key name alone would hide a
    feature flag flipping across the fleet behind one green line."""
    body = _comment([("01", _feature_flag), ("02", _feature_flag),
                     ("03", _feature_flag)])
    assert "Routine version bump" not in body, (
        "a feature-flag flip is not a version bump:\n" + body)
    assert body.count("```diff") == 3, (
        "each environment keeps its own diff block")
    assert "FEATURE_SELF_SERVE" in body


def test_the_classifier_sorts_bumps_from_flags_inside_one_pr():
    """The rule earns its keep on a MIXED fleet, which is the shape that
    actually reaches production: one PR, three environments taking the
    APPSPACE_VERSION bump and three flipping FEATURE_SELF_SERVE through the
    very same `value:` key. Both buckets are over the rollup threshold, so
    the threshold decides nothing here and `_routine_bump_key_ok` decides
    everything: the bumps must collapse to one line, the flag flips must
    each keep their diff block.

    Fold-everything (`value:` always routine) hides three flag flips behind
    a green one-liner; fold-nothing (`value:` never routine) puts the
    PR #3891 wall back. This test is red for both.
    """
    body = _comment([("01", _envvar_bump), ("02", _envvar_bump),
                     ("03", _envvar_bump),
                     ("11", _feature_flag), ("12", _feature_flag),
                     ("13", _feature_flag)])
    rollups = [l for l in body.splitlines() if "Routine version bump" in l]

    # The version side folds: exactly one rollup line, naming the three
    # bumped environments and the transition they share.
    assert len(rollups) == 1, (
        "the version bump is one fact and the flag flip is not a version "
        "bump, so there is exactly one rollup line:\n" + body)
    rollup = rollups[0]
    assert "`value`: `2603.0.0` → `2603.1.0`" in rollup, rollup
    assert "3 environments" in rollup, rollup
    for env in ("pv-c671-01", "pv-c671-02", "pv-c671-03"):
        assert env in rollup, rollup

    # The flag side stays enumerated: one diff block each, and the flag
    # name (a context line, so it can only reach the body through a
    # rendered diff) is present three times.
    assert body.count("```diff") == 3, (
        "the three flag flips each keep their own diff block:\n" + body)
    assert body.count("FEATURE_SELF_SERVE") == 3, (
        "every flag flip is shown, not summarised:\n" + body)
    for env in ("pv-c671-11", "pv-c671-12", "pv-c671-13"):
        assert env not in rollup, (
            "a flag flip must never be named as a version bump: " + rollup)
    assert "`false` → `true`" not in body, (
        "the flag transition must not appear as a folded summary:\n" + body)


# -- 1b. reorder-only keys are dropped from the signature ----------------

def test_a_reordered_label_block_still_folds_with_its_siblings():
    """pv-c671-03's render moved two label lines. Nothing about that
    environment changed differently, so it must share the signature of the
    two plain bumps and roll up with them -- three groups, one line."""
    body = _comment([("01", _image_bump), ("02", _image_bump),
                     ("03", _bump_with_reordered_labels)])
    assert "Routine version bump" in body, (
        "the reorder must not defeat the fold:\n" + body)
    assert "3 environments" in body
    assert "pv-c671-03" in body, "the moved-labels environment is named"
    assert "```diff" not in body


def test_the_reordered_labels_are_not_part_of_the_transition():
    """A key that did not change must not appear in the label the rollup
    line shows, or the reviewer reads a taxonomy change into a bump."""
    body = _comment([("01", _image_bump), ("02", _image_bump),
                     ("03", _bump_with_reordered_labels)])
    rollup = next(l for l in body.splitlines() if "Routine version bump" in l)
    assert "`image`" in rollup, rollup
    assert "tier" not in rollup and "zone" not in rollup, rollup
    assert "more field(s)" not in rollup, (
        "the reorder must not be counted as an extra changed field: " + rollup)


def test_a_genuinely_changed_label_still_blocks_the_fold():
    """The guard around the guard: dropping equal sets must not turn into
    dropping the key. A label whose value really changes is not
    version-shaped, so the whole app stays enumerated."""
    def relabelled(env):
        return ("--- \n+++ \n@@ -4,8 +4,8 @@\n"
                "       env: %s\n"
                "   labels:\n"
                "-    tier: backend\n"
                "+    tier: frontend\n"
                "-        image: appspace-ms:2603.0.0\n"
                "+        image: appspace-ms:2603.1.0\n" % env)
    body = _comment([("01", relabelled), ("02", relabelled),
                     ("03", relabelled)])
    assert "Routine version bump" not in body, (
        "tier: backend -> frontend is a real change:\n" + body)
    assert body.count("```diff") == 3


# ══ 2. the standalone VM-strip fallback ════════════════════════════════

IDENT = "gcp/prod/private-cloud/na1-a/monthly/pv-c671orph-a/customer.yaml"
STATE_PATH_MAP = {IDENT: ["pv-c671orph-a-ss", "pv-c671orph-a-ms"]}

# Armed in an EARLIER PR, VM block intact and running.
ARMED_WITH_VM = (
    "appspace:\n"
    "  customerName: orph\n"
    "  decommission: true\n"
    "  infra:\n"
    "    deployLinuxServicesK8s:\n"
    "      enabled: true\n"
    "      defaults:\n"
    "        allowDeletion: true\n"
    "      svc:\n"
    "        enabled: true\n"
    "        instances:\n"
    "          - pv-c671orph-svc-a\n"
)
# THIS PR: the VM block goes, every flag stays exactly as it was.
ARMED_STRIPPED = (
    "appspace:\n"
    "  customerName: orph\n"
    "  decommission: true\n"
    "  infra:\n"
    "    deployLinuxServicesK8s:\n"
    "      defaults:\n"
    "        allowDeletion: true\n"
)
NOT_ARMED_WITH_VM = ARMED_WITH_VM.replace("  decommission: true\n", "")
NOT_ARMED_STRIPPED = ARMED_STRIPPED.replace("  decommission: true\n", "")


def _state_panel(monkeypatch, old, new, tag):
    """Render the appspace-state panel. Each case needs its own pair of
    shas: the fetch layer memoises on (path, sha)."""
    base, pr = "base" + tag, "pr" + tag
    files = {(IDENT, base): old, (IDENT, pr): new}

    def fake(path, sha, repo=None):
        val = files.get((path, sha))
        return (val, m.BB_OK) if val is not None else (None, m.BB_NOT_FOUND)

    monkeypatch.setattr(m, "_bb_fetch_status", fake)
    return "\n".join(m._summarize_appspace_state_changes(
        [IDENT], pr, base, STATE_PATH_MAP))


def test_stripping_the_vm_of_an_already_armed_env_is_not_silent(monkeypatch):
    """No flag changes in this diff, so no arm / disarm / purge branch
    fires. Without the fallback the panel says nothing at all on the PR
    that orphans the VM."""
    out = _state_panel(monkeypatch, ARMED_WITH_VM, ARMED_STRIPPED, "c671s1")
    assert out.strip(), "the panel must not be empty for this shape"
    assert comment_render._DECOM_VM_STRIP_HDR in out
    assert "orphaned in the cloud, not deleted" in out
    assert "deployLinuxServicesK8s.svc.instances" in out, (
        "the reviewer needs to see WHICH keys went:\n" + out)


def test_the_standalone_strip_keeps_the_phase_table_honest(monkeypatch):
    """Phase 1 is broken by THIS PR; Phase 2 was armed by an earlier one
    and must read done, not 'this PR'."""
    out = _state_panel(monkeypatch, ARMED_WITH_VM, ARMED_STRIPPED, "c671s2")
    phase1 = next(l for l in out.splitlines() if "Phase 1" in l)
    phase2 = next(l for l in out.splitlines() if "Phase 2" in l)
    assert "broken" in phase1.lower(), phase1
    assert "done" in phase2, phase2
    assert "this PR" not in phase2, (
        "an earlier PR armed the cascade; this one only strips: " + phase2)


def test_the_strip_fallback_fires_on_allow_deletion_alone(monkeypatch):
    """The cascade flag is not set at all here: `allowDeletion` on its own
    is enough to prune-and-orphan, and Phase 2 must then read pending."""
    out = _state_panel(monkeypatch, NOT_ARMED_WITH_VM, NOT_ARMED_STRIPPED,
                       "c671s3")
    assert comment_render._DECOM_VM_STRIP_HDR in out
    phase2 = next(l for l in out.splitlines() if "Phase 2" in l)
    assert "pending" in phase2, phase2
    assert "✅" not in phase2, (
        "nothing armed the cascade yet, so Phase 2 must not read done: "
        + phase2)


def test_an_armed_env_whose_vm_survives_says_nothing(monkeypatch):
    """The control: the fallback must not become an always-on panel."""
    out = _state_panel(monkeypatch, ARMED_WITH_VM,
                       ARMED_WITH_VM + "  tier: gold\n", "c671s4")
    assert out.strip() == "", "no transition and no strip is not news:\n" + out


# ══ 3. the values-level VM panel ═══════════════════════════════════════

VM_BASE = (
    "appspace:\n"
    "  customerName: c\n"
    "  infra:\n"
    "    deployLinuxServicesK8s:\n"
    "      enabled: true\n"
    "      svc:\n"
    "        enabled: true\n"
    "        machineType: n2d-standard-4\n"
    "        desiredStatus: TERMINATED\n"
    "        dataDiskSizeGb: 500\n"
)
VM_DISK_SHRUNK = VM_BASE.replace("dataDiskSizeGb: 500", "dataDiskSizeGb: 200")
VM_DISK_GROWN = VM_BASE.replace("dataDiskSizeGb: 500", "dataDiskSizeGb: 800")

NO_VM = "appspace:\n  customerName: c\n"
# A provision that is armed for deletion from birth: the TAINTED shape.
PROVISION_ARMED = (
    "appspace:\n"
    "  customerName: c\n"
    "  infra:\n"
    "    deployLinuxServicesK8s:\n"
    "      enabled: true\n"
    "      defaults:\n"
    "        allowDeletion: true\n"
    "      svc:\n"
    "        enabled: true\n"
    "        machineType: n2d-standard-2\n"
)
PROVISION_CLEAN = PROVISION_ARMED.replace(
    "      defaults:\n        allowDeletion: true\n", "")
UNPARSEABLE = "appspace:\n  infra: [1, 2\n   svc: {oops\n"


def _vm_panel(monkeypatch, files, contents, app_results=None, record=None):
    """Drive _summarize_vm_changes over crafted (path, sha) contents.

    `record` collects every (path, sha) the panel asks Bitbucket for, which
    is how the per-file skip is observed: a file the panel decides not to
    look at is a file it never fetches.
    """
    def fetch(path, sha, repo=None):
        if record is not None:
            record.append((path, sha))
        val = contents.get((path, sha))
        return (val, m.BB_OK) if val is not None else (None, m.BB_NOT_FOUND)

    monkeypatch.setattr(m, "_bb_fetch_cached", fetch)
    path_map = {os.path.normpath(f.lstrip("/")): ["x-ss"] for f in files}
    return "\n".join(m._summarize_vm_changes(
        files, PR_SHA, BASE_SHA, path_map, app_results or {},
        repo="acme-config-prod"))


def _env_file(env):
    return "gcp/prod/private-cloud/na1-a/%s/customer.yaml" % env


# -- 3a. disk shrink is destructive ---------------------------------------

def test_a_smaller_data_disk_is_flagged_dangerous(monkeypatch):
    path = _env_file("pv-c671disk-a")
    out = _vm_panel(monkeypatch, [path],
                    {(path, BASE_SHA): VM_BASE,
                     (path, PR_SHA): VM_DISK_SHRUNK})
    assert "disk size DECREASES" in out, out
    assert "\U0001f6a8" in out, "a shrink must carry the danger mark:\n" + out
    assert comment_render._VM_PANEL_DANGER_HDR in out, (
        "the shrink belongs in the danger panel, not the routine one")
    assert "`500` \u2192 `200`" in out


def test_a_larger_data_disk_stays_routine(monkeypatch):
    """Growing a disk is an ordinary operation; only the shrink is the one
    GCP cannot do in place."""
    path = _env_file("pv-c671disk-a")
    out = _vm_panel(monkeypatch, [path],
                    {(path, BASE_SHA): VM_BASE,
                     (path, PR_SHA): VM_DISK_GROWN})
    assert "disk size DECREASES" not in out
    assert comment_render._VM_PANEL_ROUTINE_HDR in out
    assert "`500` \u2192 `800`" in out


# -- 3b. a tainted provision never collapses into the roster --------------

def test_a_provision_armed_for_deletion_replays_every_line(monkeypatch):
    """COPS-2635 buffers a brand-new VM domain so identical provisions can
    group. This one carries allowDeletion from birth: the danger line must
    be stated on its own, and grouping must be refused."""
    path = _env_file("pv-c671prov-a")
    out = _vm_panel(monkeypatch, [path],
                    {(path, BASE_SHA): NO_VM,
                     (path, PR_SHA): PROVISION_ARMED})
    assert "allowDeletion" in out and "the next cascade can destroy them" in out
    assert "provision" not in out, (
        "a tainted provision must not fold into the group line:\n" + out)
    assert "svc.machineType" in out, (
        "its other keys replay verbatim rather than being summarised:\n" + out)


def test_the_same_provision_without_the_arming_still_groups(monkeypatch):
    """The contrast that makes the rule visible: identical file, minus the
    armed flag, collapses to the one-line group statement."""
    path = _env_file("pv-c671prov-a")
    out = _vm_panel(monkeypatch, [path],
                    {(path, BASE_SHA): NO_VM,
                     (path, PR_SHA): PROVISION_CLEAN})
    assert "1 environment provisions a new linux VM (KCC)" in out, out
    assert "svc.machineType" not in out, (
        "a clean provision states the fact once; the keys live on the page")


# -- 3c. per-file skips ---------------------------------------------------

def test_a_file_listed_twice_is_read_and_reported_once(monkeypatch):
    """Bitbucket lists a path with and without its leading slash on some
    PRs. Both normalise to the same file: one fetch per side, one line."""
    path = _env_file("pv-c671dup-a")
    record = []
    out = _vm_panel(monkeypatch, [path, "/" + path, path],
                    {(path, BASE_SHA): VM_BASE,
                     (path, PR_SHA): VM_DISK_SHRUNK}, record=record)
    assert out.count("dataDiskSizeGb") == 1, (
        "the same change must not be reported twice:\n" + out)
    assert len(record) == 2, (
        "one fetch per side of the diff, not per listed name: %r" % record)


def test_non_yaml_changed_files_are_never_fetched(monkeypatch):
    """A PR touching a script or a README must not spend Bitbucket calls
    on files that cannot carry a values-level VM key."""
    path = _env_file("pv-c671dup-a")
    record = []
    out = _vm_panel(monkeypatch, [path, "README.md", "scripts/apply.sh"],
                    {(path, BASE_SHA): VM_BASE,
                     (path, PR_SHA): VM_DISK_SHRUNK}, record=record)
    fetched = {p for p, _sha in record}
    assert fetched == {path}, "only the YAML file may be fetched: %r" % record
    assert "disk size DECREASES" in out, "the real change is still reported"


def test_one_unparseable_value_file_does_not_take_the_panel_down(monkeypatch):
    """The input panel already flags a broken YAML file. This panel's
    contract is that it never breaks the comment, so it skips that file and
    keeps reporting every other environment."""
    bad = _env_file("pv-c671bad-a")
    good = _env_file("pv-c671disk-a")
    out = _vm_panel(monkeypatch, [bad, good],
                    {(bad, BASE_SHA): VM_BASE, (bad, PR_SHA): UNPARSEABLE,
                     (good, BASE_SHA): VM_BASE,
                     (good, PR_SHA): VM_DISK_SHRUNK})
    assert "disk size DECREASES" in out, (
        "the healthy environment must still be reported:\n" + out)
    assert "pv-c671bad-a" not in out


# -- 3d. rendered-level facts reach the routine list ----------------------

def test_a_changed_vm_object_is_reported_even_with_no_values_change(
        monkeypatch):
    """COPS-2618: labels changed on a ComputeInstance while the value files
    said nothing. 'No changes to VM infrastructure' would be a lie."""
    fact = {"kind": "ComputeInstance", "name": "pv-c671lab-svc-a",
            "fields": [("business-area", "", "appspace-platform")],
            "deleted": False, "dangerous": [], "notes": []}
    result = m.DiffResult("", [], 1, True, None, m.OUT_DIFF, None, None, None,
                          None, None, None, [fact])
    out = _vm_panel(monkeypatch, [], {},
                    app_results={"pv-c671lab-a-ss": result})
    assert "No changes to VM infrastructure" not in out, out
    assert "`pv-c671lab-a` \u00b7 `ComputeInstance pv-c671lab-svc-a`" in out
    assert "`business-area` `(absent)` \u2192 `appspace-platform`" in out
    assert comment_render._VM_PANEL_ROUTINE_HDR in out, (
        "a label change is visible, not alarming")


def test_a_vm_free_pr_still_renders_the_clean_panel(monkeypatch):
    """The control for the line above: with no facts and no VM keys the
    panel is the fixed 'nothing here' block, not a routine line."""
    out = _vm_panel(monkeypatch, [], {}, app_results={})
    assert "No changes to VM infrastructure" in out
