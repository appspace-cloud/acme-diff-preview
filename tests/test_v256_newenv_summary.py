"""v2.5.6 — new-environment detection and output improvements.

Live test PRs #6660 (pv) and #6661 (cl) on acme-config-dev showed two problems:

Finding 1 (bug): a public-cloud (cl-*) environment has sub-app folders
(api, app1, app2, cloud, constellation, user-content), each with its own
customer.yaml. _detect_new_env_candidates treated every one of them as a
separate new environment with "no appspace.version" -> 6 fake structural
failures and a RED status for a perfectly valid new environment (#6661).

Finding 2 (output): a successfully rendered new environment dumped up to
30,000 chars of raw manifest as a fake "+" diff. The useful information
for a reviewer is: this is a brand-new environment, this chart version,
these resources/applications. The full manifest is too large to display.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m


# ── Finding 1: nested candidate collapse ────────────────────────────────

CL_BASE = "gcp/qa/public-cloud/ap1/cl-test-nes2-a"

CL_FILES = [
    f"{CL_BASE}/config.yaml",
    f"{CL_BASE}/api/customer.yaml",
    f"{CL_BASE}/app1/customer.yaml",
    f"{CL_BASE}/app2/customer.yaml",
    f"{CL_BASE}/cloud/customer.yaml",
    f"{CL_BASE}/constellation/customer.yaml",
    f"{CL_BASE}/constellation/cicd-versions.yaml",
    f"{CL_BASE}/user-content/customer.yaml",
]


def test_detect_collapses_cl_subfolders_into_parent_env():
    # Live repro of PR #6661: 7 candidates were produced (the env itself
    # plus 6 sub-app folders). Only the parent env must survive.
    candidates = m._detect_new_env_candidates(CL_FILES, path_map={})
    names = sorted(c["name"] for c in candidates)
    assert names == ["cl-test-nes2-a"], f"expected only the parent env, got {names}"
    # The parent keeps all the yaml files of the whole env tree.
    assert sorted(candidates[0]["all_yaml_files"]) == sorted(CL_FILES)


def test_detect_hierarchical_defaults_do_not_hide_new_envs():
    # v2.5.7 LIVE REGRESSION (v2.5.6, PRs #6660/#6661 re-run): the config
    # repo has hierarchical defaults named config.yaml at EVERY ancestor
    # level (gcp/config.yaml, gcp/qa/config.yaml, gcp/qa/private-cloud/
    # ap1/config.yaml, ...). They are value files of existing apps, so they
    # are in path_map. v2.5.6's "Rule 2" treated each of their directories
    # as an existing env root, and since gcp/ is an ancestor of everything,
    # EVERY new-env candidate was excluded -> a false green "No ArgoCD apps
    # affected" for PRs creating whole environments. Any ancestor-based
    # exclusion is unsafe in this repo layout; this test pins that both the
    # pv and the cl shape survive a realistic path_map full of defaults.
    path_map = {
        "gcp/config.yaml": ["many"],
        "gcp/qa/config.yaml": ["many"],
        "gcp/qa/private-cloud/config.yaml": ["many"],
        "gcp/qa/private-cloud/ap1/config.yaml": ["many"],
        "gcp/qa/public-cloud/ap1/config.yaml": ["many"],
        "gcp/qa/private-cloud/ap1/custom/pv-qa-15-a/customer.yaml": ["pv-qa-15-a-ms"],
        "gcp/qa/public-cloud/ap1/cl-qa-14-a/config.yaml": ["cl-qa-14-a-ms"],
    }
    pv_new = "gcp/qa/private-cloud/ap1/custom/pv-test-nes1-a/customer.yaml"
    candidates = m._detect_new_env_candidates([pv_new] + CL_FILES, path_map)
    names = sorted(c["name"] for c in candidates)
    assert names == ["cl-test-nes2-a", "pv-test-nes1-a"], (
        f"hierarchical defaults in path_map must not hide new envs, got {names}")


def test_detect_shared_value_file_in_path_map_does_not_hide_new_envs():
    # Guard: a shared value file at a shallow directory (mapped to many
    # apps) must NOT make every env under that directory look "existing".
    shared = "gcp/qa/defaults.yaml"
    new_env = "gcp/qa/private-cloud/ap1/custom/pv-brandnew-a/customer.yaml"
    path_map = {shared: ["many-apps"]}
    candidates = m._detect_new_env_candidates([new_env], path_map)
    assert [c["name"] for c in candidates] == ["pv-brandnew-a"]


def test_detect_pv_env_still_detected():
    # Guard: the simple pv case (PR #6660) keeps working.
    files = [
        "gcp/qa/private-cloud/ap1/custom/pv-test-nes1-a/customer.yaml",
        "gcp/qa/private-cloud/ap1/custom/pv-test-nes1-a/cicd-versions.yaml",
    ]
    candidates = m._detect_new_env_candidates(files, path_map={})
    assert [c["name"] for c in candidates] == ["pv-test-nes1-a"]
    assert sorted(candidates[0]["all_yaml_files"]) == sorted(files)


def test_detect_two_independent_new_envs_both_kept():
    # Guard: two sibling new envs are NOT nested — both must survive.
    files = [
        "gcp/qa/private-cloud/ap1/custom/pv-new1-a/customer.yaml",
        "gcp/qa/private-cloud/ap1/custom/pv-new2-a/customer.yaml",
    ]
    candidates = m._detect_new_env_candidates(files, path_map={})
    assert sorted(c["name"] for c in candidates) == ["pv-new1-a", "pv-new2-a"]


# ── Finding 2: manifest summary instead of the raw diff wall ────────────

RENDERED = """\
---
# Source: appspace-micro-services/templates/deploy.yaml
kind: Deployment
apiVersion: apps/v1
metadata:
  name: account
  labels:
    app: account
---
kind: Deployment
apiVersion: apps/v1
metadata:
  name: api-gateway
---
kind: Service
apiVersion: v1
metadata:
  name: account
---
kind: ConfigMap
apiVersion: v1
metadata:
  name: account-config
---
kind: CronJob
apiVersion: batch/v1
metadata:
  name: cleanup-job
"""


def test_summarize_rendered_manifest_counts_and_names():
    total, kind_counts, workloads = m._summarize_rendered_manifest(RENDERED)
    assert total == 5
    assert kind_counts == {"Deployment": 2, "Service": 1, "ConfigMap": 1, "CronJob": 1}
    assert workloads == ["account", "api-gateway", "cleanup-job"]


def test_summarize_rendered_manifest_empty_input():
    total, kind_counts, workloads = m._summarize_rendered_manifest("")
    assert total == 0 and kind_counts == {} and workloads == []


def _mk_candidate(name="pv-sum-a"):
    return [{"name": name, "config_file": "x/customer.yaml", "env_dir": "x",
             "all_yaml_files": ["x/customer.yaml"], "version": "2603.0.1-dev"}]


def test_evaluate_new_envs_success_shows_summary_not_diff(monkeypatch):
    monkeypatch.setattr(m, "_render_new_env_diff",
        lambda env_info, pr_sha: (RENDERED, None, 5, "2603.0.1-dev"))
    lines, structural, total_new = m._evaluate_new_envs(_mk_candidate(), "prsha")
    body = "\n".join(lines)
    # No raw manifest wall in the comment anymore.
    assert "```diff" not in body
    assert "kind: Deployment" not in body
    # The reviewer-facing summary is there instead.
    assert "completely new environment" in body
    assert "too large to display" in body
    assert "2603.0.1-dev" in body
    assert "5" in body                       # total resources
    assert "Deployment" in body              # kind breakdown
    assert "account" in body and "api-gateway" in body   # app names
    assert structural == [] and total_new == 5


def test_evaluate_new_envs_success_stays_green(monkeypatch):
    monkeypatch.setattr(m, "_render_new_env_diff",
        lambda env_info, pr_sha: (RENDERED, None, 5, "2603.0.1-dev"))
    _, structural, _ = m._evaluate_new_envs(_mk_candidate(), "prsha")
    assert structural == []


def test_evaluate_new_envs_intro_no_longer_promises_preview(monkeypatch):
    # The old intro said "Below is a preview of the resources that will be
    # provisioned" even when no preview could be rendered at all.
    monkeypatch.setattr(m, "_render_new_env_diff",
        lambda env_info, pr_sha: (None, "helm template failed: Missing required value: x", 0, "2603.0.1-dev"))
    lines, structural, _ = m._evaluate_new_envs(_mk_candidate(), "prsha")
    body = "\n".join(lines)
    assert "Below is a preview" not in body
    assert structural == []                  # regression: still non-structural


def test_evaluate_new_envs_structural_error_still_red(monkeypatch):
    # Regression guard: classification (FIX E / Finding 5) is untouched.
    monkeypatch.setattr(m, "_render_new_env_diff",
        lambda env_info, pr_sha: (None, "no appspace.version found in config file", 0, None))
    _, structural, _ = m._evaluate_new_envs(_mk_candidate("pv-broken-a"), "prsha")
    assert structural == ["pv-broken-a"]
