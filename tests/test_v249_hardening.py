"""Regression tests for the v2.4.9 hardening pass.

Each finding below was reproduced with a real PR against acme-config-dev in
the July 2026 test campaign (56 PRs across categories A-J). These tests lock
in the fixes so the behaviours never regress.

Findings covered:
- FIX A: a version rejected as unsafe/invalid must NOT look like "no bump";
         it must surface as a visible, blocking failure (not a green comment).
- FIX B: the per-app header and AI summary must report the REAL resource
         count (r.n_res), not the display-truncated section count, and must
         say "showing first N of M" when truncated.
- FIX C: a renamed value file must be seen by the affected-apps detector
         (both old and new paths), not reported as "no apps affected".
- FIX D: secret redaction must cover the two-line Kubernetes env-var form
         (`- name: <sensitive>` / `value: <secret>`), not only single-line
         `key: value`.
- FIX E: a new environment whose render fails for a STRUCTURAL reason
         (missing appspace.version) must not get the same green status as a
         healthy new env.
- FIX F: an invalid-YAML render failure should be reported with a specific
         hint, distinct from the generic "helm template failed".
- FIX G: the AI summary must not claim "No changes" when the outcome is
         actually indeterminate (diff could not be computed).
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


# ── FIX A: unsafe/invalid version must be distinguishable from "no bump" ─────
def test_reason_invalid_version_exists():
    mod = _import_module()
    assert hasattr(mod, "REASON_INVALID_VERSION"), (
        "a dedicated reason is needed so a rejected version is not silently "
        "treated as 'no version bump'"
    )
    assert mod.REASON_INVALID_VERSION in mod._REASON_HINTS
    assert mod.REASON_INVALID_VERSION in mod.PERMANENT_REASONS, (
        "an author-controlled unsafe version must block the PR, not retry"
    )


def test_extract_chart_version_signals_rejection():
    """_extract_chart_version_checked must tell the caller WHY it returned no
    version: 'none' (no appspace.version present) vs 'invalid' (present but
    rejected). The plain _extract_chart_version stays None-returning for
    backward-compatible callers."""
    mod = _import_module()
    # present but unsafe -> rejected
    cfg_bad = "appspace:\n  version: '../../../../tmp/pwned'\n"
    val, status = mod._extract_chart_version_checked(cfg_bad)
    assert val is None and status == "invalid"
    # genuinely absent -> none
    cfg_absent = "appspace:\n  customerName: qa-1\n"
    val, status = mod._extract_chart_version_checked(cfg_absent)
    assert val is None and status == "none"
    # valid -> ok
    cfg_ok = "appspace:\n  version: 2.4.9-dev\n"
    val, status = mod._extract_chart_version_checked(cfg_ok)
    assert val == "2.4.9-dev" and status == "ok"


def test_pr_chart_revision_reports_invalid(monkeypatch=None):
    """When a PR changes the version to an unsafe value, _pr_chart_revision
    must signal the rejection (not return None as if unchanged)."""
    mod = _import_module()
    mod._app_chart_revision_map = {"app-a": "2.4.6"}
    mod._bb_fetch_status = lambda path, sha: (
        "appspace:\n  version: '../../../../tmp/pwned'\n", mod.BB_OK)
    # New signature returns (new_rev, invalid_flag)
    rev, invalid = mod._pr_chart_revision_checked(
        "app-a", ["gcp/dev/a/customer.yaml"], "deadbeef")
    assert rev is None
    assert invalid is True


# ── FIX B: per-app header uses real count, with explicit truncation note ─────
def test_format_app_diff_block_reports_real_count():
    mod = _import_module()
    # 3 displayed sections but n_res says 42 changed in reality
    sections = [(f"/apps/Deployment svc{i}", f"--- \n+++ \n+change{i}")
                for i in range(3)]
    lines = mod._format_app_diff_block("pv-x-a-ms", sections, "", show_diff=True,
                                       n_res=42)
    header = lines[0]
    assert "42" in header, f"header must show real count 42, got: {header!r}"
    joined = "\n".join(lines)
    assert "showing" in joined.lower() and "42" in joined, (
        "must tell the reviewer only N of 42 resources are shown"
    )


def test_format_app_diff_block_no_note_when_not_truncated():
    mod = _import_module()
    sections = [("/apps/Deployment svc0", "--- \n+++ \n+x")]
    lines = mod._format_app_diff_block("pv-x-a-ms", sections, "", show_diff=True,
                                       n_res=1)
    joined = "\n".join(lines).lower()
    assert "1 resource(s) changed" in "\n".join(lines)
    assert "showing" not in joined, "no truncation note when nothing truncated"


# ── FIX C: renamed value file is detected (old + new path both kept) ─────────
def test_changed_files_keeps_both_rename_paths():
    mod = _import_module()
    pages = [{
        "values": [
            {"old": {"path": "gcp/dev/a/customer.yaml"},
             "new": {"path": "gcp/dev/a/customer2.yaml"}},
        ],
        "next": "",
    }]
    mod.bb = lambda method, path, **kw: pages[0]
    # v2.5.4 (Finding 6): now returns (files, renames) instead of just files.
    files, renames = mod.get_pr_changed_files(1234)
    assert "gcp/dev/a/customer.yaml" in files, "OLD path (in path_map) must be kept"
    assert "gcp/dev/a/customer2.yaml" in files, "NEW path must be kept"
    assert renames == {"gcp/dev/a/customer.yaml": "gcp/dev/a/customer2.yaml"}


# ── FIX D: two-line k8s env-var secret redaction ─────────────────────────────
def test_redact_two_line_env_var_secret():
    mod = _import_module()
    diff = (
        "         - name: appspace_googleRecaptcha_secretKey\n"
        "-          value: 6Le6HjQUAAAAAF_SsLoKC8R-3IQni8BD2cCJiFjo\n"
        "+          value: sk-live-REAL-SECRET-1234567890\n"
    )
    out = mod._redact_for_display("/apps/Deployment authentication", diff)
    assert "6Le6HjQUAAAAAF_SsLoKC8R" not in out, "old secret value leaked"
    assert "sk-live-REAL-SECRET-1234567890" not in out, "new secret value leaked"
    assert "[REDACTED]" in out
    # the key name line itself must stay visible for context
    assert "appspace_googleRecaptcha_secretKey" in out


def test_redact_two_line_env_var_keeps_innocuous_values():
    mod = _import_module()
    diff = (
        "         - name: appspace_cookieDomain\n"
        "-          value: dev-01.dev.appspace.com\n"
        "+          value: dev-02.dev.appspace.com\n"
    )
    out = mod._redact_for_display("/apps/Deployment authentication", diff)
    # cookieDomain is not sensitive -> values must remain visible
    assert "dev-01.dev.appspace.com" in out
    assert "dev-02.dev.appspace.com" in out


def test_redact_single_line_still_works():
    mod = _import_module()
    diff = "-  password: hunter2\n+  password: hunter3\n"
    out = mod._redact_for_display("/ConfigMap x", diff)
    assert "hunter2" not in out and "hunter3" not in out
    assert "[REDACTED]" in out


# ── FIX E: broken new-env (missing version) must not be green ───────────────
def test_new_env_status_helper_distinguishes_structural_failure():
    mod = _import_module()
    # helper returns (bitbucket_state, is_expected) for a new-env render error
    assert mod._new_env_status("no appspace.version found in config file") == (
        "FAILED", False)
    # the legitimately-expected post-deploy credential case stays green
    state, expected = mod._new_env_status(
        "helm template failed: Missing required value for legacy-db-credentials")
    assert state == "SUCCESSFUL" and expected is True


# ── FIX F: invalid YAML gets a specific hint ────────────────────────────────
def test_invalid_yaml_reason_hint_is_specific():
    mod = _import_module()
    assert hasattr(mod, "REASON_INVALID_YAML")
    hint = mod._REASON_HINTS[mod.REASON_INVALID_YAML]
    assert "yaml" in hint.lower()
    assert hint != mod._REASON_HINTS[mod.REASON_RENDER], (
        "invalid YAML must not reuse the generic render hint"
    )


# ── FIX G: AI summary must not say "No changes" for indeterminate outcomes ──
def test_ai_summary_no_false_no_changes_when_indeterminate():
    src = _source()
    # The AI-summary path must build its 'errors' set from indeterminate/error
    # outcomes and pass an explicit note; the fallback string for an all-error
    # changeset must not read as "no changes".
    start = src.index("def generate_ai_summary(")
    end = src.index("\ndef _result(", start)
    body = src[start:end]
    assert "not confirmed unchanged" in body.lower() or "could not be computed" in body.lower(), (
        "AI summary must carry an explicit indeterminate note so it never "
        "renders a misleading 'No changes' for apps that failed to diff"
    )
