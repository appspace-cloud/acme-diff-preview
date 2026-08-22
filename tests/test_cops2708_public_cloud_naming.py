"""Public cloud must be named, explained, and detected at all (COPS-2708).

Three defects, all found by rendering real drill PRs against 2.100.1 rather
than by any test, and all of them the same root cause: the environment name
is the basename of the identity file's parent directory, which is right on
private cloud (`.../pv-qa-15-a/customer.yaml`) and wrong on public cloud,
where a block is nested under the constellation
(`.../cl-dev11-a/constellation/customer.yaml`).

  1. Panels said "NO-OP for `constellation`". There are thirteen
     constellations; the block names none of them.

  2. Every public-cloud panel explained the mechanism -- no cascade
     finalizer is templated -- and never the reason. It reads like an
     omission somebody should fix rather than the safety property it is: a
     `cl-*` namespace is shared, so no delete is safe to automate for one
     customer in it.

  3. Worst of the three: the same basename fed the guard in
     `_detect_env_decommission_candidates`, which requires every app to be
     prefixed with the environment name. `cl-dev11-a-ms` does not start with
     `constellation-`, so every public-cloud teardown candidate was dropped
     and the COPS-2701 manual-teardown panel could not fire in either config
     repo.

Block layout, read live from the hub: `constellation` owns `-ms` and `-ss`,
the shared workloads every customer in the constellation is served from.
`api`, `cloud`, `user-content` and `app1`..`app16` each own one `-glb` load
balancer in front of those same workloads. That difference is why step 3 of
the manual checklist cannot be the same sentence for both.
"""
import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import comment_render  # noqa: E402
import diff_preview as m  # noqa: E402
from decommission import (  # noqa: E402
    _public_cloud_env_name,
    _public_cloud_teardown_phase_table,
)

CONSTELLATION = "gcp/dev/public-cloud/ap1/cl-dev11-a/constellation/customer.yaml"
APP1 = "gcp/dev/public-cloud/ap1/cl-dev11-a/app1/customer.yaml"
PRIVATE = "gcp/qa/private-cloud/ap1/custom/pv-qa88-a/customer.yaml"

PATH_MAP = {
    CONSTELLATION: ["cl-dev11-a-ms", "cl-dev11-a-ss"],
    APP1: ["cl-dev11-a-app1-glb"],
    PRIVATE: ["pv-qa88-a-ms", "pv-qa88-a-ss"],
}


# ── 1. naming ────────────────────────────────────────────────────────────

def test_the_constellation_is_read_out_of_the_path():
    assert _public_cloud_env_name(CONSTELLATION) == "cl-dev11-a"
    assert _public_cloud_env_name(APP1) == "cl-dev11-a"


def test_a_path_with_no_constellation_keeps_the_callers_name():
    """Degrade to today's behaviour rather than to an empty heading."""
    assert _public_cloud_env_name(PRIVATE, "pv-qa88-a") == "pv-qa88-a"
    assert _public_cloud_env_name("gcp/dev/public-cloud/ap1/config.yaml",
                                  "ap1") == "ap1"
    assert _public_cloud_env_name("", "fallback") == "fallback"


def _noop_panel(monkeypatch, head, tag, ident=CONSTELLATION):
    base, pr = "b" + tag, "p" + tag
    files = {(ident, base): "appspace:\n  customerName: c\n", (ident, pr): head}
    monkeypatch.setattr(
        m, "_bb_fetch_status",
        lambda p, s, repo=None: (files[(p, s)], m.BB_OK)
        if (p, s) in files else (None, m.BB_NOT_FOUND))
    return "\n".join(m._summarize_appspace_state_changes(
        [ident], pr, base, {ident: PATH_MAP[ident]}))


def test_the_noop_panel_names_the_constellation_not_the_block(monkeypatch):
    out = _noop_panel(
        monkeypatch, "appspace:\n  customerName: c\n  decommission: true\n",
        "name1")
    assert "NO-OP for `cl-dev11-a`" in out, out
    assert "NO-OP for `constellation`" not in out, \
        "the block names none of the thirteen constellations"


def test_the_purge_noop_panel_names_the_constellation_too(monkeypatch):
    base_yaml = "appspace:\n  customerName: c\n  decommission: true\n"
    head = base_yaml + "  decommissionPurgeData: true\n"
    ident = CONSTELLATION
    files = {(ident, "bp"): base_yaml, (ident, "pp"): head}
    monkeypatch.setattr(
        m, "_bb_fetch_status",
        lambda p, s, repo=None: (files[(p, s)], m.BB_OK)
        if (p, s) in files else (None, m.BB_NOT_FOUND))
    out = "\n".join(m._summarize_appspace_state_changes(
        [ident], "pp", "bp", {ident: PATH_MAP[ident]}))
    assert "PURGE FLAG IS A NO-OP for `cl-dev11-a`" in out, out


# ── 2. the reason, not just the mechanism ────────────────────────────────

def test_the_noop_panel_says_why_teardown_is_manual(monkeypatch):
    """The operator's real question is not "what does the flag do", it is
    "why does this platform not do it for me". Shared tenancy is the
    answer, and it is what makes the manual procedure reasonable rather
    than a gap."""
    out = _noop_panel(
        monkeypatch, "appspace:\n  customerName: c\n  decommission: true\n",
        "why1")
    assert comment_render._DECOM_PUBLIC_CLOUD_HDR in out
    assert comment_render._DECOM_PUBLIC_CLOUD_WHY in out
    low = out.lower()
    assert "shared" in low
    assert "many customers" in low


def test_the_reason_lives_in_exactly_one_place():
    """Three panels state it. A second copy is a second thing to update."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "src",
                            "diff_preview.py")).read()
    assert "many customers from the same microservices" not in src, \
        "the sentence belongs in comment_render as _DECOM_PUBLIC_CLOUD_WHY"
    assert src.count("_DECOM_PUBLIC_CLOUD_WHY") >= 4, \
        "the constant must be imported and used by all three panels"


# ── 3. the teardown candidate must be detected at all ────────────────────

def test_a_public_cloud_block_removal_is_detected():
    """The regression that mattered: this returned nothing, so the
    manual-teardown panel built in COPS-2701 was unreachable in both config
    repos."""
    got = m._detect_env_decommission_candidates([CONSTELLATION], PATH_MAP, {})
    assert len(got) == 1, got
    assert got[0]["env_name"] == "cl-dev11-a"
    assert got[0]["block"] == "constellation"
    assert got[0]["apps"] == ["cl-dev11-a-ms", "cl-dev11-a-ss"]


def test_a_single_load_balancer_block_is_detected_and_named():
    got = m._detect_env_decommission_candidates([APP1], PATH_MAP, {})
    assert len(got) == 1, got
    assert got[0]["env_name"] == "cl-dev11-a"
    assert got[0]["block"] == "app1"


def test_private_cloud_detection_is_untouched():
    """Scope guard. The basename rule is correct there and must stay."""
    got = m._detect_env_decommission_candidates([PRIVATE], PATH_MAP, {})
    assert len(got) == 1
    assert got[0]["env_name"] == "pv-qa88-a"
    assert got[0]["block"] == ""


def test_a_shared_ancestor_is_still_rejected():
    """The guard exists to keep a cohort default out. Widening the name for
    public cloud must not widen that."""
    shared = "gcp/dev/public-cloud/ap1/config.yaml"
    got = m._detect_env_decommission_candidates(
        [shared], {shared: ["cl-dev11-a-ms", "cl-qa-14-a-ms"]}, {})
    assert got == [], got


# ── 4. step 3 depends on which block is going ────────────────────────────

def _step3(rows):
    return next(r for r in rows if r.startswith("| **3"))


def test_removing_one_load_balancer_must_not_say_delete_the_namespace():
    """`app1` and `app2` share a namespace. Telling an operator tearing down
    one of them to `kubectl delete namespace` takes every other customer in
    the constellation down with it."""
    step3 = _step3(_public_cloud_teardown_phase_table("app1"))
    assert "do NOT delete" in step3, step3
    assert "kubectl delete namespace" not in step3
    assert "shared workloads" in step3


def test_removing_the_constellation_block_still_says_delete_the_namespace():
    step3 = _step3(_public_cloud_teardown_phase_table("constellation"))
    assert "kubectl delete namespace" in step3, step3
    assert "every customer in the constellation" in step3, \
        "the blast radius has to be stated where the instruction is"


def test_the_table_keeps_its_generic_wording_with_no_block():
    """COPS-2701 callers that do not know the block must not change shape."""
    rows = _public_cloud_teardown_phase_table()
    assert len(rows) == 7
    assert "kubectl delete namespace" in _step3(rows)
