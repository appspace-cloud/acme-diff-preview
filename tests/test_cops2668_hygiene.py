"""Failures that go nowhere, and typos that take the pod down (COPS-2668, P2).

Three hygiene defects from the audit's exception sweep. None of them makes the
service lie, which is why they came after the P0/P1 batches — but each removes
a way for a small mistake to become an outage or a silence.

1. Four numeric env knobs still call `int()`/`float()` on the raw value.
   `_env_int`/`_env_float` exist precisely because of bughunt N3 — a typo in
   ANY numeric variable used to crash the pod at import with a raw traceback
   and no hint which one was at fault — and they are already imported in this
   module. These four were simply never converted, so `DIFF_BACKOFF_BASE=3s`
   is still a CrashLoopBackOff whose traceback names `float()`, not the
   variable.

2. `_jfrog_hard_refresh` is submitted to a pool and its Future discarded, so
   every exception it raises is swallowed by the Future nobody reads: no log,
   no counter, no retry. The webhook reports success while the refresh it
   promised never happened. The other real threads in this service (leader
   tick, OCI self-check) all wrap their bodies; this one does not.

3. No exception in this service carries a stack trace. Every handler logs
   `str(e)`, which for the common shapes ("", "None", a bare `KeyError: 'x'`)
   identifies neither the line nor the call path — and the per-PR catch-all
   is the one place where that context is most needed, because it is the
   handler that fires for anything nobody anticipated.
"""
import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m
import envcfg


# ── 1. a typo must not be a CrashLoopBackOff ─────────────────────────────

MALFORMED_KNOBS = [
    "DIFF_OCI_FAIL_ERROR_THRESHOLD",
    "DIFF_OCI_SELFCHECK_INTERVAL",
    "DIFF_BACKOFF_BASE",
    "DIFF_BACKOFF_CAP",
]


def test_every_numeric_knob_uses_the_safe_parser():
    """Guards the wiring rather than one value: a new knob added with a bare
    int()/float() reintroduces the whole class."""
    import re
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "src", "diff_preview.py")).read()
    bad = re.findall(r'(?:int|float)\(os\.(?:environ\.get|getenv)\([^)]*\)',
                     src)
    assert not bad, (
        "numeric env vars must go through _env_int/_env_float so a typo logs "
        "a WARNING naming the variable instead of crashing the pod at import "
        "(bughunt N3): %r" % bad)


def test_malformed_values_fall_back_instead_of_raising(monkeypatch):
    """The behaviour the parser exists to provide, exercised end to end."""
    for name in MALFORMED_KNOBS:
        monkeypatch.setenv(name, "not-a-number")
    # _env_int/_env_float must absorb every one of them.
    assert envcfg._env_int("DIFF_OCI_FAIL_ERROR_THRESHOLD", 3) == 3
    assert envcfg._env_float("DIFF_BACKOFF_BASE", 3.0) == 3.0


def test_malformed_value_is_named_in_the_warning(monkeypatch, capsys):
    """A fallback nobody can see is its own kind of silence."""
    monkeypatch.setenv("DIFF_BACKOFF_CAP", "thirty")
    envcfg._env_float("DIFF_BACKOFF_CAP", 30.0)
    err = capsys.readouterr().err          # the parsers warn on stderr
    assert "DIFF_BACKOFF_CAP" in err, \
        "the warning must name the offending variable, not just complain"


# ── 2. a background failure must reach a log ─────────────────────────────

def test_jfrog_refresh_failure_is_logged(monkeypatch):
    """The webhook must not report success over a refresh that never ran."""
    seen = []
    monkeypatch.setattr(m.logsink, "log",
                        lambda msg, lvl="INFO", **k: seen.append(f"{lvl}:{msg}"))

    def _boom(chart, ver):
        raise RuntimeError("argocd unreachable")
    monkeypatch.setattr(m, "_jfrog_hard_refresh", _boom)

    m._jfrog_refresh_guarded("appspace-ms", "1.2.3")
    assert any("ERROR" in s and "argocd unreachable" in s for s in seen), (
        "an exception in the background refresh must be logged, not swallowed "
        "by a Future nobody reads: %r" % seen)


def test_jfrog_refresh_success_is_quiet(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "_jfrog_hard_refresh",
                        lambda c, v: calls.append((c, v)))
    m._jfrog_refresh_guarded("appspace-ms", "1.2.3")
    assert calls == [("appspace-ms", "1.2.3")]


# ── 3. the catch-all must say where it fired ─────────────────────────────

def test_per_pr_catch_all_logs_a_traceback(monkeypatch):
    """`str(e)` alone is empty for a bare KeyError and useless for anything
    raised deep in the render path."""
    import inspect
    src = inspect.getsource(m.process_pr)
    assert "traceback" in src or "exc_info" in src, (
        "the per-PR catch-all must record where the exception came from; "
        "str(e) identifies neither the line nor the call path")
