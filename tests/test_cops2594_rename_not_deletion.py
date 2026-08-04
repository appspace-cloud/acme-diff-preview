"""A renamed resource must not be reported as a deletion (COPS-2594).

Field report: acme-config-prod PR 3882, an AU version bump, was headlined
"54 RESOURCE(S) DELETED" with several entries carrying the sensitive-kind
lock flag. The PR was fine. Between chart 2602.2 and 2603.0 the
`mediatransform` microservice moves from a per-pod workload-identity IAM
binding to a dedicated Google service account, so resources are recreated
under new names in the SAME PR:

  pv-ato-c-ss   Job  ...acme-secret-generator-cb71f3d8  ->  ...-3abbd629
  pv-ato-c-glb  IAMPolicyMember  ...-mediatransform-access
                                 ->  ...-mediatransform-gsa-access

Reporting those as deletions is not a cosmetic problem. The deleted block
exists because of PR 6773, where two REAL deletions were invisible and the
comment said "no critical changes detected". Filling it with renames
teaches reviewers to skim the one block they must read.

The pairing is deliberately narrow, because a false "rename" would
SUPPRESS a real deletion warning, which is strictly worse than the noise
being fixed. Two rules, both also requiring the same resource kind:

  A. hash rename        - identical except the final `-<token>` segment
  B. one token inserted - hyphen-token lists differ by exactly one token
     or removed

Anything else stays a deletion.
"""
import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m


def _del_body(name):
    """A pure deletion section: all minus lines, no context, no plus."""
    return ("--- \n+++ \n"
            "-apiVersion: batch/v1\n"
            "-kind: Job\n"
            f"-  name: {name}\n")


def _add_body(name):
    """A pure creation section: all plus lines, no context, no minus."""
    return ("--- \n+++ \n"
            "+apiVersion: batch/v1\n"
            "+kind: Job\n"
            f"+  name: {name}\n")


JOB_OLD = "/batch/Job acme-secret-generator/pv-ato-c-acme-secret-generator-cb71f3d8"
JOB_NEW = "/batch/Job acme-secret-generator/pv-ato-c-acme-secret-generator-3abbd629"
IAM_OLD = ("/iam.cnrm.cloud.google.com/IAMPolicyMember "
           "pv-ato-content-au1-a.appspacestorage.com-mediatransform-access")
IAM_NEW = ("/iam.cnrm.cloud.google.com/IAMPolicyMember "
           "pv-ato-content-au1-a.appspacestorage.com-mediatransform-gsa-access")


# ── creation detection ─────────────────────────────────────────────────────

def test_detects_a_pure_creation():
    secs = [(JOB_NEW, _add_body("pv-ato-c-acme-secret-generator-3abbd629"))]
    assert m._detect_created_resources(secs) == [JOB_NEW]


def test_partial_change_is_not_a_creation():
    """A section with context lines is an edit, not a creation."""
    body = "--- \n+++ \n kind: Job\n+  newField: x\n"
    assert m._detect_created_resources([("/batch/Job ns/j", body)]) == []


# ── Rule A: hash rename (the secret-generator Jobs) ────────────────────────

def test_hash_rename_is_not_a_deletion():
    deleted = [JOB_OLD]
    created = [JOB_NEW]
    real, renames = m._split_renames_from_deletions(deleted, created)
    assert real == []
    assert renames == [(JOB_OLD, JOB_NEW)]


# ── Rule B: one token inserted (the mediatransform IAM binding) ────────────

def test_single_token_insertion_is_not_a_deletion():
    real, renames = m._split_renames_from_deletions([IAM_OLD], [IAM_NEW])
    assert real == []
    assert renames == [(IAM_OLD, IAM_NEW)]


def test_single_token_removal_is_also_a_rename():
    real, renames = m._split_renames_from_deletions([IAM_NEW], [IAM_OLD])
    assert real == []
    assert renames == [(IAM_NEW, IAM_OLD)]


# ── the guards: a real deletion must NEVER be reclassified ─────────────────

def test_deletion_with_no_creation_stays_a_deletion():
    real, renames = m._split_renames_from_deletions([IAM_OLD], [])
    assert real == [IAM_OLD]
    assert renames == []


def test_different_kind_is_never_paired():
    """Same name, different kind: that is a delete plus an unrelated create."""
    a = "/v1/Secret ns/thing-aaaaaaaa"
    b = "/v1/ConfigMap ns/thing-bbbbbbbb"
    real, renames = m._split_renames_from_deletions([a], [b])
    assert real == [a]
    assert renames == []


def test_two_tokens_different_is_never_paired():
    """Too far apart to claim a rename. Stay loud."""
    a = "/v1/Secret ns/alpha-one-two"
    b = "/v1/Secret ns/alpha-three-four"
    real, renames = m._split_renames_from_deletions([a], [b])
    assert real == [a]
    assert renames == []


def test_unrelated_names_are_never_paired():
    a = "/v1/Secret ns/database-credentials"
    b = "/v1/Secret ns/totally-unrelated-thing"
    real, renames = m._split_renames_from_deletions([a], [b])
    assert real == [a]
    assert renames == []


def test_one_creation_pairs_with_only_one_deletion():
    """Two deletions, one creation: only one can be the rename, the other
    is a genuine deletion and must survive as such."""
    d1 = "/batch/Job ns/thing-aaaaaaaa"
    d2 = "/batch/Job ns/other-bbbbbbbb"
    c1 = "/batch/Job ns/thing-cccccccc"
    real, renames = m._split_renames_from_deletions([d1, d2], [c1])
    assert real == [d2]
    assert renames == [(d1, c1)]


# ── the latent bug found while fixing the above ────────────────────────────
# _section_kind split on the LAST slash first, so for a namespaced header it
# returned the resource NAME instead of the kind. Every namespaced Secret,
# ExternalSecret, RoleBinding etc. was therefore NOT recognised as sensitive:
# no lock flag in the deleted block, and no reserved display slot. This is
# the same class of miss the block exists to prevent.

def test_section_kind_reads_the_kind_not_the_name_when_namespaced():
    assert m._section_kind("/v1/Secret my-ns/db-credentials") == "Secret"
    assert m._section_kind("/batch/Job acme-secret-generator/pv-x-job") == "Job"


def test_section_kind_still_right_without_a_namespace():
    assert m._section_kind("/v1/Secret db-credentials") == "Secret"
    assert m._section_kind(
        "/external-secrets.io/ExternalSecret card-deployment-key") == "ExternalSecret"


def test_namespaced_secret_is_flagged_sensitive():
    """The regression that mattered: this returned False before."""
    assert m._is_sensitive_kind("/v1/Secret my-ns/db-credentials") is True
    assert m._is_sensitive_kind("/rbac.authorization.k8s.io/RoleBinding ns/rb") is True


def test_section_name_strips_the_namespace():
    assert m._section_name(
        "/batch/Job acme-secret-generator/pv-x-job-cb71f3d8") == "pv-x-job-cb71f3d8"
    assert m._section_name("/v1/Secret db-credentials") == "db-credentials"


# ── end-to-end through the comment ─────────────────────────────────────────

def test_comment_reports_a_rename_without_the_deletion_banner():
    res = m.DiffResult("d", [], 1, True, "", m.OUT_DIFF, "", None,
                       [], None, None, [(JOB_OLD, JOB_NEW)])
    body = m.format_comment("c" * 12, {"pv-ato-c-ss": res})
    assert "RESOURCE(S) DELETED" not in body
    assert "RENAMED" in body.upper()
    assert "3abbd629" in body


def test_comment_still_shouts_for_a_genuine_deletion():
    res = m.DiffResult("d", [], 1, True, "", m.OUT_DIFF, "", None,
                       ["/v1/Secret my-ns/db-credentials"], None, None, None)
    body = m.format_comment("c" * 12, {"pv-x-ss": res})
    assert "RESOURCE(S) DELETED" in body
    assert "\U0001f510" in body, "a namespaced Secret must carry the sensitive flag"
