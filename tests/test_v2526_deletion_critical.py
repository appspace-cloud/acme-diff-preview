"""v2.5.26 — resource deletions surfaced deterministically (field report PR 6773).

A 2603.0.1->2603.1.0 bump DELETED an ExternalSecret + IAMPolicyMember and
the comment said "No critical changes detected". Two stacked causes:
(1) the CRITICAL CHANGES block is 100% AI-generated, and (2) DiffResult
.sections is capped to AI_MAX_SECTIONS_PER_APP=10 AT DIFF TIME, so in a
111-resource app the deletion sections were discarded before anything
downstream could see them.

Fix: detect on the FULL pre-cap section list inside _package_sections();
carry results on DiffResult (deleted_resources / replicas_zeroed, defaulted
fields); render a deterministic top block for deletions (downgrade-warning
design language) with sensitive kinds flagged; feed both fact lists to the
AI prompt as authoritative.
"""
import os
import sys

import pytest

os.environ.setdefault("BB_USER", "test-user")
os.environ.setdefault("BB_TOKEN", "test-token")
os.environ.setdefault("ARGOCD_PASS", "test-pass")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m

DEL_BODY = ("--- \n+++ \n@@ -1,6 +0,0 @@\n"
            "-apiVersion: external-secrets.io/v1beta1\n-kind: ExternalSecret\n"
            "-metadata:\n-  name: card-deployment-key\n-spec:\n-  data: []\n")
MOD_BODY = "--- \n+++ \n@@ -3,7 +3,7 @@\n metadata:\n-  foo: old\n+  foo: new\n"
ADD_BODY = "--- \n+++ \n@@ -0,0 +1,3 @@\n+apiVersion: v1\n+kind: Service\n+metadata: {}\n"
ZERO_BODY = ("--- \n+++ \n@@ -5,7 +5,7 @@\n spec:\n-  replicas: 3\n+  replicas: 0\n"
             "   selector: {}\n")

SECTIONS = [
    ("/external-secrets.io/ExternalSecret card-deployment-key", DEL_BODY),
    ("/iam.cnrm.cloud.google.com/IAMPolicyMember card-deployment-key-secret-accessor", DEL_BODY),
    ("/apps/Deployment account", MOD_BODY),
    ("/v1/Service new-svc", ADD_BODY),
    ("/apps/Deployment sleepy", ZERO_BODY),
]


# ── detection primitives ─────────────────────────────────────────────

def test_detector_finds_only_full_deletions():
    assert m._detect_deleted_resources(SECTIONS) == [
        "/external-secrets.io/ExternalSecret card-deployment-key",
        "/iam.cnrm.cloud.google.com/IAMPolicyMember card-deployment-key-secret-accessor",
    ]


def test_detector_ignores_empty_marker_only_and_additions():
    assert m._detect_deleted_resources([("/x/Y z", "--- \n+++ \n@@ -1 +1 @@\n")]) == []
    assert m._detect_deleted_resources([("/v1/Service s", ADD_BODY)]) == []
    assert m._detect_deleted_resources([]) == []


def test_replicas_zeroed_detector():
    assert m._detect_replicas_zeroed(SECTIONS) == ["/apps/Deployment sleepy"]
    # A non-workload kind with the same body shape is ignored.
    assert m._detect_replicas_zeroed([("/v1/ConfigMap c", ZERO_BODY)]) == []
    # replicas changing 3 -> 2 is not a zeroing.
    nb = ZERO_BODY.replace("replicas: 0", "replicas: 2")
    assert m._detect_replicas_zeroed([("/apps/Deployment d", nb)]) == []


def test_sensitive_kind_flagging():
    for h in ("/external-secrets.io/ExternalSecret x",
              "/iam.cnrm.cloud.google.com/IAMPolicyMember x",
              "/v1/Secret db-creds",
              "/rbac.authorization.k8s.io/ClusterRoleBinding x",
              "/v1/PersistentVolumeClaim data",
              "/v1/Namespace tenant-a"):
        assert m._is_sensitive_kind(h), h
    for h in ("/apps/Deployment account", "/v1/Service web", "/v1/ConfigMap c"):
        assert not m._is_sensitive_kind(h), h


def test_deletion_sections_survive_the_noise_filter():
    """_filter_diff_sections drops checksum-cascade noise; a full deletion
    must never be classified as noise."""
    kept = m._filter_diff_sections([SECTIONS[0]])
    assert kept == [SECTIONS[0]]


# ── source-level packaging (the truncation fix itself) ──────────────

def test_package_sections_detects_before_the_cap(monkeypatch):
    """The 111-resource lesson: detection must see sections BEYOND
    AI_MAX_SECTIONS_PER_APP. Put the deletion LAST behind >10 fillers.

    COPS-2579: storage is no longer capped at AI_MAX_SECTIONS_PER_APP (10)
    -- that cap turned out to be the root cause of a much bigger bug (a
    large-PR comment showing 60 of 16616 real diff sections). Storage is
    now bounded by the much more generous FULL_SECTIONS_MAX_PER_APP, so all
    16 sections here (15 fillers + the deletion) survive into `capped`."""
    fillers = [(f"/apps/Deployment filler-{i}", MOD_BODY) for i in range(15)]
    full = fillers + [SECTIONS[0]]
    clean_diff, capped, deleted, zeroed, fingerprint, _ = m._package_sections(full)
    assert len(capped) == len(full)
    assert deleted == ["/external-secrets.io/ExternalSecret card-deployment-key"], \
        "deletion beyond the section cap was lost — the PR-6773 bug"
    assert zeroed == []


# ── comment rendering ────────────────────────────────────────────────

def _res(deleted=None, zeroed=None):
    return {"pv-qa88-a-ms": m.DiffResult(
        "===== /apps/Deployment account =====\n" + MOD_BODY,
        [("/apps/Deployment account", MOD_BODY)], 1, True, None,
        m.OUT_DIFF, "diff", None, deleted, zeroed)}


def test_comment_renders_top_deletions_block(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda *a, **k: None)
    body = m.format_comment("abc1234", _res(deleted=[
        "/external-secrets.io/ExternalSecret card-deployment-key",
        "/iam.cnrm.cloud.google.com/IAMPolicyMember card-deployment-key-secret-accessor",
        "/apps/Deployment harmless",
    ]), base_sha="deadbee")
    assert "RESOURCE(S) DELETED" in body
    assert "ExternalSecret card-deployment-key" in body
    assert "IAMPolicyMember card-deployment-key-secret-accessor" in body
    assert "🔐" in body                       # sensitive kinds flagged
    # The block must appear BEFORE the AI/app blocks area.
    assert body.index("RESOURCE(S) DELETED") < body.index("/apps/Deployment account")


def test_comment_deletions_block_caps_at_20(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda *a, **k: None)
    deleted = [f"/apps/Deployment d-{i}" for i in range(25)]
    body = m.format_comment("abc1234", _res(deleted=deleted), base_sha="deadbee")
    assert "d-19" in body and "d-20" not in body
    assert "+5 more" in body


def test_comment_no_deletions_no_block(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda *a, **k: None)
    body = m.format_comment("abc1234", _res(), base_sha="deadbee")
    assert "RESOURCE(S) DELETED" not in body


# ── AI prompt facts ──────────────────────────────────────────────────

def test_ai_prompt_receives_precomputed_facts(monkeypatch):
    captured = {}

    def fake_http(method, url, body=None, headers=None, auth=None):
        captured["prompt"] = body["contents"][0]["parts"][0]["text"]
        raise RuntimeError("stop after capture")

    monkeypatch.setattr(m, "http", fake_http)
    monkeypatch.setattr(m, "_gcp_access_token", lambda: "tok")
    m.generate_ai_summary(_res(
        deleted=["/external-secrets.io/ExternalSecret card-deployment-key"],
        zeroed=["/apps/Deployment sleepy"]))
    prompt = captured.get("prompt") or ""
    assert "PRE-COMPUTED FACTS" in prompt
    assert "ExternalSecret card-deployment-key" in prompt
    assert "Deployment sleepy" in prompt
    assert "Resources deleted entirely" in prompt
