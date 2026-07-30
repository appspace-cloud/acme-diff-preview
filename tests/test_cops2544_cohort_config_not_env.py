"""Cohort/defaults config.yaml at env depth is not a new environment.

Observed live on acme-config-prod PRs 3796 and 3797 (2026-07-28): moving
pv-myschroders-a into gb1-b/hardcoded/migration/weekly/ requires adding
hardcoded/migration/weekly/config.yaml, the value file the ArgoCD
ApplicationSet generator loads next to every environment folder. The new
env detector turned that defaults file into a candidate named "weekly",
rendered it as an environment, failed with "no appspace.version found",
and the build status went red for a structurally correct PR.

The discriminator is declared identity, the same signal the rename
verification trusts since v2.5.15: an environment identity file declares
customerName; a cohort/defaults config.yaml declares none. A config.yaml
candidate that declares no customerName is a values level and must be
skipped. customer.yaml candidates keep their behavior untouched, and so
does the cl-* nesting collapse (v2.5.6 Finding A), because there the
parent config.yaml DOES declare customerName.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m


COHORT_DIR   = "gcp/prod/private-cloud/gb1-b/hardcoded/migration/weekly"
COHORT_FILE  = f"{COHORT_DIR}/config.yaml"
ENV_DIR      = f"{COHORT_DIR}/pv-myschroders-a"
ENV_FILE     = f"{ENV_DIR}/customer.yaml"
OLD_ENV_FILE = "gcp/prod/private-cloud/gb1-b/weekly/pv-myschroders-a/customer.yaml"

COHORT_CONTENT = (
    "---\n"
    "# cohort defaults, no identity here\n"
    "appspace:\n"
    "  microservices:\n"
    "    defaults:\n"
    "      nodeSelector:\n"
    "        gke_node_type: migration-1\n"
)
ENV_CONTENT = (
    "---\n"
    "appspace:\n"
    "  version: 2603.0.12-rev1\n"
    "  customerName: myschroders\n"
    "  instance: pv-myschroders-a\n"
)
CL_PARENT_CONTENT = (
    "---\n"
    "appspace:\n"
    "  customerName: prod\n"
    "  suffix: b\n"
)

PR_SHA = "feedc0de"


def _mock_fetch(monkeypatch, contents: dict, fail: bool = False):
    def fake(clean, sha, repo=None):
        if fail:
            raise RuntimeError("bitbucket unavailable")
        assert sha == PR_SHA
        return contents.get(clean, ""), 200
    monkeypatch.setattr(m, "_bb_fetch_status", fake)


def test_standalone_cohort_config_is_skipped(monkeypatch):
    """PR 3797 shape: customer.yaml excluded via rename pairing, only the
    cohort config.yaml is left. It must not become a phantom environment."""
    _mock_fetch(monkeypatch, {COHORT_FILE: COHORT_CONTENT})
    changed = [COHORT_FILE, ENV_FILE]
    renames = {OLD_ENV_FILE: ENV_FILE}
    got = m._detect_new_env_candidates(
        changed, {}, renames, pr_sha=PR_SHA, repo="acme-config-prod")
    assert got == []


def test_cohort_config_yields_to_real_env(monkeypatch):
    """PR 3796 shape: no rename pairing, both files are candidates. The
    defaults level must be dropped and the real environment must survive
    (before the fix, the nesting collapse kept the parent and dropped the
    real env)."""
    _mock_fetch(monkeypatch, {COHORT_FILE: COHORT_CONTENT,
                              ENV_FILE: ENV_CONTENT})
    changed = [COHORT_FILE, ENV_FILE]
    got = m._detect_new_env_candidates(
        changed, {}, None, pr_sha=PR_SHA, repo="acme-config-prod")
    assert [c["env_dir"] for c in got] == [ENV_DIR]
    assert got[0]["name"] == "pv-myschroders-a"


def test_cl_parent_with_identity_keeps_finding_a_behavior(monkeypatch):
    """v2.5.6 Finding A regression guard: a cl-* parent config.yaml DOES
    declare customerName, so it stays a candidate and the nesting collapse
    still drops its sub-app children."""
    parent_dir  = "gcp/prod/public-cloud/na1-a/cl-prod-b"
    parent_file = f"{parent_dir}/config.yaml"
    child_file  = f"{parent_dir}/app1/customer.yaml"
    _mock_fetch(monkeypatch, {parent_file: CL_PARENT_CONTENT, child_file: ""})
    changed = [parent_file, child_file]
    got = m._detect_new_env_candidates(
        changed, {}, None, pr_sha=PR_SHA, repo="acme-config-prod")
    assert [c["env_dir"] for c in got] == [parent_dir]


def test_fetch_failure_keeps_candidate(monkeypatch):
    """A fetch failure must degrade to the conservative side: keep the
    candidate (a red finding a human looks at), never a silent skip."""
    _mock_fetch(monkeypatch, {}, fail=True)
    changed = [COHORT_FILE]
    got = m._detect_new_env_candidates(
        changed, {}, None, pr_sha=PR_SHA, repo="acme-config-prod")
    assert [c["env_dir"] for c in got] == [COHORT_DIR]


def test_no_sha_keeps_previous_behavior(monkeypatch):
    """Without a pr_sha there is nothing to fetch: behavior is unchanged
    so existing callers and tests keep their exact semantics."""
    called = {"n": 0}
    def fake(clean, sha, repo=None):
        called["n"] += 1
        return "", 200
    monkeypatch.setattr(m, "_bb_fetch_status", fake)
    changed = [COHORT_FILE]
    got = m._detect_new_env_candidates(changed, {}, None)
    assert [c["env_dir"] for c in got] == [COHORT_DIR]
    assert called["n"] == 0


# ── COPS-2544 feature 2: missing cohort config.yaml must block with a reason ─
#
# The ApplicationSet matrix loads `{{env}}/../config.yaml` next to every
# environment folder (verified live on the hub 2026-07-28: every appset with
# a customer.yaml git generator has that second generator, except the six
# gcp/aec/ ones). If the file is absent at the PR head the matrix yields
# ZERO results: no Application is generated, and a moved environment gets
# decommissioned instead of followed. The tool must block the PR and say
# exactly why, instead of a green "will be created on merge" that is false.

CAND = {
    "name": "pv-myschroders-a",
    "config_file": ENV_FILE,
    "env_dir": ENV_DIR,
    "all_yaml_files": [ENV_FILE],
}
EXPECTED_GREEN_RENDER = (None, "helm template failed: Missing required value: x", 0, None)


def test_missing_cohort_config_blocks_new_env(monkeypatch):
    def fake_fetch(clean, sha, repo=None):
        assert clean == COHORT_FILE, f"unexpected fetch: {clean}"
        return None, m.BB_NOT_FOUND
    monkeypatch.setattr(m, "_bb_fetch_status", fake_fetch)
    monkeypatch.setattr(m, "_render_new_env_diff",
                        lambda info, sha: (_ for _ in ()).throw(
                            AssertionError("must not render a blocked env")))
    lines, structural, total = m._evaluate_new_envs([dict(CAND)], PR_SHA)
    assert structural == ["pv-myschroders-a"]
    assert total == 0
    joined = "\n".join(lines)
    assert COHORT_FILE in joined
    assert "does not exist" in joined
    assert "zero Applications" in joined


def test_cohort_present_renders_normally(monkeypatch):
    calls = {"render": 0}
    def fake_fetch(clean, sha, repo=None):
        # COPS-2552: _evaluate_new_envs also resolves prefix/customerName/
        # suffix (GSA-name-length guard) before rendering, which fetches
        # the ancestor chain in addition to the cohort file. Only the
        # cohort path needs real content here; anything else legitimately
        # 404s (mirrors ignoreMissingValueFiles) and the name resolves to
        # "unresolved" -- not this test's concern.
        if clean == COHORT_FILE:
            return "---\n", m.BB_OK
        return None, m.BB_NOT_FOUND
    def fake_render(info, sha):
        calls["render"] += 1
        return EXPECTED_GREEN_RENDER
    monkeypatch.setattr(m, "_bb_fetch_status", fake_fetch)
    monkeypatch.setattr(m, "_render_new_env_diff", fake_render)
    lines, structural, total = m._evaluate_new_envs([dict(CAND)], PR_SHA)
    assert structural == []
    assert calls["render"] == 1


def test_aec_path_skips_cohort_check(monkeypatch):
    aec_dir = "gcp/aec/private-cloud/gb1-b/pv-aec-x-a"
    cand = {"name": "pv-aec-x-a", "config_file": f"{aec_dir}/customer.yaml",
            "env_dir": aec_dir, "all_yaml_files": []}
    # COPS-2552 note: the immediate parent's config.yaml is BOTH what the
    # (skipped-for-aec) cohort check would have fetched AND a legitimate
    # ancestor level the GSA-name guard fetches for every candidate,
    # aec included (the live incident this ticket fixes was itself an aec
    # environment, so that guard must run there too). A specific-path
    # assertion can no longer distinguish "cohort check ran" from "GSA
    # check's ancestor walk ran"; what actually matters, and what this test
    # verifies, is the OUTCOME: an aec candidate is never blocked as
    # "cohort missing" and still renders, regardless of which paths any
    # guard along the way happens to touch.
    def fake_fetch(clean, sha, repo=None):
        return None, m.BB_NOT_FOUND
    monkeypatch.setattr(m, "_bb_fetch_status", fake_fetch)
    monkeypatch.setattr(m, "_render_new_env_diff",
                        lambda info, sha: EXPECTED_GREEN_RENDER)
    lines, structural, total = m._evaluate_new_envs([cand], PR_SHA)
    assert structural == []
    assert not any("cohort" in l.lower() and "does not exist" in l.lower()
                   for l in lines), "aec candidate must never get the cohort-missing block"


def test_transient_cohort_fetch_error_does_not_block(monkeypatch):
    def fake_fetch(clean, sha, repo=None):
        return None, m.BB_ERROR
    monkeypatch.setattr(m, "_bb_fetch_status", fake_fetch)
    monkeypatch.setattr(m, "_render_new_env_diff",
                        lambda info, sha: EXPECTED_GREEN_RENDER)
    lines, structural, total = m._evaluate_new_envs([dict(CAND)], PR_SHA)
    assert structural == []
