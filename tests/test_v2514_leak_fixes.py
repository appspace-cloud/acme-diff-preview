"""Regression tests for v2.5.14 -- three leak fixes found during a hardening pass.

1. _ensure_chart leaked one mkdtemp() dir on every exhausted-retry pull failure.
2. _redact_secret_section leaked block-scalar Secret values (only the opener
   line was masked, the indented content lines fell through verbatim).
3. generate_ai_summary sent Secret values to Vertex AI unredacted because it
   used key-name based _redact_sensitive instead of kind-aware _redact_for_display.

Each test asserts the CORRECT behavior, so it fails against the pre-fix code.
"""
import os
import sys

os.environ.setdefault("BB_USER", "test-user")
os.environ.setdefault("BB_TOKEN", "test-token")
os.environ.setdefault("ARGOCD_PASS", "test-pass")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m


# ── Fix 1: tmp_dir leak on exhausted-retry pull failure ──────────────────

def test_ensure_chart_cleans_tmp_dir_on_pull_failure(tmp_path, monkeypatch):
    """A pull that fails all retries (transient, non-OciChartNotFound) must
    leave no orphan mkdtemp() directory behind in HELM_CACHE_DIR."""
    cache_dir = tmp_path / "helm-cache"
    cache_dir.mkdir()
    monkeypatch.setattr(m, "HELM_CACHE_DIR", str(cache_dir))
    monkeypatch.setattr(m, "OCI_PASS", "x")
    monkeypatch.setattr(m, "HELM_BIN", "helm")
    monkeypatch.setattr(m, "_helm_login", lambda registry: True)

    class _FailPull:
        returncode = 1
        stderr = "Error: connection reset by peer"
        stdout = ""

    monkeypatch.setattr(m.subprocess, "run", lambda cmd, *a, **k: _FailPull())
    monkeypatch.setattr(m.time, "sleep", lambda *_: None)  # no real backoff

    result = m._ensure_chart("reg.example.com", "appspace-micro-services", "1.2.3")
    assert result is None, "an exhausted transient pull must return None"

    orphan_tmp = [p.name for p in cache_dir.iterdir()
                  if p.name.startswith("appspace-micro-services-1.2.3-")]
    assert orphan_tmp == [], f"leaked temp dir(s) after failed pull: {orphan_tmp}"


def test_ensure_chart_cleans_tmp_dir_on_oci_not_found(tmp_path, monkeypatch):
    """A permanent OciChartNotFound must also leave no orphan temp dir."""
    cache_dir = tmp_path / "helm-cache"
    cache_dir.mkdir()
    monkeypatch.setattr(m, "HELM_CACHE_DIR", str(cache_dir))
    monkeypatch.setattr(m, "OCI_PASS", "x")
    monkeypatch.setattr(m, "HELM_BIN", "helm")
    monkeypatch.setattr(m, "_helm_login", lambda registry: True)

    class _NotFound:
        returncode = 1
        stderr = "Error: chart not found: unexpected status code: 404"
        stdout = ""

    monkeypatch.setattr(m.subprocess, "run", lambda cmd, *a, **k: _NotFound())

    try:
        m._ensure_chart("reg.example.com", "appspace-micro-services", "9.9.9")
    except m.OciChartNotFound:
        pass
    leftovers = [p.name for p in cache_dir.iterdir()
                 if p.name.startswith("appspace-micro-services-9.9.9-")]
    assert leftovers == [], f"leaked temp dir(s) after 404: {leftovers}"


# ── Fix 2: block-scalar Secret values leaked in the PR comment ───────────

def test_redact_secret_section_masks_block_scalar_body():
    """A Secret data value written as a block scalar (`tls.crt: |-`) must have
    its indented continuation lines masked, not just the opener line."""
    section = (
        " apiVersion: v1\n"
        " kind: Secret\n"
        " data:\n"
        "   tls.crt: |-\n"
        "     LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0t\n"
        "     bBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB\n"
        "   password: hunter2\n"
    )
    out = m._redact_secret_section(section)
    assert "LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0t" not in out, (
        "block-scalar cert body leaked into the redacted output")
    assert "bBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB" not in out
    # The single-line value on a later, dedented key must still be masked.
    assert "hunter2" not in out
    # The key names stay so a reviewer sees WHICH entries changed.
    assert "tls.crt:" in out
    assert "password:" in out


def test_redact_secret_section_block_scalar_in_diff_markers():
    """Same, but inside a unified-diff (+/-) context, as it appears in a real
    posted comment. The diff marker must be preserved, the body masked."""
    section = (
        "+  tls.key: |\n"
        "+    c2VjcmV0LWtleS1tYXRlcmlhbC1oZXJl\n"
        "+    bW9yZS1zZWNyZXQtYnl0ZXMtbGVha2luZw==\n"
        " kind: Secret\n"
    )
    out = m._redact_secret_section(section)
    assert "c2VjcmV0LWtleS1tYXRlcmlhbC1oZXJl" not in out
    assert "bW9yZS1zZWNyZXQtYnl0ZXMtbGVha2luZw==" not in out


def test_redact_secret_section_plain_values_still_masked():
    """Regression guard: the original single-line masking must be unchanged."""
    section = (
        " kind: Secret\n"
        " data:\n"
        "   password: c3VwZXItc2VjcmV0\n"
        "   type: Opaque\n"
    )
    out = m._redact_secret_section(section)
    assert "c3VwZXItc2VjcmV0" not in out
    # Opaque is a type marker, not a secret value -- must NOT be masked.
    assert "Opaque" in out


# ── Fix 3: Secret values leaked to Vertex AI in the prompt ───────────────

def test_ai_summary_redacts_secret_section_before_sending(monkeypatch):
    """The prompt built for Vertex AI must not contain raw Secret data, even
    when the key name looks unremarkable (tls.crt, ca.bundle) so the old
    key-name based _redact_sensitive would miss it."""
    secret_body = (
        "+  ca.bundle: c2VjcmV0LWNhLWJ1bmRsZS1jb250ZW50\n"
        "+  tls.crt: bGVha2luZy10bHMtY2VydC1oZXJl\n"
    )
    header = "/Secret prod/my-tls-secret"
    result = m.DiffResult(
        secret_body, [(header, secret_body)], 1, True, None, m.OUT_DIFF, "changes")
    app_results = {"pv-qa-15-a-ms": result}

    captured = {}

    def fake_http(method, url, headers=None, body=None, **kw):
        captured["prompt"] = body["contents"][0]["parts"][0]["text"]
        return {
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": "ok"}]},
            }],
            "usageMetadata": {},
        }

    monkeypatch.setattr(m, "_gcp_access_token", lambda: "fake-token")
    monkeypatch.setattr(m, "http", fake_http)

    m.generate_ai_summary(app_results)

    prompt = captured.get("prompt", "")
    assert prompt, "AI summary did not build a prompt"
    assert "c2VjcmV0LWNhLWJ1bmRsZS1jb250ZW50" not in prompt, (
        "ca.bundle secret value leaked into the Vertex AI prompt")
    assert "bGVha2luZy10bHMtY2VydC1oZXJl" not in prompt, (
        "tls.crt secret value leaked into the Vertex AI prompt")


def test_ai_summary_non_secret_diff_still_included(monkeypatch):
    """Regression guard: a normal (non-Secret) diff must still reach the
    prompt so the summary is not gutted by the kind-aware redaction switch."""
    body = (
        "@@\n"
        "-        image: acme/library:1.0.0\n"
        "+        image: acme/library:1.1.0\n"
    )
    header = "/Deployment prod/library"
    result = m.DiffResult(
        body, [(header, body)], 1, True, None, m.OUT_DIFF, "changes")
    app_results = {"pv-qa-15-a-ms": result}

    captured = {}

    def fake_http(method, url, headers=None, body=None, **kw):
        captured["prompt"] = body["contents"][0]["parts"][0]["text"]
        return {
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": "ok"}]},
            }],
            "usageMetadata": {},
        }

    monkeypatch.setattr(m, "_gcp_access_token", lambda: "fake-token")
    monkeypatch.setattr(m, "http", fake_http)

    m.generate_ai_summary(app_results)
    prompt = captured.get("prompt", "")
    assert "acme/library:1.1.0" in prompt, (
        "a normal image-bump diff must still be sent to the AI summary")
