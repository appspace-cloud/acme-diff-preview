"""Battle-test fixes (COPS-2545): identity moves, broken cohorts, real previews.

Findings from the live battery on acme-config-prod PR #3800 (2026-07-29),
cycling the shapes of PRs 3796/3797 and variants:

F2: an unpaired folder move (identity file deleted and recreated elsewhere
    with very different content, 12 -> 497 lines in the real case) still
    reported an environment decommission PLUS a duplicate broken new env.
    Fix: synthesize the rename pairing from declared identity, the signal
    Bitbucket's content-similarity pairing cannot see.

F4: a cohort config.yaml with INVALID YAML was silently tolerated (the
    ancestor chain never fed it to helm), so a file the ApplicationSet
    generator cannot parse would merge green. Fix: the cohort guard from
    v2.13.2 now also parses the file and blocks with the reason.

F1: _render_new_env_diff only passed the env's own files to helm, never
    the ancestor cascade, so every new env failed on values defined at
    upper levels (cloudShortName lives in gcp/config.yaml) and the preview
    degraded to boilerplate. Fix: build the full root-to-leaf value chain.

F3: helm nil-pointer errors on microservices.definitions entries without
    an image mapping are cryptic. Fix: append a hint to the comment.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m


MAIN_SHA = "aaaa1111"
PR_SHA   = "bbbb2222"

OLD_ENV  = "gcp/prod/private-cloud/gb1-b/weekly/pv-ukhsa-a/customer.yaml"
NEW_ENV  = "gcp/prod/private-cloud/gb1-b/hardcoded/migration/weekly/pv-ukhsa-a/customer.yaml"
COHORT   = "gcp/prod/private-cloud/gb1-b/hardcoded/migration/weekly/config.yaml"

OLD_CONTENT = "---\nappspace:\n  customerName: ukhsa\n  instance: pv-ukhsa-a\n"
NEW_CONTENT = ("---\nappspace:\n  version: 2603.0.12-rev1\n"
               "  customerName: ukhsa\n  instance: pv-ukhsa-a\n"
               "  microservices:\n    defaults:\n      nodeSelector:\n"
               "        gke_node_type: migration-1\n")
OTHER_CONTENT = "---\nappspace:\n  customerName: sainsburys\n  instance: pv-sainsburys-a\n"

PATH_MAP = {OLD_ENV: ["pv-ukhsa-a-ms", "pv-ukhsa-a-ss", "pv-ukhsa-a-glb"]}


def _fetch_factory(monkeypatch, files_main: dict, files_pr: dict, fail: bool = False):
    def fake(clean, sha, repo=None):
        if fail:
            raise RuntimeError("bitbucket down")
        src = files_main if sha == MAIN_SHA else files_pr
        if clean in src:
            return src[clean], m.BB_OK
        return None, m.BB_NOT_FOUND
    monkeypatch.setattr(m, "_bb_fetch_status", fake)


# ── F2: identity-based rename synthesis ─────────────────────────────────────

def test_identity_move_synthesizes_rename(monkeypatch):
    _fetch_factory(monkeypatch,
                   files_main={OLD_ENV: OLD_CONTENT},
                   files_pr={NEW_ENV: NEW_CONTENT})
    changed = [OLD_ENV, NEW_ENV, COHORT]
    renames = m._augment_renames_with_identity_moves(
        changed, {}, PATH_MAP, MAIN_SHA, PR_SHA, repo="acme-config-prod")
    assert renames == {OLD_ENV: NEW_ENV}


def test_identity_mismatch_does_not_pair(monkeypatch):
    _fetch_factory(monkeypatch,
                   files_main={OLD_ENV: OLD_CONTENT},
                   files_pr={NEW_ENV: OTHER_CONTENT})
    renames = m._augment_renames_with_identity_moves(
        [OLD_ENV, NEW_ENV], {}, PATH_MAP, MAIN_SHA, PR_SHA)
    assert renames == {}


def test_ambiguous_match_stays_unpaired(monkeypatch):
    second_new = "gcp/prod/private-cloud/eu1-b/monthly/pv-ukhsa-a/customer.yaml"
    _fetch_factory(monkeypatch,
                   files_main={OLD_ENV: OLD_CONTENT},
                   files_pr={NEW_ENV: NEW_CONTENT, second_new: NEW_CONTENT})
    renames = m._augment_renames_with_identity_moves(
        [OLD_ENV, NEW_ENV, second_new], {}, PATH_MAP, MAIN_SHA, PR_SHA)
    assert renames == {}


def test_existing_pairing_untouched(monkeypatch):
    _fetch_factory(monkeypatch,
                   files_main={OLD_ENV: OLD_CONTENT},
                   files_pr={NEW_ENV: NEW_CONTENT})
    prior = {OLD_ENV: "somewhere/else/customer.yaml"}
    renames = m._augment_renames_with_identity_moves(
        [OLD_ENV, NEW_ENV], dict(prior), PATH_MAP, MAIN_SHA, PR_SHA)
    assert renames == prior


def test_fetch_failure_stays_unpaired(monkeypatch):
    _fetch_factory(monkeypatch, {}, {}, fail=True)
    renames = m._augment_renames_with_identity_moves(
        [OLD_ENV, NEW_ENV], {}, PATH_MAP, MAIN_SHA, PR_SHA)
    assert renames == {}


def test_process_pr_calls_the_augmenter():
    src = open(os.path.join(os.path.dirname(__file__), "..", "src",
                            "diff_preview.py")).read()
    assert "_augment_renames_with_identity_moves(" in src.replace(
        "def _augment_renames_with_identity_moves(", "", 1), (
        "process_pr must call _augment_renames_with_identity_moves")


# ── F4: cohort config.yaml with invalid YAML blocks with the reason ─────────

CAND = {
    "name": "pv-copstest-a",
    "config_file": "gcp/prod/private-cloud/gb1-b/hardcoded/migrationtest/pv-copstest-a/customer.yaml",
    "env_dir": "gcp/prod/private-cloud/gb1-b/hardcoded/migrationtest/pv-copstest-a",
    "all_yaml_files": [],
}
CAND_COHORT = "gcp/prod/private-cloud/gb1-b/hardcoded/migrationtest/config.yaml"


def test_unparseable_cohort_blocks(monkeypatch):
    def fake(clean, sha, repo=None):
        assert clean == CAND_COHORT
        return "---\nappspace:\n  broken\n    defaults: [oops\n  :::\n", m.BB_OK
    monkeypatch.setattr(m, "_bb_fetch_status", fake)
    monkeypatch.setattr(m, "_render_new_env_diff",
                        lambda info, sha: (_ for _ in ()).throw(
                            AssertionError("must not render past a broken cohort")))
    lines, structural, total = m._evaluate_new_envs([dict(CAND)], PR_SHA)
    assert structural == ["pv-copstest-a"]
    joined = "\n".join(lines)
    assert CAND_COHORT in joined
    assert "cannot be parsed" in joined


def test_parseable_cohort_does_not_block(monkeypatch):
    def fake(clean, sha, repo=None):
        return "---\n# placeholder\n", m.BB_OK
    monkeypatch.setattr(m, "_bb_fetch_status", fake)
    monkeypatch.setattr(m, "_render_new_env_diff",
                        lambda info, sha: (None,
                            "helm template failed: Missing required value: x",
                            0, None))
    lines, structural, total = m._evaluate_new_envs([dict(CAND)], PR_SHA)
    assert structural == []


# ── F1: full root-to-leaf value chain for new env renders ───────────────────

def test_new_env_render_includes_ancestor_chain(monkeypatch):
    env_dir = "gcp/prod/private-cloud/gb1-b/hardcoded/migrationtest/pv-copstest-a"
    files_pr = {
        f"{env_dir}/customer.yaml":
            "---\nappspace:\n  version: 1.0.0\n  customerName: copstest\n",
        "gcp/config.yaml": "---\nappspace:\n  cloudShortName: gcp\n",
        "gcp/prod/private-cloud/gb1-b/config.yaml": "---\nappspace: {}\n",
        CAND_COHORT: "---\n# placeholder\n",
    }
    def fake(clean, sha, repo=None):
        if clean in files_pr:
            return files_pr[clean], m.BB_OK
        return None, m.BB_NOT_FOUND
    monkeypatch.setattr(m, "_bb_fetch_status", fake)
    monkeypatch.setattr(m, "_ensure_chart", lambda reg, chart, ver: "/tmp/fake-chart")
    captured = {}
    def fake_helm(chart_path, release, namespace, vals):
        captured["vals"] = list(vals.keys())
        return "kind: ConfigMap\n", None
    monkeypatch.setattr(m, "_helm_template", fake_helm)
    cand = {"name": "pv-copstest-a",
            "config_file": f"{env_dir}/customer.yaml",
            "env_dir": env_dir,
            "all_yaml_files": [f"{env_dir}/customer.yaml"]}
    rendered, err = m._render_new_env_diff(cand, PR_SHA)[:2]
    assert err is None
    keys = captured["vals"]
    root   = "$config/gcp/config.yaml"
    cohort = f"$config/{CAND_COHORT}"
    leaf   = f"$config/{env_dir}/customer.yaml"
    assert root in keys and cohort in keys and leaf in keys
    assert keys.index(root) < keys.index(cohort) < keys.index(leaf)


# ── F3: cryptic nil pointer errors carry a hint ─────────────────────────────

def test_nil_pointer_error_gets_hint(monkeypatch):
    err = ("helm template failed: Error: template: appspace-micro-services/"
           "templates/configmaps/micro-versions-info.yaml:15:124: executing "
           "at <$microservice.image.tag>: nil pointer evaluating interface")
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda clean, sha, repo=None: ("---\n", m.BB_OK))
    monkeypatch.setattr(m, "_render_new_env_diff",
                        lambda info, sha: (None, err, 0, "1.0.0"))
    lines, structural, total = m._evaluate_new_envs([dict(CAND)], PR_SHA)
    joined = "\n".join(lines)
    assert "microservices.definitions" in joined
    assert "image/version mapping" in joined
