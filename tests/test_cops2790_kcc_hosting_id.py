"""COPS-2790: a new GCP env that enables a KCC linux VM with no hostingID.

The new-env preview only helm-templates appspace-micro-services, so the
supporting-services `required()` on appspace.hostingID never ran. COPS-2789
pv-qa-perf-01-a merged green (acme-config-dev #7450), then Argo SS went
Unknown / ComparisonError and MS pods sat in CreateContainerConfigError.

The gate is kcc_hosting_id. Nothing lifts it: the fix is always a value.
It matches the chart: a rendered role (KCC on and svc/mongo/rabbit enabled),
not the top-level enabled flag alone.
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

from test_cops2766_vm_disk_family import (  # noqa: E402
    CHECK, CUSTOM, ENV, GCP, K, KCC_ON, NEW, PR_SHA, _cand, _doc, _flat, _serve,
)

pytestmark = pytest.mark.prereq_reads

REASON = ("appspace.hostingID is required when deployLinuxServicesK8s.enabled "
          "is true")
LINE = (f"- \u26d4 `{ENV}` \u00b7 **linux VM (KCC)**: `appspace.hostingID` is missing "
        "while `deployLinuxServicesK8s` has `svc` enabled. The supporting-services "
        f"chart fails helm with `{REASON}`. Argo sync goes Unknown "
        "(ComparisonError); Mongo/Rabbit secrets never appear and microservices stay "
        "CreateContainerConfigError. Set `appspace.hostingID` in `customer.yaml` "
        '(QA usually `"00000000"`).')
WHY = "no appspace.hostingID, svc enabled"


def test_cops2789_svc_without_hosting_id_is_a_blocking_line():
    """The live customer.yaml: svc on, n2d, no hostingID."""
    flat = _flat({"svc": {"machineType": "n2d-standard-2", "createNewBootDisk": True}},
                 hosting_id=None)
    errors, lines = va._new_env_prereq_findings(flat, ENV)
    assert errors == []
    assert lines == [LINE]


def test_whitespace_hosting_id_counts_as_missing():
    flat = _flat({"svc": {"createNewBootDisk": True}}, hosting_id="  ")
    assert va._new_env_prereq_findings(flat, ENV)[1] == [LINE]


def test_a_set_hosting_id_is_clean():
    flat = _flat({"svc": {"createNewBootDisk": True}}, hosting_id="00000000")
    assert va._new_env_prereq_findings(flat, ENV) == ([], [])


def test_kcc_on_with_no_role_is_still_only_the_no_vm_check():
    """Chart required() sits inside each role template, so this must not block."""
    assert va._new_env_prereq_findings({K + "enabled": True}, ENV) == ([], [
        f"{CHECK}`{ENV}` renders no Linux VM (`svc`, `mongo` or `rabbit`) from "
        "`deployLinuxServicesK8s`, so KCC creates no VM for it. Enable the roles it "
        "needs, unless it runs with no VM on purpose."])


def test_kcc_off_with_a_role_does_not_need_hosting_id():
    flat = _flat({"svc": {"createNewBootDisk": True}}, on=False, hosting_id=None)
    assert va._new_env_prereq_findings(flat, ENV)[1] == [
        f"{CHECK}`{ENV}` renders no Linux VM (`svc`, `mongo` or `rabbit`) from "
        "`deployLinuxServicesK8s`, so KCC creates no VM for it. Enable the roles it "
        "needs, unless it runs with no VM on purpose."]


def test_hosting_id_line_comes_before_disk_and_boot_checks():
    flat = _flat({"svc": {"machineType": "n4-standard-2"}}, hosting_id=None)
    lines = va._new_env_prereq_findings(flat, ENV)[1]
    assert lines[0] == LINE
    assert "hyperdisk-balanced" in lines[1]
    assert "adopts a boot disk" in lines[2]


def test_mongo_and_rabbit_are_named():
    flat = _flat({"mongo": {"createNewBootDisk": True},
                  "rabbit": {"createNewBootDisk": True}}, hosting_id=None)
    line = va._new_env_prereq_findings(flat, ENV)[1][0]
    assert "`mongo`" in line and "`rabbit`" in line and "`svc`" not in line


def test_nothing_lifts_the_gate():
    assert cr.GATES["kcc_hosting_id"][1] is None
    assert cr.GATES["kcc_hosting_id"][2] == "blocked"


def test_new_env_prereqs_fail_the_build(monkeypatch):
    _serve(monkeypatch, {NEW: _doc(["machineType: n2d-standard-2",
                                    "createNewBootDisk: true"], hosting_id=None)})
    gates, lines = m._new_env_prereqs([_cand(NEW)], PR_SHA)
    assert gates == [{"kind": "kcc_hosting_id", "env": ENV, "why": WHY}]
    assert lines == {NEW: [LINE]}


def test_hosting_id_on_a_parent_config_is_enough(monkeypatch):
    parent = KCC_ON.replace("appspace:\n", 'appspace:\n  hostingID: "00000000"\n')
    _serve(monkeypatch, {NEW: _doc(["createNewBootDisk: true"], hosting_id=None),
                         GCP: parent})
    assert m._new_env_prereqs([_cand(NEW)], PR_SHA) == ([], {NEW: []})
