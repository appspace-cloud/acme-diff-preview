"""v2.6.3 azure-in-scope contract (follow-up of COPS-2517).

The azure/ tree in acme-config-stage is now ArgoCD-managed: the environment
pv-stage-corporate-b (AKS cluster az-prod-pv-na1-b) was onboarded as the
first Azure spoke, with real Applications (ms/ss/glb) whose git source is
acme-config-stage and whose manifest-generate-paths cover the azure/ tree.
The stage instance therefore serves that tree too, via the DIFF_REPOS scope
"gcp/|azure/".

The engine has always been cloud-agnostic: paths come from ArgoCD app
annotations and from the file layout, never from a hardcoded cloud prefix.
These tests pin that contract on the REAL production layout
(azure/stage/private-cloud/na1-b/custom/pv-stage-corporate-b/...) so no
future change can silently reintroduce a gcp-only assumption in scope
filtering, app matching, new-env detection, or hierarchy version walk.
The aws/ tree stays out of scope and silent.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m  # noqa: E402

DEV = "acme-config-dev"
STG = "acme-config-stage"

# Real production paths (verified live on the hub, 2026-07-19).
AZ_ENV_DIR = "azure/stage/private-cloud/na1-b/custom/pv-stage-corporate-b"
AZ_MAP     = {f"{AZ_ENV_DIR}/customer.yaml": ["pv-stage-corporate-b-ms"],
              "azure/stage/private-cloud/na1-b/config.yaml":
                  ["pv-stage-corporate-b-ms"]}
GCP_ENV    = "gcp/stage/private-cloud/na1/custom/pv-stage1-a"

NEW_SCOPES = ["gcp/", "azure/"]


def _mk_pr(pr_id, sha="e" * 12):
    return {"id": pr_id, "title": f"pr {pr_id}",
            "source": {"commit": {"hash": sha}, "branch": {"name": "f"}},
            "destination": {"branch": {"name": "main"}}}


@pytest.fixture()
def stage_world(monkeypatch):
    sinks = {"upserts": [], "statuses": []}
    monkeypatch.setattr(m, "REPOS",
                        {DEV: {"scopes": []}, STG: {"scopes": list(NEW_SCOPES)}})
    monkeypatch.setattr(m, "find_existing_comment",
                        lambda pr_id, repo=None: (None, "", ""))
    monkeypatch.setattr(m, "upsert_comment",
                        lambda pr_id, body, existing_id=None, repo=None:
                        sinks["upserts"].append((repo, body)))
    monkeypatch.setattr(m, "post_build_status",
                        lambda sha, st, d, pr_id=None, repo=None:
                        sinks["statuses"].append((repo, st)))
    monkeypatch.setattr(m, "fix_stuck_inprogress", lambda *a, **k: None)
    monkeypatch.setattr(m, "_touch_progress", lambda: None)
    m._seen.clear(); m._force_recompute.clear()
    yield sinks
    m._seen.clear(); m._force_recompute.clear()


# ── scope: azure PRs are now first-class in stage ────────────────────────

def test_azure_only_pr_produces_comment_and_status(stage_world, monkeypatch):
    files = [f"{AZ_ENV_DIR}/customer.yaml"]
    monkeypatch.setattr(m, "get_pr_changed_files",
                        lambda pr_id, repo=None: (list(files), {}))
    monkeypatch.setattr(m, "argocd_diff",
                        lambda *a, **k: m.DiffResult("", [], 0, False, "",
                                                     m.OUT_NO_DIFF, ""))
    m.process_pr(_mk_pr(201), AZ_MAP, base_sha="b" * 12, repo=STG)
    assert stage_world["upserts"], \
        "azure PR must get a bot comment when azure/ is in scope"
    assert stage_world["statuses"], \
        "azure PR must get a build status when azure/ is in scope"


def test_mixed_gcp_azure_pr_keeps_both_files(stage_world, monkeypatch):
    files = [f"{AZ_ENV_DIR}/customer.yaml", f"{GCP_ENV}/customer.yaml"]
    seen = {}
    real_match = m._match_files_to_apps
    def spy(changed, pmap):
        seen["files"] = list(changed)
        return real_match(changed, pmap)
    monkeypatch.setattr(m, "_match_files_to_apps", spy)
    monkeypatch.setattr(m, "get_pr_changed_files",
                        lambda pr_id, repo=None: (list(files), {}))
    monkeypatch.setattr(m, "argocd_diff",
                        lambda *a, **k: m.DiffResult("", [], 0, False, "",
                                                     m.OUT_NO_DIFF, ""))
    m.process_pr(_mk_pr(202), AZ_MAP, base_sha="b" * 12, repo=STG)
    assert sorted(seen["files"]) == sorted(files), \
        "both gcp/ and azure/ files must survive the scope filter"


def test_aws_only_pr_is_still_silent(stage_world, monkeypatch):
    files = ["aws/stage/private-cloud/na1/custom/pv-aws-x/customer.yaml"]
    monkeypatch.setattr(m, "get_pr_changed_files",
                        lambda pr_id, repo=None: (list(files), {}))
    m.process_pr(_mk_pr(203), AZ_MAP, base_sha="b" * 12, repo=STG)
    assert stage_world["upserts"] == [], \
        "aws/ tree stays out of scope: no comment"
    assert stage_world["statuses"] == [], \
        "aws/ tree stays out of scope: no build status"


# ── new-env detection works at the azure tree depth ──────────────────────

def test_azure_new_env_candidate_detected():
    new_env = "azure/stage/private-cloud/na1-b/custom/pv-stage-corporate-c"
    cands = m._detect_new_env_candidates(
        [f"{new_env}/customer.yaml"], AZ_MAP, {})
    assert len(cands) == 1
    assert cands[0]["name"] == "pv-stage-corporate-c"
    assert cands[0]["env_dir"] == new_env


# ── hierarchy version walk resolves over azure ancestors (Finding B) ─────

def _fetch_factory(files):
    def fake_fetch(path, sha, repo=None):
        if path in files:
            return files[path], m.BB_OK
        return None, m.BB_NOT_FOUND
    return fake_fetch


def test_azure_version_inherited_from_na1b_cohort(monkeypatch):
    new_env = "azure/stage/private-cloud/na1-b/custom/pv-stage-corporate-c"
    files = {
        f"{new_env}/customer.yaml": "appspace:\n  customerName: x\n",
        "azure/stage/private-cloud/na1-b/custom/config.yaml":
            "appspace:\n  version: 2603.1.1-rev1-dev\n",
        "azure/config.yaml": "appspace:\n  version: 0.0.1\n",
    }
    monkeypatch.setattr(m, "_bb_fetch_status", _fetch_factory(files))
    picked = {}
    def fake_ensure(registry, chart, version):
        picked["version"] = version
        raise RuntimeError("stop-here")
    monkeypatch.setattr(m, "_ensure_chart", fake_ensure)
    env = {"name": "pv-stage-corporate-c",
           "config_file": f"{new_env}/customer.yaml",
           "env_dir": new_env,
           "all_yaml_files": [f"{new_env}/customer.yaml"]}
    try:
        m._render_new_env_diff(env, "c" * 12)
    except RuntimeError:
        pass
    assert picked.get("version") == "2603.1.1-rev1-dev", \
        "most specific azure ancestor level must win, same as gcp"
