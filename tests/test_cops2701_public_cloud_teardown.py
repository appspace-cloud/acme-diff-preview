"""Public-cloud (cl-*) folder removal must not use the pv-* decommission panel (COPS-2701).

What went wrong on acme-config-stage PR #2830 (cl-adapter-a folder delete)
------------------------------------------------------------------------
The decommission panel treated a public-cloud path like private-cloud: it
rendered the Phase 1/2/3 table, told the reviewer to set
`appspace.decommission: true` (COPS-2539), and implied Phase 3 deletes
managed resources / KCC objects.

None of that is true on cl-*. COPS-2700 deliberately left every
`argocd-apps-cl-*` ApplicationSet with `preserveResourcesOnDeletion: true`
and no decommission templatePatch. Setting the flag is a silent no-op.

This ticket pins the panel and the merge-summary verdict to the manual
teardown checklist instead.
"""
import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import comment_render
import decommission as d
import diff_preview as m


IDENT = "gcp/stage/public-cloud/na1/cl-adapter-a/customer.yaml"
LIVE = "---\nappspace:\n  customerName: adapter\n  version: 1.0.0\n"
# Even with the private-cloud gate set, public cloud must stay manual.
ARMED = ("---\nappspace:\n  customerName: adapter\n  version: 1.0.0\n"
         "  decommission: true\n")


def _candidate(env="cl-adapter-a", identity=IDENT):
    return {"env_name": env, "identity_file": identity,
            "apps": [f"{env}-ms"], "env_dir": os.path.dirname(identity)}


def _fetch(main_content):
    def fake(path, sha, repo=None):
        if sha == "prsha":
            return (None, m.BB_NOT_FOUND)
        return (main_content, m.BB_OK)
    return fake


def _resources():
    return {("apps/Deployment", "cl-adapter-a", "web"): {},
            ("v1/Service", "cl-adapter-a", "web"): {}}


def _clear_caches():
    with m._vf_cache_lock:
        m._vf_cache.clear()
    m._yaml_cache.clear()


# ── unit: detection ───────────────────────────────────────────────────────

def test_public_cloud_path_is_detected():
    assert d._is_public_cloud_env(IDENT, "cl-adapter-a") is True
    assert d._is_public_cloud_env(IDENT, "") is True


def test_cl_prefix_alone_is_detected():
    assert d._is_public_cloud_env(
        "gcp/stage/something/cl-foo-a/customer.yaml", "cl-foo-a") is True


def test_private_cloud_pv_is_not_public():
    assert d._is_public_cloud_env(
        "gcp/prod/private-cloud/na2-a/monthly/pv-foo-c/customer.yaml",
        "pv-foo-c") is False


# ── panel: wording ────────────────────────────────────────────────────────

def test_cl_folder_delete_uses_manual_teardown_panel(monkeypatch):
    """The reviewer must see the cl-* checklist, not Phase 1/2/3."""
    _clear_caches()
    monkeypatch.setattr(m, "_bb_fetch_status", _fetch(LIVE))
    monkeypatch.setattr(m, "_render_main_side_resources",
                        lambda app, sha: _resources())
    body = "\n".join(m._evaluate_env_decommissions(
        [_candidate()], "prsha", "mainsha")[0])

    assert "PUBLIC CLOUD MANUAL TEARDOWN" in body
    assert "ENVIRONMENT DECOMMISSION" not in body
    assert "kubectl delete namespace" in body
    assert "abandon" in body.lower() or "abandoned" in body.lower()
    # Private-cloud remediation must never appear.
    assert "COPS-2539" not in body
    assert "set `appspace.decommission: true`" not in body.lower()
    assert "appspace.decommission: true" not in body or "no-op" in body.lower()
    assert "COPS-2700" in body
    assert "Phase 2" not in body
    assert "Phase 3" not in body


def test_cl_folder_delete_ignores_decommission_flag(monkeypatch):
    """appspace.decommission: true must not flip the panel to cascade.

    Paint the false confidence in red: the flag was set, but it arms nothing.
    """
    _clear_caches()
    monkeypatch.setattr(m, "_bb_fetch_status", _fetch(ARMED))
    monkeypatch.setattr(m, "_render_main_side_resources",
                        lambda app, sha: _resources())
    body = "\n".join(m._evaluate_env_decommissions(
        [_candidate()], "prsha", "mainsha")[0])

    assert "PUBLIC CLOUD MANUAL TEARDOWN" in body
    assert "will be removed" not in body
    assert comment_render._DECOM_PUBLIC_CLOUD_NOOP_HDR in body
    assert "silent no-op" in body.lower() or "no-op" in body.lower()
    assert "nothing is auto-deleted" in body.lower()
    low = body.lower()
    assert "orphan" in low or "keep running" in low


def test_arming_decommission_on_live_cl_env_is_red_noop(monkeypatch):
    """Setting the flag on a still-present cl-* env must not say ARMED."""
    _clear_caches()
    live = "appspace:\n  customerName: adapter\n"
    armed = live + "  decommission: true\n"
    path_map = {IDENT: ["cl-adapter-a-ms"]}

    def fake(path, sha, repo=None):
        if path != IDENT:
            return (None, m.BB_NOT_FOUND)
        return ((armed if sha == "prsha" else live), m.BB_OK)

    monkeypatch.setattr(m, "_bb_fetch_status", fake)
    monkeypatch.setattr(m, "_merged_kcc_flat_for_env",
                        lambda *a, **k: {})
    body = "\n".join(m._summarize_appspace_state_changes(
        [IDENT], "prsha", "mainsha", path_map))

    assert "PUBLIC CLOUD: DECOMMISSION FLAG IS A NO-OP" in body
    assert "DECOMMISSION ARMED" not in body
    assert "Phase 2" not in body
    assert "cascade-delete finalizer" not in body
    assert comment_render._DECOM_PUBLIC_CLOUD_NOOP_HDR in body
    assert "COPS-2700" in body


def test_merge_summary_blocks_on_public_cloud_noop_arming():
    """Verdict must BLOCK and must not claim cascade eligibility."""
    panel = [
        "## 🚨 PUBLIC CLOUD: DECOMMISSION FLAG IS A NO-OP for `cl-x` 🚨",
        "",
        "🚨 " + comment_render._DECOM_PUBLIC_CLOUD_NOOP_HDR,
    ]
    out = "\n".join(comment_render._build_merge_summary(
        {}, {}, None, None, panel, None, False))
    assert "Public-cloud decommission flag is a NO-OP" in out
    assert "DO NOT MERGE" in out
    assert "eligible for cascade" not in out


def test_merge_summary_labels_public_cloud_teardown():
    """Verdict must not read as a private-cloud decommission that can be armed."""
    panel = [
        "# 🗑️⚠️ PUBLIC CLOUD MANUAL TEARDOWN ⚠️🗑️",
        "",
        "⚠️ " + comment_render._DECOM_PUBLIC_CLOUD_HDR,
        "",
        "⚠️ " + comment_render._DECOM_ORPHAN_HDR + " — they keep running.**",
    ]
    out = "\n".join(comment_render._build_merge_summary(
        {}, {}, None, panel, None, None, False))
    assert "Public-cloud teardown" in out
    assert "Environment decommission" not in out
    assert "COPS-2700" in out
    assert "DO NOT MERGE" in out


def test_private_cloud_orphan_panel_unchanged(monkeypatch):
    """Regression: pv-* without the gate still gets the COPS-2539 hint."""
    _clear_caches()
    ident = "gcp/prod/private-cloud/na2-a/monthly/pv-foo-c/customer.yaml"
    monkeypatch.setattr(m, "_bb_fetch_status", _fetch(LIVE))
    monkeypatch.setattr(m, "_render_main_side_resources",
                        lambda app, sha: _resources())
    body = "\n".join(m._evaluate_env_decommissions(
        [_candidate(env="pv-foo-c", identity=ident)],
        "prsha", "mainsha")[0])
    assert "ENVIRONMENT DECOMMISSION" in body
    assert "PUBLIC CLOUD MANUAL TEARDOWN" not in body
    assert "COPS-2539" in body
