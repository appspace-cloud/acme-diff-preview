"""COPS-2766 block 3, item 5 (row 13, C21): an n4 or c4 VM with a pd- disk.

The n4, n4a, n4d, c4, c4a and c4d machine families take only Hyperdisk. KCC
renders the VM, GCP rejects the pd- disk, and the env waits on a KCC error
(acme-config-prod #4482: a new env on n4d-highmem-2 with the chart default
pd-ssd, 11 h; #4331: a live env that enabled svc on n4-highmem-2). This is the
`vm_disk` gate, and nothing lifts it: the fix is always a config change.

A VM that runs at base keeps its disks, so moving it into these families is
the error whatever the disk values say (the critic). A new GCP private-cloud
env also gets check lines, warnings only: a VM that adopts a boot disk it does
not have yet, and no VM at all. There is no deployWindows or noCore check,
because that Terraform path is going away (Marcos, 2026-09-29).
"""
import os
import posixpath
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import comment_render as cr  # noqa: E402
import diff_preview as m  # noqa: E402
import vm_analysis as va  # noqa: E402

from test_coverage_orchestration import (  # noqa: E402,F401
    world, _mk_pr, PATH_MAP, BASE_SHA, PR_SHA, IDENTITY, IDENTITY_YAML, ANCILLARY)

pytestmark = pytest.mark.prereq_reads

K = "appspace.infra.deployLinuxServicesK8s."
REASON = va._VM_DISK_FAMILY_REASON
TEXT = cr.GATES["vm_disk"][0]
FIX = ("Set `dataDiskType` (and `bootDiskType` when `createNewBootDisk` is true) to "
       "`hyperdisk-balanced`.")
CHECK = cr._NEW_ENV_CHECK_PREFIX
ENV = "pv-use1-test-a"


@pytest.fixture(autouse=True)
def no_vertex(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)


def _flat(roles, on=True, extra=None):
    """A value chain: KCC `on`, and role -> {leaf: value}, each role enabled."""
    flat = {K + "enabled": on, **(extra or {})}
    for role, leaves in roles.items():
        flat[f"{K}{role}.enabled"] = True
        flat.update({f"{K}{role}.{k}": v for k, v in leaves.items()})
    return flat


N4D = {"machineType": "n4d-highmem-2", "createNewBootDisk": True}     # #4482
HD = {"dataDiskType": "hyperdisk-balanced", "bootDiskType": "hyperdisk-balanced"}


def _new_text(mt, *disks):
    return (f"`{mt}` with " + " and ".join(f"{d} disk `{t}`" for d, t in disks)
            + f": {REASON}. {FIX}")


def _move_text(old, new):
    return (f"`machineType` `{old}` \u2192 `{new}`: {REASON}. The running VM keeps its "
            "disks, because a disk type cannot change in place. Keep the current family, "
            "or plan a disk migration to Hyperdisk (snapshot and restore).")


# ── vm_analysis._vm_disk_family_errors ───────────────────────────────────

@pytest.mark.parametrize("flat,want", [
    # #4482: no disk type, so the chart default pd-ssd, on data and boot
    (_flat({"svc": N4D}), [("svc", "n4d-highmem-2", "data", "pd-ssd"),
                           ("svc", "n4d-highmem-2", "boot", "pd-ssd")]),
    # hyperdisk data, and the boot disk is adopted as it is
    (_flat({"svc": {"machineType": "n4-standard-2", "dataDiskType": "hyperdisk-balanced",
                    "createNewBootDisk": False}}), []),
    (_flat({"mongo": {"machineType": "c4a-standard-4", "dataDiskType": "pd-balanced",
                      "createNewBootDisk": True, "bootDiskType": "hyperdisk-balanced"}}),
     [("mongo", "c4a-standard-4", "data", "pd-balanced")]),
    (_flat({"svc": {}, "mongo": {}, "rabbit": {"createNewBootDisk": True}}), []),  # chart n2d
    # the legacy Terraform key, in any case
    (_flat({"rabbit": {}}, extra={"appspace.infra.deployLinuxServices.rabbit.machineType":
                                  " 'N4-Standard-2' "}),
     [("rabbit", "n4-standard-2", "data", "pd-ssd")]),
    (_flat({"svc": {}}, extra={"appspace.infra.deployLinuxServices.machineType": "c4-standard-2"}),
     [("svc", "c4-standard-2", "data", "pd-ssd")]),
    # the role key wins over the legacy one
    (_flat({"svc": {"machineType": "n2d-highmem-2"}},
           extra={"appspace.infra.deployLinuxServices.machineType": "n4-standard-2"}), []),
    # defaults.gcp, and createNewBootDisk from defaults
    (_flat({"svc": {"createNewBootDisk": True}},
           extra={K + "defaults.gcp.svcMachineType": "c4d-highmem-2",
                  K + "defaults.gcp.dataDiskType": "hyperdisk-balanced"}),
     [("svc", "c4d-highmem-2", "boot", "pd-ssd")]),
    (_flat({"svc": {"machineType": "n4a-standard-2"}},
           extra={K + "defaults.createNewBootDisk": True,
                  K + "defaults.gcp.bootDiskType": "pd-balanced",
                  K + "defaults.gcp.dataDiskType": "hyperdisk-balanced"}),
     [("svc", "n4a-standard-2", "boot", "pd-balanced")]),
    (_flat({"svc": N4D}, on=False), []),                       # KCC off: nothing renders
    (_flat({"svc": N4D}, on="false"), []),
    ({K + "enabled": True, K + "svc.enabled": False, K + "svc.machineType": "n4-standard-2"}, []),
    (_flat({"svc": {"machineType": "n2-custom-12-49152"}}), []),
    ({}, []),
    (None, []),
])
def test_vm_disk_family_errors(flat, want):
    assert va._vm_disk_family_errors(flat) == want


# ── vm_analysis._new_env_prereq_findings ─────────────────────────────────

ERR_4482 = (f"- \u26d4 `{ENV}` \u00b7 **linux VM (KCC) \u00b7 svc**: "
            + _new_text("n4d-highmem-2", ("data", "pd-ssd"), ("boot", "pd-ssd")))
W1 = (f"{CHECK}`{ENV}` adopts a boot disk for `svc` that a new VM does not have yet "
      "(`createNewBootDisk` is not true). Set `createNewBootDisk: true`, unless the VM or "
      "its boot disk already exists (a move, or a disk restored by hand).")
W2 = (f"{CHECK}`{ENV}` renders no Linux VM (`svc`, `mongo` or `rabbit`) from "
      "`deployLinuxServicesK8s`, so KCC creates no VM for it. Enable the roles it needs, "
      "unless it runs with no VM on purpose.")


def test_pr_4482_is_an_error_with_the_fix():
    errors, lines = va._new_env_prereq_findings(_flat({"svc": N4D}), ENV)
    assert errors == va._vm_disk_family_errors(_flat({"svc": N4D}))
    assert lines == [ERR_4482]


def test_one_error_line_per_role():
    flat = _flat({"svc": dict(N4D, **HD), "mongo": {"machineType": "c4-standard-4",
                                                    "createNewBootDisk": True},
                  "rabbit": {"machineType": "n4-standard-2", "createNewBootDisk": True,
                             "bootDiskType": "hyperdisk-balanced"}})
    _errors, lines = va._new_env_prereq_findings(flat, ENV)
    assert lines == [
        f"- \u26d4 `{ENV}` \u00b7 **linux VM (KCC) \u00b7 mongo**: "
        + _new_text("c4-standard-4", ("data", "pd-ssd"), ("boot", "pd-ssd")),
        f"- \u26d4 `{ENV}` \u00b7 **linux VM (KCC) \u00b7 rabbit**: "
        + _new_text("n4-standard-2", ("data", "pd-ssd"))]


def test_a_new_vm_that_adopts_its_boot_disk_is_a_check():
    for flat in (_flat({"svc": {}}), _flat({"svc": {"createNewBootDisk": "false"}})):
        assert va._new_env_prereq_findings(flat, ENV) == ([], [W1])
    many = _flat({"svc": {}, "mongo": {"createNewBootDisk": True}, "rabbit": {}})
    assert va._new_env_prereq_findings(many, ENV)[1] == [
        W1.replace("for `svc`", "for `rabbit`, `svc`")]


def test_the_boot_disk_check_comes_after_the_error():
    flat = _flat({"svc": {"machineType": "n4-standard-2"}})
    assert va._new_env_prereq_findings(flat, ENV)[1] == [
        f"- \u26d4 `{ENV}` \u00b7 **linux VM (KCC) \u00b7 svc**: "
        + _new_text("n4-standard-2", ("data", "pd-ssd")), W1]


@pytest.mark.parametrize("flat", [
    {K + "enabled": True},
    {},
    _flat({"svc": {"createNewBootDisk": True}}, on=False),      # a role, but KCC is off
])
def test_no_vm_at_all_is_a_check(flat):
    assert va._new_env_prereq_findings(flat, ENV) == ([], [W2])


def test_a_clean_env_has_no_line_and_no_windows_check():
    """No deployWindows and no noCore: that path is going away, so no check."""
    flat = _flat({"svc": {"createNewBootDisk": True}},
                 extra={"appspace.infra.deployWindows": False})
    assert va._new_env_prereq_findings(flat, ENV) == ([], [])


# ── vm_analysis._vm_disk_family_changes (live envs) ──────────────────────

def test_pr_4331_a_role_enabled_on_n4_is_an_error():
    old = {K + "enabled": True}
    new = _flat({"svc": {"machineType": "n4-highmem-2", "createNewBootDisk": True}})
    assert va._vm_disk_family_changes(old, new) == [
        ("svc", _new_text("n4-highmem-2", ("data", "pd-ssd"), ("boot", "pd-ssd")))]


def test_pr_4331_values_with_hyperdisk_are_clean():
    old = {K + "enabled": True}
    new = _flat({"svc": dict({"machineType": "n4-highmem-2", "createNewBootDisk": True}, **HD)})
    assert va._vm_disk_family_changes(old, new) == []


def test_a_running_vm_moved_into_the_family_is_an_error_whatever_the_disks():
    old = _flat({"svc": {"machineType": "n2d-highmem-2"}})
    for leaves in ({}, HD, dict(HD, createNewBootDisk=True)):
        new = _flat({"svc": dict({"machineType": "n4-highmem-2"}, **leaves)})
        assert va._vm_disk_family_changes(old, new) == [
            ("svc", _move_text("n2d-highmem-2", "n4-highmem-2"))]


def test_a_running_mongo_moved_into_the_family_is_an_error():
    old = _flat({"mongo": {"instances": ["pv-x-mongo1-a", {"name": "pv-x-mongo2-a"}]}})
    new = _flat({"mongo": {"instances": ["pv-x-mongo1-a", {"name": "pv-x-mongo2-a"}],
                           "machineType": "c4-standard-4", "dataDiskType": "hyperdisk-balanced"}})
    assert va._vm_disk_family_changes(old, new) == [
        ("mongo", _move_text("n2d-highmem-4", "c4-standard-4"))]


def test_a_new_vm_name_is_a_new_vm():
    """New instance names build new VMs, so their disk values decide."""
    old = _flat({"svc": {"instanceName": "pv-x-svc-a"},
                 "mongo": {"instances": ["pv-x-mongo1-a"]}})
    new = _flat({"svc": dict({"instanceName": "pv-x-svc2-a", "machineType": "n4-highmem-2",
                              "createNewBootDisk": True}, **HD),
                 "mongo": {"instances": [{"name": "pv-x-mongo9-a"}], "machineType": "c4-standard-4"}})
    assert va._vm_disk_family_changes(old, new) == [
        ("mongo", _new_text("c4-standard-4", ("data", "pd-ssd")))]


@pytest.mark.parametrize("base,err", [
    ({"createNewBootDisk": True}, False),     # data and boot disks KCC made are Hyperdisk
    ({}, True),                               # the adopted boot disk can be pd-
    ({"createNewBootDisk": True, "bootDiskType": "pd-balanced"}, True),
])
def test_a_running_vm_on_hyperdisk_can_move_into_the_family(base, err):
    old = _flat({"svc": dict(HD, machineType="c3-highmem-4", **base)})
    new = _flat({"svc": dict(HD, machineType="n4-highmem-2", **base)})
    want = [("svc", _move_text("c3-highmem-4", "n4-highmem-2"))] if err else []
    assert va._vm_disk_family_changes(old, new) == want


def test_a_hyperdisk_vm_that_moves_with_a_pd_disk_is_an_error():
    old = _flat({"svc": dict(HD, machineType="c3-highmem-4", createNewBootDisk=True)})
    new = _flat({"svc": dict(HD, machineType="n4-highmem-2", createNewBootDisk=True,
                             dataDiskType="pd-ssd")})
    assert va._vm_disk_family_changes(old, new) == [
        ("svc", _new_text("n4-highmem-2", ("data", "pd-ssd")))]


def test_the_svc_default_name_follows_the_env():
    ident = {"appspace.prefix": "pv", "appspace.customerName": "x", "appspace.suffix": "a"}
    old = _flat({"svc": {}}, extra=ident)
    new = _flat({"svc": {"machineType": "n4-highmem-2"}}, extra=ident)
    assert va._vm_disk_family_changes(old, new)[0][1] == _move_text("n2d-highmem-2", "n4-highmem-2")
    renamed = dict(new, **{"appspace.suffix": "b"})   # pv-x-svc-b is another VM
    assert va._vm_disk_family_changes(old, renamed) == [
        ("svc", _new_text("n4-highmem-2", ("data", "pd-ssd")))]


def test_an_error_already_on_main_does_not_count():
    broken = _flat({"svc": {"machineType": "n4-highmem-2", "dataDiskSizeGb": 128}})
    grown = _flat({"svc": {"machineType": "n4d-highmem-2", "dataDiskSizeGb": 256}})
    assert va._vm_disk_family_changes(broken, grown) == []


def test_a_pd_disk_on_a_vm_already_in_the_family_is_an_error():
    old = _flat({"svc": dict({"machineType": "n4-highmem-2"}, **HD)})
    new = _flat({"svc": {"machineType": "n4-highmem-2", "bootDiskType": "hyperdisk-balanced",
                         "dataDiskType": "pd-ssd"}})
    assert va._vm_disk_family_changes(old, new) == [
        ("svc", _new_text("n4-highmem-2", ("data", "pd-ssd")))]


@pytest.mark.parametrize("old,new", [(None, _flat({"svc": N4D})), ({}, None), (None, None)])
def test_an_unparsable_side_is_not_checked(old, new):
    assert va._vm_disk_family_changes(old, new) == []


# ── diff_preview._new_env_prereqs ────────────────────────────────────────

GCP = "gcp/config.yaml"
KCC_ON = "appspace:\n  infra:\n    deployLinuxServicesK8s:\n      enabled: true\n"
CUSTOM = posixpath.dirname(posixpath.dirname(IDENTITY))
COHORT = f"{CUSTOM}/config.yaml"
COHORT_DOC = "appspace:\n  version: 2603.0.1-dev\n"
NEW = f"{CUSTOM}/{ENV}/customer.yaml"


def _doc(svc=None, name="use1-test"):
    """A customer.yaml; `svc` is the YAML of its svc role, or no VM."""
    return (f"appspace:\n  customerName: {name}\n  suffix: a\n  version: 2603.0.1-dev\n"
            + ("  infra:\n    deployLinuxServicesK8s:\n      svc:\n        enabled: true\n"
               + "".join(f"        {line}\n" for line in svc) if svc is not None else ""))


DOC_4482 = _doc(["machineType: n4d-highmem-2", "createNewBootDisk: true"])
WHY_4482 = "svc n4d-highmem-2, data pd-ssd"


def _serve(monkeypatch, head, base=None, error=()):
    """head and base: {path: text} on top of the KCC switch and the cohort."""
    base = dict({GCP: KCC_ON, COHORT: COHORT_DOC, IDENTITY: IDENTITY_YAML}, **(base or {}))
    head = dict(base, **head)

    def fetch(path, sha, repo=None):
        if path in error:
            return None, m.BB_ERROR
        side = head if sha == PR_SHA else base
        return (side[path], m.BB_OK) if path in side else (None, m.BB_NOT_FOUND)
    monkeypatch.setattr(m, "_bb_fetch_status", fetch)


def _cand(path):
    return {"name": path.split("/")[-2], "config_file": path,
            "env_dir": path.rsplit("/", 1)[0], "all_yaml_files": [path]}


def test_new_env_prereqs_give_the_gate_and_the_lines(monkeypatch):
    _serve(monkeypatch, {NEW: DOC_4482})
    gates, lines = m._new_env_prereqs([_cand(NEW)], PR_SHA)
    assert gates == [{"kind": "vm_disk", "env": ENV, "why": WHY_4482}]
    assert lines == {NEW: [ERR_4482]}


def test_the_kcc_switch_comes_from_the_chain(monkeypatch):
    _serve(monkeypatch, {NEW: DOC_4482, GCP: "appspace: {}\n"})
    assert m._new_env_prereqs([_cand(NEW)], PR_SHA) == ([], {NEW: [W2]})


@pytest.mark.parametrize("path", [
    "azure/prod/private-cloud/na1-a/monthly/pv-x-a/customer.yaml",   # ASO, not KCC
    "gcp/prod/public-cloud/na1-a/cl-prod-b/api/customer.yaml",
    "gcp/prod/private-cloud/na1-a/config.yaml",
])
def test_only_gcp_private_cloud_envs_are_read(monkeypatch, path):
    monkeypatch.setattr(m, "_bb_fetch_status", lambda *a, **k: pytest.fail("read"))
    assert m._new_env_prereqs([_cand(path), {"name": "no-file"}], PR_SHA) == ([], {})
    assert m._new_env_prereqs(None, PR_SHA) == ([], {})


def test_a_failed_read_retries(monkeypatch):
    _serve(monkeypatch, {NEW: DOC_4482}, error=(COHORT,))
    with pytest.raises(m.ValueFileUnreadable):
        m._new_env_prereqs([_cand(NEW)], PR_SHA)


def test_values_that_cannot_be_parsed_are_a_check(monkeypatch):
    _serve(monkeypatch, {NEW: DOC_4482, COHORT: "appspace: [\n"})
    assert m._new_env_prereqs([_cand(NEW)], PR_SHA) == ([], {NEW: [
        f"{CHECK}`{ENV}`: the VM checks did not run, because its values cannot be parsed."]})


# ── diff_preview._summarize_vm_changes and _merge_gates (live envs) ──────

DOC_4331 = IDENTITY_YAML + ("  infra:\n    deployLinuxServicesK8s:\n      svc:\n"
                            "        enabled: true\n        machineType: n4-highmem-2\n"
                            "        createNewBootDisk: true\n")
LINE_4331 = ("- \U0001f6a8 `pv-orch-a` \u00b7 **linux VM (KCC) \u00b7 svc**: "
             + _new_text("n4-highmem-2", ("data", "pd-ssd"), ("boot", "pd-ssd")))


def _vm_lines(monkeypatch, changed, head, base=None, error=()):
    _serve(monkeypatch, head, base, error)
    return m._summarize_vm_changes(changed, PR_SHA, BASE_SHA, PATH_MAP, {})


def test_pr_4331_is_a_danger_and_a_gate(monkeypatch):
    lines = _vm_lines(monkeypatch, [IDENTITY], {IDENTITY: DOC_4331})
    assert LINE_4331 in lines
    assert m._merge_gates((), vm_change_lines=lines) == [
        {"arg": "pv-orch-a", "kind": "vm_disk", "env": "pv-orch-a", "why": "svc n4-highmem-2",
         "lifted": False}]


def test_a_line_of_another_shape_still_blocks():
    assert m._merge_gates((), vm_change_lines=[f"- \U0001f6a8 {REASON}"]) == [
        {"arg": "", "kind": "vm_disk", "env": "", "why": "", "lifted": False}]


def test_pr_4331_with_hyperdisk_has_no_disk_line(monkeypatch):
    doc = DOC_4331 + "        dataDiskType: hyperdisk-balanced\n        bootDiskType: hyperdisk-balanced\n"
    lines = _vm_lines(monkeypatch, [IDENTITY], {IDENTITY: doc})
    assert lines and not any(REASON in line for line in lines)
    assert m._merge_gates((), vm_change_lines=lines) == []


def test_a_failed_chain_read_retries(monkeypatch):
    with pytest.raises(m.ValueFileUnreadable):
        _vm_lines(monkeypatch, [IDENTITY], {IDENTITY: DOC_4331}, error=(GCP,))


def test_an_azure_env_and_a_cohort_file_are_not_read(monkeypatch):
    """ASO renders Azure VMs, and only an env's own customer.yaml is checked."""
    az = "azure/prod/private-cloud/na1-a/monthly/pv-x-a/customer.yaml"
    cohort = f"{CUSTOM}/pv-orch-b/config.yaml"
    head = {az: DOC_4331, cohort: DOC_4331}
    base = {az: IDENTITY_YAML, cohort: IDENTITY_YAML}
    lines = _vm_lines(monkeypatch, [az, cohort], head, base,
                      error=("azure/config.yaml", GCP))
    assert lines and not any(REASON in line for line in lines)


def test_a_change_with_no_vm_key_reads_no_chain(monkeypatch):
    doc = IDENTITY_YAML.replace("2603.0.1-dev", "2603.0.2-dev")
    lines = _vm_lines(monkeypatch, [IDENTITY], {IDENTITY: doc}, error=(GCP,))
    assert lines[0] == va._VM_PANEL_CLEAN_HDR


def test_a_vm_key_the_rule_does_not_read_reads_no_chain(monkeypatch):
    """A disk size or a schedule cannot add the error: no chain read
    (test_cops2671_cov_dp_c pins one fetch per side for a size change)."""
    svc = "  infra:\n    deployLinuxServicesK8s:\n      svc:\n        enabled: true\n"
    base = IDENTITY_YAML + svc + "        machineType: n4-highmem-2\n        dataDiskSizeGb: 128\n"
    head = base.replace("128", "256")
    lines = _vm_lines(monkeypatch, [IDENTITY], {IDENTITY: head}, {IDENTITY: base}, error=(GCP,))
    assert lines and not any(REASON in line for line in lines)


# ── process_pr ───────────────────────────────────────────────────────────

RENDERED = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: platform-config\n"
GATE_FIX = ". Use hyperdisk-balanced on a new VM, or keep the machine family"
DESC_4482 = f"Blocked - {TEXT} ({WHY_4482}) in {ENV}{GATE_FIX} (see PR comment)"
CLEAN_DESC = "1 new environment(s), ~1 resource(s) to create"
SK = (m.BB_REPO, 991)


@pytest.fixture()
def run(world, monkeypatch):
    """`run(changed, head, base)`: process the PR and give (body, status)."""
    sinks, _plan = world
    monkeypatch.setattr(m, "_render_new_env_diff",
                        lambda env_info, pr_sha: (RENDERED, None, 1, "2603.0.1-dev"))
    monkeypatch.setattr(m, "_pr_commit_messages",
                        lambda *a: pytest.fail("commits read with no trailer to find"))

    def go(changed, head, base=None):
        _serve(monkeypatch, head, base)
        monkeypatch.setattr(m, "get_pr_changed_files", lambda pr_id, repo=None: (changed, {}))
        m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
        return sinks.upserts[-1], sinks.statuses[-1]
    return go


def test_pr_4482_blocks_the_new_env_path(run):
    body, (state, desc) = run([NEW], {NEW: DOC_4482})
    assert m._extract_status_token(body) == "blocked"
    assert state == "FAILED" and desc == DESC_4482
    assert f"- \u26d4 **{TEXT} ({WHY_4482})** in `{ENV}`" in body
    assert ERR_4482 in body and "to merge anyway" not in body
    assert body.index(cr.MERGE_SUMMARY_HDR) < body.index("New Environment(s) Detected") \
        < body.index(ERR_4482)
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA)


def test_pr_4482_blocks_the_diff_path(run):
    body, (state, desc) = run([ANCILLARY, NEW], {NEW: DOC_4482})
    assert m._extract_status_token(body) == "blocked"
    assert state == "FAILED" and desc == DESC_4482
    assert ERR_4482 in body


def test_a_new_env_on_hyperdisk_is_clean(run):
    doc = _doc(["machineType: n4d-highmem-2", "createNewBootDisk: true",
                "dataDiskType: hyperdisk-balanced", "bootDiskType: hyperdisk-balanced"])
    body, (state, desc) = run([NEW], {NEW: doc})
    assert m._extract_status_token(body) == "clean"
    assert state == "SUCCESSFUL" and desc == CLEAN_DESC
    assert REASON not in body and CHECK not in body
    assert "- \U0001f195 **New environment** in this PR" in body


@pytest.mark.parametrize("svc,check,lead", [
    ([], W1, "adopts a boot disk for svc that a new VM does not have yet "
             "(createNewBootDisk is not true)"),
    (None, W2, "renders no Linux VM (svc, mongo or rabbit) from deployLinuxServicesK8s, so "
               "KCC creates no VM for it"),
])
def test_a_check_stays_green_and_leads_the_status(run, svc, check, lead):
    body, (state, desc) = run([NEW], {NEW: _doc(svc)})
    assert m._extract_status_token(body) == "clean" and state == "SUCCESSFUL"
    assert desc == (f"\u26a0\ufe0f New environment: 1 check(s) to review - {ENV} {lead}"
                    f" | {CLEAN_DESC}")
    assert check in body and cr._VERDICTS[cr._SEV_REVIEW] in body
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA)


def test_a_failed_prereq_read_retries_the_pr(run, monkeypatch):
    real = m._merged_kcc_flat_for_env

    def flaky(path, sha, repo=None, strict=False):
        if strict and path == NEW:
            raise m.ValueFileUnreadable("Bitbucket is down")
        return real(path, sha, repo, strict)
    monkeypatch.setattr(m, "_merged_kcc_flat_for_env", flaky)
    body, (state, desc) = run([NEW], {NEW: DOC_4482})
    assert m._extract_status_token(body) == "transient"
    assert state == "FAILED" and "will retry" in desc and TEXT not in body
    assert SK not in m._seen and SK in m._retry_backoff


def test_pr_4331_blocks_the_diff_path(run):
    body, (state, desc) = run([IDENTITY], {IDENTITY: DOC_4331})
    assert m._extract_status_token(body) == "blocked"
    assert state == "FAILED" and desc == (f"Blocked - {TEXT} (svc n4-highmem-2) in pv-orch-a"
                                          f"{GATE_FIX} (see PR comment)")
    assert f"- \u26d4 **{TEXT} (svc n4-highmem-2)** in `pv-orch-a`" in body and LINE_4331 in body
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA)


def test_pr_4331_with_hyperdisk_is_clean(run):
    doc = DOC_4331 + "        dataDiskType: hyperdisk-balanced\n        bootDiskType: hyperdisk-balanced\n"
    body, (state, _desc) = run([IDENTITY], {IDENTITY: doc})
    assert m._extract_status_token(body) == "clean" and state == "SUCCESSFUL"
    assert REASON not in body and TEXT not in body


def test_a_running_vm_moved_to_n4_blocks_even_on_hyperdisk(run):
    """Live proof 5, the critic's version: an existing VM, hyperdisk values."""
    svc = "  infra:\n    deployLinuxServicesK8s:\n      svc:\n        enabled: true\n"
    base = IDENTITY_YAML + svc + "        machineType: n2d-highmem-2\n"
    head = (IDENTITY_YAML + svc + "        machineType: n4-highmem-2\n"
            "        desiredStatus: TERMINATED\n        dataDiskType: hyperdisk-balanced\n")
    body, (state, desc) = run([IDENTITY], {IDENTITY: head}, {IDENTITY: base})
    assert m._extract_status_token(body) == "blocked"
    assert state == "FAILED" and desc == (f"Blocked - {TEXT} (svc n2d-highmem-2 to "
                                          f"n4-highmem-2) in pv-orch-a{GATE_FIX} (see PR comment)")
    assert ("- \U0001f6a8 `pv-orch-a` \u00b7 **linux VM (KCC) \u00b7 svc**: "
            + _move_text("n2d-highmem-2", "n4-highmem-2")) in body
