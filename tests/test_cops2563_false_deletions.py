"""Minus-only partial changes must not be reported as full deletions (COPS-2563).

acme-config-prod PR 3829 (enable acme-ping-scaler for pv-chaostest-a) said
"110 RESOURCE(S) DELETED ... removes the following resources entirely" for 110
Deployments whose only real change was ONE removed line (`replicas: N` -- the
chart stops rendering the field once the scaler owns it). acme-config-dev PR
6956 (remove microservices.defaults.tolerations) said 2480 the same way. The
detail section of the same comment showed them correctly as changed, so the
comment contradicted itself, and the same false list was fed to the AI summary
as an authoritative fact.

Root cause: _detect_deleted_resources flagged "at least one minus line and no
plus lines", which is also the signature of a change that only REMOVES lines
from a manifest that still exists. The real discriminator: section bodies are
built by difflib.unified_diff with its default 3 context lines, so a partial
change where any line survives always carries context lines, while a manifest
diffed against empty (a true deletion) never does. Deleted therefore means:
minus lines present, zero plus lines, zero context lines.
"""
import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m

# The PR-3829 shape: one removed line inside a surviving manifest.
REPLICAS_REMOVED = (
    "--- \n+++ \n@@ -20,7 +20,6 @@\n"
    "     app.kubernetes.io/deployment-type: \"pv-prod\"\n"
    " spec:\n"
    "   \n"
    "-  replicas: 2\n"
    "   \n"
    "   strategy:\n"
    "     rollingUpdate:\n")

# The PR-6956 shape: a whole block removed, manifest survives.
TOLERATIONS_REMOVED = (
    "--- \n+++ \n@@ -30,10 +30,6 @@\n"
    "       serviceAccountName: x\n"
    "-      tolerations:\n"
    "-      - effect: NoSchedule\n"
    "-        key: cloud.google.com/gke-spot\n"
    "-        operator: Equal\n"
    "       containers:\n")

# A genuine full deletion: manifest diffed against empty, no context lines.
TRUE_DELETION = (
    "--- \n+++ \n@@ -1,6 +0,0 @@\n"
    "-apiVersion: v1\n-kind: Service\n-metadata:\n"
    "-  name: gone\n-spec:\n-  ports: []\n")


def test_minus_only_partial_change_is_not_a_deletion():
    secs = [("/apps/Deployment accesscontrol-background", REPLICAS_REMOVED),
            ("/apps/Deployment broadcast", TOLERATIONS_REMOVED)]
    assert m._detect_deleted_resources(secs) == []


def test_true_deletion_is_still_flagged_among_partials():
    secs = [("/apps/Deployment broadcast", REPLICAS_REMOVED),
            ("/v1/Service gone", TRUE_DELETION),
            ("/apps/Deployment other", TOLERATIONS_REMOVED)]
    assert m._detect_deleted_resources(secs) == ["/v1/Service gone"]


def test_end_to_end_replicas_removal_produces_no_deletion():
    """Through the REAL producer (_diff_resources via _diff_manifests), so
    the detector's context-line assumption stays pinned to what unified_diff
    actually emits. If the diff is ever switched to zero context lines, this
    test is the tripwire that fires."""
    main_yaml = (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n"
        "spec:\n  replicas: 2\n  selector:\n    matchLabels:\n      app: web\n"
        "  template:\n    metadata:\n      labels:\n        app: web\n")
    pr_yaml = main_yaml.replace("  replicas: 2\n", "")
    diff = m._diff_manifests(main_yaml, pr_yaml)
    secs = m.parse_diff_sections(diff)
    assert secs, "removing a line must still produce a changed section"
    assert m._detect_deleted_resources(secs) == []


def test_end_to_end_true_deletion_still_flagged():
    main_yaml = (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n"
        "spec:\n  replicas: 2\n"
        "---\n"
        "apiVersion: v1\nkind: Service\nmetadata:\n  name: web-svc\n"
        "spec:\n  ports: []\n")
    pr_yaml = main_yaml.split("---\n")[0]
    diff = m._diff_manifests(main_yaml, pr_yaml)
    secs = m.parse_diff_sections(diff)
    deleted = m._detect_deleted_resources(secs)
    assert len(deleted) == 1 and "Service" in deleted[0], deleted
