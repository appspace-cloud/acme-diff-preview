"""COPS-2656: Phase 2 "done" is a declaration, not a fact.

`_decommission_cascades()` reads `appspace.decommission` from the file in
git. The string `finalizers` appears nowhere else in the service. So the
panel reports the cascade as armed on the strength of a config key, and
then tells the reviewer **Resources that will be removed** and lists the
inventory it believes will be cleaned up.

The sequence that breaks:

1. PR 1 merges `appspace.decommission: true`. Phase 2 flips to done the
   moment it lands on main.
2. ArgoCD has not synced yet, so the finalizer is not on the Applications.
3. PR 2 removes the folder. The panel promises a cleanup.
4. The Application is deleted without a finalizer, everything is
   orphaned, and the reviewer was actively reassured rather than warned.

If the environment also has `appspace.autosync: false` (COPS-2583) the
flag will NEVER sync, so the panel reads done indefinitely while the
finalizer never arrives. Two individually correct features combine into
a durable lie.

Same failure mode as the backtick-link defect: verifying presence is not
verifying meaning.

WHAT THIS IS NOT: the backlog note asked to block whenever the finalizer
is not live. That would block EVERY decommission, because orphaning is
the documented default and 0 of 1030 Applications carry a finalizer
today. Only the mismatch is blocked -- config claims the cascade, the
cluster does not have it.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_preview as dp  # noqa: E402

APPS = ["pv-doomed-a-glb", "pv-doomed-a-ms", "pv-doomed-a-ss"]
FINALIZER = "resources-finalizer.argocd.argoproj.io"


class _Argo:
    """Fake `argocd app get -o json` at the subprocess boundary."""

    def __init__(self, finalizers=None, fail=False):
        self.finalizers = finalizers
        self.fail = fail
        self.calls = []

    def install(self, monkeypatch):
        import json as _json
        outer = self

        def fake_run(cmd, **kw):
            outer.calls.append(cmd)

            class R:
                returncode = 1 if outer.fail else 0
                stderr = "connection refused" if outer.fail else ""
                stdout = "" if outer.fail else _json.dumps(
                    {"metadata": {"finalizers": outer.finalizers or []}})
            return R()

        monkeypatch.setattr(dp.subprocess, "run", fake_run)
        return self


# -- the live check ---------------------------------------------------------

def test_a_live_finalizer_is_detected(monkeypatch):
    argo = _Argo(finalizers=[FINALIZER]).install(monkeypatch)
    assert dp._cascade_finalizer_live(APPS) is True
    assert argo.calls, "the check never asked ArgoCD anything"


def test_an_absent_finalizer_is_detected(monkeypatch):
    """The dangerous state: config says armed, cluster has nothing."""
    _Argo(finalizers=[]).install(monkeypatch)
    assert dp._cascade_finalizer_live(APPS) is False


def test_an_unrelated_finalizer_does_not_count(monkeypatch):
    """Only ArgoCD's cascade finalizer causes the cascade. Anything else
    on the object is somebody else's cleanup hook."""
    _Argo(finalizers=["kubernetes.io/pvc-protection"]).install(monkeypatch)
    assert dp._cascade_finalizer_live(APPS) is False


def test_an_unreachable_argocd_returns_unknown_not_false(monkeypatch):
    """THE safety property. False would mean 'config lies, block the PR';
    unknown means 'cannot tell, do not block'. Conflating them would stop
    every decommission during an ArgoCD outage."""
    _Argo(fail=True).install(monkeypatch)
    assert dp._cascade_finalizer_live(APPS) is None


def test_it_asks_once_per_app_not_more(monkeypatch):
    """Folder-removal PRs are rare, but this still runs inside a diff. One
    call per Application, no retries, no per-resource walk."""
    argo = _Argo(finalizers=[FINALIZER]).install(monkeypatch)
    dp._cascade_finalizer_live(APPS)
    assert len(argo.calls) <= len(APPS), (
        f"{len(argo.calls)} ArgoCD calls for {len(APPS)} apps")


def test_a_partial_arming_is_not_treated_as_armed(monkeypatch):
    """One Application carrying the finalizer while its siblings do not
    means a partial sync. Reporting that as armed would promise a cleanup
    for the two that will orphan."""
    import json as _json
    seen = []

    def fake_run(cmd, **kw):
        app = cmd[-1] if isinstance(cmd, list) else ""
        seen.append(app)
        fins = [FINALIZER] if "glb" in str(app) else []

        class R:
            returncode = 0
            stderr = ""
            stdout = _json.dumps({"metadata": {"finalizers": fins}})
        return R()

    monkeypatch.setattr(dp.subprocess, "run", fake_run)
    assert dp._cascade_finalizer_live(APPS) is False


# -- what the reviewer sees -------------------------------------------------

def _panel(monkeypatch, cascade, live):
    monkeypatch.setattr(dp, "_decommission_cascades", lambda *a, **k: cascade)
    monkeypatch.setattr(dp, "_cascade_finalizer_live", lambda apps: live)
    return dp._cascade_mismatch_note("pv-doomed-a", APPS, cascade)


def test_armed_and_live_says_nothing_extra(monkeypatch):
    """The happy path must stay exactly as it is."""
    assert _panel(monkeypatch, cascade=True, live=True) == []


def test_not_armed_says_nothing_extra(monkeypatch):
    """Orphaning is the documented default and the panel already warns
    about it loudly. This ticket must not add a second voice."""
    assert _panel(monkeypatch, cascade=False, live=False) == []


def test_armed_but_absent_is_called_out(monkeypatch):
    lines = _panel(monkeypatch, cascade=True, live=False)
    txt = "\n".join(lines).lower()
    assert lines, "the mismatch produced no warning"
    assert "not" in txt and "finalizer" in txt
    assert "orphan" in txt, (
        "the warning must say what actually happens, not just that "
        "something is inconsistent")


def test_an_unknown_result_does_not_warn(monkeypatch):
    """Cannot tell is not the same as knowing it is wrong. Rendering a
    scary block on a failed lookup would train reviewers to ignore it."""
    assert _panel(monkeypatch, cascade=True, live=None) == []
