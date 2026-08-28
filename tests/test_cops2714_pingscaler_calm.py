"""Enabling acme-ping-scaler must read as a handover, not an incident (COPS-2714).

Field report: acme-config-prod PR #4444 enabled the ping-scaler on one AEC
environment and the comment opened with "DO NOT MERGE" over "23 RESOURCE(S)
DELETED" — all 23 were HorizontalPodAutoscalers removed by the chart's own
contract (hpa.yaml: "Skip all HPA rendering when acmePingScaler is enabled
to prevent replica conflicts"). The alarm block exists so that a reviewer
reads every deletion that can destroy access or data; filling it with a
documented, reversible handover teaches operators to skim it, which is the
same failure the block was built to prevent (the PR-6773 lesson).

The reclassification is deliberately narrow, mirroring the rename split
(COPS-2594) and the KCC abandon split (COPS-2682), because a false match
would SUPPRESS a real deletion warning:

  - the acme-ping-scaler Deployment must be CREATED in this same diff, and
  - only HorizontalPodAutoscaler headers are ever reclassified.

An HPA deleted any other way, or any other kind deleted alongside, still
alarms exactly as before.
"""
import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m
import manifest


PS_NEW  = "/apps/Deployment acme-ping-scaler"
PS_NS   = "/apps/Deployment pv-ford--aec1-b/acme-ping-scaler"
HPA_A   = "/autoscaling/HorizontalPodAutoscaler accesscontrol"
HPA_B   = "/autoscaling/HorizontalPodAutoscaler account"
SECRET  = "/v1/Secret db-credentials"
IMPOSTOR = "/apps/Deployment acme-ping-scaler-lookalike"


# ── detection: the narrow pairing ───────────────────────────────────────

def test_activation_reclassifies_only_the_hpas():
    got = manifest._detect_pingscaler_takeover(
        [HPA_A, HPA_B, SECRET], [PS_NEW])
    assert got == [HPA_A, HPA_B], (
        "the Secret must never ride along on the calm path")


def test_no_activation_means_no_reclassification():
    assert manifest._detect_pingscaler_takeover([HPA_A, HPA_B], []) is None


def test_activation_with_no_hpas_is_nothing_special():
    assert manifest._detect_pingscaler_takeover([SECRET], [PS_NEW]) is None


def test_namespaced_deployment_header_also_counts():
    assert manifest._detect_pingscaler_takeover([HPA_A], [PS_NS]) == [HPA_A]


def test_a_lookalike_name_does_not_activate():
    """Suffix-matching would let acme-ping-scaler-lookalike unlock the calm
    path; the name must match exactly."""
    assert manifest._detect_pingscaler_takeover([HPA_A], [IMPOSTOR]) is None


# ── package level: the fact is computed pre-cap, deletions stay honest ──

def _del_body(kind, name):
    return f"--- \n+++ \n-apiVersion: x\n-kind: {kind}\n-  name: {name}\n"


def _add_body(kind, name):
    return f"--- \n+++ \n+apiVersion: x\n+kind: {kind}\n+  name: {name}\n"


def test_detection_composes_from_raw_sections():
    """The same composition argocd_diff runs on the full pre-cap list:
    detect deletions and creations from the sections, then pair them."""
    secs = [
        (PS_NEW, _add_body("Deployment", "acme-ping-scaler")),
        (HPA_A, _del_body("HorizontalPodAutoscaler", "accesscontrol")),
        (HPA_B, _del_body("HorizontalPodAutoscaler", "account")),
    ]
    deleted = m._detect_deleted_resources(secs)
    created = m._detect_created_resources(secs)
    assert manifest._detect_pingscaler_takeover(deleted, created) == [HPA_A, HPA_B]
    # deleted stays the full honest fact; render layers reclassify.
    assert set(deleted) == {HPA_A, HPA_B}


# ── comment level: calm panel, and the alarm only when deserved ─────────

def _mk(deleted, takeover):
    return m.DiffResult("d", [], len(deleted), True, "", m.OUT_DIFF, "",
                        None, deleted, None, None, None,
                        pingscaler_takeover=takeover)


def test_pure_activation_is_calm_review_not_block(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda *a, **k: None)
    res = _mk([HPA_A, HPA_B], [HPA_A, HPA_B])
    body = m.format_comment("c" * 12, {"pv-ford--aec1-b-ms": res})
    assert "RESOURCE(S) DELETED" not in body
    assert "DO NOT MERGE" not in body
    assert "Review before merging" in body, (
        "a handover is REVIEW, never routine-green")
    assert "acme-ping-scaler activated" in body
    assert "takes over replica control" in body
    assert "2 HPA(s) removed by design" in body
    assert m.PINGSCALER_DOCS_URL in body
    assert "scaling.md" in body


def test_a_secret_deleted_alongside_still_blocks(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda *a, **k: None)
    res = _mk([HPA_A, HPA_B, SECRET], [HPA_A, HPA_B])
    body = m.format_comment("c" * 12, {"pv-ford--aec1-b-ms": res})
    assert "DO NOT MERGE" in body
    assert "1 RESOURCE(S) DELETED" in body, (
        "the count must exclude the reclassified HPAs, not hide the Secret")
    assert "\U0001f510" in body
    assert "takes over replica control" in body, (
        "the calm panel and the alarm coexist; each says its own truth")


def test_hpas_without_the_field_still_shout(monkeypatch):
    """The reclassification lives in the DETECTED fact, never in kind-name
    pattern-matching at render time: an HPA deleted with no ping-scaler
    activation is a real deletion."""
    monkeypatch.setattr(m, "generate_ai_summary", lambda *a, **k: None)
    res = _mk([HPA_A, HPA_B], None)
    body = m.format_comment("c" * 12, {"pv-x-ms": res})
    assert "2 RESOURCE(S) DELETED" in body
    assert "DO NOT MERGE" in body
    assert "takes over replica control" not in body
