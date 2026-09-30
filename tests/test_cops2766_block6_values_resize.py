"""COPS-2766 block 6, item 4: the values-level machineType lets the render decide.

The values panel flagged every KCC `machineType` key change as a resize. Four
real prod PRs were false positives: #4640 (dpdhl, rabbit added while `enabled`
exists at base), #4406 (chaostest, rabbit next to svc), #4400 (heb, mongo
added) and #3784 (wca34, the dead top-level machineType moved to svc). All
four are GCP envs that render, and the render shows a new CR or nothing.

Now, when every app mapped to the file rendered, the rendered level decides:
it flags a real ComputeInstance resize once (block 6 S2), and the values line
is routine. When the render cannot confirm it (no render, a failed app, an app
not diffed) the values line keeps the danger with the KCC resize reason.

The role bug goes too: for a key right under the prefix the old code read
`<prefix>machineType.desiredStatus`, and a TERMINATED escape no KCC resize
needs any more. ASO on Azure reads no desiredStatus: every resize there is
flagged, with its own text. Legacy Terraform and Windows keep the rule and
text of main: the render level cannot see those machines, and the Terraform
path is being replaced by KCC and ASO.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m  # noqa: E402

PR_SHA, BASE_SHA = "pr6s3", "base6s3"
LEGACY_REASON = ("machineType changes while desiredStatus is not TERMINATED "
                 "\u2014 the runbook requires stopping the VM first")

# ── the four false positives, rebuilt from the real diffs ────────────────

DPDHL = "gcp/prod/private-cloud/eu1-b/monthly-friday/pv-dpdhl-c/customer.yaml"
DPDHL_OLD = (
    "appspace:\n"
    "  customerName: dpdhl\n"
    "  infra:\n"
    "    deployLinuxServicesK8s:\n"
    "      enabled: false\n")
DPDHL_NEW = (
    "appspace:\n"
    "  customerName: dpdhl\n"
    "  infra:\n"
    "    deployLinuxServicesK8s:\n"
    "      enabled: true\n"
    "      defaults:\n"
    "        vmTags:\n"
    "          - iap-mongo\n"
    "      extraLabels:\n"
    "        instance-name: dpdhl\n"
    "      rabbit:\n"
    "        enabled: true\n"
    "        ignoreDesiredStatus: true\n"
    "        machineType: n2d-standard-8\n"
    "        createNewBootDisk: false\n"
    "        manageMetadata: false\n"
    "        instances:\n"
    "          - name: pv-dpdhl-rab1-a\n"
    "            internalIpAddress: \"10.132.0.196\"\n")

CHAOS = "gcp/prod/private-cloud/nachaos-a/accelerated/pv-chaostest-a/customer.yaml"
CHAOS_OLD = (
    "appspace:\n"
    "  customerName: chaostest\n"
    "  infra:\n"
    "    noCore: true\n"
    "    deployLinuxServicesK8s:\n"
    "      enabled: true\n"
    "      svc:\n"
    "        enabled: true\n"
    "        machineType: n2d-standard-8\n"
    "        dataDiskSizeGb: 128\n"
    "        createNewBootDisk: false\n"
    "        manageMetadata: false\n")
CHAOS_NEW = CHAOS_OLD + (
    "      rabbit:\n"
    "        enabled: true\n"
    "        machineType: n2d-highmem-2\n"
    "        dataDiskSizeGb: 128\n"
    "        createNewBootDisk: false\n"
    "        manageMetadata: false\n"
    "        instances:\n"
    "          - pv-chaostest-rab1-a\n")

HEB = "gcp/prod/private-cloud/na3-a/monthly/pv-heb-a/customer.yaml"
HEB_OLD = (
    "appspace:\n"
    "  customerName: heb\n"
    "  infra:\n"
    "    deployLinuxServicesK8s:\n"
    "      enabled: true\n"
    "      rabbit:\n"
    "        enabled: true\n"
    "        instances:\n"
    "        - pv-heb-rab1-a\n"
    "        machineType: n2d-standard-4\n"
    "        createNewBootDisk: true\n"
    "        manageMetadata: true\n")
HEB_NEW = HEB_OLD.replace(
    "      enabled: true\n      rabbit:\n",
    "      enabled: true\n"
    "      defaults:\n"
    "        vmTags:\n"
    "          - iap-mongo\n"
    "      rabbit:\n") + (
    "      mongo:\n"
    "        enabled: true\n"
    "        instances:\n"
    "        - pv-heb-mongo4-a\n"
    "        machineType: n2d-standard-4\n"
    "        dataDiskSizeGb: 250\n"
    "        createNewBootDisk: false\n"
    "        manageMetadata: false\n")

WCA = "gcp/prod/private-cloud/sa1-a/weekly/pv-wca34-a/customer.yaml"
WCA_OLD = (
    "appspace:\n"
    "  customerName: wca34\n"
    "  infra:\n"
    "    deployWindows: true\n"
    "    deployLinuxServicesK8s:\n"
    "      enabled: true\n"
    "      deployVM: true\n"
    "      machineType: n2d-standard-2\n")
WCA_NEW = (
    "appspace:\n"
    "  customerName: wca34\n"
    "  infra:\n"
    "    deployWindows: true\n"
    "    deployLinuxServicesK8s:\n"
    "      enabled: true\n"
    "      svc:\n"
    "        machineType: n2d-standard-4\n"
    "        createNewBootDisk: true\n")

FP_SHAPES = {
    "4640-dpdhl-rabbit": (DPDHL, DPDHL_OLD, DPDHL_NEW, 1),
    "4406-chaostest-rabbit": (CHAOS, CHAOS_OLD, CHAOS_NEW, 1),
    "4400-heb-mongo": (HEB, HEB_OLD, HEB_NEW, 1),
    "3784-wca34-key-move": (WCA, WCA_OLD, WCA_NEW, 2),
}


def _env(path):
    return path.split("/")[-2]


def _result(outcome):
    return m.DiffResult("d", [], 0, outcome == m.OUT_DIFF, None, outcome, "r")


def _rendered(path, ss=m.OUT_DIFF, ms=m.OUT_NO_DIFF):
    """path_map and app_results the way process_pr passes them."""
    env = _env(path)
    return ({path: [env + "-ms", env + "-ss"]},
            {env + "-ms": _result(ms), env + "-ss": _result(ss)})


def _panel(monkeypatch, path, old, new, path_map, app_results):
    table = {(path, BASE_SHA): old, (path, PR_SHA): new}

    def fetch(p, sha, repo=None):
        v = table.get((p, sha))
        return (v, m.BB_OK) if v is not None else (None, m.BB_NOT_FOUND)

    monkeypatch.setattr(m, "_bb_fetch_cached", fetch)
    return m._summarize_vm_changes([path], PR_SHA, BASE_SHA, path_map,
                                   app_results)


def _mt_lines(lines):
    return [ln for ln in lines if "machineType`" in ln]


# ── (a) the four shapes ──────────────────────────────────────────────────

@pytest.mark.parametrize("shape", sorted(FP_SHAPES))
def test_a_false_positive_shape_with_a_full_render_is_routine(monkeypatch, shape):
    path, old, new, n = FP_SHAPES[shape]
    lines = _panel(monkeypatch, path, old, new, *_rendered(path))
    text = "\n".join(lines)
    assert lines[0] == m._VM_PANEL_ROUTINE_HDR, text
    assert len(_mt_lines(lines)) == n, text
    for ln in _mt_lines(lines):
        assert ln.startswith("- `%s`" % _env(path)), ln
        assert "\u2014" not in ln, "the render decides, the line has no reason"
    assert m._VM_RESIZE_REASON not in text
    assert "runbook requires" not in text


@pytest.mark.parametrize("shape", sorted(FP_SHAPES))
def test_the_same_shape_without_a_render_keeps_the_danger(monkeypatch, shape):
    path, old, new, n = FP_SHAPES[shape]
    lines = _panel(monkeypatch, path, old, new, _rendered(path)[0], {})
    assert lines[0] == m._VM_PANEL_DANGER_HDR
    mt = _mt_lines(lines)
    assert len(mt) == n
    for ln in mt:
        assert ln.startswith("- \U0001f6a8 ") and ln.endswith(
            "\u2014 " + m._VM_RESIZE_REASON), ln
    assert "runbook requires" not in "\n".join(lines)


@pytest.mark.parametrize("outcome", [m.OUT_INDETERMINATE, m.OUT_ERROR,
                                     m.OUT_DECOMMISSIONED,
                                     m.OUT_LEFTOVER_DECOMMISSIONED])
def test_one_app_that_did_not_render_keeps_the_danger(monkeypatch, outcome):
    path_map, results = _rendered(DPDHL, ss=outcome)
    lines = _panel(monkeypatch, DPDHL, DPDHL_OLD, DPDHL_NEW, path_map, results)
    assert lines[0] == m._VM_PANEL_DANGER_HDR
    assert m._VM_RESIZE_REASON in _mt_lines(lines)[0]


def test_an_app_missing_from_the_results_keeps_the_danger(monkeypatch):
    """Over the cap, or never diffed: nothing confirms the resize."""
    path_map, results = _rendered(CHAOS)
    del results["pv-chaostest-a-ss"]
    lines = _panel(monkeypatch, CHAOS, CHAOS_OLD, CHAOS_NEW, path_map, results)
    assert lines[0] == m._VM_PANEL_DANGER_HDR


def test_a_rendered_resize_is_flagged_once_by_the_render(monkeypatch):
    """A real resize with a full render: the ComputeInstance line carries the
    reason, and the values line is routine. The danger is said once."""
    old = CHAOS_OLD
    new = CHAOS_OLD.replace("machineType: n2d-standard-8",
                            "machineType: n2d-standard-16")
    hdr = ("/compute.cnrm.cloud.google.com/ComputeInstance "
           "pv-chaostest-a/pv-chaostest-svc-a")
    body = ("     zone: us-east1-b\n"
            "-    machineType: n2d-standard-8\n"
            "+    machineType: n2d-standard-16\n")
    path_map, results = _rendered(CHAOS)
    results["pv-chaostest-a-ss"] = m.DiffResult(
        body, [(hdr, body)], 1, True, None, m.OUT_DIFF, "changes",
        vm_changes=m._detect_vm_changes([(hdr, body)]))
    lines = _panel(monkeypatch, CHAOS, old, new, path_map, results)
    text = "\n".join(lines)
    assert lines[0] == m._VM_PANEL_DANGER_HDR
    assert text.count(m._VM_RESIZE_REASON) == 1, text
    ci = next(ln for ln in lines if "ComputeInstance" in ln)
    assert ci.startswith("- \U0001f6a8 ") and m._VM_RESIZE_REASON in ci
    values = next(ln for ln in lines if "`svc.machineType`" in ln)
    assert not values.startswith("- \U0001f6a8") and "\u2014" not in values
    assert lines.index(values) > lines.index("Routine VM changes in the same PR:")


# ── (b) _render_covers ───────────────────────────────────────────────────

def test_render_covers_needs_every_mapped_app_rendered():
    ok = {"a": _result(m.OUT_DIFF), "b": _result(m.OUT_NO_DIFF)}
    assert m._render_covers(["a", "b"], ok) is True
    assert m._render_covers(("a",), ok) is True
    assert m._render_covers({"b"}, ok) is True
    assert m._render_covers(["a", "c"], ok) is False, "c was not diffed"
    bad = dict(ok, b=_result(m.OUT_INDETERMINATE))
    assert m._render_covers(["a", "b"], bad) is False


@pytest.mark.parametrize("apps", [True, None, [], (), "pv-x-ss", 1])
def test_render_covers_is_false_when_the_apps_are_not_a_list(apps):
    """cops2635 passes True as the path_map value: that names no app."""
    ok = {"pv-x-ss": _result(m.OUT_DIFF)}
    assert m._render_covers(apps, ok) is False


@pytest.mark.parametrize("results", [None, {}, []])
def test_render_covers_is_false_with_no_results(results):
    assert m._render_covers(["pv-x-ss"], results) is False


def test_render_covers_finds_an_app_by_its_short_name():
    """The golden harness maps `argocd/pv-x-ss` and keys results `pv-x-ss`."""
    ok = {"pv-x-ss": _result(m.OUT_NO_DIFF)}
    assert m._render_covers(["argocd/pv-x-ss"], ok) is True
    assert m._render_covers(["argocd/pv-y-ss"], ok) is False


def test_a_path_map_value_of_true_is_not_covered(monkeypatch):
    results = _rendered(DPDHL)[1]
    lines = _panel(monkeypatch, DPDHL, DPDHL_OLD, DPDHL_NEW, {DPDHL: True},
                   results)
    assert lines[0] == m._VM_PANEL_DANGER_HDR
    assert m._VM_RESIZE_REASON in _mt_lines(lines)[0]


def test_an_argocd_prefixed_app_counts_as_rendered(monkeypatch):
    results = _rendered(DPDHL)[1]
    path_map = {DPDHL: ["argocd/pv-dpdhl-c-ms", "argocd/pv-dpdhl-c-ss"]}
    lines = _panel(monkeypatch, DPDHL, DPDHL_OLD, DPDHL_NEW, path_map, results)
    assert lines[0] == m._VM_PANEL_ROUTINE_HDR


# ── (c) the role bug, and the paths that keep today's rule ───────────────

def test_a_parked_kcc_vm_without_a_render_is_still_a_resize(monkeypatch):
    """The TERMINATED escape is gone: KCC stops, resizes and starts the VM
    by itself, and parking it is what loops (block 6 S2)."""
    old = CHAOS_OLD.replace("        manageMetadata: false\n",
                            "        manageMetadata: false\n"
                            "        desiredStatus: TERMINATED\n")
    new = old.replace("machineType: n2d-standard-8",
                      "machineType: n2d-standard-16")
    lines = _panel(monkeypatch, CHAOS, old, new, _rendered(CHAOS)[0], {})
    assert lines[0] == m._VM_PANEL_DANGER_HDR
    assert m._VM_RESIZE_REASON in _mt_lines(lines)[0]


def test_a_top_level_kcc_key_no_longer_reads_a_bogus_role(monkeypatch):
    """The role bug: for `deployLinuxServicesK8s.machineType` the role was the
    leaf itself. A defaults TERMINATED does not hide it any more."""
    old = WCA_OLD + "      defaults:\n        desiredStatus: TERMINATED\n"
    new = old.replace("machineType: n2d-standard-2",
                      "machineType: n2d-standard-4")
    lines = _panel(monkeypatch, WCA, old, new, _rendered(WCA)[0], {})
    assert lines[0] == m._VM_PANEL_DANGER_HDR
    assert _mt_lines(lines)[0].endswith("\u2014 " + m._VM_RESIZE_REASON)
    covered = _panel(monkeypatch, WCA, old, new, *_rendered(WCA))
    assert covered[0] == m._VM_PANEL_ROUTINE_HDR


def test_a_legacy_terraform_resize_keeps_its_rule_with_a_render(monkeypatch):
    """The #3844 deloitte shape. The Terraform path is being replaced by KCC
    and ASO, so it keeps today's line and text."""
    path = "gcp/prod/private-cloud/au1-b/monthly/pv-deloitte-c/customer.yaml"
    old = ("appspace:\n  infra:\n    deployLinuxServices:\n"
           "      machineType: n2d-custom-16-49152\n")
    new = old.replace("n2d-custom-16-49152", "n2-custom-12-49152")
    lines = _panel(monkeypatch, path, old, new, *_rendered(path))
    assert lines[0] == m._VM_PANEL_DANGER_HDR
    ln = _mt_lines(lines)[0]
    assert ln.startswith("- \U0001f6a8 ") and "linux VM (legacy)" in ln
    assert ln.endswith("\u2014 " + LEGACY_REASON), ln
    assert m._VM_RESIZE_REASON not in "\n".join(lines)


def test_a_windows_resize_keeps_its_rule_with_a_render(monkeypatch):
    path = "gcp/prod/private-cloud/na1-a/weekly/pv-bos-b/customer.yaml"
    old = ("appspace:\n  infra:\n    deployWindows:\n"
           "      machineType: n2d-standard-4\n")
    new = old.replace("n2d-standard-4", "n2d-standard-8")
    lines = _panel(monkeypatch, path, old, new, *_rendered(path))
    assert lines[0] == m._VM_PANEL_DANGER_HDR
    assert _mt_lines(lines)[0].endswith("\u2014 " + LEGACY_REASON)


ASO = "azure/prod/private-cloud/na1-b/weekly/pv-nicewatch-a/customer.yaml"
ASO_OLD = (
    "appspace:\n"
    "  customerName: nicewatch\n"
    "  infra:\n"
    "    noCore: true\n"
    "    deployLinuxServicesK8s:\n"
    "      enabled: true\n"
    "      svc:\n"
    "        enabled: true\n"
    "        instanceName: pv-nicewatch-svc-a\n"
    "        machineType: \"Standard_E4as_v5\"\n"
    "        createNewBootDisk: true\n")
ASO_NEW = ASO_OLD.replace("Standard_E4as_v5", "Standard_E8as_v5")


def test_an_aso_resize_on_azure_is_flagged_with_its_own_text(monkeypatch):
    """The pv-nicewatch-a shape. On Azure the same key renders an ASO
    VirtualMachine, and the render level reads only KCC kinds, so a full
    render cannot confirm this resize. ASO reads no desiredStatus, so the
    text does not name it."""
    lines = _panel(monkeypatch, ASO, ASO_OLD, ASO_NEW, *_rendered(ASO))
    assert lines[0] == m._VM_PANEL_DANGER_HDR
    ln = _mt_lines(lines)[0]
    assert ln.startswith("- \U0001f6a8 `pv-nicewatch-a`")
    assert ln.endswith("\u2014 " + m._VM_ASO_RESIZE_REASON), ln
    assert m._VM_ASO_RESIZE_REASON == (
        "machineType changes: Azure restarts the VM to resize it. Merge in "
        "a window")
    assert "desiredStatus" not in m._VM_ASO_RESIZE_REASON


def test_a_terminated_sibling_hides_no_aso_resize(monkeypatch):
    """ASO reads no desiredStatus, so the old escape hid a real resize."""
    old = ASO_OLD + "        desiredStatus: TERMINATED\n"
    new = old.replace("Standard_E4as_v5", "Standard_E8as_v5")
    lines = _panel(monkeypatch, ASO, old, new, *_rendered(ASO))
    assert lines[0] == m._VM_PANEL_DANGER_HDR
    ln = _mt_lines(lines)[0]
    assert ln.startswith("- \U0001f6a8 `pv-nicewatch-a`")
    assert ln.endswith("\u2014 " + m._VM_ASO_RESIZE_REASON), ln


@pytest.mark.parametrize("parked", [
    "      svc:\n        desiredStatus: TERMINATED\n",
    "      defaults:\n        desiredStatus: TERMINATED\n"])
def test_a_parked_legacy_resize_stays_as_on_main(monkeypatch, parked):
    """Marcos: the Terraform path is replaced by KCC and ASO, so it keeps
    the rule of main as it is. A TERMINATED role or defaults hides it."""
    path = "gcp/prod/private-cloud/au1-b/monthly/pv-deloitte-c/customer.yaml"
    old = ("appspace:\n  infra:\n    deployLinuxServices:\n" + parked
           + "        machineType: n2d-custom-16-49152\n")
    new = old.replace("n2d-custom-16-49152", "n2-custom-12-49152")
    lines = _panel(monkeypatch, path, old, new, _rendered(path)[0], {})
    assert lines[0] == m._VM_PANEL_ROUTINE_HDR, lines
    assert LEGACY_REASON not in "\n".join(lines)
    running = old.replace("TERMINATED", "RUNNING")
    lines = _panel(monkeypatch, path, running,
                   running.replace("n2d-custom-16-49152", "n2-custom-12-49152"),
                   _rendered(path)[0], {})
    assert _mt_lines(lines)[0].endswith("\u2014 " + LEGACY_REASON)


def test_an_ancestor_file_needs_every_app_below_it_rendered(monkeypatch):
    """A cohort config.yaml feeds every env below it: all must render."""
    path = "gcp/prod/private-cloud/nachaos-a/config.yaml"
    old = CHAOS_OLD
    new = CHAOS_OLD.replace("machineType: n2d-standard-8",
                            "machineType: n2d-standard-16")
    apps = ["pv-chaostest-a-ss", "pv-other-a-ss"]
    full = {a: _result(m.OUT_NO_DIFF) for a in apps}
    lines = _panel(monkeypatch, path, old, new, {path: apps}, full)
    assert lines[0] == m._VM_PANEL_ROUTINE_HDR
    part = dict(full)
    del part["pv-other-a-ss"]
    lines = _panel(monkeypatch, path, old, new, {path: apps}, part)
    assert lines[0] == m._VM_PANEL_DANGER_HDR
    assert _mt_lines(lines)[0].endswith("— " + m._VM_RESIZE_REASON)
