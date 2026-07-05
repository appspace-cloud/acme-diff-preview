"""Regression tests for the v2.5.1 post-merge live-verification fix.

Found during a live confirmation round against acme-config-dev after v2.5.0
was merged and deployed: the top "X app(s) updated - Y resource(s) changed"
summary line still used the display-truncated section count instead of the
real DiffResult.n_res, even though v2.4.9 FIX B already corrected the
per-app inline header for the same underlying reason.
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m


def _make_big_app(n_real):
    """A DiffResult with n_real changed resources, but sections truncated
    to AI_MAX_SECTIONS_PER_APP like the real diff engine produces."""
    sections = [(f"apps/Deployment d/svc{i}", f"@@\n+  key: v{i}\n")
                for i in range(min(n_real, m.AI_MAX_SECTIONS_PER_APP))]
    text = "\n".join(f"===== /Deployment d/svc{i} =====\n+  key: v{i}\n"
                      for i in range(min(n_real, m.AI_MAX_SECTIONS_PER_APP)))
    return m.DiffResult(text, sections, n_real, True, None, m.OUT_DIFF, None)


def test_ai_summary_head_uses_real_resource_count(monkeypatch):
    # Two apps, each really changed 103 resources but sections capped at 10.
    app_results = {
        "pv-dev-01-a-ms": _make_big_app(103),
        "pv-dev-01-a-glb": _make_big_app(5),
    }
    # Stub the token fetch and the HTTP call so the test never hits the
    # network — we only care about the deterministic `head` line prepended
    # before the AI text, not the model's own words.
    monkeypatch.setattr(m, "_gcp_access_token", lambda: "fake-token")

    def fake_http(method, url, headers=None, body=None):
        return {
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": "Some AI text."}]},
            }],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
        }
    monkeypatch.setattr(m, "http", fake_http)

    out = m.generate_ai_summary(app_results)
    assert out is not None
    first_line = out.splitlines()[0]
    # Real total is 103 + 5 = 108, NOT 10 + 5 = 15 (the truncated sum).
    assert "108 resource(s) changed" in first_line, first_line
    assert "2 app(s) updated" in first_line, first_line
