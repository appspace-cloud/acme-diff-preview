"""COPS-2721: higher-layer redundant config gets a REVIEW callout.

acme-config-prod #4520 pasted HPA metrics/behavior into customer.yaml that
gcp/config.yaml already set identically. Diff Preview said "No manifest
changes" with no explanation, and the operator thought HPAs or the tool
were broken. The finding names the ancestor and the keys so the quiet
render is impossible to misread.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import yaml

import values_redundancy as vr
import diff_preview as m
from comment_render import _VALUES_REDUNDANCY_HDR, _build_merge_summary

CUSTOMER = ("gcp/prod/private-cloud/na1-b/weekly/"
            "pv-fedex-c/customer.yaml")
GCP_CFG = "gcp/config.yaml"
PR_SHA, BASE_SHA = "f00d" * 10, "beef" * 10

# Same shape Travis pasted in #4520 (already present in gcp/config.yaml).
HPA_METRICS_KEY = (
    "appspace.microservices.definitions.network.hpa.metrics")
HPA_BEHAVIOR_KEY = (
    "appspace.microservices.definitions.network.hpa.behavior")
HPA_METRICS = [{"type": "Resource", "resource": {
    "name": "cpu",
    "target": {"type": "Utilization", "averageUtilization": 110}}}]
HPA_BEHAVIOR = {
    "scaleDown": {"stabilizationWindowSeconds": 600},
    "scaleUp": {"stabilizationWindowSeconds": 180},
}


# ── the pure half ────────────────────────────────────────────────────────

def test_changed_keys_covers_add_remove_and_value_change():
    old = {"a.b": 1, "a.c": 2, "gone": 3}
    new = {"a.b": 1, "a.c": 9, "fresh": 4}
    assert vr.changed_keys(old, new) == {"a.c", "gone", "fresh"}


def test_merge_flats_last_wins():
    assert vr.merge_flats([
        {"a": 1, "b": 2},
        {"b": 9, "c": 3},
    ]) == {"a": 1, "b": 9, "c": 3}


def test_source_of_returns_last_ancestor_that_sets_the_key():
    chain = [
        (GCP_CFG, {HPA_METRICS_KEY: HPA_METRICS}),
        ("gcp/prod/config.yaml", {}),
    ]
    assert vr.source_of(HPA_METRICS_KEY, chain) == GCP_CFG
    chain.append(("cohort/config.yaml", {HPA_METRICS_KEY: HPA_METRICS}))
    assert vr.source_of(HPA_METRICS_KEY, chain) == "cohort/config.yaml"


def test_assess_flags_keys_already_identical_in_parent():
    old = {
        "appspace.microservices.definitions.network.hpa.enabled": True,
        "appspace.microservices.definitions.network.hpa.minReplicas": 6,
    }
    new = dict(old)
    new[HPA_METRICS_KEY] = HPA_METRICS
    new[HPA_BEHAVIOR_KEY] = HPA_BEHAVIOR
    parent = [(GCP_CFG, {
        HPA_METRICS_KEY: HPA_METRICS,
        HPA_BEHAVIOR_KEY: HPA_BEHAVIOR,
        "appspace.microservices.definitions.network.hpa.minReplicas": 2,
    })]
    finding = vr.assess(CUSTOMER, old, new, parent)
    assert finding is not None
    assert finding["all_redundant"] is True
    keys = {i["key"] for i in finding["redundant"]}
    assert keys == {HPA_METRICS_KEY, HPA_BEHAVIOR_KEY}
    assert all(i["source"] == GCP_CFG for i in finding["redundant"])


def test_assess_keeps_effective_keys_out_of_redundancy():
    """minReplicas 6 is NOT in the parent (parent has 2) - not redundant."""
    old = {}
    new = {
        "appspace.microservices.definitions.network.hpa.minReplicas": 6,
        HPA_METRICS_KEY: HPA_METRICS,
    }
    parent = [(GCP_CFG, {
        HPA_METRICS_KEY: HPA_METRICS,
        "appspace.microservices.definitions.network.hpa.minReplicas": 2,
    })]
    finding = vr.assess(CUSTOMER, old, new, parent)
    assert finding is not None
    assert finding["all_redundant"] is False
    assert [i["key"] for i in finding["redundant"]] == [HPA_METRICS_KEY]
    assert finding["effective"] == [
        "appspace.microservices.definitions.network.hpa.minReplicas"]


def test_assess_quiet_when_nothing_matches_parent():
    old, new = {}, {"appspace.foo": 1}
    parent = [(GCP_CFG, {"appspace.other": 2})]
    assert vr.assess(CUSTOMER, old, new, parent) is None


def test_assess_ignores_removals_and_version_pins():
    old = {"appspace.gone": 1, "appspace.version": "2603.2.11"}
    new = {"appspace.version": "2603.2.12"}
    parent = [(GCP_CFG, {"appspace.version": "2603.2.12"})]
    assert vr.assess(CUSTOMER, old, new, parent) is None


def test_render_names_ancestor_and_explains_quiet_diff():
    finding = vr.assess(
        CUSTOMER,
        {},
        {HPA_METRICS_KEY: HPA_METRICS},
        [(GCP_CFG, {HPA_METRICS_KEY: HPA_METRICS})],
    )
    body = "\n".join(vr.render_lines([finding], _VALUES_REDUNDANCY_HDR))
    assert _VALUES_REDUNDANCY_HDR in body
    assert CUSTOMER in body
    assert GCP_CFG in body
    assert "No manifest changes" in body
    assert "already provided" in body


def test_noop_status_hint_distinguishes_redundancy_from_plain_input():
    assert "higher layer" in vr.noop_status_hint(True, True)
    assert "config YAML changed" in vr.noop_status_hint(False, True)
    assert vr.noop_status_hint(False, False) == "No manifest changes"


# ── the service half ─────────────────────────────────────────────────────

def _wire(monkeypatch, old_customer, new_customer, parent_yaml):
    """One env, one app, value chain: gcp/config.yaml then customer.yaml."""
    app = "pv-fedex-c-ms"
    monkeypatch.setattr(m, "_app_value_files_map", {
        app: [f"$config/{GCP_CFG}", f"$config/{CUSTOMER}"],
    })
    monkeypatch.setattr(m, "_app_repo_map", {app: "acme-config-prod"})

    def fetch(path, sha, repo=None):
        clean = path.split("$config/", 1)[-1].lstrip("/")
        if clean == GCP_CFG:
            return parent_yaml, m.BB_OK
        if clean == CUSTOMER:
            body = new_customer if sha == PR_SHA else old_customer
            return body, m.BB_OK
        return "", m.BB_NOT_FOUND

    monkeypatch.setattr(m, "_bb_fetch_cached", fetch)
    return {CUSTOMER: [app]}


def test_panel_fires_on_pasted_hpa_metrics_already_in_gcp_config(monkeypatch):
    parent = yaml.dump({
        "appspace": {"microservices": {"definitions": {"network": {"hpa": {
            "metrics": HPA_METRICS,
            "behavior": HPA_BEHAVIOR,
        }}}}}
    })
    old = yaml.dump({
        "appspace": {"microservices": {"definitions": {"network": {"hpa": {
            "enabled": True,
            "minReplicas": 6,
            "maxReplicas": 25,
        }}}}}
    })
    new = yaml.dump({
        "appspace": {"microservices": {"definitions": {"network": {"hpa": {
            "enabled": True,
            "minReplicas": 6,
            "maxReplicas": 25,
            "metrics": HPA_METRICS,
            "behavior": HPA_BEHAVIOR,
        }}}}}
    })
    path_map = _wire(monkeypatch, old, new, parent)
    lines = m._values_redundancy_lines(
        [CUSTOMER], PR_SHA, BASE_SHA, path_map, repo="acme-config-prod")
    body = "\n".join(lines)
    assert _VALUES_REDUNDANCY_HDR in body
    assert GCP_CFG in body
    assert "hpa.metrics" in body or "hpa.behavior" in body


def test_panel_fail_open_on_bb_error_status(monkeypatch):
    monkeypatch.setattr(
        m, "_bb_fetch_cached",
        lambda *a, **k: ("", m.BB_ERROR))
    assert m._values_redundancy_lines(
        [CUSTOMER], PR_SHA, BASE_SHA, {CUSTOMER: ["pv-x-ms"]},
        repo="r") == []


def test_merge_summary_reviews_higher_layer_redundancy():
    lines = [
        "### Higher-layer values already cover this PR",
        "",
        f"already provided by `{GCP_CFG}`: `appspace.foo`",
        "",
        f"*{_VALUES_REDUNDANCY_HDR} Drop the redundant copies.*",
        "",
    ]
    result = m.DiffResult("", [], 0, False, None, m.OUT_NO_DIFF, None)
    summary = "\n".join(_build_merge_summary(
        {"pv-fedex-c-ms": result}, {}, [], [], lines, [], False))
    assert "Higher-layer" in summary
    assert "item" in summary  # REVIEW verdict carries "(N item(s))"


def test_clean_status_mentions_higher_layer_when_redundant():
    """Footer / build status must not read as a silent miss (#4520)."""
    hint = m._clean_status_description(
        has_redundancy=True, has_input_changes=True)
    assert "No manifest changes" in hint
    assert "higher layer" in hint
    assert hint != "No manifest changes"
