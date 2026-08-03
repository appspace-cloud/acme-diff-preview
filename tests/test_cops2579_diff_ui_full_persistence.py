"""COPS-2579 item 4: the /diff web UI page must show the COMPLETE diff for
every distinct change in a large PR, not the same per-app-capped text as
the Bitbucket comment.

Before this ticket, _save_diff_ui_artifact persisted the exact `body`
string format_comment produced -- and that body was itself capped to 10
sections per app and 6 apps inline, so the "full output" page was byte for
byte identical to the truncated comment (measured on acme-config-prod PR
#3837: 60 of 16616 real diff sections visible, in BOTH places).

Once format_comment stores full (memory-bounded) sections per app and
groups identical diffs instead of picking an arbitrary top-N, the SAME
persistence mechanism automatically carries the complete content -- this
test proves that end to end: build a large-PR-shaped body, persist it
through the real diff_ui module, load it back, and confirm every group's
full diff and every affected app name is present in the stored artifact.
"""
import os
import sys
import tempfile

os.environ.setdefault("BB_USER", "test-user")
os.environ.setdefault("BB_TOKEN", "test-token")
os.environ.setdefault("ARGOCD_PASS", "test-pass")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m
import diff_ui


def _make_shared_change_body(n_apps=200, n_resources=67):
    """Simulate the acme-config-prod PR #3837 shape: n_apps environments,
    all sharing the exact same n_resources-resource change."""
    sections = [
        (f"/apps/Deployment svc-{i:03d}",
         f"--- \n+++ \n@@ -1,3 +1,1 @@\n-nodeSelector: spot\n context\n")
        for i in range(n_resources)
    ]
    fp = m._fingerprint_sections(sections)
    text = "\n".join(f"===== {h} =====\n{b}" for h, b in sections)
    results = {
        f"pv-env-{i:03d}-ms": m.DiffResult(
            text, sections, n_resources, True, None, m.OUT_DIFF, "changes",
            None, None, None, fp)
        for i in range(n_apps)
    }
    return m.format_comment("a" * 40, results, base_sha="b" * 40)


def test_diff_ui_artifact_contains_the_full_diff_for_every_app(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda *a, **k: None)
    body = _make_shared_change_body(n_apps=200, n_resources=67)

    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(m, "DIFF_UI_DIR", tmp)
        m._save_diff_ui_artifact("acme-config-prod", 3837, "c" * 40, body,
                                 base_sha="b" * 40,
                                 outcome_counts={"diff": 200}, app_count=200)
        artifact = diff_ui.load_artifact(tmp, "acme-config-prod", 3837, "c" * 40)
        assert artifact is not None, "artifact was not persisted where expected"
        stored_body = artifact["body"]

    # Every one of the 200 apps must be named somewhere in the stored body
    # (the overview table lists every app individually even when grouped).
    missing_apps = [f"pv-env-{i:03d}-ms" for i in range(200)
                    if f"pv-env-{i:03d}-ms" not in stored_body]
    assert not missing_apps, f"apps missing from persisted artifact: {missing_apps[:5]}..."

    # The full 67-resource diff must be present, not a 10-section subset.
    resource_headers = [f"/apps/Deployment svc-{i:03d}" for i in range(67)]
    missing_resources = [h for h in resource_headers if h not in stored_body]
    assert not missing_resources, (
        f"resources missing from persisted artifact: {missing_resources[:5]}... "
        f"({len(missing_resources)} of 67 missing)")

    # And it must say so as ONE group, not 200 individual diff dumps.
    assert "Identical diff across **200 environments**" in stored_body
