"""A DELETED identity file is not a new environment.

Observed live on acme-config-prod PR #4733 (2026-10-05): the PR removes the
whole `aws/` tree, five files, all deletions. The comment came back red with

    New environment ... Files added:
      aws/prod/private-cloud/na1-a/pv-amzn-a/customer.yaml
    Blocked: a required cohort `config.yaml` is missing.

telling the author to re-add the exact file the PR deletes on purpose.

Three things line up to produce it:

1. `get_pr_changed_files` flattens both sides of the Bitbucket diffstat into
   one list of paths and drops the added/removed/modified status, so
   `_detect_new_env_candidates` cannot tell a deletion from an addition.
2. `_detect_env_decommission_candidates`, which owns removals, bails on
   `if not apps: continue`. It only sees environments that have live ArgoCD
   Applications. `aws/` is deployed by the legacy pipeline and has none, so
   the removal was owned by nobody and fell through to the new-env path.
3. The v2.13.2 identity filter could have caught it, but it returns early
   for anything that is not a `config.yaml`, so a deleted `customer.yaml`
   walked straight past it.

The discriminator is existence at the PR head: whatever its basename, a file
that is genuinely absent there cannot be a new environment. BB_NOT_FOUND is
a stable, cacheable 404 so it drops the candidate; BB_ERROR is transient and
must keep it, the same conservative rule the identity filter already uses.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m


PR_SHA = "dc6f27c7"

# The exact changed-file list of acme-config-prod PR #4733, both sides of the
# diffstat flattened the way get_pr_changed_files produces it.
PR4733_FILES = [
    "README.md",
    "aws/config.yaml",
    "aws/prod/config.yaml",
    "aws/prod/private-cloud/config.yaml",
    "aws/prod/private-cloud/na1-a/config.yaml",
    "aws/prod/private-cloud/na1-a/pv-amzn-a/customer.yaml",
    "docs/contributing.md",
    "docs/repository-layout.md",
]

NEW_ENV_FILE = "gcp/prod/private-cloud/na1-b/monthly/pv-newcust-a/customer.yaml"
NEW_ENV_CONTENT = (
    "---\n"
    "appspace:\n"
    "  version: 2603.1.38\n"
    "  customerName: newcust\n"
    "  suffix: a\n"
)


def _fetch(monkeypatch, table, default=(None, "not_found")):
    """Mock the cached fetch. `table` maps path -> (content, status)."""
    def fake(filepath, sha, repo=None):
        assert sha == PR_SHA
        return table.get(filepath, (default[0], getattr(m, "BB_NOT_FOUND")))
    monkeypatch.setattr(m, "_bb_fetch_cached", fake)


def test_pr4733_full_tree_deletion_yields_no_new_env(monkeypatch):
    """The live shape. Every aws file is gone at the PR head and the tree has
    no ArgoCD app, so nothing here is a new environment."""
    _fetch(monkeypatch, {})  # everything 404s: all five files are deleted
    got = m._detect_new_env_candidates(PR4733_FILES, {}, {}, pr_sha=PR_SHA)
    assert got == [], (
        "a deleted environment was reported as new: "
        f"{[c['name'] for c in got]}")


def test_deleted_customer_yaml_alone_is_not_new(monkeypatch):
    """Narrowest form of the bug: one deleted customer.yaml, no ArgoCD app.

    This is the case the v2.13.2 identity filter skipped, because it returns
    early for every basename that is not config.yaml.
    """
    deleted = "aws/prod/private-cloud/na1-a/pv-amzn-a/customer.yaml"
    _fetch(monkeypatch, {})
    assert m._detect_new_env_candidates([deleted], {}, {}, pr_sha=PR_SHA) == []


def test_genuinely_new_environment_is_still_detected(monkeypatch):
    """Guard the fix: a customer.yaml that EXISTS at the PR head is still a
    new environment. Without this the fix would silence real new envs."""
    _fetch(monkeypatch, {NEW_ENV_FILE: (NEW_ENV_CONTENT, m.BB_OK)})
    got = m._detect_new_env_candidates([NEW_ENV_FILE], {}, {}, pr_sha=PR_SHA)
    assert [c["name"] for c in got] == ["pv-newcust-a"]
    assert got[0]["config_file"] == NEW_ENV_FILE


def test_transient_fetch_error_raises_instead_of_deciding(monkeypatch):
    """BB_ERROR is not evidence of absence, so it must never be read as "this
    environment was deleted". The COPS-2668 contract applies here exactly as
    it does to the identity filter: a failed read raises and the PR is retried,
    it does not quietly produce an answer.
    """
    import pytest
    _fetch(monkeypatch, {NEW_ENV_FILE: (None, m.BB_ERROR)})
    with pytest.raises(m.ValueFileUnreadable):
        m._detect_new_env_candidates([NEW_ENV_FILE], {}, {}, pr_sha=PR_SHA)


def test_fetch_exception_keeps_the_candidate(monkeypatch):
    """Same conservative rule when the fetch raises instead of returning."""
    def boom(filepath, sha, repo=None):
        raise RuntimeError("bitbucket unavailable")
    monkeypatch.setattr(m, "_bb_fetch_cached", boom)
    got = m._detect_new_env_candidates([NEW_ENV_FILE], {}, {}, pr_sha=PR_SHA)
    assert [c["name"] for c in got] == ["pv-newcust-a"]


def test_no_pr_sha_keeps_pre_existing_behavior(monkeypatch):
    """Without pr_sha there is nothing to check existence against, so the
    detector must behave exactly as it did before."""
    got = m._detect_new_env_candidates([NEW_ENV_FILE], {}, {})
    assert [c["name"] for c in got] == ["pv-newcust-a"]


def test_existing_app_still_wins_over_deletion_check(monkeypatch):
    """An identity file already mapped to a live ArgoCD app was never a
    new-env candidate and still is not, without needing a fetch."""
    def never(filepath, sha, repo=None):
        raise AssertionError("path_map hit must short-circuit before fetching")
    monkeypatch.setattr(m, "_bb_fetch_cached", never)
    path_map = {NEW_ENV_FILE: ["argocd/pv-newcust-a-ms"]}
    assert m._detect_new_env_candidates(
        [NEW_ENV_FILE], path_map, {}, pr_sha=PR_SHA) == []
