"""The dark branches of the VM detectors and the version fold (COPS-2671).

Both modules were sliced out of diff_preview.py during COPS-2658 with their
tests left behind on the happy paths. What stayed uncovered is, in both
cases, the *conservative* half of each module -- the code that decides NOT
to say something -- and that half is exactly where a silent regression is
invisible. A detector that stops shouting looks like a quiet PR; a
classifier that starts folding looks like a tidy comment.

What these tests pin down, module by module:

`vm_analysis`
  * the workload-shutdown ratio only counts real workload kinds. A
    ConfigMap carrying an embedded values file with its own `replicas:`
    line is the realistic way that ratio gets poisoned, and the ratio is
    what separates "one service scaled down" from "this environment is
    being switched off" in the merge summary.
  * `_detect_vm_changes` parses a hunk line by line, and three of its
    filters were never exercised: a VM already parked TERMINATED in
    context (the second, separate escape hatch from the machineType
    danger -- the covered one is a `desiredStatus` +/- pair), a changed
    line with no `key: value` shape at all, and a `type:` whose value is
    not disk-shaped. The last two both feed the COPS-2618 untracked-keys
    note, so a regression there does not go quiet: it invents fields.
  * a paired-but-identical tracked value (a re-quote) is not a change.
  * two severity verdicts nothing reached: a snapshot-policy attachment
    disappearing, and a zone move.
  * the Terraform -> KCC classifier's refusals: legacy stripped with no
    KCC role behind it is a VM being switched off, not an adoption; and
    the cross-move disk comparison must not fabricate a shrink out of a
    size the new side does not pin. That comparison is the only place two
    DIFFERENT keys are compared, so its numeric contract is pinned at both
    ends: numbers are read as numbers (not as text, in either direction),
    unit-suffixed values are skipped rather than digit-stripped, and every
    size leaf is matched only against the same leaf.

`version_fold`
  * `_fold_pairs` refusing to pair: an unbalanced key count.
  * a YAML sequence item (`- targetRevision: X`) must have its dash
    stripped before the key is read, or every list-shaped carrier stops
    folding.
  * `_split_image` refusing a `registry:port/name` reference that carries
    no tag -- read naively, the port becomes the "version".
  * the three refusals in `_classify_fold_pair`: a checksum whose value is
    not a bare digest, a `helm.sh/chart` whose chart NAME changed, and a
    chart-label key whose value did not actually move.
  * the non-`helm.sh/chart` chart-label branch itself (`targetRevision`,
    `appVersion`), which is what carries the transition on ArgoCD
    Applications.
  * the app-level `version_change` seed, which is the only thing that lets
    a bare `value:` env pair fold at all.

Every case asserts the observable consequence -- the fact dict, the panel
text, the merge-summary sentence, or the fold verdict -- never the source.
"""
import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import diff_preview as m          # noqa: E402
import version_fold as vf         # noqa: E402
import vm_analysis as vma         # noqa: E402


# ══ Part 1 ── the workload-shutdown ratio ════════════════════════════════
# _detect_workload_shutdown answers "how many of this app's workloads end at
# zero", and _is_env_shutdown turns that ratio into the "Environment
# shutting down" finding. Anything that inflates the denominator downgrades
# a shutdown to a partial scale-down.

DEPLOY_ZEROED = (
    "--- \n+++ \n@@ -21,6 +21,7 @@\n"
    " spec:\n"
    "+  replicas: 0\n"
    "   strategy:\n"
)

# A ConfigMap holding a rendered values file. The embedded document has its
# own `replicas:` key, so the line-scanner sees a workload that ends
# POSITIVE -- which is what makes this the realistic denominator poison.
CFG_HDR = "/v1/ConfigMap pv-dev-01-a/chart-overrides"
CFG_EMBEDDED_REPLICAS = (
    "--- \n+++ \n@@ -3,7 +3,7 @@\n"
    " data:\n"
    "   values.yaml: |\n"
    "-    replicas: 1\n"
    "+    replicas: 2\n"
)


def _result(sections, stats):
    return m.DiffResult(
        text="x", sections=sections, n_res=len(sections), has_diff=True,
        error=None, outcome=m.OUT_DIFF, reason=None, version_change=None,
        deleted_resources=[],
        replicas_zeroed=m._detect_replicas_zeroed(sections),
        fingerprint="fp", renamed_resources=[], vm_changes=[],
        version_fold=None, shutdown_stats=stats)


def _summary(results):
    return "\n".join(m._build_merge_summary(results, {}, [], [], [], [], None))


MIXED = [
    ("/apps/Deployment accesscontrol", DEPLOY_ZEROED),
    ("/apps/StatefulSet mongo", DEPLOY_ZEROED),
    (CFG_HDR, CFG_EMBEDDED_REPLICAS),
]


def test_non_workload_sections_stay_out_of_the_shutdown_ratio():
    """A ConfigMap is not a workload however its data reads.

    Counting it makes the denominator 3 against 2 zeroed, and the whole
    point of the ratio is that "all of them" and "most of them" are
    different events.
    """
    assert vma._detect_workload_shutdown(MIXED) == {"zeroed": 2,
                                                    "workloads": 2,
                                                    "hpas_remaining": 0}


def test_the_summary_still_calls_it_a_shutdown_with_a_configmap_present():
    """The consequence of the line above, at the surface a reviewer reads."""
    out = _summary({"pv-dev-01-a-ms": _result(
        MIXED, vma._detect_workload_shutdown(MIXED))})
    assert "Environment shutting down" in out, out
    assert "every workload (2)" in out, (
        "the count must be workloads, not sections:\n" + out)


def test_a_pr_with_no_workloads_reports_no_ratio_at_all():
    """None, not {"zeroed": 0, "workloads": 0}.

    The merge summary indexes this dict (`shutdown_stats["workloads"]`) and
    prints the number. "0 workloads, 0 zeroed" is a claim about an app whose
    workloads this PR never looked at; absence is the honest answer.
    """
    assert vma._detect_workload_shutdown([(CFG_HDR,
                                           CFG_EMBEDDED_REPLICAS)]) is None


# ══ Part 2 ── _detect_vm_changes: reading the hunk ═══════════════════════

CI_HDR = "/compute.cnrm.cloud.google.com/ComputeInstance pv-bos-a/pv-bos-svc-a"
CD_HDR = ("/compute.cnrm.cloud.google.com/ComputeDisk "
          "pv-bos-a/pv-bos-svc-a-data")
ATT_HDR = ("/compute.cnrm.cloud.google.com/ComputeDiskResourcePolicyAttachment "
           "pv-bos-a/pv-bos-svc-a-data-daily")


def _fact(header, body):
    facts = vma._detect_vm_changes([(header, body)])
    assert facts, "the section must produce a fact at all"
    return facts[0]


# -- 2a. the VM was already parked before this PR -------------------------
# The covered escape hatch is a `desiredStatus: RUNNING -> TERMINATED` pair
# in the same hunk. The other one is a VM that was ALREADY stopped in an
# earlier PR: desiredStatus then appears only as unchanged context, and the
# resize is the whole diff. That is the ordinary two-PR runbook sequence.

RESIZE_ALREADY_PARKED = (
    "     zone: europe-west1-d\n"
    "     desiredStatus: TERMINATED\n"
    "-    machineType: n2d-standard-4\n"
    "+    machineType: n2d-standard-8\n"
)
RESIZE_WHILE_RUNNING = RESIZE_ALREADY_PARKED.replace("TERMINATED", "RUNNING")


def test_resize_on_a_vm_parked_in_an_earlier_pr_is_not_dangerous():
    f = _fact(CI_HDR, RESIZE_ALREADY_PARKED)
    assert ("machineType", "n2d-standard-4", "n2d-standard-8") in f["fields"]
    assert not f["dangerous"], (
        "the VM is already TERMINATED in the unchanged context; the runbook "
        "step has been done: %r" % (f["dangerous"],))


def test_the_same_resize_on_a_running_vm_still_is_dangerous():
    """The contrast that gives the test above its meaning: only the word in
    the context line differs between the two bodies."""
    f = _fact(CI_HDR, RESIZE_WHILE_RUNNING)
    assert any("runbook" in d for d in f["dangerous"]), f["dangerous"]


# -- 2b. a changed line with no `key: value` shape ------------------------
# COPS-2618 made every unrecognised key show up in a note. A changed line
# inside a literal block (a startup script, a cloud-init document) has no
# colon and is not a field at all -- naming it would put a shell path in the
# list of "field(s) changed".

SCRIPT_LINE_AND_LABEL = (
    "   metadata:\n"
    "     labels:\n"
    "-      business-area: platform\n"
    "+      business-area: appspace-platform\n"
    "     startup-script: |\n"
    "-      /opt/appspace/bootstrap.sh\n"
    "+      /opt/appspace/bootstrap-v2.sh\n"
)


def test_a_colonless_changed_line_is_not_reported_as_a_field():
    f = _fact(CI_HDR, SCRIPT_LINE_AND_LABEL)
    note = " ".join(f["notes"])
    assert "business-area" in note, (
        "the real untracked change must still be named: " + note)
    assert "bootstrap" not in note, (
        "a line from inside a literal block is not a field name: " + note)
    assert not any("bootstrap" in str(x) for x in f["fields"])


# -- 2c. `type:` that is not a disk type ----------------------------------
# `type` is tracked because a disk type is immutable, and the panel calls
# that destroy-and-recreate. A ComputeInstance body also carries
# `accessConfig.type`, which is a NAT mode and changes freely.

NAT_TYPE_AND_STATUS = (
    "   networkInterface:\n"
    "     - accessConfig:\n"
    "-        type: ONE_TO_ONE_NAT\n"
    "+        type: DIRECT_IPV6\n"
    "-    desiredStatus: RUNNING\n"
    "+    desiredStatus: TERMINATED\n"
)


def test_a_network_access_config_type_is_not_a_disk_type():
    f = _fact(CI_HDR, NAT_TYPE_AND_STATUS)
    assert ("desiredStatus", "RUNNING", "TERMINATED") in f["fields"], (
        "the rest of the section must still be read: %r" % (f["fields"],))
    assert not any(k == "type" for k, _, _ in f["fields"]), (
        "a NAT mode must not be reported as a disk type: %r" % (f["fields"],))
    assert not any("immutable" in d for d in f["dangerous"]), f["dangerous"]


# -- 2d. a value that is only re-quoted -----------------------------------

REQUOTED_SIZE = (
    "-    size: \"200\"\n"
    "+    size: 200\n"
    "-    location: europe-west1-d\n"
    "+    location: europe-west4-a\n"
)


def test_a_requoted_size_is_not_a_changed_field():
    """_vm_unquote exists so `"200"` and `200` are the same size. If the
    equality check after it did not skip the pair, every re-quote would
    render as a disk resize."""
    f = _fact(CD_HDR, REQUOTED_SIZE)
    assert ("location", "europe-west1-d", "europe-west4-a") in f["fields"]
    assert not any(k == "size" for k, _, _ in f["fields"]), (
        "200 did not become 200: %r" % (f["fields"],))
    assert not f["dangerous"]


# ══ Part 3 ── _detect_vm_changes: two unreached verdicts ═════════════════

# A pure deletion: every line removed, no context, no additions.
ATTACHMENT_GONE = (
    "-apiVersion: compute.cnrm.cloud.google.com/v1beta1\n"
    "-kind: ComputeDiskResourcePolicyAttachment\n"
    "-metadata:\n"
    "-  name: pv-bos-svc-a-data-daily\n"
    "-spec:\n"
    "-  resourceID: daily-snapshot-schedule\n"
)


def test_a_disappearing_snapshot_attachment_names_the_backup_schedule():
    """This kind gets its own wording, because the generic "removed from the
    render" sentence does not tell the reviewer what actually stops: the
    disk keeps running and quietly stops being backed up.

    COPS-2682: that note is routine (not 🚨). Pruning the attachment does
    not destroy the VM, disk, IP, or existing snapshots.
    """
    f = _fact(ATT_HDR, ATTACHMENT_GONE)
    assert f["deleted"] and not f["created"]
    assert not f["dangerous"]
    joined = " ".join(f["notes"]).lower()
    assert ("snapshot" in joined or "schedule" in joined
            or "snap" in joined), joined


def test_a_deleted_instance_defaults_to_abandon_orphan_wording():
    """COPS-2682: ComputeInstance leaving the render without an explicit
    deletion-policy: delete is unmanage under the chart abandon default.
    Contrast: only deletion-policy: delete (or unknown kinds) stay dangerous.
    """
    f = _fact(CI_HDR, ATTACHMENT_GONE.replace(
        "ComputeDiskResourcePolicyAttachment", "ComputeInstance"))
    assert f["deleted"] and f.get("orphaned")
    assert not f["dangerous"]
    joined = " ".join(f["notes"])
    assert "ComputeInstance" in joined and "abandon" in joined.lower()


def test_a_deleted_instance_with_delete_policy_is_dangerous():
    body = (
        "-apiVersion: compute.cnrm.cloud.google.com/v1beta1\n"
        "-kind: ComputeInstance\n"
        "-metadata:\n"
        "-  name: pv-bos-svc-a\n"
        "-  annotations:\n"
        "-    cnrm.cloud.google.com/deletion-policy: delete\n"
        "-spec:\n"
        "-  machineType: n2d-standard-4\n")
    f = _fact(CI_HDR, body)
    assert f["deleted"] and not f.get("orphaned")
    joined = " ".join(f["dangerous"])
    assert "deletion-policy: delete" in joined
    assert "destroy" in joined.lower()


ZONE_MOVE = (
    "-    zone: europe-west1-d\n"
    "+    zone: europe-west4-a\n"
)


def test_a_zone_move_is_destroy_and_recreate():
    f = _fact(CI_HDR, ZONE_MOVE)
    assert ("zone", "europe-west1-d", "europe-west4-a") in f["fields"]
    joined = " ".join(f["dangerous"])
    assert "immutable" in joined and "destroy-and-recreate" in joined, joined


# ══ Part 4 ── the Terraform -> KCC move ══════════════════════════════════
# Driven through the real panel: _summarize_vm_changes fetches both sides of
# the changed value file, classifies the move and decides what to say.

IDENT = "gcp/prod/private-cloud/ap1-b/monthly/pv-bos-b/customer.yaml"
PATH_MAP = {IDENT: ["pv-bos-b-ss"]}

LEGACY = """appspace:
  customerName: bos
  infra:
    deployLinuxServices:
      deployVM: true
      machineType: n2d-highmem-2
      dataDiskSizeGb: 256
"""

# The legacy block simply deleted, with nothing taking it over.
NO_VM_AT_ALL = """appspace:
  customerName: bos
"""

# A real adoption that does NOT pin a disk size: the role inherits whatever
# the chart defaults to, so there is no new value to compare 256 against.
ADOPTED_NO_DISK_PIN = """appspace:
  customerName: bos
  infra:
    deployLinuxServicesK8s:
      enabled: true
      svc:
        enabled: true
        machineType: n2d-highmem-2
        createNewBootDisk: false
        manageMetadata: false
"""


def _panel(monkeypatch, old, new, sfx):
    b, h = "main" + sfx, "pr" + sfx
    store = {(IDENT, b): old, (IDENT, h): new}

    def fake(path, sha, repo=None):
        v = store.get((path, sha))
        return (v, m.BB_OK) if v is not None else (None, m.BB_NOT_FOUND)

    monkeypatch.setattr(m, "_bb_fetch_status", fake)
    return "\n".join(m._summarize_vm_changes([IDENT], h, b, PATH_MAP, []))


def test_legacy_stripped_with_no_kcc_role_is_a_shutdown_not_an_adoption(
        monkeypatch):
    """The classifier's own docstring calls this out: legacy keys removed
    with no KCC role enabled at all is a VM being switched off, and the
    danger rules exist for exactly that. Reading it as an ownership transfer
    would suppress the machineType verdict on the one PR that needs it."""
    out = _panel(monkeypatch, LEGACY, NO_VM_AT_ALL, "s442")
    assert "ADOPTION" not in out.upper(), out
    assert out.startswith(m._VM_PANEL_DANGER_HDR), (
        "stripping a live VM's config must render the danger panel:\n" + out)
    assert "machineType" in out


def test_an_adoption_that_pins_no_disk_size_is_not_a_shrink_to_zero(
        monkeypatch):
    """The cross-move comparison only fires between two sizes it can see.
    An unset leaf means the chart default applies -- it is not a 256 -> 0
    shrink, and claiming one would block every adoption that leaves the
    disk size to the chart."""
    out = _panel(monkeypatch, LEGACY, ADOPTED_NO_DISK_PIN, "s492")
    assert "ADOPTION" in out.upper(), out
    assert "DECREASES" not in out, out
    assert not out.startswith(m._VM_PANEL_DANGER_HDR), out


# The same comparison, unit tested at both ends: it is the only place in the
# service where two DIFFERENT keys are compared, so the values it accepts
# and the values it refuses are its whole contract.
_L = "appspace.infra.deployLinuxServices."
_K = "appspace.infra.deployLinuxServicesK8s."


def test_a_comparable_shrink_across_the_move_is_reported_with_both_sizes():
    reason = vma._kcc_move_disk_shrink(
        {_L + "dataDiskSizeGb": 256}, {_K + "svc.dataDiskSizeGb": 128},
        ["svc"])
    assert "DECREASES" in reason
    assert "256" in reason and "128" in reason and "svc" in reason


def _shrink(old_leaf, old_v, new_leaf, new_v):
    return vma._kcc_move_disk_shrink({_L + old_leaf: old_v},
                                     {_K + "svc." + new_leaf: new_v},
                                     ["svc"])


def test_the_two_sizes_are_read_as_numbers_not_as_text():
    """The two ends are compared numerically, in both directions.

    The tempting shortcut here is `str(new) < str(old)`, because the sizes
    arrive as YAML scalars of whichever type the file happened to use. It
    is wrong at both ends and the two cases below are the ones that show
    it: lexicographically "900" is NOT below "1000" (a real 100GB loss
    goes unreported) while "100" IS below "90" (a growth is announced as
    data loss on a PR that adds capacity).
    """
    shrank = _shrink("dataDiskSizeGb", 1000, "dataDiskSizeGb", 900)
    assert "DECREASES" in shrank, (
        "1000 -> 900 is a shrink whatever the digits look like: %r" % shrank)
    assert "1000" in shrank and "900" in shrank, shrank

    assert _shrink("dataDiskSizeGb", 90, "dataDiskSizeGb", 100) == "", (
        "90 -> 100 adds capacity; only a text compare calls it a decrease")


def test_a_size_carrying_a_unit_suffix_is_skipped_in_both_directions():
    """`256Gi` is not a number and the comparison refuses to guess at it.

    The other tempting shortcut is to strip the non-digits and compare what
    is left. That reads `256Gi -> 128Gi` as a shrink -- which is right only
    by luck, since the same rule turns `1Ti -> 900Gi` into "growth" -- so
    the contract is the conservative one: a value float() cannot read is
    not compared at all, and no claim is made in either direction.
    """
    assert _shrink("dataDiskSizeGb", "256Gi",
                   "dataDiskSizeGb", "512Gi") == ""
    assert _shrink("dataDiskSizeGb", "256Gi",
                   "dataDiskSizeGb", "128Gi") == "", (
        "digits stripped out of a unit-suffixed value are not a size")


def test_sizes_are_matched_leaf_to_leaf_never_across_disk_keys():
    """A boot disk and a data disk are two different disks.

    The loop walks five possible size leaves, and each one is compared only
    against the same leaf on the KCC side. Pool them and a PR that moves a
    10GB boot disk while pinning a 5GB data disk reads as a shrink of
    something -- and the sentence names a leaf nobody changed. The contrast
    is the second half: the very same 10 -> 5, on one leaf, is reported.
    """
    assert _shrink("bootDiskSizeGb", 10, "dataDiskSizeGb", 5) == "", (
        "a data disk pinned at 5 says nothing about a boot disk of 10")
    same_leaf = _shrink("bootDiskSizeGb", 10, "bootDiskSizeGb", 5)
    assert "DECREASES" in same_leaf and "bootDiskSizeGb" in same_leaf, (
        "the walk must reach the non-first leaves too: %r" % same_leaf)


# ══ Part 5 ── version_fold: refusing to pair ═════════════════════════════
# Every helper below is reached through _classify_version_fold, because the
# only thing that matters is which sections come back foldable. Three real
# bump sections ride along in each case so the fold minimum is met and the
# verdict for the odd section is unambiguous.


def _app_section(i, old="2603.1.9", new="2603.1.10"):
    """An ArgoCD Application whose version rides on a `sources:` LIST item.

    The dash has to be stripped before the key is read, or `targetRevision`
    parses as `- targetRevision` and stops being a known carrier.
    """
    return (f"/argoproj.io/Application pv-bos-a-{i:03d}",
            "--- \n+++ \n@@ -8,7 +8,7 @@\n"
            " spec:\n"
            "   sources:\n"
            f"-    - targetRevision: {old}\n"
            f"+    - targetRevision: {new}\n")


BASELINE = [_app_section(i) for i in range(3)]


def _verdict(extra):
    """(does `extra` fold?, how many sections folded in total)."""
    fold = vf._classify_version_fold(BASELINE + [extra])
    assert fold is not None, "the three baseline sections must still fold"
    return extra[0] in fold["headers"], fold["n_foldable"]


def test_a_list_item_carrier_folds_and_names_the_transition():
    fold = vf._classify_version_fold(BASELINE)
    assert fold["n_foldable"] == 3
    assert fold["label"] == "2603.1.9 → 2603.1.10"
    assert fold["classes"] == ("chart labels",)


def test_an_unbalanced_key_count_keeps_the_section_inline():
    """Two images removed, one added. Zipping them positionally would pair
    the first two and silently drop the deletion of the second container --
    a resource losing a sidecar, folded away as a version bump."""
    unbalanced = ("/apps/Deployment pv-bos-a-sidecar-dropped",
                  "--- \n+++ \n@@ -12,7 +12,6 @@\n"
                  "       containers:\n"
                  "-        image: registry.example/svc:2603.1.9\n"
                  "-        image: registry.example/logshipper:2603.1.9\n"
                  "+        image: registry.example/svc:2603.1.10\n")
    folded, total = _verdict(unbalanced)
    assert not folded and total == 3


def test_an_untagged_image_on_a_ported_registry_keeps_the_section_inline():
    """`registry.example:5000/svc-a` has no tag: the colon belongs to the
    port. Split naively, the "tag" becomes `5000/svc-a` and a repository
    rename would fold as a version bump."""
    ported = ("/apps/Deployment pv-bos-a-ported",
              "--- \n+++ \n@@ -12,7 +12,7 @@\n"
              "       containers:\n"
              "-        image: registry.example:5000/svc-a\n"
              "+        image: registry.example:5000/svc-b\n")
    folded, total = _verdict(ported)
    assert not folded and total == 3


# ══ Part 6 ── version_fold: refusing to classify ═════════════════════════

def test_a_checksum_that_is_not_a_bare_digest_keeps_the_section_inline():
    """The checksum class exists for `checksum/*: <hex>` and nothing else.
    A prefixed value (`sha256:...`) is not a digest by this rule, and the
    safe direction for an unrecognised value is to keep the section."""
    prefixed = ("/apps/Deployment pv-bos-a-prefixed",
                "--- \n+++ \n@@ -4,4 +4,4 @@\n"
                "   annotations:\n"
                "-    checksum/config.yaml: sha256:67a9616016f2b8cc\n"
                "+    checksum/config.yaml: sha256:89801d07a5be6329\n")
    folded, total = _verdict(prefixed)
    assert not folded and total == 3


def test_a_chart_rename_is_not_a_chart_version_bump():
    """`helm.sh/chart` carries name-version. Only the version may move: a
    resource switching to a different chart is a different chart, however
    similar the label looks."""
    renamed = ("/apps/Deployment pv-bos-a-rechart",
               "--- \n+++ \n@@ -4,4 +4,4 @@\n"
               "   labels:\n"
               "-    helm.sh/chart: appspace-ms-2603.1.9\n"
               "+    helm.sh/chart: appspace-microservice-2603.1.10\n")
    folded, total = _verdict(renamed)
    assert not folded and total == 3


def test_a_chart_label_that_did_not_move_keeps_the_section_inline():
    """A re-quoted `appVersion` pairs up with itself. There is no transition
    to attribute the section to, so it is not provably version noise -- and
    a section whose ONLY other content is unexplained is a needle."""
    requoted = ("/apps/Deployment pv-bos-a-requote",
                "--- \n+++ \n@@ -4,4 +4,4 @@\n"
                "   annotations:\n"
                "-    appVersion: \"2603.1.10\"\n"
                "+    appVersion: 2603.1.10\n")
    folded, total = _verdict(requoted)
    assert not folded and total == 3


def test_an_appversion_that_did_move_does_fold():
    """The contrast for the case above, on the same key."""
    moved = ("/apps/Deployment pv-bos-a-appversion",
             "--- \n+++ \n@@ -4,4 +4,4 @@\n"
             "   annotations:\n"
             "-    appVersion: 2603.1.9\n"
             "+    appVersion: 2603.1.10\n")
    folded, total = _verdict(moved)
    assert folded and total == 4


# ══ Part 7 ── version_fold: the entry guards ═════════════════════════════

def test_no_sections_yields_no_fold_facts():
    """The renderer tests the result for truth. `None` -- never an empty
    fold dict -- is what keeps the fold line off a comment with nothing to
    fold, and the guard also makes the classifier total over the `None` the
    packing path can hand it."""
    assert vf._classify_version_fold([]) is None
    assert vf._classify_version_fold(None) is None


# The app-level chart version is the seed of the transition vocabulary. A
# bare `value:` under an env var is the single most common carrier in a
# platform bump and the single most ambiguous key in the diff, so it folds
# ONLY when it repeats a transition something unambiguous already vouched
# for. When no chart label or image tag changed in the whole PR -- an
# environment whose apps are pinned by env var alone -- that vouching can
# only come from the app's own chart version.

def _env_value_section(i, old, new):
    return (f"/apps/Deployment pv-bos-a-env-{i:03d}",
            "--- \n+++ \n@@ -12,7 +12,7 @@\n"
            "         env:\n"
            "         - name: APPSPACE_PLATFORM_VERSION\n"
            f"-          value: {old}\n"
            f"+          value: {new}\n")


ENV_ONLY = [_env_value_section(i, "2603.1.9", "2603.1.10") for i in range(3)]


def test_bare_env_values_do_not_fold_without_a_vouched_transition():
    assert vf._classify_version_fold(ENV_ONLY) is None, (
        "nothing in these sections proves 2603.1.9 -> 2603.1.10 is a version")


def test_the_app_chart_version_vouches_for_the_same_env_values():
    fold = vf._classify_version_fold(
        ENV_ONLY, version_change=("2603.1.9", "2603.1.10"))
    assert fold is not None, "the chart version is the missing witness"
    assert fold["n_foldable"] == 3
    assert fold["classes"] == ("version env values",)
    assert fold["label"] == "2603.1.9 → 2603.1.10"


def test_a_chart_label_carrier_vouches_for_bare_env_values():
    """The two-pass design in one case, asserted the only way that proves
    it: the answer must not depend on section order.

    Pass one collects the transition from the unambiguous carriers -- here
    `targetRevision`, with no image tag and no `helm.sh/chart` anywhere in
    the PR -- and pass two is what lets the ambiguous `value:` pairs fold
    against it. Drop the collection step and a single pass still folds
    everything *as long as the Applications happen to come first*, because
    the classifier shares one mutable candidate set and the chart labels
    seed it on their way past. Order the Deployments first and that luck
    runs out: the env values are judged against an empty vocabulary and
    every one of them stays inline. Sections arrive in whatever order the
    render produced them, so both orders must give the same five.
    """
    envs = [_env_value_section(i, "2603.1.9", "2603.1.10") for i in range(2)]
    for name, secs in (("applications first", BASELINE + envs),
                       ("deployments first", envs + BASELINE)):
        fold = vf._classify_version_fold(secs)
        assert fold is not None, name
        assert fold["n_foldable"] == 5, (name, fold)
        assert set(fold["classes"]) == {"chart labels",
                                        "version env values"}, (name, fold)
        assert fold["label"] == "2603.1.9 → 2603.1.10", (name, fold)


def test_a_chart_version_that_disagrees_vouches_for_nothing():
    """The seed is a specific transition, not a licence to fold any pair."""
    assert vf._classify_version_fold(
        ENV_ONLY, version_change=("2602.4.0", "2602.5.0")) is None


def test_a_numeric_chart_version_is_compared_as_rendered_text():
    """YAML hands back an int for a bare numeric `appVersion`, while the
    manifest carries it as text. Without the string coercion the seed never
    matches the diff it was collected for."""
    sections = [_env_value_section(i, "2603", "2604") for i in range(3)]
    fold = vf._classify_version_fold(sections, version_change=(2603, 2604))
    assert fold is not None and fold["n_foldable"] == 3
    assert fold["label"] == "2603 → 2604"
