"""Enabling acme-ping-scaler must read as a handover, not an incident (COPS-2714).

Field report, in two acts. Act one: acme-config-prod PR #4444 enabled the
ping-scaler on one AEC environment and the comment opened with "DO NOT
MERGE" over "23 RESOURCE(S) DELETED" -- all 23 were HorizontalPodAutoscalers
removed by the chart's own contract (hpa.yaml: "Skip all HPA rendering when
acmePingScaler is enabled to prevent replica conflicts").

Act two: the first fix paired creation and deletion within ONE app's diff,
and its live probe on that same PR still shouted -- because the two halves
of the contract live in DIFFERENT apps of the same environment. The
ping-scaler Deployment is rendered by supporting-services ({env}-ss); the
HPAs it displaces belong to micro-services ({env}-ms). These tests encode
that real shape, not the convenient same-app one.

The reclassification stays deliberately narrow, mirroring the rename split
(COPS-2594) and the KCC abandon split (COPS-2682), because a false match
would SUPPRESS a real deletion warning:

  - the acme-ping-scaler Deployment must be CREATED by this PR (exact
    name; a lookalike does not activate), and
  - only HorizontalPodAutoscaler headers are ever reclassified, and
  - only in the SAME ENVIRONMENT: an activation in pv-a never explains a
    deletion in pv-b.
"""
import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m
import comment_render
import manifest


PS_NEW  = "/apps/Deployment acme-ping-scaler"
PS_NS   = "/apps/Deployment pv-ford--aec1-b/acme-ping-scaler"
HPA_A   = "/autoscaling/HorizontalPodAutoscaler accesscontrol"
HPA_B   = "/autoscaling/HorizontalPodAutoscaler account"
SECRET  = "/v1/Secret db-credentials"
IMPOSTOR = "/apps/Deployment acme-ping-scaler-lookalike"


# ── detection: the creation half, per app ───────────────────────────────

def test_creation_detected():
    assert manifest._detect_pingscaler_created([PS_NEW]) is True


def test_namespaced_deployment_header_also_counts():
    assert manifest._detect_pingscaler_created([PS_NS]) is True


def test_a_lookalike_name_does_not_activate():
    """Suffix-matching would let acme-ping-scaler-lookalike unlock the calm
    path; the name must match exactly."""
    assert manifest._detect_pingscaler_created([IMPOSTOR]) is False
    assert manifest._detect_pingscaler_created([]) is False


def test_hpa_headers_selects_only_hpas():
    assert manifest._hpa_headers([HPA_A, SECRET, HPA_B]) == [HPA_A, HPA_B]


# ── the pairing: same environment, across apps ──────────────────────────

def _mk(deleted=None, created=False, n=1):
    return m.DiffResult("d", [], n, True, "", m.OUT_DIFF, "",
                        None, deleted, None, None, None,
                        pingscaler_created=created)


def test_pairing_crosses_apps_within_one_environment():
    """The real #4444 shape: Deployment created in -ss, HPAs deleted in -ms."""
    results = {
        "pv-ford--aec1-b-ss": _mk(created=True),
        "pv-ford--aec1-b-ms": _mk(deleted=[HPA_A, HPA_B]),
    }
    got = comment_render._pingscaler_reclass(results)
    assert got == {"pv-ford--aec1-b-ms": {HPA_A, HPA_B}}


def test_an_activation_in_another_environment_explains_nothing():
    results = {
        "pv-other-a-ss": _mk(created=True),
        "pv-ford--aec1-b-ms": _mk(deleted=[HPA_A]),
    }
    assert comment_render._pingscaler_reclass(results) == {}


def test_no_activation_anywhere_means_no_reclassification():
    results = {"pv-ford--aec1-b-ms": _mk(deleted=[HPA_A, HPA_B])}
    assert comment_render._pingscaler_reclass(results) == {}


def test_only_hpa_kinds_reclassify():
    results = {
        "pv-ford--aec1-b-ss": _mk(created=True),
        "pv-ford--aec1-b-ms": _mk(deleted=[HPA_A, SECRET]),
    }
    assert comment_render._pingscaler_reclass(results) == {
        "pv-ford--aec1-b-ms": {HPA_A}}


# ── detection composes from raw sections, pre-cap ───────────────────────

def _del_body(kind, name):
    return f"--- \n+++ \n-apiVersion: x\n-kind: {kind}\n-  name: {name}\n"


def _add_body(kind, name):
    return f"--- \n+++ \n+apiVersion: x\n+kind: {kind}\n+  name: {name}\n"


def test_detection_composes_from_raw_sections():
    secs = [(PS_NEW, _add_body("Deployment", "acme-ping-scaler"))]
    assert manifest._detect_pingscaler_created(
        m._detect_created_resources(secs)) is True
    # A modified (not created) ping-scaler Deployment does not activate:
    # enabling is the event, running is not.
    mod = [(PS_NEW, "--- \n+++ \n kind: Deployment\n+  image: new\n")]
    assert manifest._detect_pingscaler_created(
        m._detect_created_resources(mod)) is False


def _fake_diff_text(sections):
    return "\n".join(f"===== {h} ======\n{b}" for h, b in sections)


def test_argocd_diff_sets_created_only_for_a_true_creation(monkeypatch):
    """Through the REAL argocd_diff, which is where the first fix's mutation
    survived: the fact must come from the CREATED subset of sections. A
    MODIFIED ping-scaler Deployment (an image bump on an environment where
    it already runs) must not activate the calm path."""
    made = {"text": ""}

    def fake_run(*a, **k):
        return (made["text"], None, "", None, 0, None)
    monkeypatch.setattr(m, "_run_one_diff", fake_run)

    made["text"] = _fake_diff_text(
        [(PS_NEW, _add_body("Deployment", "acme-ping-scaler"))])
    r = m.argocd_diff("pv-ford--aec1-b-ss", "aaaa1111", "bbbb2222")
    assert r.outcome == m.OUT_DIFF
    assert r.pingscaler_created is True

    made["text"] = _fake_diff_text(
        [(PS_NEW, "--- \n+++ \n kind: Deployment\n+  image: new\n")])
    r = m.argocd_diff("pv-ford--aec1-b-ss", "cccc3333", "dddd4444")
    assert r.outcome == m.OUT_DIFF
    assert r.pingscaler_created is False


# ── comment level: calm panel, and the alarm only when deserved ─────────

def _ford(deleted_ms, extra_ms_deleted=()):
    return {
        "pv-ford--aec1-b-ss": _mk(created=True, n=5),
        "pv-ford--aec1-b-ms": _mk(
            deleted=list(deleted_ms) + list(extra_ms_deleted), n=109),
    }


def test_pure_activation_is_calm_review_not_block(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda *a, **k: None)
    body = m.format_comment("c" * 12, _ford([HPA_A, HPA_B]))
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
    body = m.format_comment("c" * 12, _ford([HPA_A, HPA_B], [SECRET]))
    assert "DO NOT MERGE" in body
    assert "1 RESOURCE(S) DELETED" in body, (
        "the count must exclude the reclassified HPAs, not hide the Secret")
    assert "\U0001f510" in body
    assert "takes over replica control" in body, (
        "the calm panel and the alarm coexist; each says its own truth")


def test_hpas_without_an_activation_still_shout(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda *a, **k: None)
    body = m.format_comment("c" * 12, {
        "pv-x-ms": _mk(deleted=[HPA_A, HPA_B])})
    assert "2 RESOURCE(S) DELETED" in body
    assert "DO NOT MERGE" in body
    assert "takes over replica control" not in body
