"""COPS-2766 row 17: a rename pairs only a hash, not another object.

Rule A of `_is_rename_of` paired any two names that differ in the final
token. In the replay that was 76 pairs, and 55 of them were not renames but
backend swaps: acme-config-prod #4684 deleted `bs-ncdn` and created
`bs-dvc`, and the RENAMED panel said "so nothing is lost".

Now Rule A needs a hash in the last token on both sides: 6 or more hex
characters (not 8, the uptime policy name uses `trunc 6`). A word there is
another object, so the pair stays a deletion, and since block 1 a deletion
is a REVIEW item with a green build. Rule B (one token added or removed)
does not change.
"""
import os
import sys

import pytest

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m  # noqa: E402

BS = "/compute.cnrm.cloud.google.com/ComputeBackendService"
HC = "/compute.cnrm.cloud.google.com/ComputeHealthCheck"
BQ = "/bigquery.cnrm.cloud.google.com/BigQueryDataset"
JOB = "/batch/Job acme-secret-generator"
UP = "/monitoring.cnrm.cloud.google.com/MonitoringAlertPolicy"
IAM = "/iam.cnrm.cloud.google.com/IAMPolicyMember"


@pytest.mark.parametrize("old, new", [
    # #4684: two backends swapped for two other backends.
    (f"{BS} pv-intramax-a/pv-intramax-a-bs-ncdn",
     f"{BS} pv-intramax-a/pv-intramax-a-bs-dvc"),
    (f"{BS} pv-intramax-a/pv-intramax-a-bs-cdn",
     f"{BS} pv-intramax-a/pv-intramax-a-bs-acl"),
    # #4669: a health check and a backend.
    (f"{HC} pv-bcg-a/pv-bcg-a-hc-envoy",
     f"{HC} pv-bcg-a/pv-bcg-a-hc-analytics"),
    (f"{BS} pv-bcg-a/pv-bcg-a-bs-envoy",
     f"{BS} pv-bcg-a/pv-bcg-a-bs-agw"),
    # #4602 and #4565.
    (f"{BS} pv-dpdhl-c/pv-dpdhl-c-bs-dvc",
     f"{BS} pv-dpdhl-c/pv-dpdhl-c-bs-acc"),
    (f"{BS} pv-3ds-c/pv-3ds-c-bs-cdn",
     f"{BS} pv-3ds-c/pv-3ds-c-bs-pcs"),
    # #4351: a suffix change is another dataset, not a rename.
    (f"{BQ} pv-gsk--aec1-c/pv-gsk--aec1-analytics-a",
     f"{BQ} pv-gsk--aec1-c/pv-gsk--aec1-analytics-c"),
    # 5 hex characters is too short to be a hash.
    (f"{JOB}/pv-x-a-job-abc12", f"{JOB}/pv-x-a-job-def34"),
    # A hash on one side only.
    (f"{JOB}/pv-x-a-name-3abbd629", f"{JOB}/pv-x-a-name-v2"),
])
def test_another_object_is_not_a_rename(old, new):
    assert m._is_rename_of(old, new) is False


@pytest.mark.parametrize("old, new", [
    # The secret-generator Job carries an 8-hex content hash.
    (f"{JOB}/pv-ato-c-acme-secret-generator-cb71f3d8",
     f"{JOB}/pv-ato-c-acme-secret-generator-3abbd629"),
    # The uptime policy name uses `trunc 6`.
    (f"{UP} pv-x-a/pv-x-a-uptime-3f2a1b", f"{UP} pv-x-a/pv-x-a-uptime-9c0d4e"),
    # Rule B does not change.
    (f"{IAM} pv-ato-content-au1-a.appspacestorage.com-mediatransform-access",
     f"{IAM} pv-ato-content-au1-a.appspacestorage.com-mediatransform-gsa-access"),
])
def test_a_hash_rename_still_pairs(old, new):
    assert m._is_rename_of(old, new) is True


def test_the_4684_backend_swap_stays_two_deletions():
    deleted = [f"{BS} pv-intramax-a/pv-intramax-a-bs-cdn",
               f"{BS} pv-intramax-a/pv-intramax-a-bs-ncdn"]
    created = [f"{BS} pv-intramax-a/pv-intramax-a-bs-acl",
               f"{BS} pv-intramax-a/pv-intramax-a-bs-dvc"]
    real, renames = m._split_renames_from_deletions(deleted, created)
    assert real == deleted
    assert renames == []


def test_the_renamed_panel_does_not_say_nothing_is_lost():
    old = f"{JOB}/pv-ato-c-acme-secret-generator-cb71f3d8"
    new = f"{JOB}/pv-ato-c-acme-secret-generator-3abbd629"
    res = m.DiffResult("d", [], 1, True, "", m.OUT_DIFF, "", None,
                       [], None, None, [(old, new)])
    body = m.format_comment("c" * 12, {"pv-ato-c-ss": res})
    assert "nothing is lost" not in body
    assert ("Deleted and recreated under a new name in this PR. Common when "
            "a name carries a content hash, or when a resource moves to a "
            "new identity.") in body
