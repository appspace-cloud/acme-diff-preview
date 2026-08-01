"""COPS-2572: tell the reviewer the truth about a cascade delete.

Two separate lies the decommission warning currently tells when an
environment has opted into cascade deletion.

1. It counts every rendered resource as "will be removed". ArgoCD does not
   delete all of them. `shouldBeDeleted` in controller/appcontroller.go
   excludes three classes:

       !kube.IsCRD(obj) && !isSelfReferencedApp(...) &&
       (deleteOption == nil || *deleteOption != "false") &&
       !HasAnnotationOption(obj, helm.ResourcePolicyAnnotation, keep)

   On the pv-qa-99-a pilot the warning promised 513 removals and 507
   happened: 5 shared MongoDB CRDs and the namespace survived. Overstating
   a destructive number is bad, but silently hiding survivors is worse:
   the CRDs are shared with other environments, and the namespace surviving
   is exactly what left 12 Kyverno-cloned secrets behind.

2. It cannot distinguish the two armed states. With `decommission` alone
   the BigQuery dataset and the content bucket are abandoned and
   recoverable. With `decommissionPurgeData` as well they are emptied and
   deleted for good. Those are very different PRs to approve and the
   reviewer currently cannot tell them apart.
"""
import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m

IDENT = "gcp/prod/private-cloud/eu1-b/weekly/pv-foo-c/customer.yaml"
NOT_ARMED = "---\nappspace:\n  customerName: foo\n  version: 1.0.0\n"
CASCADE_ONLY = "---\nappspace:\n  customerName: foo\n  decommission: true\n"
CASCADE_AND_PURGE = ("---\nappspace:\n  customerName: foo\n"
                     "  decommission: true\n  decommissionPurgeData: true\n")
PURGE_WITHOUT_CASCADE = ("---\nappspace:\n  customerName: foo\n"
                         "  decommissionPurgeData: true\n")

PLAIN_DEPLOY = "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n"
A_CRD = ("apiVersion: apiextensions.k8s.io/v1\nkind: CustomResourceDefinition\n"
         "metadata:\n  name: mongodbs.mongodbcommunity.mongodb.com\n")
KEEP_NS = ("apiVersion: v1\nkind: Namespace\nmetadata:\n  name: pv-foo-c\n"
           "  annotations:\n    helm.sh/resource-policy: keep\n")
DELETE_FALSE_SECRET = ("apiVersion: v1\nkind: Secret\nmetadata:\n  name: keepme\n"
                       "  annotations:\n"
                       "    argocd.argoproj.io/sync-options: Delete=false\n")


def _candidate():
    return {"env_name": "pv-foo-c", "identity_file": IDENT,
            "apps": ["pv-foo-c-ms"], "env_dir": os.path.dirname(IDENT)}


def _fetch(main_content):
    def fake(path, sha, repo=None):
        if sha == "prsha":
            return (None, m.BB_NOT_FOUND)
        return (main_content, m.BB_OK)
    return fake


def _run(monkeypatch, identity_content, resources):
    monkeypatch.setattr(m, "_bb_fetch_status", _fetch(identity_content))
    monkeypatch.setattr(m, "_render_main_side_resources", lambda app, sha: resources)
    lines, _envs = m._evaluate_env_decommissions([_candidate()], "prsha", "mainsha")
    return "\n".join(lines)


# --- 1. the count must exclude what ArgoCD will not delete -----------------

def test_crds_are_not_counted_as_removed(monkeypatch):
    """CRDs are shared with other environments. Promising to delete them is
    both wrong and alarming."""
    body = _run(monkeypatch, CASCADE_ONLY, {
        ("apps/Deployment", "ns", "web"): PLAIN_DEPLOY,
        ("apiextensions.k8s.io/CustomResourceDefinition", "", "mongodbs"): A_CRD,
    })
    assert "1 total" in body, f"expected only the Deployment counted:\n{body}"
    assert "2 total" not in body


def test_resource_policy_keep_is_not_counted_as_removed(monkeypatch):
    """The namespace carries this. Its survival is what left 12 cloned
    secrets behind on pv-qa-99-a."""
    body = _run(monkeypatch, CASCADE_ONLY, {
        ("apps/Deployment", "ns", "web"): PLAIN_DEPLOY,
        ("v1/Namespace", "", "pv-foo-c"): KEEP_NS,
    })
    assert "1 total" in body, f"expected only the Deployment counted:\n{body}"


def test_delete_false_is_not_counted_as_removed(monkeypatch):
    body = _run(monkeypatch, CASCADE_ONLY, {
        ("apps/Deployment", "ns", "web"): PLAIN_DEPLOY,
        ("v1/Secret", "ns", "keepme"): DELETE_FALSE_SECRET,
    })
    assert "1 total" in body, f"expected only the Deployment counted:\n{body}"


def test_survivors_are_reported_not_silently_dropped(monkeypatch):
    """Hiding them is worse than overcounting: the reviewer needs to know
    what is left behind and has to be cleaned up by hand."""
    body = _run(monkeypatch, CASCADE_ONLY, {
        ("apps/Deployment", "ns", "web"): PLAIN_DEPLOY,
        ("apiextensions.k8s.io/CustomResourceDefinition", "", "mongodbs"): A_CRD,
        ("v1/Namespace", "", "pv-foo-c"): KEEP_NS,
    })
    low = body.lower()
    assert "retained" in low or "survive" in low or "not deleted" in low, body
    assert "CustomResourceDefinition" in body, "must name what survives"
    assert "Namespace" in body


def test_no_survivor_section_when_everything_is_deleted(monkeypatch):
    """No noise on the common case."""
    body = _run(monkeypatch, CASCADE_ONLY, {
        ("apps/Deployment", "ns", "web"): PLAIN_DEPLOY,
    })
    assert "1 total" in body
    assert "retained" not in body.lower()


def test_orphan_branch_still_counts_everything(monkeypatch):
    """When nothing cascades, every rendered resource really is left running,
    including the CRDs. The exclusions must not leak into this branch."""
    body = _run(monkeypatch, NOT_ARMED, {
        ("apps/Deployment", "ns", "web"): PLAIN_DEPLOY,
        ("apiextensions.k8s.io/CustomResourceDefinition", "", "mongodbs"): A_CRD,
    })
    assert "2 total" in body, f"orphan branch must not exclude anything:\n{body}"


# --- 2. the two armed states are not the same PR ---------------------------

def test_cascade_without_purge_says_data_is_kept(monkeypatch):
    body = _run(monkeypatch, CASCADE_ONLY, {("apps/Deployment", "ns", "web"): PLAIN_DEPLOY})
    low = body.lower()
    assert "purge" not in low or "not" in low, body
    assert "abandon" in low or "kept" in low or "recoverable" in low, body


def test_purge_armed_is_called_out_loudly(monkeypatch):
    body = _run(monkeypatch, CASCADE_AND_PURGE, {("apps/Deployment", "ns", "web"): PLAIN_DEPLOY})
    low = body.lower()
    assert "purge" in low, f"must say data will be purged:\n{body}"
    assert "bigquery" in low, "name what gets destroyed"
    assert "bucket" in low


def test_purge_flag_alone_does_not_claim_a_purge(monkeypatch):
    """decommissionPurgeData is inert without decommission. The chart gates on
    both, so the warning must too, or it scares people over a no-op."""
    body = _run(monkeypatch, PURGE_WITHOUT_CASCADE, {("apps/Deployment", "ns", "web"): PLAIN_DEPLOY})
    low = body.lower()
    assert "orphan" in low or "left running" in low, "still the orphan case"
    assert "will be purged" not in low, f"claims a purge that cannot happen:\n{body}"
