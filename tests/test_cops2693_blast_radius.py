"""COPS-2693 Plan B: wide-reach shared-config changes get a REVIEW callout.

The cadence cohorts stage version bumps; a non-version edit to a shared
config.yaml bypasses that staging and lands on everything under it ~5 minutes
after merge. The finding makes that reach impossible to miss in the verdict.

The exemptions ARE the design: a version-only bump touching 100 environments
is the routine flow and must never fire — a finding that cries wolf on the
monthly bump gets muted by the third occurrence, and then it protects nothing.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import blast_radius as br
import diff_preview as m
from comment_render import _BLAST_RADIUS_HDR

SHARED = "gcp/prod/private-cloud/config.yaml"
PR_SHA, BASE_SHA = "f00d" * 10, "beef" * 10


# ── the pure half ────────────────────────────────────────────────────────

def test_changed_keys_covers_add_remove_and_value_change():
    old = {"a.b": 1, "a.c": 2, "gone": 3}
    new = {"a.b": 1, "a.c": 9, "fresh": 4}
    assert br.changed_keys(old, new) == {"a.c", "gone", "fresh"}


def test_version_only_is_exempt_whatever_the_reach():
    """The monthly bump shape: appspace.version in a cohort config.yaml."""
    assert br.is_version_only({"appspace.version"})
    assert br.is_version_only(set())
    assert not br.is_version_only({"appspace.version", "appspace.replicas"})
    # 'versionX' or 'version' mid-path is NOT a version pin
    assert not br.is_version_only({"appspace.versionPolicy"})
    assert not br.is_version_only({"version.pinned"})


def test_spoke_extraction_both_fleet_shapes():
    assert br.spoke_of(
        "gcp/prod/private-cloud/na2-a/monthly/pv-x-a/customer.yaml") == "na2-a"
    assert br.spoke_of(
        "gcp/prod/public-cloud/na1-a/cl-prod-b/app3/customer.yaml") == "na1-a"
    assert br.spoke_of("weird/path/customer.yaml") == "?"


def _envs(n_envs, n_spokes):
    return [f"gcp/prod/private-cloud/sp{i % n_spokes}-a/pv-e{i}-a/customer.yaml"
            for i in range(n_envs)]


def test_assess_fires_on_envs_or_spokes_threshold():
    keys = {"appspace.replicas"}
    assert br.assess(SHARED, keys, _envs(30, 2), 30, 4) is not None   # envs
    assert br.assess(SHARED, keys, _envs(10, 4), 30, 4) is not None   # spokes
    assert br.assess(SHARED, keys, _envs(29, 3), 30, 4) is None       # below both


def test_assess_never_fires_version_only_or_empty():
    assert br.assess(SHARED, {"appspace.version"}, _envs(200, 10), 30, 4) is None
    assert br.assess(SHARED, set(), _envs(200, 10), 30, 4) is None


def test_render_names_file_counts_and_keys():
    f = br.assess(SHARED, {"appspace.replicas", "appspace.features.x"},
                  _envs(40, 5), 30, 4)
    lines = br.render_lines([f], _BLAST_RADIUS_HDR, 30, 4)
    body = "\n".join(lines)
    assert _BLAST_RADIUS_HDR in body
    assert SHARED in body
    assert "40 environments across 5 spoke(s)" in body
    assert "appspace.replicas" in body
    assert "DIFF_BLAST_ENVS" in body      # thresholds visible for tuning


# ── the service half ─────────────────────────────────────────────────────

def _wire(monkeypatch, old_yaml, new_yaml, n_envs=40, n_spokes=5):
    apps, vf_map, path_map_apps = [], {}, []
    for i in range(n_envs):
        app = f"pv-e{i}-a-ss"
        apps.append(app)
        vf_map[app] = ["$config/gcp/prod/private-cloud/"
                       f"sp{i % n_spokes}-a/pv-e{i}-a/customer.yaml"]
        path_map_apps.append(app)
    monkeypatch.setattr(m, "_app_value_files_map", vf_map)
    monkeypatch.setattr(
        m, "_bb_fetch_cached",
        lambda p, sha, repo=None: ((new_yaml if sha == PR_SHA else old_yaml),
                                   m.BB_OK))
    return {SHARED: path_map_apps}


def test_panel_fires_on_wide_nonversion_change(monkeypatch):
    pm = _wire(monkeypatch,
               "appspace:\n  replicas: 2\n  version: 1.0.0\n",
               "appspace:\n  replicas: 3\n  version: 1.0.0\n")
    lines = m._blast_radius_lines([SHARED], PR_SHA, BASE_SHA, pm)
    body = "\n".join(lines)
    assert _BLAST_RADIUS_HDR in body
    assert "40 environments across 5 spoke(s)" in body
    assert "appspace.replicas" in body


def test_panel_silent_on_the_monthly_bump(monkeypatch):
    """The critical exemption: a version-only change across 40 envs."""
    pm = _wire(monkeypatch,
               "appspace:\n  version: 1.0.0\n",
               "appspace:\n  version: 1.0.1\n")
    assert m._blast_radius_lines([SHARED], PR_SHA, BASE_SHA, pm) == []


def test_panel_silent_below_thresholds(monkeypatch):
    pm = _wire(monkeypatch,
               "appspace:\n  replicas: 2\n",
               "appspace:\n  replicas: 3\n", n_envs=5, n_spokes=1)
    assert m._blast_radius_lines([SHARED], PR_SHA, BASE_SHA, pm) == []


def test_panel_ignores_customer_yaml_and_unreadable_sides(monkeypatch):
    pm = _wire(monkeypatch, "a: 1\n", "a: 2\n")
    # customer.yaml is never a candidate, whatever its reach
    assert m._blast_radius_lines(
        ["gcp/prod/private-cloud/sp0-a/pv-e0-a/customer.yaml"],
        PR_SHA, BASE_SHA, pm) == []
    # an added/removed file (one side unreadable) belongs to other panels
    monkeypatch.setattr(m, "_bb_fetch_cached",
                        lambda p, sha, repo=None: (None, m.BB_NOT_FOUND))
    assert m._blast_radius_lines([SHARED], PR_SHA, BASE_SHA, pm) == []


def test_verdict_carries_the_review_line_with_the_reach():
    f = br.assess(SHARED, {"appspace.replicas"}, _envs(40, 5), 30, 4)
    lines = br.render_lines([f], _BLAST_RADIUS_HDR, 30, 4)
    body = m.format_comment(PR_SHA, {}, base_sha=BASE_SHA,
                            appspace_state_lines=lines)
    assert "Wide-reach config change" in body
    assert "reaches 40 environments across 5 spoke(s)" in body
    # REVIEW, not BLOCK: the merge stays a human decision
    assert "DO NOT MERGE" not in body
