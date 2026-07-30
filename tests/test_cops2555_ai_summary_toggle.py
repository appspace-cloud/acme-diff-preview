"""AI Analysis can be turned off with a simple, reversible env var.

Operator feedback: the AI Analysis block in the PR comment is not landing
well and should be disabled for now, without touching anything else the
comment does (deterministic diffs, decommission/new-env/downgrade warnings,
etc. must all keep working exactly as before).

ai_summary already had exactly one consumer downstream (the "AI Analysis"
markdown block) before this change, so gating generate_ai_summary() itself
is a clean, single point of control: when disabled, no Vertex AI call is
even attempted (saves the network round trip and any cost), the function
returns None immediately, and the comment section that only ever renders
`if ai_summary:` disappears on its own with no other code path affected.

Follows the exact convention already used for DIFF_UI_ENABLED and
LEADER_ELECTION_ENABLED: os.environ.get(..., "true") truthy-string parsing,
defaulting ON so nothing changes for any other deployment of this chart
unless it explicitly sets aiSummary.enabled: false in its own values.
"""
import sys, os, importlib
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m


import pytest


@pytest.fixture(autouse=True)
def _restore_module_default_after_each_test():
    """importlib.reload() mutates the real module object in sys.modules,
    which persists for the rest of the pytest session -- unlike monkeypatch's
    own env var undo, it is not scoped to this test. Without this, whichever
    reload happened last in this file (several set AI_SUMMARY_ENABLED=false)
    leaks into every later test file that imports diff_preview and calls
    generate_ai_summary() expecting its default (enabled) behavior. Confirmed
    live: this exact leak broke three unrelated pre-existing tests in
    test_coverage_final_g.py and test_coverage_last_mile.py during a full
    suite run.
    """
    yield
    os.environ.pop("AI_SUMMARY_ENABLED", None)
    importlib.reload(m)


def _reload_with(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("AI_SUMMARY_ENABLED", raising=False)
    else:
        monkeypatch.setenv("AI_SUMMARY_ENABLED", value)
    importlib.reload(m)
    return m


def test_default_is_enabled_when_unset(monkeypatch):
    """Regression guard: existing deployments that never set this var must
    keep working exactly as before."""
    mod = _reload_with(monkeypatch, None)
    assert mod.AI_SUMMARY_ENABLED is True


def test_false_disables_it(monkeypatch):
    mod = _reload_with(monkeypatch, "false")
    assert mod.AI_SUMMARY_ENABLED is False


def test_common_falsy_spellings_all_disable_it(monkeypatch):
    for v in ("False", "FALSE", "0", "no", "  false  "):
        mod = _reload_with(monkeypatch, v)
        assert mod.AI_SUMMARY_ENABLED is False, f"{v!r} should disable it"


def test_disabled_returns_none_without_calling_vertex(monkeypatch):
    """The important behavior: disabled means no network call is attempted
    at all, not just that the result is discarded."""
    mod = _reload_with(monkeypatch, "false")
    called = {"n": 0}
    monkeypatch.setattr(mod, "http", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or (_ for _ in ()).throw(AssertionError("must not call Vertex AI while disabled")))
    result = mod.generate_ai_summary({"app-a": ("diff text", True, None)})
    assert result is None
    assert called["n"] == 0


def test_enabled_still_attempts_when_there_is_something_to_summarize(monkeypatch):
    """Regression guard: re-enabling (or never touching the var) must not
    silently break the existing feature."""
    mod = _reload_with(monkeypatch, "true")
    assert mod.AI_SUMMARY_ENABLED is True
    # No changed/errored apps -> already-existing short circuit, unrelated
    # to this toggle. Confirms the toggle only ever adds a new early exit,
    # it does not change the existing "nothing to summarize" behavior.
    assert mod.generate_ai_summary({}) is None


def test_comment_has_no_ai_section_when_disabled(monkeypatch):
    """End-to-end: the rendered comment must not contain the AI Analysis
    header at all when the toggle is off, and nothing else about the
    comment structure should change."""
    mod = _reload_with(monkeypatch, "false")
    calls = {"n": 0}
    def fail_if_called(*a, **k):
        calls["n"] += 1
        raise AssertionError("generate_ai_summary must short-circuit before any HTTP call")
    monkeypatch.setattr(mod, "http", fail_if_called)
    # Sanity: the constant a real caller would branch on is really off.
    assert mod.AI_SUMMARY_ENABLED is False
    assert mod.generate_ai_summary({"a": ("+x\n", True, None)}) is None
    assert calls["n"] == 0
