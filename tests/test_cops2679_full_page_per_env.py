"""COPS-2679: full-diff page must keep one block per environment.

On acme-config-prod PR #4316, 256 apps shared one byte-identical ES
nodeSelector change. The Bitbucket comment correctly collapsed them
(COPS-2579), but the FULL artifact did the same: one representative hunk,
`Identical diff across N (+N more)` roster truncated to 8 names, and
`#app-<name>` deep links from the overview table dead for every non-
representative. Operators opening "see the full diff view" could not
inspect all environments.

Shape and failure grouping already clear on is_complete_record
(COPS-2629). Fingerprint grouping must honour the same page contract.
COMMENT profile keeps collapse for size/scannability.
"""
import os
import sys

os.environ.setdefault("BB_USER", "test-user")
os.environ.setdefault("BB_TOKEN", "test-token")
os.environ.setdefault("ARGOCD_PASS", "test-pass")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m
import diff_ui


def _fleet(n_apps=256, n_resources=1):
    sections = [
        (f"/elasticsearch.k8s.elastic.co/Elasticsearch elasticsearch",
         "--- \n+++ \n@@ -1,3 +1,3 @@\n"
         "-                    - standard-high-8c-resource\n"
         "+                    - standard-super-resource\n"
         " context\n")
        for _ in range(n_resources)
    ]
    fp = m._fingerprint_sections(sections)
    text = "\n".join(f"===== {h} =====\n{b}" for h, b in sections)
    return {
        f"pv-env-{i:03d}-ss": m.DiffResult(
            text, sections, n_resources, True, None, m.OUT_DIFF, "changes",
            None, None, None, fp)
        for i in range(n_apps)
    }


def test_full_page_renders_one_block_per_identical_app(monkeypatch):
    """Red before fix: fingerprint collapse left one Identical-diff line."""
    monkeypatch.setattr(m, "generate_ai_summary", lambda *a, **k: None)
    body = m.format_comment(
        "a" * 40, _fleet(40), base_sha="b" * 40, readable_budget=0)

    assert "Identical diff across" not in body
    # One changed-header per app (⚠️ **`app`** — N resource(s) changed).
    for i in range(40):
        assert f"`pv-env-{i:03d}-ss`" in body
    assert body.count("resource(s) changed") == 40


def test_full_page_outline_anchors_every_changed_app(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda *a, **k: None)
    body = m.format_comment(
        "a" * 40, _fleet(25), base_sha="b" * 40, readable_budget=0)
    outline = diff_ui.build_outline(body)
    names = {a["name"] for a in outline}
    ids = {a["id"] for a in outline}
    for i in range(25):
        app = f"pv-env-{i:03d}-ss"
        assert app in names
        assert diff_ui.app_anchor(app) in ids
        assert any(a["resources"] for a in outline if a["name"] == app)


def test_comment_still_collapses_identical_fleet(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda *a, **k: None)
    body = m.format_comment(
        "a" * 40, _fleet(40), base_sha="b" * 40)  # COMMENT default
    assert "Identical diff across **40 environments**" in body
    assert body.count("resource(s) changed") == 1
