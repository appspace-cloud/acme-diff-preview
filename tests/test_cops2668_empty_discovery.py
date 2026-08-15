"""An empty app inventory is a discovery failure, not a clean fleet (COPS-2668).

`discover_path_app_map` raises on rc!=0 and on invalid JSON, and
`main_iteration` handles that case carefully: it logs, re-logs-in, and returns
WITHOUT touching any PR, with a comment explaining why ("Do NOT mass-FAILED all
open PRs"). But `argocd app list` exiting 0 with a list nobody annotated -- an
AppProject RBAC narrowing, a renamed or dropped
`argocd.argoproj.io/manifest-generate-paths` in the Application/ApplicationSet
template -- returns `{}` perfectly normally.

`{}` then means "no app matches any changed file" for every open PR, so each
one gets a SUCCESSFUL "No ArgoCD apps affected" with a `[clean]` footer. The
decommission, VM-strip and disk-shrink panels are never computed, because
nothing reached the code that computes them. The service is at its most
confident precisely when it knows least.

The rest of the codebase already holds the opposite posture explicitly:

    no rendered diff would make the danger obvious, so we refuse instead of
    commenting a green diff

`_path_map_count` was already being written for exactly this purpose and never
read anywhere -- not even in `_diff_stats`.

Note on a genuinely empty fleet: returning early is harmless there too, since
an inventory with no annotated apps gives this service nothing to do.
"""
import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m
import logsink


def _harness(monkeypatch, path_map):
    """Run one main_iteration with a controlled discovery result."""
    logs = []
    monkeypatch.setattr(logsink, "log", lambda msg, lvl="INFO", **k: logs.append(f"{lvl}:{msg}"))
    monkeypatch.setattr(logsink, "debug", lambda *a, **k: None)
    monkeypatch.setattr(m, "_prune_helm_cache", lambda: None)
    monkeypatch.setattr(m, "discover_path_app_map", lambda: path_map)
    monkeypatch.setattr(m, "argocd_login", lambda: None)
    monkeypatch.setattr(m, "_argocd_token", "", raising=False)

    progressed = []
    monkeypatch.setattr(m, "_touch_progress", lambda: progressed.append(1))

    # Anything reaching Bitbucket means we walked past the guard. The per-repo
    # poll runs inside its own try by design (one repo's failure must not
    # starve the others), so this records and raises an ordinary error rather
    # than asserting -- an assertion here would be swallowed by that handler.
    polled = []

    def _boom(*a, **k):
        polled.append(1)
        raise RuntimeError("bitbucket unavailable in test")
    monkeypatch.setattr(m, "http", _boom)

    m.main_iteration()
    return logs, progressed, polled


def test_empty_discovery_does_not_poll_or_comment(monkeypatch):
    """The bug: {} sailed through and every open PR got a false green."""
    logs, progressed, polled = _harness(monkeypatch, {})
    assert not polled, "an empty inventory must stop before any PR is touched"
    # main_iteration touches progress twice on the happy path: once on entry
    # ("iteration is alive") and once after discovery ("discovery succeeded").
    # An empty inventory must record the first and never the second.
    assert len(progressed) == 1, (
        "the 'discovery succeeded' checkpoint must not fire on an empty "
        "inventory (saw %d touches)" % len(progressed))


def test_empty_discovery_logs_an_error(monkeypatch):
    """It must be diagnosable, not a silent early return."""
    logs, _, _ = _harness(monkeypatch, {})
    assert any(l.startswith("ERROR:") and "discovery failure" in l for l in logs), (
        "an empty inventory must be logged at ERROR and named as a discovery "
        "failure, not left as a silent early return: %r" % logs)


def test_non_empty_discovery_still_proceeds(monkeypatch):
    """The guard must not break the normal path: a populated map still polls."""
    _, progressed, polled = _harness(monkeypatch, {"env/dev/app": ["argocd/app-1"]})
    assert polled, "a populated inventory must reach the repo poll"
    assert progressed, "a populated inventory must check the progress point"
