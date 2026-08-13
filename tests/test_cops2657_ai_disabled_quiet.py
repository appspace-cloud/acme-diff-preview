"""COPS-2657: a switched-off feature must not report itself as broken.

Measured on the live leader at 2.65.0, one pod lifetime:

    152x DEBUG   [AI] AI_SUMMARY_ENABLED=false - skipping AI call
    152x WARNING [comment] AI summary absent despite changed apps:
                 the Vertex call failed or returned nothing
      0x         AI summary included

The pod env confirms AI_SUMMARY_ENABLED=false. So the service says it is
deliberately skipping the call, and then warns that the call it never
made failed.

The branch it lands in was written by COPS-2617 for a real reason: before
that, a Vertex outage looked exactly like a quiet day, because "nothing
to summarise" and "the call failed" shared one message. That distinction
is correct and must survive. It simply had no third case for the feature
being switched OFF.

Why it is worth a release rather than a shrug: this is the ONLY recurring
WARNING the service emits. Everything else is INFO or DEBUG. So the
entire warning channel - the one COPS-2652 just made queryable by
severity - is one false alarm repeated on every PR, and the next real
warning arrives somewhere nobody trusts.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_preview as dp  # noqa: E402

URL = "https://argocd.appspace.com/diff/acme-config-dev/1/abc12345"


def _changed():
    hdrs = ["/apps/Deployment d0", "/apps/Deployment d1"]
    return {"pv-x-a-ms": dp.DiffResult(
        "\n".join("--- %s" % h for h in hdrs),
        [(h, "  image: acme/ms:1") for h in hdrs],
        2, True, None, dp.OUT_DIFF, None)}


def _unchanged():
    return {"pv-x-a-ms": dp.DiffResult("", [], 0, False, None,
                                       dp.OUT_NO_DIFF, None)}


def _logs(monkeypatch, results, enabled, summary):
    """Render a comment and collect (severity, message) pairs."""
    seen = []
    real = dp.log

    def spy(msg, sev="INFO", **kw):
        seen.append((sev, msg))
        return None

    monkeypatch.setattr(dp, "log", spy)
    monkeypatch.setattr(dp, "AI_SUMMARY_ENABLED", enabled)
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: summary)
    dp.format_comment("f" * 40, results, base_sha="b" * 40, artifact_url=URL)
    monkeypatch.setattr(dp, "log", real)
    return seen


def _warnings(seen):
    return [m for sev, m in seen if sev in ("WARNING", "ERROR")]


# -- the defect -------------------------------------------------------------

def test_a_disabled_feature_emits_no_warning(monkeypatch):
    """THE gate. Reproduces the live shape: feature off, apps changed."""
    seen = _logs(monkeypatch, _changed(), enabled=False, summary=None)
    assert not _warnings(seen), (
        "a switched-off feature reported itself as broken: "
        + "; ".join(_warnings(seen)))


def test_the_disabled_case_is_still_recorded_somewhere(monkeypatch):
    """Silence is not the goal - honesty at the right level is. A reader
    should still be able to tell the summary is absent on purpose."""
    seen = _logs(monkeypatch, _changed(), enabled=False, summary=None)
    txt = " ".join(m for _s, m in seen).lower()
    assert "ai summary" in txt, "the absence is not recorded at all"


# -- what must NOT change ---------------------------------------------------

def test_a_real_vertex_failure_still_warns(monkeypatch):
    """COPS-2617's whole point. Feature ON, changes present, no summary
    means the call failed, and that must stay loud."""
    seen = _logs(monkeypatch, _changed(), enabled=True, summary=None)
    warns = _warnings(seen)
    assert warns, "a genuine Vertex failure stopped warning"
    assert any("vertex" in w.lower() for w in warns), warns


def test_nothing_to_summarise_stays_quiet(monkeypatch):
    """No changed apps means there was nothing to ask for. Unchanged."""
    seen = _logs(monkeypatch, _unchanged(), enabled=True, summary=None)
    assert not _warnings(seen)


def test_a_successful_summary_is_unchanged(monkeypatch):
    seen = _logs(monkeypatch, _changed(), enabled=True, summary="all routine")
    assert not _warnings(seen)
    assert any("included" in m for _s, m in seen)


def test_a_disabled_feature_with_no_changes_is_also_quiet(monkeypatch):
    seen = _logs(monkeypatch, _unchanged(), enabled=False, summary=None)
    assert not _warnings(seen)


# -- the comment body itself must not move ----------------------------------

def test_the_rendered_comment_is_identical_either_way(monkeypatch):
    """This ticket is about a log line. If the comment body changes, the
    fix reached further than it should have."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    monkeypatch.setattr(dp, "AI_SUMMARY_ENABLED", False)
    off = dp.format_comment("f" * 40, _changed(), base_sha="b" * 40,
                            artifact_url=URL)
    monkeypatch.setattr(dp, "AI_SUMMARY_ENABLED", True)
    on = dp.format_comment("f" * 40, _changed(), base_sha="b" * 40,
                           artifact_url=URL)
    assert off == on, "the comment body changed with the feature flag"
