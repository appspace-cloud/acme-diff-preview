"""Regression tests for the three v2.4.7 improvements:

1. _gcp_access_token() is now lock-protected against concurrent refreshes.
2. post_build_status links to the PR itself instead of the ArgoCD server
   (the ArgoCD login page told a reviewer nothing about the actual diff).
3. An OCI-not-found error shows the exact missing chart:version prominently
   in the PR comment instead of a generic, unhelpful hint.
"""
import importlib
import os
import sys
import threading

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


def _import_module():
    os.environ.setdefault("BB_USER", "test")
    os.environ.setdefault("BB_TOKEN", "test")
    os.environ.setdefault("ARGOCD_PASS", "test")
    os.environ.setdefault("JFROG_WEBHOOK_SECRET", "testsecret")
    if SRC not in sys.path:
        sys.path.insert(0, SRC)
    mod = importlib.import_module("diff_preview")
    return importlib.reload(mod)


# ── 1. GCP token lock ────────────────────────────────────────────────────────
def test_gcp_token_lock_exists():
    mod = _import_module()
    assert hasattr(mod, "_gcp_token_lock")
    assert isinstance(mod._gcp_token_lock, type(threading.Lock()))


def test_gcp_token_concurrent_fetch_is_race_free(monkeypatch):
    mod = _import_module()
    mod._gcp_token = ""
    mod._gcp_token_exp = 0.0
    fetch_count = {"n": 0}
    fetch_lock = threading.Lock()

    def fake_http(method, url, **kw):
        with fetch_lock:
            fetch_count["n"] += 1
        return {"access_token": f"tok-{fetch_count['n']}", "expires_in": 3600}

    monkeypatch.setattr(mod, "http", fake_http)
    results = []
    def worker():
        results.append(mod._gcp_access_token())
    threads = [threading.Thread(target=worker) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert fetch_count["n"] == 1, (
        f"expected exactly 1 metadata-server fetch under concurrent access, got {fetch_count['n']}"
    )
    assert len(set(results)) == 1, "all 20 concurrent callers must see the same token"


# ── 2. post_build_status links to the PR, not ArgoCD ────────────────────────
def test_post_build_status_links_to_pr(monkeypatch):
    mod = _import_module()
    posted = {}
    monkeypatch.setattr(mod, "bb", lambda method, path, body=None, **kw: posted.update(body or {}))
    mod.post_build_status("a" * 40, "SUCCESSFUL", "No manifest changes", pr_id=555)
    assert posted["url"] == f"https://bitbucket.org/{mod.BB_WORKSPACE}/{mod.BB_REPO}/pull-requests/555"
    assert "argocd" not in posted["url"].lower()


def test_post_build_status_falls_back_without_pr_id(monkeypatch):
    mod = _import_module()
    posted = {}
    monkeypatch.setattr(mod, "bb", lambda method, path, body=None, **kw: posted.update(body or {}))
    mod.post_build_status("a" * 40, "SUCCESSFUL", "No manifest changes")
    assert posted["url"] == f"https://{mod.ARGOCD_SERVER}"


def test_all_process_pr_call_sites_pass_pr_id():
    """Sentinel: every post_build_status call inside process_pr's body must
    pass pr_id, or a future edit could silently regress back to the ArgoCD
    link for some code paths."""
    src_path = os.path.join(SRC, "diff_preview.py")
    src = open(src_path).read()
    start = src.index("def process_pr(")
    end = src.index("\ndef ", start + 10)
    body = src[start:end]
    calls = [l for l in body.splitlines() if "post_build_status(" in l and "def post_build_status" not in l]
    # Multi-line calls: check the call's full statement (a couple lines) has pr_id
    lines = body.splitlines()
    for i, l in enumerate(lines):
        if "post_build_status(" in l:
            window = "\n".join(lines[i:i+3])
            assert "pr_id=pr_id" in window, f"missing pr_id at process_pr line: {l.strip()}"


# ── 3. OCI-not-found shows the specific missing chart:version ──────────────
def test_oci_not_found_shows_specific_chart_prominently(monkeypatch):
    mod = _import_module()
    monkeypatch.setattr(mod, "generate_ai_summary", lambda *a, **k: None)
    detail = "Chart maps:1.110.0-ci.20260630007 not found in asia-east1-docker.pkg.dev/appspace-devops/artifact-engineering. Check that the version exists in the OCI registry."
    results = {"maps": mod.DiffResult("", [], 0, False, detail, mod.OUT_INDETERMINATE, mod.REASON_OCI_NOT_FOUND)}
    comment = mod.format_comment("a" * 40, results, base_sha="b" * 40)
    assert "maps:1.110.0-ci.20260630007" in comment, (
        "the specific missing chart:version must be visible in the comment, "
        "not hidden behind a generic hint"
    )
    # COPS-2676: top RENDER BLOCKED panel uses the uppercase label; the
    # specific chart:version still has to be in the body.
    assert ("CHART VERSION NOT FOUND" in comment
            or "**chart version not found in OCI registry**" in comment)


def test_other_indeterminate_reasons_unaffected(monkeypatch):
    """Only REASON_OCI_NOT_FOUND gets the prominent callout; other reasons
    keep the existing generic-hint format (no regression for those)."""
    mod = _import_module()
    monkeypatch.setattr(mod, "generate_ai_summary", lambda *a, **k: None)
    results = {"app-a": mod.DiffResult("", [], 0, False, "render blew up",
                                       mod.OUT_INDETERMINATE, mod.REASON_RENDER)}
    comment = mod.format_comment("a" * 40, results, base_sha="b" * 40)
    assert "diff unavailable" in comment
    assert "chart version not found" not in comment.lower()
