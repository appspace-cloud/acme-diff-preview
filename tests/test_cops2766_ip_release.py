"""COPS-2766 block 2, item 6: a static IP or DNS record that leaves a live env.

A ComputeAddress or a DNSRecordSet that leaves the render with no explicit
`deletion-policy: abandon` is released in GCP: the IP goes back to the pool
and the name stops resolving. Customers allow-list those IPs, so a new one
is an outage. Before, three things hid it:
- the rename rule read `-ip` -> `-ip-ext` as the same object renamed. A new
  name is a new IP, so the old one is released;
- a deleted ComputeInstance, ComputeDisk or ComputeAddress with no policy line
  read as abandon, the chart default. Every kcc-linux-services template writes
  the policy on every active line, so no line means KCC's own default, delete;
- the deletion was only a warning.

Now it is the `ip` merge gate, lifted by `Confirm-IP-Release: <env>`. A
teardown is not an IP release here: its apps are OUT_DECOMMISSIONED.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import comment_render as cr  # noqa: E402
import diff_preview as m  # noqa: E402
import identity  # noqa: E402
import vm_analysis as vma  # noqa: E402

from test_coverage_orchestration import (  # noqa: E402,F401
    world, _mk_pr, PATH_MAP, BASE_SHA, PR_SHA, IDENTITY_YAML)

CA = "/compute.cnrm.cloud.google.com/ComputeAddress pv-orch-a/pv-orch-a-ip"
DNS = "/dns.cnrm.cloud.google.com/DNSRecordSet pv-orch-a/pv-orch-a-dns-record"
CI = "/compute.cnrm.cloud.google.com/ComputeInstance pv-orch-a/pv-orch-svc-a"
SECRET = "/v1/Secret pv-orch-a/pv-orch-a-ip"
TRAILER = "Confirm-IP-Release: pv-orch-a"


def _gone(kind, policy=None):
    """The body of a CR that leaves the render, with or without a policy line."""
    return ("-apiVersion: v1beta1\n" f"-kind: {kind}\n" "-metadata:\n"
            "-  name: x\n" "-  annotations:\n"
            + (f"-    cnrm.cloud.google.com/deletion-policy: {policy}\n" if policy else "")
            + "-spec:\n" "-  location: global\n")


def _new(kind):
    return f"+apiVersion: v1beta1\n+kind: {kind}\n+metadata:\n+  name: y\n"


@pytest.fixture(autouse=True)
def no_vertex(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)


# ── (a) a new name is a new IP ───────────────────────────────────────────

@pytest.mark.parametrize("old", [CA, DNS])
def test_an_address_or_record_is_never_renamed(old):
    assert not identity._is_rename_of(old, old + "-ext")
    assert not identity._is_rename_of(old + "-cb71f3d8", old + "-3abbd629")
    assert identity._split_renames_from_deletions([old], [old + "-ext"]) == ([old], [])


def test_other_kinds_keep_the_rename_rule():
    assert identity._is_rename_of(SECRET, SECRET + "-ext")


# ── (b) which deletions release an IP ────────────────────────────────────

def test_released_addresses():
    secs = [(CA, _gone("ComputeAddress", "delete")),
            (CA + "-b", _gone("ComputeAddress")),
            (CA + "-c", _gone("ComputeAddress", "abandon")),
            (DNS, _gone("DNSRecordSet", "delete")),
            (SECRET, _gone("Secret")),
            (CA + "-d", "-  address: 10.0.0.1\n+  address: 10.0.0.2\n")]
    deleted = [h for h, _ in secs[:5]]
    assert vma._released_addresses(secs, deleted) == [CA, CA + "-b", DNS]
    assert vma._released_addresses(secs, []) == []
    assert vma._released_addresses([], None) == []


def test_argocd_diff_counts_a_renamed_address_as_released(monkeypatch):
    """On the full list, before the cap. The abandon rename is kept in GCP."""
    diff = "".join(f"===== {h} =====\n{b}" for h, b in [
        (CA, _gone("ComputeAddress", "delete")), (CA + "-ext", _new("ComputeAddress")),
        (DNS, _gone("DNSRecordSet", "abandon")), (DNS + "-ext", _new("DNSRecordSet"))])
    monkeypatch.setattr(m, "_run_one_diff", lambda *a, **k: (diff, None, None))
    r = m.argocd_diff("pv-orch-a-glb", PR_SHA, BASE_SHA)
    assert r.outcome == m.OUT_DIFF
    assert r.ip_released == [CA]
    assert set(r.deleted_resources) == {CA, DNS} and not r.renamed_resources


def test_ip_released_defaults_to_none():
    assert m.DiffResult("", [], 0, False, None, m.OUT_NO_DIFF, "").ip_released is None


# ── (d) no policy line is KCC's default, delete ──────────────────────────

@pytest.mark.parametrize("kind", ["ComputeInstance", "ComputeDisk", "ComputeAddress"])
def test_a_deleted_cr_with_no_policy_line_is_dangerous(kind):
    f = vma._detect_vm_changes([(CI.replace("ComputeInstance", kind), _gone(kind))])[0]
    assert f["deleted"] and not f.get("orphaned")
    assert f["dangerous"] == [f"{kind} removed from the render entirely (an enabled "
                              "flag turned off, or the environment dropped the domain)"]


def test_explicit_abandon_is_still_an_orphan():
    f = vma._detect_vm_changes([(CI, _gone("ComputeInstance", "abandon"))])[0]
    assert f.get("orphaned") and not f["dangerous"]


# ── _merge_gates ─────────────────────────────────────────────────────────

def _ip_result(released):
    return m.DiffResult("", [], 1, True, None, m.OUT_DIFF, "changes", ip_released=released)


def test_merge_gates_adds_one_ip_gate_per_env():
    results = {"pv-orch-a-ss": _ip_result([CA]), "pv-orch-a-glb": _ip_result([DNS]),
               "pv-orch-b-ss": _ip_result([]),
               "pv-orch-c-ss": m.DiffResult("", [], 0, False, None, m.OUT_DECOMMISSIONED, "")}
    assert m._merge_gates(None, None, None, None, results) == [
        {"kind": "ip", "env": "pv-orch-a", "arg": "pv-orch-a", "lifted": False}]
    assert m._merge_gates(None, None, None, None, None) == []


# ── (c) process_pr end to end ────────────────────────────────────────────

IP_DESC = (f"Blocked - {cr.GATES['ip'][0]} in pv-orch-a. To merge anyway, add "
           f"'{TRAILER}' to a commit message (see PR comment)")


@pytest.fixture()
def ip_pr(world, monkeypatch):
    """pv-orch-a-ss loses one CR; the PR's commits carry `messages`, and None
    means they must not be read."""
    sinks, plan = world

    def run(header, body, messages):
        secs = [(header, body)]
        plan["pv-orch-a-ss"] = m.DiffResult(
            "--- main\n+++ pr", secs, 1, True, "", m.OUT_DIFF, "",
            deleted_resources=[header], vm_changes=vma._detect_vm_changes(secs),
            ip_released=vma._released_addresses(secs, [header]))
        monkeypatch.setattr(m, "_pr_commit_messages", lambda *a: messages
                            if messages is not None else pytest.fail("no gate"))
        m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
        return sinks.upserts[-1], sinks.statuses[-1]
    return run


@pytest.mark.parametrize("header,kind", [(CA, "ComputeAddress"), (DNS, "DNSRecordSet")])
def test_a_live_env_losing_an_ip_is_blocked(ip_pr, header, kind):
    body, (state, desc) = ip_pr(header, _gone(kind, "delete"), ["Drop the old IP"])
    assert m._extract_status_token(body) == "blocked"
    assert state == "FAILED" and desc == IP_DESC
    assert (f"- ⛔ **{cr.GATES['ip'][0]}** in `pv-orch-a` - to merge anyway, "
            f"add `{TRAILER}` to a commit message") in body
    assert f"Data or IP deleted: 1 {kind}" in body


def test_the_trailer_lifts_it(ip_pr):
    body, (state, _desc) = ip_pr(CA, _gone("ComputeAddress", "delete"),
                                 [f"Move to the new IP\n\n{TRAILER}\n"])
    assert m._extract_status_token(body) == "clean" and state == "SUCCESSFUL"
    assert f"☑️ **Confirmed in a commit:** `{TRAILER}`" in body


def test_a_trailer_for_another_env_does_not(ip_pr):
    body, (state, _desc) = ip_pr(CA, _gone("ComputeAddress"),
                                 ["Confirm-IP-Release: pv-orch-b"])
    assert m._extract_status_token(body) == "blocked" and state == "FAILED"


def test_an_abandon_removal_is_clean_and_never_reads_the_commits(ip_pr):
    body, (state, _desc) = ip_pr(CA.replace("pv-orch-a-ip", "pv-orch-svc-a-iip"),
                                 _gone("ComputeAddress", "abandon"), None)
    assert m._extract_status_token(body) == "clean" and state == "SUCCESSFUL"
    assert "**1 KCC resource(s) unmanaged** (abandon / schedule attachment" in body
    assert cr.GATES["ip"][0] not in body
