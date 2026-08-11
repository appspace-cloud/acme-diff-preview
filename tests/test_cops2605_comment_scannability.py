"""Comment scannability redesign: VM panel, readable-size budget, bump rollup.

Operator feedback (from the two reviewers who read these comments daily):

1. VM-domain changes (the KCC linux-services resources rendered from
   appspace.infra.deployLinuxServicesK8s) carry the same visual weight as a
   label change, while a botched VM change is the slowest thing in the whole
   platform to recover from. They need their own severity-tiered panel,
   detected deterministically at both the values level and the rendered
   level, on the FULL pre-cap section list (the PR-6773 lesson).
2. Nothing sits between "readable" and Bitbucket's 245KB hard cap. A much
   lower proactive budget must keep every critical panel intact and push
   bulk diff content to the full-diff artifact, linking to it directly.
3. Comments must read short by default: environments taking the same
   routine version bump collapse to one line per distinct change, on top of
   the byte-identical fingerprint grouping (which keeps its one full
   representative diff), while anything risky stays fully enumerated.

Golden discipline is the same as test_cops2565_golden_comments.py: the
comment a reviewer reads is the contract.
"""
import os
import sys

import pytest

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m

GOLDEN_DIR = os.path.join(os.path.dirname(__file__), "golden")
FIXED_TS = "2026-01-01 00:00 UTC"
PR_SHA = "abc12345def67890abc12345def67890abc12345"
BASE_SHA = "0000111122223333444455556666777788889999"
ART_URL = "https://diffs.appspace.example/diff/acme-config-prod/42/abc12345"


@pytest.fixture(autouse=True)
def deterministic(monkeypatch):
    monkeypatch.setattr(m, "_ts", lambda: FIXED_TS)
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)
    monkeypatch.setattr(m, "_repo_for_sha", lambda sha: "acme-config-prod")


def _result(text="", sections=None, n_res=0, has_diff=False, error=None,
            outcome=None, reason=None, version_change=None,
            deleted_resources=None, replicas_zeroed=None, fingerprint=None,
            vm_changes=None):
    return m.DiffResult(text, sections if sections is not None else [], n_res,
                        has_diff, error, outcome or m.OUT_NO_DIFF, reason,
                        version_change, deleted_resources, replicas_zeroed,
                        fingerprint, None, vm_changes)


def _assert_golden(name, body):
    os.makedirs(GOLDEN_DIR, exist_ok=True)
    path = os.path.join(GOLDEN_DIR, name + ".md")
    if os.environ.get("UPDATE_GOLDEN") == "1":
        with open(path, "w") as f:
            f.write(body)
        pytest.skip("golden rewritten: " + name)
    if not os.path.exists(path):
        pytest.fail("no golden for %r. Review the output, then commit it "
                    "with UPDATE_GOLDEN=1:\n\n%s" % (name, body))
    with open(path) as f:
        expected = f.read()
    if body != expected:
        import difflib
        delta = "\n".join(difflib.unified_diff(
            expected.splitlines(), body.splitlines(),
            fromfile="golden/%s.md (committed)" % name,
            tofile="produced now", lineterm=""))
        pytest.fail("the comment a reviewer would read changed for %r.\n"
                    "If intended, say WHY in the PR description and "
                    "regenerate with UPDATE_GOLDEN=1.\n\n%s" % (name, delta))


# -- Shared fixtures: realistic diff section bodies --------------------------

ORDINARY = (
    "--- \n+++ \n@@ -10,7 +10,7 @@\n     spec:\n       containers:\n"
    "-        image: appspace-ms:2603.0.0\n"
    "+        image: appspace-ms:2603.1.0\n         ports:\n")

TRUE_DELETION = (
    "--- \n+++ \n@@ -1,5 +0,0 @@\n"
    "-apiVersion: v1\n-kind: Service\n-metadata:\n-  name: gone\n-spec: {}\n")

CI_HDR = "/compute.cnrm.cloud.google.com/ComputeInstance pv-acme-a/pv-acme-svc-a"
CD_HDR = "/compute.cnrm.cloud.google.com/ComputeDisk pv-acme-a/pv-acme-svc-a-data"

MACHINE_TYPE_LIVE = (
    "--- \n+++ \n@@ -40,9 +40,9 @@\n"
    "   resourceID: \"pv-acme-svc-a\"\n"
    "   zone: \"us-central1-a\"\n"
    "-  machineType: \"n2d-standard-4\"\n"
    "+  machineType: \"n2d-standard-8\"\n"
    "   canIpForward: false\n"
    "   desiredStatus: \"RUNNING\"\n")

MACHINE_TYPE_STOPPED = (
    "--- \n+++ \n@@ -40,9 +40,9 @@\n"
    "   resourceID: \"pv-acme-svc-a\"\n"
    "-  machineType: \"n2d-standard-4\"\n"
    "-  desiredStatus: \"RUNNING\"\n"
    "+  machineType: \"n2d-standard-8\"\n"
    "+  desiredStatus: \"TERMINATED\"\n"
    "   canIpForward: false\n")

DISK_GROW = (
    "--- \n+++ \n@@ -30,7 +30,7 @@\n"
    "   location: \"us-central1-a\"\n"
    "   physicalBlockSizeBytes: 4096\n"
    "-  size: 500\n"
    "+  size: 1000\n"
    "   type: \"pd-ssd\"\n")

DISK_SHRINK = (
    "--- \n+++ \n@@ -30,7 +30,7 @@\n"
    "   location: \"us-central1-a\"\n"
    "-  size: 1000\n"
    "+  size: 500\n"
    "   type: \"pd-ssd\"\n")

POLICY_FLIP = (
    "--- \n+++ \n@@ -20,9 +20,9 @@\n"
    "     app.kubernetes.io/part-of: linux-svc\n"
    "-    cnrm.cloud.google.com/deletion-policy: abandon\n"
    "+    cnrm.cloud.google.com/deletion-policy: delete\n"
    "     cnrm.cloud.google.com/state-into-spec: absent\n"
    "@@ -80,7 +80,7 @@\n"
    "   shieldedInstanceConfig:\n"
    "-  deletionProtection: true\n"
    "+  deletionProtection: false\n"
    "   attachedDisk:\n")

VM_DELETED = (
    "--- \n+++ \n@@ -1,6 +0,0 @@\n"
    "-apiVersion: compute.cnrm.cloud.google.com/v1beta1\n"
    "-kind: ComputeInstance\n"
    "-metadata:\n"
    "-  name: pv-acme-svc-a\n"
    "-spec:\n"
    "-  machineType: \"n2d-standard-4\"\n")

OLD_CUSTOMER = (
    "appspace:\n"
    "  infra:\n"
    "    deployLinuxServicesK8s:\n"
    "      enabled: true\n"
    "      svc:\n"
    "        enabled: true\n"
    "        machineType: n2d-standard-4\n")

NEW_CUSTOMER = (
    "appspace:\n"
    "  infra:\n"
    "    deployLinuxServicesK8s:\n"
    "      enabled: true\n"
    "      svc:\n"
    "        enabled: true\n"
    "        machineType: n2d-standard-8\n"
    "        allowDeletion: true\n")

NO_VM_OLD = ("appspace:\n  microservices:\n    definitions:\n      web:\n"
             "        version: 1.0.0\n")
NO_VM_NEW = ("appspace:\n  microservices:\n    definitions:\n      web:\n"
             "        version: 1.1.0\n")


def _fetch_stub(table):
    def fake(path, sha, repo=None):
        try:
            return table[(path, sha)], m.BB_OK
        except KeyError:
            return "", None
    return fake


# -- 1. VM panel: rendered-level fact extraction ------------------------------

def test_vm_facts_machine_type_change_without_stop_is_dangerous():
    facts = m._detect_vm_changes([(CI_HDR, MACHINE_TYPE_LIVE)])
    assert len(facts) == 1
    f = facts[0]
    assert f["kind"] == "ComputeInstance"
    assert ("machineType", "n2d-standard-4", "n2d-standard-8") in f["fields"]
    assert f["dangerous"], \
        "machineType moving on a VM not parked TERMINATED must be dangerous"


def test_vm_facts_machine_type_change_with_stop_is_not_dangerous():
    facts = m._detect_vm_changes([(CI_HDR, MACHINE_TYPE_STOPPED)])
    assert len(facts) == 1
    assert not facts[0]["dangerous"]
    assert ("desiredStatus", "RUNNING", "TERMINATED") in facts[0]["fields"]


def test_vm_facts_disk_grow_is_routine_shrink_is_dangerous():
    grow = m._detect_vm_changes([(CD_HDR, DISK_GROW)])
    shrink = m._detect_vm_changes([(CD_HDR, DISK_SHRINK)])
    assert grow and not grow[0]["dangerous"]
    assert ("size", "500", "1000") in grow[0]["fields"]
    assert shrink and shrink[0]["dangerous"]


def test_vm_facts_deletion_policy_flip_is_dangerous():
    facts = m._detect_vm_changes([(CI_HDR, POLICY_FLIP)])
    assert facts and facts[0]["dangerous"]
    assert "deletion" in " ".join(facts[0]["dangerous"]).lower()


def test_vm_facts_whole_instance_deletion_is_dangerous():
    facts = m._detect_vm_changes([(CI_HDR, VM_DELETED)])
    assert facts and facts[0]["deleted"] and facts[0]["dangerous"]


def test_non_vm_sections_never_produce_vm_facts():
    lookalike = ("--- \n+++ \n@@ -5,7 +5,7 @@\n   spec:\n"
                 "-  size: 100\n+  size: 200\n   type: \"pd-ssd\"\n")
    secs = [("/apps/Deployment broadcast", lookalike),
            ("/v1/Service web", lookalike),
            ("/apps/Deployment svc", ORDINARY)]
    assert m._detect_vm_changes(secs) == []


def test_package_sections_carries_vm_facts_and_reserves_display_slot():
    filler = [("/apps/Deployment svc-%02d" % i, ORDINARY) for i in range(10)]
    secs = filler + [(CI_HDR, MACHINE_TYPE_LIVE)]
    out = m._package_sections(secs)
    vm_facts = out[6]
    assert vm_facts and vm_facts[0]["kind"] == "ComputeInstance"
    assert CI_HDR in out[0], "the VM section must hold one of the display slots"


# -- 1b. VM panel: values level + assembly ------------------------------------

def test_vm_panel_values_level(monkeypatch):
    path = "gcp/dev/pv-acme-a/customer.yaml"
    monkeypatch.setattr(m, "_bb_fetch_cached", _fetch_stub({
        (path, PR_SHA): NEW_CUSTOMER, (path, BASE_SHA): OLD_CUSTOMER}))
    lines = m._summarize_vm_changes([path], PR_SHA, BASE_SHA,
                                    {path: ["argocd/pv-acme-a-ss"]}, {})
    body = "\n".join(lines)
    assert "VM INFRASTRUCTURE" in body
    assert "pv-acme-a" in body
    assert "svc" in body
    assert "n2d-standard-4" in body and "n2d-standard-8" in body
    assert "allowDeletion" in body


def test_vm_panel_rendered_level_only():
    secs = [(CI_HDR, MACHINE_TYPE_LIVE)]
    results = {"pv-acme-a-ss": _result(
        MACHINE_TYPE_LIVE, secs, 1, True, outcome=m.OUT_DIFF,
        vm_changes=m._detect_vm_changes(secs))}
    lines = m._summarize_vm_changes([], PR_SHA, BASE_SHA, {}, results)
    body = "\n".join(lines)
    assert "VM INFRASTRUCTURE" in body
    assert "machineType" in body and "n2d-standard-8" in body


def test_vm_panel_reports_no_changes_when_domain_untouched(monkeypatch):
    """Superseded contract: the panel used to return [] here. Operators
    asked for a fixed place to look, so silence is no longer acceptable --
    see test_vm_section_is_always_present_even_with_no_vm_changes."""
    path = "gcp/dev/pv-acme-a/customer.yaml"
    monkeypatch.setattr(m, "_bb_fetch_cached", _fetch_stub({
        (path, PR_SHA): NO_VM_NEW, (path, BASE_SHA): NO_VM_OLD}))
    lines = m._summarize_vm_changes([path], PR_SHA, BASE_SHA,
                                    {path: ["argocd/pv-acme-a-ss"]}, {})
    assert lines[0] == m._VM_PANEL_CLEAN_HDR
    assert "\U0001f6a8" not in "\n".join(lines), "no false alarm"


def test_vm_panel_position_between_decommission_and_downgrade():
    body = m.format_comment(PR_SHA, {
        "pv-acme-a-ms": _result(ORDINARY, [("/apps/Deployment b", ORDINARY)],
                                1, True, outcome=m.OUT_DIFF,
                                version_change=("2603.1.0", "2603.0.0")),
    }, base_sha=BASE_SHA,
        decommission_lines=["## DECOMMISSION SENTINEL", ""],
        vm_change_lines=["## VM INFRASTRUCTURE SENTINEL", ""])
    i_dec = body.index("DECOMMISSION SENTINEL")
    i_vm = body.index("VM INFRASTRUCTURE SENTINEL")
    i_down = body.index("CHART VERSION DOWNGRADE")
    assert i_dec < i_vm < i_down


def test_golden_vm_panel(monkeypatch):
    path = "gcp/prod/pv-acme-a/customer.yaml"
    monkeypatch.setattr(m, "_bb_fetch_cached", _fetch_stub({
        (path, PR_SHA): NEW_CUSTOMER, (path, BASE_SHA): OLD_CUSTOMER}))
    secs = [(CI_HDR, MACHINE_TYPE_LIVE)]
    results = {"pv-acme-a-ss": _result(
        MACHINE_TYPE_LIVE, secs, 1, True, outcome=m.OUT_DIFF,
        vm_changes=m._detect_vm_changes(secs))}
    vm_lines = m._summarize_vm_changes([path], PR_SHA, BASE_SHA,
                                       {path: ["argocd/pv-acme-a-ss"]}, results)
    body = m.format_comment(PR_SHA, results, base_sha=BASE_SHA,
                            vm_change_lines=vm_lines)
    assert "VM INFRASTRUCTURE" in body
    _assert_golden("vm_change_panel", body)


# -- 2. Readable-size budget ---------------------------------------------------

def _distinct_change(i, pad_lines=120):
    ctx = "".join("     pad-%03d-%02d: value\n" % (i, j)
                  for j in range(pad_lines))
    return ("--- \n+++ \n@@ -10,%d +10,%d @@\n" % (pad_lines + 3, pad_lines + 3)
            + "     env: pv-big-%03d\n" % i
            + "-  replicas: 2\n+  replicas: 3\n" + ctx)


def test_readable_budget_collapses_bulk_keeps_risk_and_links_artifact():
    results = {}
    for i in range(40):
        body_i = _distinct_change(i)
        secs = [("/apps/Deployment broadcast", body_i)]
        results["pv-big-%03d-ms" % i] = _result(
            body_i, secs, 1, True, outcome=m.OUT_DIFF,
            fingerprint=m._fingerprint_sections(secs))
    risk_secs = [("/v1/Service gone", TRUE_DELETION)]
    results["pv-zzz-risky-ms"] = _result(
        TRUE_DELETION, risk_secs, 1, True, outcome=m.OUT_DIFF,
        deleted_resources=["/v1/Service gone"],
        fingerprint=m._fingerprint_sections(risk_secs))
    body = m.format_comment(PR_SHA, results, base_sha=BASE_SHA,
                            artifact_url=ART_URL)
    nbytes = len(body.encode("utf-8"))
    assert nbytes < m.COMMENT_READABLE_BYTES + 12_000, nbytes
    assert ART_URL in body
    assert "[clean] [base:00001111]" in body
    tail = body.split("pv-zzz-risky-ms")[-1]
    # COPS-2612: the guarantee was "a risk-flagged app is never folded away
    # by the budget", and it holds unchanged -- but what proves it is no
    # longer a fence. The deleted resource is still NAMED in the comment
    # (that is the guard firing); its hunk is on the page. Split so the
    # test says which half is which, because collapsing the two is how a
    # real regression would slip through as a golden update.
    assert "/v1/Service gone" in body, \
        "a deletion must still be named in the comment"
    assert "```diff" not in tail, "the hunk itself moved to the page"
    inline = m.format_comment(PR_SHA, results, base_sha=BASE_SHA,
                              artifact_url=ART_URL,
                              profile=m.COMMENT_PROFILE.replace(
                                  inline_diffs=True))
    itail = inline.split("pv-zzz-risky-ms")[-1]
    assert "```diff" in itail and "/v1/Service gone" in itail, \
        "a risk-flagged app must keep its full diff even past the budget"
    assert "41 resource(s) will change" in body
    assert body.count("| \u26a0\ufe0f changed |") <= m._OVERVIEW_TABLE_MAX_ROWS


def test_artifact_url_only_adds_the_full_view_lines():
    """COPS-2609 deliberately changed this contract.

    Under COPS-2605 the artifact URL was used only by the truncation note,
    so passing it changed nothing on a comment that fitted the budget --
    which is precisely why a comment where nothing was truncated had no way
    to reach the full-diff page at all. The URL is now rendered in two fixed
    places on every comment.

    What must still hold is the original spirit: the URL perturbs nothing
    *else*. Stripping the two full-view lines gives back the body rendered
    without a URL, modulo the unavailable-page notice that replaces them.
    """
    results = {"pv-acme-a-ms": _result(ORDINARY,
                                       [("/apps/Deployment b", ORDINARY)],
                                       1, True, outcome=m.OUT_DIFF)}
    a = m.format_comment(PR_SHA, results, base_sha=BASE_SHA)
    b = m.format_comment(PR_SHA, results, base_sha=BASE_SHA,
                         artifact_url=ART_URL)
    assert ART_URL in b, "the full-diff page must be reachable from the body"
    assert ART_URL not in a

    # COPS-2612 changed this contract again, and deliberately. The URL is no
    # longer decoration on an otherwise fixed body: it is the precondition
    # for moving the YAML off the comment. Without it there is no page to
    # move to, so the comment keeps the hunks and says why. The two bodies
    # are therefore SUPPOSED to differ by more than two lines now.
    #
    # What still holds, and is what this test now pins, is the invariant
    # underneath: the page-less body is the strictly larger one, and every
    # app named in one is named in the other. Losing a URL may cost brevity;
    # it may never cost information.
    # COPS-2636 moved the with-URL naming into the overview table, so
    # comparing the LINE SHAPES no longer works; the invariant is about
    # the APPS, and it is pinned by name.
    for _app in results:
        assert _app in a and _app in b, \
            "the same apps must be named whether or not a page exists"
    assert "```diff" in a, "no page means the comment keeps the evidence"
    assert "```diff" not in b, "a page means the evidence lives there"
    assert len(a) > len(b), \
        "the fallback body is the larger one; that is the whole point"


def test_truncate_comment_links_artifact():
    filler = "x" * 300_000
    body = ("## head\n\n" + filler +
            "\n---\n**Status:** ok\n*ts \u2014 acme-diff-preview "
            "[clean] [base:00001111]*")
    out = m._truncate_comment(body, artifact_url=ART_URL)
    assert len(out.encode()) <= m.MAX_COMMENT_BYTES
    assert ART_URL in out
    assert "[clean] [base:00001111]" in out
    legacy = m._truncate_comment(body)
    assert ART_URL not in legacy


def test_golden_readable_budget_collapse(monkeypatch):
    monkeypatch.setattr(m, "COMMENT_READABLE_BYTES", 2_500)
    results = {}
    for i in range(6):
        # Each block must be big enough that the running body crosses the
        # patched budget partway through the loop: the point of the golden
        # is to freeze a comment where SOME diffs render in full and the
        # rest collapse into the pointer line.
        body_i = _distinct_change(i, pad_lines=20)
        secs = [("/apps/Deployment broadcast", body_i)]
        results["pv-env-%d-ms" % i] = _result(
            body_i, secs, 1, True, outcome=m.OUT_DIFF,
            fingerprint=m._fingerprint_sections(secs))
    body = m.format_comment(PR_SHA, results, base_sha=BASE_SHA,
                            artifact_url=ART_URL)
    assert ART_URL in body
    _assert_golden("readable_budget_collapse", body)


# -- 3. Routine-bump rollup ------------------------------------------------------

def _bump_result(i):
    body = ("--- \n+++ \n@@ -10,8 +10,8 @@\n     spec:\n"
            "     env: pv-fleet-%02d\n" % i
            + "-        image: appspace-ms:2603.0.0\n"
            "+        image: appspace-ms:2603.1.0\n         ports:\n")
    secs = [("/apps/Deployment broadcast", body)]
    return _result(body, secs, 1, True, outcome=m.OUT_DIFF,
                   fingerprint=m._fingerprint_sections(secs))


def test_routine_bump_signature_classifier():
    assert m._routine_bump_signature(_bump_result(0)) is not None
    mixed_body = ("--- \n+++ \n@@ -10,8 +10,8 @@\n"
                  "-        image: appspace-ms:2603.0.0\n"
                  "+        image: appspace-ms:2603.1.0\n"
                  "-  replicas: 2\n+  replicas: 3\n")
    mixed = _result(mixed_body, [("/apps/Deployment b", mixed_body)], 1, True,
                    outcome=m.OUT_DIFF)
    assert m._routine_bump_signature(mixed) is None
    down = _result(ORDINARY, [("/apps/Deployment b", ORDINARY)], 1, True,
                   outcome=m.OUT_DIFF, version_change=("2603.1.0", "2603.0.0"))
    assert m._routine_bump_signature(down) is None
    risky = _result(TRUE_DELETION, [("/v1/Service gone", TRUE_DELETION)], 1,
                    True, outcome=m.OUT_DIFF,
                    deleted_resources=["/v1/Service gone"])
    assert m._routine_bump_signature(risky) is None


def test_routine_bumps_collapse_to_one_line():
    results = {"pv-fleet-%02d-ms" % i: _bump_result(i) for i in range(5)}
    body = m.format_comment(PR_SHA, results, base_sha=BASE_SHA)
    assert "```diff" not in body, "routine bumps must not render diff blocks"
    assert "5 environments" in body
    assert "2603.0.0" in body and "2603.1.0" in body
    assert "5 resource(s) will change" in body


def test_dangerous_change_never_folds_into_rollup():
    results = {"pv-fleet-%02d-ms" % i: _bump_result(i) for i in range(4)}
    risky_secs = [("/apps/Deployment broadcast", _bump_result(9).text),
                  ("/v1/Service gone", TRUE_DELETION)]
    results["pv-fleet-99-ms"] = _result(
        TRUE_DELETION, risky_secs, 2, True, outcome=m.OUT_DIFF,
        deleted_resources=["/v1/Service gone"],
        fingerprint=m._fingerprint_sections(risky_secs))
    body = m.format_comment(PR_SHA, results, base_sha=BASE_SHA)
    assert "4 environments" in body
    tail = body.split("pv-fleet-99-ms")[-1]
    assert "```diff" in tail, "the risky app must stay fully enumerated"
    assert "RESOURCE(S) DELETED" in body


def test_no_rollup_below_three_groups():
    results = {"pv-fleet-%02d-ms" % i: _bump_result(i) for i in range(2)}
    body = m.format_comment(PR_SHA, results, base_sha=BASE_SHA)
    assert body.count("```diff") == 2


def test_identical_fingerprint_group_keeps_representative_diff():
    secs = [("/apps/Deployment broadcast", ORDINARY)]
    fp = m._fingerprint_sections(secs)
    results = {"pv-env-%02d-ms" % i: _result(ORDINARY, secs, 1, True,
                                             outcome=m.OUT_DIFF,
                                             fingerprint=fp)
               for i in range(6)}
    body = m.format_comment(PR_SHA, results, base_sha=BASE_SHA)
    assert body.count("```diff") == 1
    assert "Identical diff across" in body


def test_golden_routine_bump_rollup():
    results = {"pv-fleet-%02d-ms" % i: _bump_result(i) for i in range(5)}
    body = m.format_comment(PR_SHA, results, base_sha=BASE_SHA)
    _assert_golden("routine_bump_rollup", body)


# -- 4. Merge summary + always-on VM section -----------------------------------
# Audit of the last 40 merged acme-config-prod PRs (2026-08):
#   * 20/40 are version bumps, 10/40 truncated at the 245KB wall;
#   * 6 decommission-phase PRs (arm cascade / arm data purge / arm VM
#     deletion) produced 489-571 byte comments with NO panel at all -- the
#     most destructive changes in the fleet were the quietest ones;
#   * "GCP unify guard: Windows/LinuxVM off" switched VMs off with no VM
#     panel anywhere.
# The merge summary exists so an operator can decide "is this safe to
# merge?" from the first screen, in every one of those shapes.

def test_vm_section_is_always_present_even_with_no_vm_changes(monkeypatch):
    path = "gcp/prod/pv-acme-a/customer.yaml"
    monkeypatch.setattr(m, "_bb_fetch_cached", _fetch_stub({
        (path, PR_SHA): NO_VM_NEW, (path, BASE_SHA): NO_VM_OLD}))
    lines = m._summarize_vm_changes([path], PR_SHA, BASE_SHA,
                                    {path: ["argocd/pv-acme-a-ss"]}, {})
    body = "\n".join(lines)
    assert lines, "the VM section must always render, even when untouched"
    assert "VM" in body
    assert "no changes" in body.lower()


def test_vm_instance_type_and_disk_type_are_highlighted(monkeypatch):
    path = "gcp/prod/pv-acme-a/customer.yaml"
    old = ("appspace:\n  infra:\n    deployLinuxServicesK8s:\n      svc:\n"
           "        machineType: n2d-standard-4\n"
           "        dataDiskType: pd-ssd\n")
    new = ("appspace:\n  infra:\n    deployLinuxServicesK8s:\n      svc:\n"
           "        machineType: n2d-standard-8\n"
           "        dataDiskType: pd-balanced\n")
    monkeypatch.setattr(m, "_bb_fetch_cached", _fetch_stub({
        (path, PR_SHA): new, (path, BASE_SHA): old}))
    body = "\n".join(m._summarize_vm_changes(
        [path], PR_SHA, BASE_SHA, {path: ["argocd/pv-acme-a-ss"]}, {}))
    assert "machineType" in body and "n2d-standard-8" in body
    assert "dataDiskType" in body and "pd-balanced" in body
    assert body.count("\U0001f6a8") >= 2, \
        "instance-type and disk-type changes must both be flagged"
    assert "immutable" in body


def _merge_summary_of(body):
    """The summary block, as an operator sees it above everything else."""
    assert "Merge summary" in body, "every comment needs the merge summary"
    return body.split("Merge summary", 1)[1].split("\n---", 1)[0]


def test_merge_summary_blocks_on_real_deletions():
    secs = [("/v1/Service gone", TRUE_DELETION)]
    results = {"pv-acme-a-ms": _result(
        TRUE_DELETION, secs, 1, True, outcome=m.OUT_DIFF,
        deleted_resources=["/v1/Service gone"])}
    body = m.format_comment(PR_SHA, results, base_sha=BASE_SHA)
    head = _merge_summary_of(body)
    assert "DO NOT MERGE" in head
    assert "delet" in head.lower()
    assert "pv-acme-a" in head


def test_merge_summary_is_green_for_a_pure_routine_bump():
    results = {"pv-fleet-%02d-ms" % i: _bump_result(i) for i in range(5)}
    body = m.format_comment(PR_SHA, results, base_sha=BASE_SHA)
    head = _merge_summary_of(body)
    assert "DO NOT MERGE" not in head
    assert "\u2705" in head
    # Point 3: operators speak in environments and versions.
    assert "5 environment" in head
    assert "2603.1.0" in head


def test_merge_summary_names_the_environments_jumping_version():
    results = {"pv-fleet-%02d-ms" % i: _bump_result(i) for i in range(3)}
    body = m.format_comment(PR_SHA, results, base_sha=BASE_SHA)
    head = _merge_summary_of(body)
    for env in ("pv-fleet-00", "pv-fleet-01", "pv-fleet-02"):
        assert env in head, "the summary must name the environments"
    assert "-ms" not in head.split("environment")[0][-80:], \
        "operators read environments, not ArgoCD app names"


def test_merge_summary_surfaces_a_quiet_decommission_arming():
    """PR #3892 shape: a config-only change that arms destruction and
    rendered a 571-byte all-green comment."""
    results = {"pv-adaptive-b-ms": _result(outcome=m.OUT_NO_DIFF)}
    body = m.format_comment(
        PR_SHA, results, base_sha=BASE_SHA,
        vm_change_lines=[m._VM_PANEL_DANGER_HDR, "",
                         "- \U0001f6a8 `pv-adaptive-b` \u00b7 **defaults**: "
                         "**added** `defaults.allowDeletion` = `True`", ""])
    head = _merge_summary_of(body)
    assert "DO NOT MERGE" in head
    assert "VM" in head
    assert "No manifest changes" not in head


def test_merge_summary_flags_downgrades_and_zeroed_replicas():
    results = {
        "pv-a-ms": _result(ORDINARY, [("/apps/Deployment b", ORDINARY)], 1,
                           True, outcome=m.OUT_DIFF,
                           version_change=("2603.1.0", "2603.0.0")),
        "pv-b-ms": _result(ORDINARY, [("/apps/Deployment c", ORDINARY)], 1,
                           True, outcome=m.OUT_DIFF,
                           replicas_zeroed=["/apps/Deployment c"]),
    }
    head = _merge_summary_of(m.format_comment(PR_SHA, results,
                                              base_sha=BASE_SHA))
    assert "downgrade" in head.lower()
    assert "replica" in head.lower()
    assert "\u26a0" in head or "DO NOT MERGE" in head


def test_merge_summary_survives_a_clean_no_change_pr():
    results = {"pv-a-ms": _result(outcome=m.OUT_NO_DIFF)}
    head = _merge_summary_of(m.format_comment(PR_SHA, results,
                                              base_sha=BASE_SHA))
    assert "\u2705" in head
    assert "DO NOT MERGE" not in head


def test_golden_merge_summary_mixed():
    """One PR carrying a bump fleet, a deletion and a VM change at once."""
    results = {"pv-fleet-%02d-ms" % i: _bump_result(i) for i in range(4)}
    risky = [("/v1/Service gone", TRUE_DELETION)]
    results["pv-zzz-risky-ms"] = _result(
        TRUE_DELETION, risky, 1, True, outcome=m.OUT_DIFF,
        deleted_resources=["/v1/Service gone"],
        fingerprint=m._fingerprint_sections(risky))
    vm_secs = [(CI_HDR, MACHINE_TYPE_LIVE)]
    results["pv-vm-a-ss"] = _result(
        MACHINE_TYPE_LIVE, vm_secs, 1, True, outcome=m.OUT_DIFF,
        vm_changes=m._detect_vm_changes(vm_secs),
        fingerprint=m._fingerprint_sections(vm_secs))
    vm_lines = m._summarize_vm_changes([], PR_SHA, BASE_SHA, {}, results)
    body = m.format_comment(PR_SHA, results, base_sha=BASE_SHA,
                            vm_change_lines=vm_lines, artifact_url=ART_URL)
    _assert_golden("merge_summary_mixed", body)


def test_full_input_panel_covers_every_file_uncapped(monkeypatch):
    """Point 7: the full-diff view must detail every changed file, while
    the comment keeps its tight caps."""
    table, paths = {}, []
    for i in range(14):
        p = "gcp/prod/pv-env-%02d/customer.yaml" % i
        paths.append(p)
        table[(p, BASE_SHA)] = "appspace:\n  version: 2603.0.0\n"
        table[(p, PR_SHA)] = "appspace:\n  version: 2603.1.0\n"
    monkeypatch.setattr(m, "_bb_fetch_cached", _fetch_stub(table))
    capped = "\n".join(m._summarize_input_changes(paths, PR_SHA, BASE_SHA))
    full = "\n".join(m._summarize_input_changes(paths, PR_SHA, BASE_SHA,
                                                full=True))
    assert "pv-env-13" not in capped, "the comment stays capped"
    for i in range(14):
        assert "pv-env-%02d" % i in full, "the full view must show every file"
    assert len(full) > len(capped)


# -- 5. Format findings from 50 real acme-config-prod comments ----------------

def test_legacy_deployLinuxServices_machine_spec_is_detected(monkeypatch):
    """acme-config-prod PR #3844, "Change the Deloitte svc machine spec":
    appspace.infra.deployLinuxServices.machineType went from
    n2d-custom-16-49152 to n2-custom-12-49152 (a downsize) and the posted
    comment said "No manifest changes". The legacy non-KCC key path is as
    live as the K8s one (x9 vs x17 across the last 50 PRs), so missing it
    is exactly the silent failure the VM panel exists to prevent."""
    path = "gcp/prod/private-cloud/au1-b/monthly/pv-deloitte-c/customer.yaml"
    old = ("appspace:\n  infra:\n    deployLinuxServices:\n"
           "      machineType: n2d-custom-16-49152\n")
    new = ("appspace:\n  infra:\n    deployLinuxServices:\n"
           "      machineType: n2-custom-12-49152\n      dataDiskSize: 500\n")
    monkeypatch.setattr(m, "_bb_fetch_cached", _fetch_stub({
        (path, PR_SHA): new, (path, BASE_SHA): old}))
    body = "\n".join(m._summarize_vm_changes(
        [path], PR_SHA, BASE_SHA, {path: ["argocd/pv-deloitte-c-ss"]}, {}))
    assert "n2d-custom-16-49152" in body and "n2-custom-12-49152" in body
    assert "\U0001f6a8" in body, "an unparked machineType change is dangerous"
    assert "legacy" in body, "say which VM domain the key belongs to"


def test_windows_vm_disable_is_detected(monkeypatch):
    """PR #3858 "GCP unify guard: Windows/LinuxVM off" switched VMs off
    with no VM wording anywhere in the comment."""
    path = "gcp/prod/private-cloud/na1-a/pv-x-a/customer.yaml"
    monkeypatch.setattr(m, "_bb_fetch_cached", _fetch_stub({
        (path, BASE_SHA): "appspace:\n  infra:\n    deployWindows:\n      enabled: true\n",
        (path, PR_SHA): "appspace:\n  infra:\n    deployWindows:\n      enabled: false\n"}))
    body = "\n".join(m._summarize_vm_changes(
        [path], PR_SHA, BASE_SHA, {path: ["argocd/pv-x-a-ss"]}, {}))
    assert "Windows" in body and "\U0001f6a8" in body


def test_config_panel_separates_its_bullets_from_the_file_heading(monkeypatch):
    """44 of the last 50 comments render their cause bullets INLINE into
    the file heading because no blank line separates them (verified in the
    browser on PR #3893). Markdown needs the blank line."""
    path = "gcp/prod/pv-env-a/customer.yaml"
    monkeypatch.setattr(m, "_bb_fetch_cached", _fetch_stub({
        (path, BASE_SHA): "appspace:\n  version: 2603.0.0\n",
        (path, PR_SHA): "appspace:\n  version: 2603.1.0\n"}))
    lines = m._summarize_input_changes([path], PR_SHA, BASE_SHA)
    for i, l in enumerate(lines):
        if l.startswith("- ") and i:
            prev = lines[i - 1]
            assert prev.strip() == "" or prev.startswith("- "), (
                "bullet %r is glued to %r and will render inline" % (l, prev))


def test_no_golden_comment_has_a_markdown_rendering_hazard():
    """Corpus guard. Measured on the last 50 merged acme-config-prod
    comments: 44/50 rendered bullets inline because they were glued to the
    line above, and 5/50 carried a >350-char prose wall. Both are invisible
    in the raw markdown and only show up in the browser, so they get an
    automated check rather than another round of manual review."""
    import glob
    import re as _re

    def prose(txt):
        out, fence = [], False
        for line in txt.split("\n"):
            if line.startswith("```"):
                fence = not fence
                continue
            if not fence:
                out.append(line)
        return out

    problems = []
    for path in sorted(glob.glob(os.path.join(GOLDEN_DIR, "*.md"))):
        name = os.path.basename(path)
        if name == "README.md":
            continue          # documentation, not a rendered comment
        with open(path) as f:
            lines = prose(f.read())
        for i, line in enumerate(lines):
            if (_re.match(r"^\s*[-*] ", line) and i and lines[i - 1].strip()
                    and not _re.match(r"^\s*[-*] ", lines[i - 1])
                    and not lines[i - 1].lstrip().startswith(("|", ">"))):
                problems.append("%s: bullet glued to %r \u2014 markdown will "
                                "inline it" % (name, lines[i - 1][:60]))
            if len(line) > 350:
                problems.append("%s: %d-char prose wall \u2014 wraps into an "
                                "unreadable block" % (name, len(line)))
    assert not problems, "rendering hazards:\n" + "\n".join(problems)


def test_merge_summary_never_contradicts_an_armed_state_panel():
    """acme-config-dev PR #7024, real output: the body shouted
    "DECOMMISSION ARMED" while the summary said "Routine - nothing
    dangerous detected" and the footer said "No manifest changes". Arming
    destruction is config-only, so it never reaches the manifest diff --
    the summary has to read the state panel."""
    results = {"pv-qa-13-a-ms": _result(outcome=m.OUT_NO_DIFF)}
    armed = m.format_comment(
        PR_SHA, results, base_sha=BASE_SHA,
        appspace_state_lines=["## \U0001f512\u26a0\ufe0f DECOMMISSION ARMED "
                              "for `pv-qa-13-a` \u26a0\ufe0f\U0001f512", ""])
    head = _merge_summary_of(armed)
    assert "DO NOT MERGE" in head
    assert "Decommission ARMED" in head
    assert "nothing dangerous detected" not in head

    purge = m.format_comment(
        PR_SHA, results, base_sha=BASE_SHA,
        appspace_state_lines=["## \U0001f6a8 PURGE ARMED for "
                              "already-decommissioned `pv-x` \U0001f6a8", ""])
    head_p = _merge_summary_of(purge)
    assert "DO NOT MERGE" in head_p
    assert "purge" in head_p.lower()


def test_merge_summary_treats_disarming_as_safe():
    results = {"pv-a-ms": _result(outcome=m.OUT_NO_DIFF)}
    head = _merge_summary_of(m.format_comment(
        PR_SHA, results, base_sha=BASE_SHA,
        appspace_state_lines=["### \U0001f513 Decommission DISARMED for "
                              "`pv-a`", ""]))
    assert "DO NOT MERGE" not in head
    assert "disarmed" in head.lower()
