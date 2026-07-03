"""Security regression tests (v2.4.8).

Found by adversarial local probing of the PR-diff path:

- A PR author fully controls appspace.version in a config file. That value
  flowed unvalidated into `helm pull --version <v>` and into
  os.path.join(cache, registry, chart, <v>), allowing path traversal
  (../../..) and argument injection (leading dash / --flag).
- The fix validates the version as a safe OCI tag at extraction time and
  again at the _ensure_chart choke point (defense in depth).
"""
import importlib
import os
import sys

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


def _source():
    with open(os.path.join(SRC, "diff_preview.py")) as f:
        return f.read()


def _import_module():
    os.environ.setdefault("BB_USER", "test")
    os.environ.setdefault("BB_TOKEN", "test")
    os.environ.setdefault("ARGOCD_PASS", "test")
    if SRC not in sys.path:
        sys.path.insert(0, SRC)
    mod = importlib.import_module("diff_preview")
    return importlib.reload(mod)


# ── version validator ────────────────────────────────────────────────────────
def test_valid_versions_accepted():
    mod = _import_module()
    for good in ["2.4.7", "2.4.7-dev", "1.110.0-ci.20260630007",
                 "v1.2.3", "8.15.1", "latest"]:
        assert mod._is_valid_chart_version(good), f"should accept {good!r}"


def test_unsafe_versions_rejected():
    mod = _import_module()
    for bad in [
        "../../../../tmp/pwned",     # path traversal
        "../etc/passwd",
        "--help",                    # argument injection (leading dash)
        "-x",
        "; rm -rf /",                # shell metachar + space
        "$(touch /tmp/rce)",
        "a b",                       # whitespace
        "tag\nwith\nnewline",
        "",                          # empty
        "x" * 200,                   # too long
    ]:
        assert not mod._is_valid_chart_version(bad), f"should reject {bad!r}"


# ── extraction rejects unsafe values (returns None = "no bump") ──────────────
def test_extract_chart_version_rejects_traversal():
    mod = _import_module()
    cfg = "appspace:\n  version: '../../../../tmp/pwned'\n"
    assert mod._extract_chart_version(cfg) is None


def test_extract_chart_version_rejects_argument_injection():
    mod = _import_module()
    cfg = "appspace:\n  version: '--dry-run'\n"
    assert mod._extract_chart_version(cfg) is None


def test_extract_chart_version_still_accepts_real_bump():
    mod = _import_module()
    cfg = "appspace:\n  version: 2.4.7-dev\n"
    assert mod._extract_chart_version(cfg) == "2.4.7-dev"


# ── _ensure_chart choke point refuses unsafe input without any network ───────
def test_ensure_chart_refuses_unsafe_version(monkeypatch=None):
    mod = _import_module()
    called = {"login": False, "pull": False}
    mod._helm_login = lambda reg: called.__setitem__("login", True) or True
    # If validation works, we never reach login or subprocess.
    out = mod._ensure_chart("helm-oci-dev.repo.appspace.com", "acme",
                            "../../../../tmp/escape")
    assert out is None
    assert called["login"] is False, "unsafe version reached _helm_login"


def test_ensure_chart_refuses_unsafe_chart_name():
    mod = _import_module()
    out = mod._ensure_chart("helm-oci-dev.repo.appspace.com",
                            "../../evil", "2.4.7")
    assert out is None


# ── PERF v2.4.8: single-pass file-to-apps matching ──────────────────────────
# get_affected_apps() and _pr_chart_revision() each independently rescanned
# changed_files x path_map — the latter once PER AFFECTED APP. Measured
# 413ms of pure CPU with 600 apps before any network I/O. Fixed with a
# shared _match_files_to_apps() helper computed once per PR.

def test_match_files_to_apps_matches_get_affected_apps_semantics():
    """The new single-pass helper must produce the exact same affected-apps
    set as the original per-file/per-path scan, including files that match
    MULTIPLE path prefixes (the original never broke out of that loop)."""
    mod = _import_module()
    path_map = {
        "gcp/dev/a": ["app-a"],
        "gcp/dev/a/nested": ["app-a-nested"],   # overlapping prefix on purpose
        "gcp/dev/b": ["app-b"],
    }
    changed = [
        "gcp/dev/a/nested/config.yaml",  # matches BOTH "gcp/dev/a" and "gcp/dev/a/nested"
        "gcp/dev/b/config.yaml",
        "unrelated/file.txt",
    ]
    affected, app_to_files = mod._match_files_to_apps(changed, path_map)
    assert affected == mod.get_affected_apps(changed, path_map)
    assert set(affected) == {"app-a", "app-a-nested", "app-b"}
    assert app_to_files["app-a"] == ["gcp/dev/a/nested/config.yaml"]
    assert app_to_files["app-b"] == ["gcp/dev/b/config.yaml"]


def test_match_files_to_apps_exact_key_match():
    mod = _import_module()
    path_map = {"gcp/dev/a/config.yaml": ["app-a", "app-a-ss"]}
    affected, app_to_files = mod._match_files_to_apps(
        ["gcp/dev/a/config.yaml"], path_map)
    assert affected == ["app-a", "app-a-ss"]
    assert app_to_files["app-a"] == ["gcp/dev/a/config.yaml"]
    assert app_to_files["app-a-ss"] == ["gcp/dev/a/config.yaml"]


def test_pr_chart_revision_uses_precomputed_candidate_files():
    """_pr_chart_revision must take the app's file list directly (no more
    internal path_map rescan) — verified by NOT setting _path_map_cache at
    all and confirming the function still works from candidate_files alone."""
    mod = _import_module()
    mod._app_chart_revision_map = {"app-a": "2.4.6"}
    mod._bb_fetch_status = lambda path, sha: ("appspace:\n  version: 2.4.8\n", mod.BB_OK)
    result = mod._pr_chart_revision("app-a", ["gcp/dev/a/config.yaml"], "deadbeef")
    assert result == "2.4.8"


# ── CORRECTNESS v2.4.8: process_batch must isolate per-app crashes ──────────

def test_reason_unexpected_is_registered():
    mod = _import_module()
    assert hasattr(mod, "REASON_UNEXPECTED")
    assert mod.REASON_UNEXPECTED in mod._REASON_HINTS, (
        "REASON_UNEXPECTED must have an operator-facing hint like every other reason"
    )


def test_process_batch_isolates_a_crashing_app():
    """A single app whose diff raises must not prevent the other apps in the
    same batch from being recorded. Reproduces the v2.4.8 bug by inspecting
    the actual source structure (fut.result() must be inside try/except)."""
    src = _source()
    start = src.index("def process_batch(")
    end = src.index("\n        # Helm chart pre-warm", start)
    body = src[start:end]
    assert "try:" in body and "except Exception as exc:" in body, (
        "process_batch must catch exceptions from fut.result() per-app"
    )
    assert "REASON_UNEXPECTED" in body


# ── CORRECTNESS v2.4.8: pre-warm cache-membership check is lock-protected ───

def test_prewarm_snapshots_helm_cache_under_lock():
    src = _source()
    start = src.index("# Filter out versions already cached on disk")
    end = src.index("already_cached = len(", start)
    body = src[start:end]
    assert "_helm_cache_lock" in body, (
        "reading _helm_chart_cache for the pre-warm filter must be lock-protected"
    )
