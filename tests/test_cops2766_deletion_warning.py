"""COPS-2766: a deleted resource is a warning, not a merge block.

The deletion finding was the only BLOCK that never changed the build status,
so ⛔ went on every planned cleanup while the check stayed green. Approvers
learned to merge through it (12 PRs approved within 90 s). It is REVIEW now,
and the bullet says which kinds go, so the fact is still there to read:

  🗑️ **N resource(s) deleted** in E environment(s) (2 Kind, 1 Kind, +K kind(s)): envs

GCP (KCC) kinds come first, then the count. Three kinds are all named.

A data or public-address kind replaces the top-two list, is always named,
and leads the bullet, so a bucket cannot hide among IAM bindings
(acme-config-prod #4672), not even in the ~30 characters Bitbucket shows of
the green status:

  🗑️ **Data or IP deleted: 1 StorageBucket, ...** (N resource(s) in E environment(s)): envs
"""
import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import comment_render as cr  # noqa: E402
import diff_preview as m  # noqa: E402

KCC = "/storage.cnrm.cloud.google.com"


def _summary(deleted):
    results = {"pv-nbc-a-ms": m.DiffResult(
        "d", [], len(deleted), True, "", m.OUT_DIFF, "", None, deleted)}
    return "\n".join(cr._build_merge_summary(results, {}, None, None, None,
                                             None, False))


def test_a_deletion_is_review_and_names_its_kind():
    out = _summary(["/v1/Secret pv-nbc-a/db-credentials"])
    assert "Review before merging" in out
    assert "DO NOT MERGE" not in out
    assert ("- \U0001f5d1\ufe0f **1 resource(s) deleted** in 1 environment(s) "
            "(1 Secret): pv-nbc-a") in out, out


def test_the_top_two_kinds_lead_and_the_rest_are_counted():
    out = _summary(
        [f"/compute.cnrm.cloud.google.com/ComputeBackendService bs-{i}"
         for i in range(3)]
        + [f"/compute.cnrm.cloud.google.com/ComputeHealthCheck hc-{i}"
           for i in range(3)]
        + ["/v1/Service a", "/v1/ConfigMap b"])
    assert ("**8 resource(s) deleted** in 1 environment(s) "
            "(3 ComputeBackendService, 3 ComputeHealthCheck, +2 kind(s)): "
            "pv-nbc-a") in out, out


def test_three_kinds_are_all_named_gcp_first():
    """The #4565 shape. '+1 kind(s)' hid the BackendServices, the kind
    behind the #4684 outage."""
    out = _summary([f"/v1/Secret pv-nbc-a/s-{i}" for i in range(57)]
                   + [f"/v1/ConfigMap pv-nbc-a/c-{i}" for i in range(57)]
                   + [f"/compute.cnrm.cloud.google.com/v1beta1/"
                      f"ComputeBackendService bs-{i}" for i in range(56)])
    assert ("**170 resource(s) deleted** in 1 environment(s) "
            "(56 ComputeBackendService, 57 ConfigMap, 57 Secret): "
            "pv-nbc-a") in out, out


def test_gcp_kinds_rank_before_a_larger_core_kind():
    out = _summary(["/compute.cnrm.cloud.google.com/v1beta1/ComputeBackendService bs"]
                   + [f"/v1/Service pv-nbc-a/s-{i}" for i in range(5)]
                   + ["/v1/Secret pv-nbc-a/x", "/v1/ConfigMap pv-nbc-a/y"])
    assert "(1 ComputeBackendService, 5 Service, +2 kind(s))" in out, out


_NBC = ([f"/iam.cnrm.cloud.google.com/IAMPolicyMember pv-nbc-a/m-{i}"
         for i in range(60)]
        + [f"{KCC}/StorageBucket pv-nbc-a/uc",
           "/bigquery.cnrm.cloud.google.com/BigQueryDataset pv-nbc-a/ds",
           "/dns.cnrm.cloud.google.com/DNSRecordSet pv-nbc-a/rs",
           "/compute.cnrm.cloud.google.com/ComputeAddress pv-nbc-a/ip",
           "/compute.cnrm.cloud.google.com/ComputeAddress pv-nbc-a/ip2"])


def test_data_kinds_replace_the_top_two_and_are_all_named():
    """The #4672 shape: 60 IAM bindings would lead a count-only list."""
    out = _summary(_NBC)
    assert ("- \U0001f5d1\ufe0f **Data or IP deleted: 2 ComputeAddress, "
            "1 BigQueryDataset, 1 DNSRecordSet, 1 StorageBucket** "
            "(65 resource(s) in 1 environment(s)): pv-nbc-a") in out, out
    assert "IAMPolicyMember" not in out
    assert "Review before merging" in out


def test_data_leads_what_bitbucket_shows_of_the_green_status():
    """Bitbucket shows ~30 characters before '...'. A routine GLB cleanup
    reads '⚠️ 1044 resource(s) deleted in', so the data must come first."""
    lead = cr.status_lead(_summary(_NBC) + "\n---\n")
    assert lead.startswith("\u26a0\ufe0f Data or IP deleted: 2 ComputeAddress"), lead


def test_every_listed_data_kind_is_recognised():
    for kind in sorted(cr._DATA_KINDS):
        assert "**Data or IP deleted: 1 " + kind + "**" in \
            _summary([f"/x/{kind} ns/name"])
