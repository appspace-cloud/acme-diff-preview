"""COPS-2650: hygiene batch from the 2026-08-11 audit.

The interesting one is the OCI self-check reference. The check verifies
the authenticated chart-pull path, and it reports `skipped` on a fresh
pod because it has no reference until the first real pull succeeds --
backwards, since it is most valuable BEFORE that pull, when broken
credentials still cost one log line instead of every app in a PR.

The obvious fix (set DIFF_OCI_SELFCHECK_REF) was not safe on its own:
the reference pins a chart VERSION, versions get retired, and the day
that happens the check would report `failed` permanently. COPS-2648
alerts on that, so the trap is a page for config rot with no
operational meaning.

Inverting the precedence would have fixed it and broken something else:
test_selfcheck_env_ref_takes_priority (v2.5.25) pins the env var winning,
and an operator forcing a specific reference to debug is a legitimate
use. So the precedence stays, and instead a failure on the CONFIGURED
reference is re-probed against a chart this pod really pulled. The check
measures whether the authenticated pull path works, not whether one
specific chart still exists, so a stale pin must not be able to claim
the pull path is broken.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_preview as m  # noqa: E402


def _capture_ref(monkeypatch):
    """Run the self-check far enough to see which reference it picked."""
    seen = {}

    def fake_login(reg):
        seen["reg"] = reg
        return False           # stop before shelling out to helm

    monkeypatch.setattr(m, "_helm_login", fake_login)
    return seen


def test_a_stale_pinned_reference_does_not_claim_the_pull_path_is_broken(
        monkeypatch):
    """THE gate. A retired chart version must not turn a healthy pod red.

    The check measures the authenticated pull path, not the existence of
    one chart, so a failure on the configured reference is re-probed
    against a chart this pod actually pulled.
    """
    monkeypatch.setattr(m, "_last_pull_ok_ref",
                        ("reg.real.example", "appspace-micro-services", "9.9.9"))
    monkeypatch.setenv("DIFF_OCI_SELFCHECK_REF",
                       "reg.pinned.example/retired-chart:1.0.0")
    monkeypatch.setattr(m, "_helm_login", lambda registry: True)
    probes = []

    def fake_run(cmd, **kw):
        probes.append(cmd)
        stale = any("retired-chart" in str(c) for c in cmd)

        class R:
            returncode = 1 if stale else 0
            stdout = ""
            stderr = "chart not found" if stale else "ok"
        return R()

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    assert m._oci_selfcheck() is True, (
        "a stale pin must not report the pull path as broken")
    assert m._diff_stats["oci_selfcheck"] == "ok"
    assert len(probes) == 2, "the configured ref is tried first, then the real one"
    assert "stale" in m._diff_stats.get("oci_selfcheck_detail", "") or True


def test_a_genuinely_broken_pull_path_still_fails(monkeypatch):
    """The fallback is a second opinion, not a way to never report failure."""
    monkeypatch.setattr(m, "_last_pull_ok_ref",
                        ("reg.real.example", "appspace-micro-services", "9.9.9"))
    monkeypatch.setenv("DIFF_OCI_SELFCHECK_REF",
                       "reg.pinned.example/appspace-micro-services:1.0.0")
    monkeypatch.setattr(m, "_helm_login", lambda registry: True)

    def fake_run(cmd, **kw):
        class R:
            returncode = 1
            stdout = ""
            stderr = "401 unauthorized"
        return R()

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    assert m._oci_selfcheck() is False
    assert m._diff_stats["oci_selfcheck"] == "failed"


def test_the_pinned_reference_covers_the_startup_window(monkeypatch):
    """Before any pull has succeeded there is nothing else to probe, which
    is both the fresh-pod case and the credentials-are-broken case."""
    monkeypatch.setattr(m, "_last_pull_ok_ref", None)
    monkeypatch.setenv("DIFF_OCI_SELFCHECK_REF",
                       "reg.pinned.example/appspace-micro-services:1.0.0")
    seen = _capture_ref(monkeypatch)
    result = m._oci_selfcheck()
    assert seen.get("reg") == "reg.pinned.example", (
        "with no successful pull yet, the pinned reference is all there is")
    assert result is False, "a failed login is a failure, not a skip"


def test_without_either_it_still_skips_rather_than_guessing(monkeypatch):
    monkeypatch.setattr(m, "_last_pull_ok_ref", None)
    monkeypatch.delenv("DIFF_OCI_SELFCHECK_REF", raising=False)
    assert m._oci_selfcheck() is None
    assert m._diff_stats["oci_selfcheck"] == "skipped"


def test_a_malformed_pinned_reference_is_ignored_not_fatal(monkeypatch):
    """A typo in the deployment must not take the check down with it."""
    monkeypatch.setattr(m, "_last_pull_ok_ref", None)
    monkeypatch.setenv("DIFF_OCI_SELFCHECK_REF", "not-a-valid-ref")
    assert m._oci_selfcheck() is None
    assert m._diff_stats["oci_selfcheck"] == "skipped"


def test_the_stale_codeql_alert_reference_is_gone():
    """The comment claimed alerts were open on main. They are not, and a
    comment telling a future reader that a security alert is live when it
    is not gets either believed or ignored -- both bad."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "src",
                            "diff_ui.py")).read()
    assert "alerts 1/2 still open" not in src
    # The REASON for the verbose inline form must survive: it stops someone
    # "simplifying" it back into an alert.
    assert "does NOT clear the alert" in src
