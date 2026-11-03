"""A suffix migration (pv-<customer>-a to pv-<customer>-b) gives both cells the
same public LB record. The new cell must not take the name before the cutover,
two cells must never write it at once, and the old cell must not delete it.
"""
import copy
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import suffix_migration as sm  # noqa: E402
from manifest import _flatten_yaml  # noqa: E402


def cell(suffix="a", tier="pv-prod", cloud="gcp", dns=None, **appspace):
    values = {"appspace": {
        "environmentType": "private-cloud", "cloudShortName": cloud, "deploymentType": tier,
        "prefix": "pv", "customerName": "example", "suffix": suffix, "version": "2603.4.10",
        "infra": {"gcp": {"project": "example-project"}},
    }}
    if dns is not None:
        values["appspace"]["loadBalancers"] = {"dns": {"managedZone": {"enabled": dns}}}
    for key, value in appspace.items():
        node = values["appspace"]
        *path, leaf = key.split("__")
        for p in path:
            node = node.setdefault(p, {})
        node[leaf] = value
    return _flatten_yaml(values)


def kinds(base, head):
    return sorted((f["kind"], f["env"]) for f in sm.findings(base, head))


def test_both_cells_have_the_same_record():
    a, b = cell("a", dns=True), cell("b", dns=True)
    assert sm.record_owner(a) == sm.record_owner(b) == ("appspace-com", "example.cloud.appspace.com")


def test_new_cell_with_the_record_on_is_blocked_in_prod():
    base = {"pv-example-a": cell("a")}
    head = {**base, "pv-example-b": cell("b", dns=True, zeroPods=True)}
    assert kinds(base, head) == [("dns_new_sibling", "pv-example-b")]


def test_new_cell_takes_a_name_served_by_an_sfw_cell():
    base = {"pv-example-a": cell("a", cloudServices__cloudSfwAddress="example-sfw")}
    head = {**base, "pv-example-b": cell("b", dns=True, zeroPods=True)}
    assert kinds(base, head) == [("dns_new_sibling", "pv-example-b")]


def test_new_cell_in_dev_is_blocked_too():
    base = {"pv-example-a": cell("a", tier="pv-dev", dns=False)}
    head = {**base, "pv-example-b": cell("b", tier="pv-dev", dns=True, zeroPods=True)}
    assert kinds(base, head) == [("dns_new_sibling", "pv-example-b")]


def test_new_cell_with_the_record_off_passes_the_dns_check():
    base = {"pv-example-a": cell("a", dns=True)}
    head = {**base, "pv-example-b": cell("b", dns=False, zeroPods=True)}
    assert kinds(base, head) == []


def test_new_cell_while_the_old_one_writes_the_record():
    base = {"pv-example-a": cell("a", dns=True)}
    head = {**base, "pv-example-b": cell("b", dns=True, zeroPods=True)}
    assert kinds(base, head) == [("dns_two_owners", "pv-example-b")]


def test_off_and_on_in_one_pr_is_blocked():
    base = {"pv-example-a": cell("a", dns=True), "pv-example-b": cell("b", dns=False)}
    head = {"pv-example-a": cell("a", dns=False), "pv-example-b": cell("b", dns=True)}
    found = sm.findings(base, head)
    assert [(f["kind"], f["env"], f["others"]) for f in found] == [
        ("dns_two_owners", "pv-example-b", ["pv-example-a"])]
    assert "on main" in found[0]["text"]


def test_cutover_after_the_old_cell_turned_it_off_passes():
    base = {"pv-example-a": cell("a", dns=False), "pv-example-b": cell("b", dns=False)}
    head = {**base, "pv-example-b": cell("b", dns=True)}
    assert kinds(base, head) == []


def test_turning_the_old_cell_off_is_silent():
    base = {"pv-example-a": cell("a", dns=True), "pv-example-b": cell("b", dns=False)}
    head = {**base, "pv-example-a": cell("a", dns=False)}
    assert kinds(base, head) == []


def test_a_test_name_then_the_real_name():
    base = {"pv-example-a": cell("a", dns=True),
            "pv-example-b": cell("b", dns=True, customerSubdomain="example-new")}
    assert kinds({"pv-example-a": base["pv-example-a"]}, base) == []
    head = {**base, "pv-example-b": cell("b", dns=True)}
    assert kinds(base, head) == [("dns_two_owners", "pv-example-b")]


def test_removing_the_old_folder_while_turning_the_new_one_on():
    base = {"pv-example-a": cell("a", dns=True), "pv-example-b": cell("b", dns=False)}
    head = {"pv-example-b": cell("b", dns=True)}
    assert kinds(base, head) == [("dns_two_owners", "pv-example-b")]


def test_decommission_of_the_cell_that_writes_a_shared_name():
    base = {"pv-example-a": cell("a", dns=True), "pv-example-b": cell("b", dns=False)}
    head = {**base, "pv-example-a": cell("a", dns=True, decommission=True)}
    assert kinds(base, head) == [("dns_decommission_shared", "pv-example-a")]


def test_decommission_after_the_record_is_off_passes():
    base = {"pv-example-a": cell("a", dns=False), "pv-example-b": cell("b", dns=True)}
    head = {**base, "pv-example-a": cell("a", dns=False, decommission=True)}
    assert kinds(base, head) == []


def test_another_customer_is_not_a_sibling():
    base = {"pv-example-a": cell("a", dns=True)}
    head = {**base, "pv-other-a": cell("a", dns=True, customerName="other", zeroPods=True)}
    assert kinds(base, head) == []


def test_a_copied_vm_name_is_blocked():
    svc = {"infra__deployLinuxServicesK8s__enabled": True,
           "infra__deployLinuxServicesK8s__svc__enabled": True}
    old = cell("a", **svc)
    base = {"pv-example-a": old}
    head = {**base, "pv-example-b": cell(
        "b", zeroPods=True, infra__deployLinuxServicesK8s__svc__instanceName="pv-example-svc-a", **svc)}
    assert kinds(base, head) == [("vm_shared", "pv-example-b")]
    head["pv-example-b"] = cell("b", zeroPods=True, **svc)
    assert kinds(base, head) == []



def test_azure_record_counts_from_the_flag():
    az = dict(cloud="az", infra__az__loadBalancers__nginxFrontend__subdomainType="cloud.appspace.com")
    base = {"pv-example-a": cell("a", **az)}
    head = {**base, "pv-example-b": cell("b", dns=True, zeroPods=True, **az)}
    assert kinds(base, head) == [("dns_new_sibling", "pv-example-b")]



def test_cells_with_no_public_record():
    assert sm.public_name(cell("a", environmentType="public-cloud")) is None
    assert sm.public_name(cell("a", cloud="aws")) is None
    assert sm.public_name(cell("a", customerName="")) is None
    assert sm.record_owner(cell("a", dns=True, cloudServices__cloudSfwAddress="example-sfw")) is None
    az_dev = dict(cloud="az", tier="pv-dev", dns=True)
    assert sm.record_owner(cell("a", infra__az__loadBalancers__nginxFrontend__subdomainType="cloud.appspace.com",
                                **az_dev)) is None
    assert sm.record_owner(cell("a", infra__az__loadBalancers__nginxFrontend__subdomainType="dev.appspace.com",
                                **az_dev)) == ("dev-appspace-com", "example.dev.appspace.com")



def test_a_state_already_on_main_does_not_block_another_pr():
    both = {"pv-example-a": cell("a", dns=True), "pv-example-b": cell("b", dns=True)}
    head = copy.deepcopy(both)
    head["pv-other-a"] = cell("a", customerName="other", zeroPods=True)
    assert kinds(both, head) == []
