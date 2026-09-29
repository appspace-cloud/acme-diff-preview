"""COPS-2766 block 5 S3: a NEG and its BackendService removed together.

GKE deletes a NEG only when no BackendService uses it. acme-config-prod
#3888 removed NEG Services and their BackendServices in one merge, the NEGs
got stuck, and 22 `-glb` apps went Degraded. The fix on the day was to
delete the BackendService, never the finalizer. The comment only showed a
deletion count.

Now an environment whose render loses a Service NEG annotation and deletes
a ComputeBackendService in the same PR gets a REVIEW finding that leads the
deletion bullet. `appspace.loadBalancers.gatewayApi.legacyBackends` going
from false to true (the chart default is true) gets a panel line too: the
legacy BackendServices come back in `-glb` and their NEGs in `-ms`. Both
are review items, and the build stays green.
"""
import collections
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import comment_render as cr  # noqa: E402
import diff_preview as m  # noqa: E402

from test_coverage_orchestration import (  # noqa: E402,F401
    world, _mk_pr, BASE_SHA, PR_SHA, IDENTITY, IDENTITY_YAML, ANCILLARY)

ANN = "    cloud.google.com/neg: '{\"exposed_ports\": {\"80\":{\"name\": \"pv-x-neg-usr\"}}}'"
SVC = "/Service pv-x/user"
BS = "/compute.cnrm.cloud.google.com/ComputeBackendService pv-x/pv-x-bs-user"
DASHES = ("—", "–")

DELETED_SVC = ("--- \n+++ \n-apiVersion: v1\n-kind: Service\n-metadata:\n"
               "-  annotations:\n-" + ANN + "\n-  name: user\n")
LOST_ANN = ("--- \n+++ \n@@ -1,7 +1,6 @@\n apiVersion: v1\n kind: Service\n"
            " metadata:\n   annotations:\n-" + ANN + "\n   labels:\n     app: user\n")


# ── (a) _detect_neg_removed ──────────────────────────────────────────────

def test_a_deleted_service_loses_its_neg():
    assert m._detect_neg_removed([(SVC, DELETED_SVC)]) == [SVC]


def test_a_service_that_stays_and_drops_the_annotation_loses_it():
    assert m._detect_neg_removed([(SVC, LOST_ANN)]) == [SVC]


def test_a_changed_or_added_annotation_is_not_a_loss():
    changed = LOST_ANN + "+" + ANN.replace("usr", "usr2") + "\n"
    added = LOST_ANN.replace("-" + ANN, "+" + ANN)
    assert m._detect_neg_removed([(SVC, changed), (SVC, added)]) == []


def test_only_a_service_counts():
    body = DELETED_SVC.replace("Service", "ConfigMap")
    assert m._detect_neg_removed([("/apps/Deployment pv-x/user", LOST_ANN),
                                  ("/ConfigMap pv-x/user", body)]) == []


def test_the_diff_header_lines_do_not_count():
    """'--- ' and '+++ ' open every unified diff: neither is a removed or an
    added annotation line."""
    only_hdr = "--- " + ANN + "\n+++ \n context\n"
    kept_by_hdr = "--- \n+++ " + ANN + "\n-" + ANN + "\n"
    assert m._detect_neg_removed([(SVC, only_hdr)]) == []
    assert m._detect_neg_removed([(SVC, kept_by_hdr)]) == [SVC]


def test_the_neg_status_annotation_is_not_the_neg():
    body = LOST_ANN.replace("cloud.google.com/neg:", "cloud.google.com/neg-status:")
    assert m._detect_neg_removed([(SVC, body)]) == []


# ── (b) argocd_diff carries it ───────────────────────────────────────────

def test_argocd_diff_sets_neg_removed_on_the_full_list(monkeypatch):
    made = {"text": ""}
    monkeypatch.setattr(m, "_run_one_diff",
                        lambda *a, **k: (made["text"], None, "", None, 0, None))
    made["text"] = f"===== {SVC} ======\n{LOST_ANN}"
    r = m.argocd_diff("pv-x-ms", "aaaa1111", "bbbb2222")
    assert r.outcome == m.OUT_DIFF and r.neg_removed == [SVC]
    made["text"] = ""
    r = m.argocd_diff("pv-x-ms", "cccc3333", "dddd4444")
    assert r.outcome == m.OUT_NO_DIFF and r.neg_removed is None


# ── (c) the merge summary ────────────────────────────────────────────────

def _r(neg=None, deleted=None):
    return m.DiffResult("d", [], 1, True, "", m.OUT_DIFF, "", None, deleted,
                        neg_removed=neg)


def _bullets(results, state=None):
    lines = cr._build_merge_summary(results, {}, None, None, state, None, False)
    return lines[2], [l[2:] for l in lines if l.startswith("- ")]


NEG_FINDING = ("\U0001f517 **NEG and BackendService removed together** in `pv-x`: "
               "after the sync, check `kubectl get svcneg -n <namespace>`, and if one "
               "is stuck, delete the BackendService, never the finalizer. GKE deletes "
               "a NEG only when no BackendService uses it (acme-config-prod #3888).")


def test_a_neg_and_a_backend_service_in_one_env_lead_the_deletions():
    verdict, b = _bullets({"pv-x-ms": _r(neg=[SVC], deleted=[SVC]),
                           "pv-x-glb": _r(deleted=[BS])})
    assert verdict.startswith("⚠️ **Review before merging** (2 item(s))"), verdict
    assert b[0] == NEG_FINDING
    assert b[1].startswith("\U0001f5d1️ **2 resource(s) deleted**")
    assert not any(d in b[0] for d in DASHES)
    lead = cr.status_lead("\n".join(cr._build_merge_summary(
        {"pv-x-ms": _r(neg=[SVC]), "pv-x-glb": _r(deleted=[BS])},
        {}, None, None, None, None, False)))
    assert lead.startswith("⚠️ NEG and BackendService removed together in pv-x"), lead


def test_only_one_of_the_two_is_no_finding():
    for results in ({"pv-x-ms": _r(neg=[SVC])},
                    {"pv-x-glb": _r(deleted=[BS])},
                    {"pv-x-ms": _r(neg=[SVC]), "pv-x-glb": _r(deleted=[SVC])}):
        _verdict, b = _bullets(results)
        assert not any(l.startswith("\U0001f517") for l in b), b


def test_a_public_cloud_block_pairs_with_its_constellation():
    """The NEGs live in cl-qa-11-a-ms, the BackendServices in the block's
    cl-qa-11-a-app1-glb."""
    _verdict, b = _bullets({"cl-qa-11-a-ms": _r(neg=[SVC]),
                            "cl-qa-11-a-app1-glb": _r(deleted=[BS])})
    assert b[0].startswith("\U0001f517 **NEG and BackendService removed together** "
                           "in `cl-qa-11-a`:"), b


def test_two_different_envs_do_not_pair():
    """Private cloud pairs by the exact env name: pv-a-b is not part of pv-a."""
    _verdict, b = _bullets({"pv-a-ms": _r(neg=[SVC]), "pv-b-glb": _r(deleted=[BS]),
                            "pv-a-b-ss": _r(deleted=[BS])})
    assert not any(l.startswith("\U0001f517") for l in b), b


def test_several_envs_are_named():
    _verdict, b = _bullets({"pv-a-ms": _r(neg=[SVC]), "pv-a-glb": _r(deleted=[BS]),
                            "pv-b-ms": _r(neg=[SVC]), "pv-b-glb": _r(deleted=[BS])})
    assert b[0].startswith("\U0001f517 **NEG and BackendService removed together** "
                           "in `pv-a`, `pv-b`:"), b


def test_the_green_status_keeps_the_action_at_the_3888_size():
    """The action comes first and only 3 names are shown, so the status
    lead cut at 255 bytes still says what to do. #3888 had 22 envs."""
    tail = "44 resource(s) will change - review comment"
    for n, action in ((1, "delete the BackendService, never the finalizer."),
                      (22, "check kubectl get svcneg -n <namespace>, and if one is "
                           "stuck, delete")):
        results = {}
        for i in range(n):
            results[f"pv-cust{i:02}--aec1-a-ms"] = _r(neg=[SVC])
            results[f"pv-cust{i:02}--aec1-a-glb"] = _r(deleted=[BS])
        desc = cr.join_status_lead(cr.status_lead("\n".join(cr._build_merge_summary(
            results, {}, None, None, None, None, False))), tail)
        assert len(desc.encode()) <= 255 and desc.endswith(" | " + tail), desc
        assert action in desc, desc
    assert "`pv-cust02--aec1-a` (+19 more): after the sync" in _bullets(results)[1][0]


def test_a_result_without_the_field_does_not_crash():
    Old = collections.namedtuple(
        "Old", [f for f in m.DiffResult._fields if f != "neg_removed"],
        defaults=[None] * 10)
    old = Old("d", [], 1, True, "", m.OUT_DIFF, "", None, [BS])
    _verdict, b = _bullets({"pv-x-glb": old, "pv-x-ms": _r(neg=[SVC])})
    assert b[0].startswith("\U0001f517"), b
    _verdict, b = _bullets({"pv-x-glb": old})
    assert b[0].startswith("\U0001f5d1"), b


# ── (d) legacyBackends false to true ─────────────────────────────────────

LK = m._LEGACY_BACKENDS_KEY
BASE, HEAD = "base0001", "head0001"
QA88 = "gcp/qa/private-cloud/ap1/custom/pv-qa88-a/customer.yaml"
QA88_APPS = ["pv-qa88-a-glb", "pv-qa88-a-ms", "pv-qa88-a-ss"]
ENV = "appspace:\n  customerName: qa88\n  suffix: a\n"


def _lb(value):
    return ENV + f"  loadBalancers:\n    gatewayApi:\n      legacyBackends: {value}\n"


def _files(monkeypatch, files):
    """files: {(path, sha): body}. Any other path is a 404."""
    with m._vf_cache_lock:
        m._vf_cache.clear()
    m._yaml_cache.clear()
    monkeypatch.setattr(m, "_bb_fetch_status", lambda path, sha, repo=None: (
        (files[(path, sha)], m.BB_OK) if (path, sha) in files else (None, m.BB_NOT_FOUND)))


def _legacy(monkeypatch, old_body, new_body, path=QA88, apps=QA88_APPS):
    _files(monkeypatch, {(path, BASE): old_body, (path, HEAD): new_body})
    return m._key_changes([path], {}, {path: apps}, HEAD, BASE, LK, default=True)


BACK_LINE = (
    "⚠️ **Legacy backends come back.** "
    "`appspace.loadBalancers.gatewayApi.legacyBackends` goes from `false` to "
    "`true` in 1 environment(s): `pv-qa88-a`. The chart creates the legacy "
    "BackendServices again in the `-glb` app, and the NEGs they use come from "
    "the `-ms` app. Until both apps are Synced, KCC reports those "
    "BackendServices as not ready. After the sync, check `kubectl get svcneg -n "
    "<namespace>` and that every BackendService is UpToDate.")


def test_unset_to_false_uses_the_chart_default_and_is_not_listed(monkeypatch):
    changes = _legacy(monkeypatch, ENV, _lb("false"))
    assert [(c["old"], c["new"]) for c in changes] == [(True, False)]
    assert m._legacy_backends_lines(changes) == []


def test_false_to_true_is_listed(monkeypatch):
    changes = _legacy(monkeypatch, _lb("false"), _lb("true"))
    assert [(c["env"], c["old"], c["new"]) for c in changes] == [("pv-qa88-a", False, True)]
    assert m._legacy_backends_lines(changes) == [BACK_LINE, ""]
    assert not any(d in BACK_LINE for d in DASHES)


def test_dropping_a_false_brings_them_back_too(monkeypatch):
    """The key removed: the chart default (true) applies again."""
    changes = _legacy(monkeypatch, _lb("false"), ENV)
    assert m._legacy_backends_lines(changes) == [BACK_LINE, ""]


def test_only_gcp_private_cloud_is_listed(monkeypatch):
    """Azure moves nginx-frontend with this key, and public cloud has no
    legacy BackendServices: neither has the NEGs the line talks about."""
    for path, apps in (
            ("azure/prod/private-cloud/na1-a/weekly/pv-az-a/customer.yaml",
             ["pv-az-a-glb", "pv-az-a-ms"]),
            ("gcp/qa/public-cloud/ap1/cl-qa-11-a/app1/customer.yaml",
             ["cl-qa-11-a-app1-glb"])):
        changes = _legacy(monkeypatch, _lb("false"), _lb("true"), path, apps)
        assert len(changes) == 1 and m._legacy_backends_lines(changes) == [], path


def test_other_keys_and_unknowns_are_ignored():
    base = {"env": "pv-qa88-a", "path": QA88, "moved_from": None, "key": LK,
            "old": False, "new": True, "pinned": True, "src": QA88, "value": True}
    assert m._legacy_backends_lines(None) == []
    assert m._legacy_backends_lines([dict(base, key=m._NOCORE_KEY),
                                     dict(base, old=None), dict(base, new=None)]) == []
    assert m._legacy_backends_lines([base]) == [BACK_LINE, ""]


def test_the_summary_finding_counts_the_envs():
    base = {"env": "pv-qa88-a", "path": QA88, "moved_from": None, "key": LK,
            "old": False, "new": True, "pinned": True, "src": QA88, "value": True}
    lines = m._legacy_backends_lines([base, dict(base, env="pv-qa89-a")])
    verdict, b = _bullets({}, state=lines)
    assert verdict.startswith("⚠️ **Review before merging** (1 item(s))"), verdict
    assert b == ["\U0001f517 **Legacy backends come back** in 2 environment(s): sync "
                 "`-ms` and `-glb`, then check `kubectl get svcneg`"]


# ── (e) process_pr ───────────────────────────────────────────────────────

SK = ("acme-config-dev", 991)
ORCH_APPS = ["pv-orch-a-glb", "pv-orch-a-ms", "pv-orch-a-ss"]
PMAP = {IDENTITY: ORCH_APPS, ANCILLARY: ORCH_APPS}
ORCH_SVC = "/Service pv-orch-a/user"
ORCH_BS = "/compute.cnrm.cloud.google.com/ComputeBackendService pv-orch-a/pv-orch-a-bs-user"


@pytest.fixture()
def orch(world, monkeypatch):
    sinks, plan = world
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)
    monkeypatch.setitem(m._app_chart_map, "pv-orch-a-glb", "appspace-glb")
    monkeypatch.setitem(m._app_chart_revision_map, "pv-orch-a-glb", "2603.0.1-dev")
    return sinks, plan


def test_a_neg_and_backend_service_plan_stays_green(orch):
    sinks, plan = orch
    plan["pv-orch-a-ms"] = m.DiffResult(
        "d", [(ORCH_SVC, LOST_ANN)], 1, True, "", m.OUT_DIFF, "changes",
        neg_removed=[ORCH_SVC])
    plan["pv-orch-a-glb"] = m.DiffResult(
        "d", [(ORCH_BS, "--- \n+++ \n-kind: ComputeBackendService\n")], 1, True, "",
        m.OUT_DIFF, "changes", deleted_resources=[ORCH_BS])
    m.process_pr(_mk_pr(), PMAP, base_sha=BASE_SHA)
    body, (state, desc) = sinks.upserts[-1], sinks.statuses[-1]
    assert state == "SUCCESSFUL", desc
    assert m._extract_status_token(body) == "clean"
    assert desc.startswith("⚠️ NEG and BackendService removed together in pv-orch-a"), desc
    summary = body.split("\n---\n")[0]
    assert summary.index("\U0001f517 **NEG and BackendService") < summary.index("\U0001f5d1")


def _orch_files(monkeypatch, old_body, new_body):
    _files(monkeypatch, {(IDENTITY, BASE_SHA): old_body, (IDENTITY, PR_SHA): new_body})
    monkeypatch.setattr(m, "get_pr_changed_files",
                        lambda pr_id, repo=None: ([IDENTITY], {}))


ORCH_LB = IDENTITY_YAML + "  loadBalancers:\n    gatewayApi:\n      legacyBackends: {}\n"


def test_legacy_backends_back_is_a_green_review(orch, monkeypatch):
    sinks, _plan = orch
    _orch_files(monkeypatch, ORCH_LB.format("false"), ORCH_LB.format("true"))
    m.process_pr(_mk_pr(), PMAP, base_sha=BASE_SHA)
    body, (state, desc) = sinks.upserts[-1], sinks.statuses[-1]
    assert state == "SUCCESSFUL" and m._extract_status_token(body) == "clean", desc
    assert "- \U0001f517 **Legacy backends come back** in 1 environment(s)" in body
    assert "goes from `false` to `true` in 1 environment(s): `pv-orch-a`." in body
    assert desc.startswith("⚠️ Legacy backends come back in 1 environment(s)"), desc


def _fail_on_legacy(monkeypatch, exc):
    real = m._key_changes

    def flaky(*a, **k):
        if a[5] == m._LEGACY_BACKENDS_KEY:
            raise exc
        return real(*a, **k)
    monkeypatch.setattr(m, "_key_changes", flaky)


def test_a_failed_legacy_read_retries(orch, monkeypatch):
    sinks, _plan = orch
    _orch_files(monkeypatch, ORCH_LB.format("false"), ORCH_LB.format("true"))
    _fail_on_legacy(monkeypatch, m.ValueFileUnreadable("value file unreadable: x"))
    m.process_pr(_mk_pr(), PMAP, base_sha=BASE_SHA)
    assert m._extract_status_token(sinks.upserts[-1]) == "transient"
    assert sinks.statuses[-1][0] == "FAILED"
    assert SK not in m._seen and SK in m._retry_backoff


def test_a_bug_in_the_legacy_check_is_only_logged(orch, monkeypatch):
    sinks, _plan = orch
    _orch_files(monkeypatch, ORCH_LB.format("false"), ORCH_LB.format("true"))
    _fail_on_legacy(monkeypatch, RuntimeError("boom"))
    m.process_pr(_mk_pr(), PMAP, base_sha=BASE_SHA)
    body, (state, desc) = sinks.upserts[-1], sinks.statuses[-1]
    assert state == "SUCCESSFUL" and m._extract_status_token(body) == "clean", desc
    assert "Legacy backends" not in body
