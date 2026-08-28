"""A cohort config.yaml is not an environment provisioning a VM (COPS-2717).

Field report: acme-config-prod #4449 added
`deployLinuxServicesK8s.svc.ignoreDesiredStatus: true` to
`gcp/aec/config.yaml` and the comment opened with

    DO NOT MERGE - 1 environment(s) provision a NEW linux VM
    1 environment provisions a new linux VM (KCC) - svc: aec

Nothing was provisioned. That file is a cohort config with no
`customer.yaml`, the added key is not `enabled`, and the rendered diff
carried zero ComputeInstance sections across all 682 hunks.

`_summarize_vm_changes` reads "the domain is new in this file and this key
had no old value" as a provision. The function already knows the
difference -- it builds `scope` as "ancestor <path> (inherited by every
environment below it)" and carries `_env_file`, and the routine-line path
tags an environment "only when this really is one". The provision path did
not.

The reclassification is deliberately narrow, because a BLOCK that goes
missing is far worse than one that is merely loud. It requires ALL of:

  * the file is not an environment, and
  * no key in the provision is dangerous (a tainted one never groups
    anyway), and
  * every app rendered, and none of those renders creates a KCC
    ComputeInstance.

A real environment, an ancestor file that does provision, a dangerous key,
and a PR where anything failed to render all keep the warning they had.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_preview as dp  # noqa: E402


BASE = "appspace:\n  customerName: c\n"

# The #4449 shape: a cohort file gaining ONLY ignoreDesiredStatus.
COHORT_NEW = (BASE +
              "  infra:\n"
              "    deployLinuxServicesK8s:\n"
              "      svc:\n"
              "        ignoreDesiredStatus: true\n")

# A real provision: the role is enabled and the machine is described.
ENV_NEW_VM = (BASE +
              "  infra:\n"
              "    deployLinuxServicesK8s:\n"
              "      enabled: true\n"
              "      svc:\n"
              "        enabled: true\n"
              "        machineType: n2d-standard-2\n"
              "        createNewBootDisk: true\n")

COHORT = "gcp/aec/config.yaml"
ENVFILE = "gcp/aec/private-cloud/na2-a/pv-x--aec1-a/customer.yaml"

VM_HDR = "/compute.cnrm.cloud.google.com/ComputeInstance pv-x--aec1-a/pv-x-svc-a"
DEPLOY_HDR = "/apps/Deployment account"


def _created(hdr):
    return (hdr, "--- \n+++ \n+apiVersion: x\n+kind: K\n+  name: n\n")


def _modified(hdr):
    return (hdr, "--- \n+++ \n kind: K\n+  field: new\n")


def _result(outcome, sections=()):
    return dp.DiffResult("d", list(sections), len(sections),
                         outcome == dp.OUT_DIFF, "", outcome, "r")


def _panel(monkeypatch, path, new_text, app_results, is_env=None):
    """is_env is derived from the filename, exactly as production does.

    path_map is ALWAYS populated: that is the shape that reproduces the
    bug. A cohort config.yaml is a valueFile for every application below
    it, so `path_map.get(file)` is truthy for ancestor files too -- which
    is precisely why it could not be used to tell the two apart.
    """
    def fetch(p, sha, repo=None):
        # The base side is the SAME file without the domain -- the real
        # shape. An empty/absent base is a different case entirely.
        return (BASE if sha == "base" * 10 else new_text), dp.BB_OK
    monkeypatch.setattr(dp, "_bb_fetch_cached", fetch)
    return "\n".join(dp._summarize_vm_changes(
        [path], path_map={path: ["pv-x--aec1-a-ss", "pv-y--aec1-b-ss"]},
        app_results=app_results, repo="acme-config-prod",
        pr_sha="a" * 40, base_sha="base" * 10))


def _is_danger(out):
    return out.startswith(dp._VM_PANEL_DANGER_HDR)


# ── the corroborator on its own ──────────────────────────────────────────

def test_no_render_at_all_keeps_the_warning():
    """Nothing to corroborate with must never drop a provisioning flag."""
    assert dp._render_creates_a_kcc_vm({}) is True
    assert dp._render_creates_a_kcc_vm(None) is True


def test_an_app_that_failed_to_render_keeps_the_warning():
    assert dp._render_creates_a_kcc_vm(
        {"a-ss": _result(dp.OUT_INDETERMINATE)}) is True


def test_a_created_compute_instance_is_a_real_provision():
    assert dp._render_creates_a_kcc_vm(
        {"a-ss": _result(dp.OUT_DIFF, [_created(VM_HDR)])}) is True


def test_a_modified_compute_instance_is_not_a_provision():
    """Editing an existing machine is not building one."""
    assert dp._render_creates_a_kcc_vm(
        {"a-ss": _result(dp.OUT_DIFF, [_modified(VM_HDR)])}) is False


def test_other_created_kinds_are_not_a_provision():
    assert dp._render_creates_a_kcc_vm(
        {"a-ms": _result(dp.OUT_DIFF, [_created(DEPLOY_HDR)])}) is False


def test_clean_renders_with_nothing_created_corroborate():
    assert dp._render_creates_a_kcc_vm(
        {"a-ms": _result(dp.OUT_NO_DIFF),
         "a-ss": _result(dp.OUT_DIFF, [_modified(DEPLOY_HDR)])}) is False


# ── the panel: the reported false positive ───────────────────────────────

def test_a_cohort_file_no_longer_claims_a_new_vm(monkeypatch):
    out = _panel(monkeypatch, COHORT, COHORT_NEW,
                 {"pv-x--aec1-a-ss": _result(dp.OUT_DIFF,
                                             [_modified(DEPLOY_HDR)])})
    assert not _is_danger(out), out
    assert "provision" not in out.lower(), out
    assert "ignoreDesiredStatus" in out, "the change must still be reported"
    # The line still opens with `aec`, the scope wording this panel uses for
    # every file that feeds an application. Renaming that is a separate and
    # much wider decision -- it would change the wording for every ancestor
    # file in the fleet -- and this ticket is about the false BLOCK, not
    # about how a routine line is labelled. Deliberately unchanged.
    assert "(routine)" in out


def test_the_same_cohort_change_still_warns_when_a_vm_really_appears(monkeypatch):
    """The render is the corroborating fact, not the file's path."""
    out = _panel(monkeypatch, COHORT, COHORT_NEW,
                 {"pv-x--aec1-a-ss": _result(dp.OUT_DIFF,
                                             [_created(VM_HDR)])})
    assert _is_danger(out), out
    assert "provisions a new" in out


def test_a_cohort_change_warns_when_nothing_rendered(monkeypatch):
    """No corroboration available -> keep the warning."""
    out = _panel(monkeypatch, COHORT, COHORT_NEW, {})
    assert _is_danger(out), out


def test_a_cohort_change_warns_when_an_app_failed_to_render(monkeypatch):
    out = _panel(monkeypatch, COHORT, COHORT_NEW,
                 {"pv-x--aec1-a-ss": _result(dp.OUT_INDETERMINATE)})
    assert _is_danger(out), out


# ── everything that must NOT change ──────────────────────────────────────

def test_a_real_environment_provisioning_is_untouched(monkeypatch):
    """The whole point of the block. A customer.yaml gaining an enabled VM
    role still shouts, whatever the render says."""
    out = _panel(monkeypatch, ENVFILE, ENV_NEW_VM,
                 {"pv-x--aec1-a-ss": _result(dp.OUT_DIFF,
                                             [_modified(DEPLOY_HDR)])})
    assert _is_danger(out), out
    assert "provisions a new" in out


def test_a_dangerous_key_on_a_cohort_file_still_shouts(monkeypatch):
    """allowDeletion armed from birth is dangerous wherever it is written;
    a tainted provision never took the grouping path anyway."""
    armed = (BASE +
             "  infra:\n"
             "    deployLinuxServicesK8s:\n"
             "      svc:\n"
             "        ignoreDesiredStatus: true\n"
             "        allowDeletion: true\n")
    out = _panel(monkeypatch, COHORT, armed,
                 {"pv-x--aec1-a-ss": _result(dp.OUT_DIFF,
                                             [_modified(DEPLOY_HDR)])})
    assert _is_danger(out), out
