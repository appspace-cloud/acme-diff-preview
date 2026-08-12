"""COPS-2653: a DNS blip at startup should not restart the container.

Observed on 2026-08-12 during the COPS-2646 rollout:

    Sub-task pool ready (32 workers)
    ArgoCD login failed (attempt 1): <urlopen error [Errno -3]
        Temporary failure in name resolution>

The node's DNS was not ready yet. `main()` calls `argocd_login()` with the
comment "raises on failure so the container restarts immediately", so the
process exited and Kubernetes restarted it. The second attempt worked and
the incident cost about 20 seconds.

Fail-fast is right for ONE class of failure. A wrong password should
produce a loud CrashLoopBackOff, not a pod that limps along pretending to
be healthy. It is wrong for the other class: DNS, connection resets and a
momentarily unreachable API say nothing about whether this pod is
configured correctly, and they clear on their own in seconds.

Why it scales badly: CrashLoopBackOff adds exponential backoff, so a
20-second blip can cost minutes long after the cause has cleared; a
rollout starts both replicas at once, so a cluster-wide wobble can leave
no leader at all; and the reason ends up in the PREVIOUS container's log,
which is gone after the next restart.

The transient/permanent split is the same judgement COPS-2647 made for
GCS uploads, and the budget is sized against the real probes: readiness
is 30s initial + 30s period + 3 failures, liveness restarts at roughly
360s. Retrying inside 60s costs at most two readiness failures - which
only removes the pod from endpoints, where it belongs while it has no
session - and never reaches the liveness restart.
"""
import os
import sys
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_preview as m  # noqa: E402


def _no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(m.time, "sleep", lambda s: slept.append(s))
    return slept


def _fetch_raising(monkeypatch, errors):
    """Queue of exceptions (None = success) for _argocd_fetch_token."""
    calls = []

    def fake():
        calls.append(1)
        exc = errors[min(len(calls) - 1, len(errors) - 1)]
        if exc is not None:
            raise exc
        return "fake-jwt"

    monkeypatch.setattr(m, "_argocd_fetch_token", fake)
    return calls


def _dns_error():
    return urllib.error.URLError(
        OSError(-3, "Temporary failure in name resolution"))


# -- the observed incident --------------------------------------------------

def test_a_dns_blip_at_startup_is_retried_instead_of_crashing(monkeypatch):
    """THE gate. This is the exact failure seen in production."""
    _no_sleep(monkeypatch)
    calls = _fetch_raising(monkeypatch, [_dns_error(), _dns_error(), None])
    m._startup_argocd_login()          # must NOT raise
    assert len(calls) == 3, f"expected two retries then success, got {len(calls)}"
    assert m._ready is True


def test_a_transient_failure_that_never_clears_still_fails_loudly(monkeypatch):
    """The retry is a grace period, not a way to hide a dead ArgoCD."""
    _no_sleep(monkeypatch)
    calls = _fetch_raising(monkeypatch, [_dns_error()])
    try:
        m._startup_argocd_login()
        raise AssertionError("a persistently unreachable ArgoCD must raise")
    except Exception as e:
        assert not isinstance(e, AssertionError), e
    assert 1 < len(calls) <= 8, f"unbounded or absent retries: {len(calls)}"


# -- permanent failures must stay immediate ---------------------------------

def test_a_bad_password_still_crashes_on_the_first_attempt(monkeypatch):
    """A 401 will still be a 401 in two seconds. Retrying it delays the
    loud, correct CrashLoopBackOff that tells an operator the config is
    wrong."""
    _no_sleep(monkeypatch)
    err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
    calls = _fetch_raising(monkeypatch, [err])
    try:
        m._startup_argocd_login()
        raise AssertionError("a 401 must raise")
    except Exception as e:
        assert not isinstance(e, AssertionError), e
    assert len(calls) == 1, (
        f"a permanent error must not be retried, got {len(calls)} attempts")


def test_a_403_is_also_permanent(monkeypatch):
    _no_sleep(monkeypatch)
    err = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
    calls = _fetch_raising(monkeypatch, [err])
    try:
        m._startup_argocd_login()
    except Exception:
        pass
    assert len(calls) == 1


def test_a_500_is_transient(monkeypatch):
    """ArgoCD restarting behind the load balancer is not a config error."""
    _no_sleep(monkeypatch)
    err = urllib.error.HTTPError("u", 503, "Service Unavailable", {}, None)
    calls = _fetch_raising(monkeypatch, [err, None])
    m._startup_argocd_login()
    assert len(calls) == 2


# -- the budget must respect the probes -------------------------------------

def test_the_retry_budget_stays_inside_the_probe_tolerance(monkeypatch):
    """Readiness: 30s initial, 30s period, 3 failures. Liveness restarts at
    roughly 360s. Retrying must cost at most a couple of readiness failures
    and must never approach the liveness restart, or the graceful path
    becomes a slower version of the crash it replaces."""
    slept = _no_sleep(monkeypatch)
    _fetch_raising(monkeypatch, [_dns_error()])
    try:
        m._startup_argocd_login()
    except Exception:
        pass
    assert sum(slept) <= 60, (
        f"total backoff {sum(slept)}s exceeds the 60s budget")
    assert all(s <= 30 for s in slept), f"a single sleep too long: {slept}"


def test_backoff_actually_backs_off(monkeypatch):
    """Hammering a struggling ArgoCD every 100ms helps nobody."""
    slept = _no_sleep(monkeypatch)
    _fetch_raising(monkeypatch, [_dns_error()])
    try:
        m._startup_argocd_login()
    except Exception:
        pass
    assert len(slept) >= 2 and slept == sorted(slept), (
        f"expected non-decreasing backoff, got {slept}")


# -- the running loop must be untouched -------------------------------------

def test_the_running_loop_login_still_raises_on_the_first_failure(monkeypatch):
    """argocd_login() is called mid-loop for session refresh, where the
    caller already handles failure and _consecutive_login_fails drives
    readiness. Only STARTUP gets the grace period."""
    _no_sleep(monkeypatch)
    calls = _fetch_raising(monkeypatch, [_dns_error()])
    try:
        m.argocd_login()
        raise AssertionError("argocd_login must still raise")
    except Exception as e:
        assert not isinstance(e, AssertionError), e
    assert len(calls) == 1, (
        "argocd_login() itself must not retry; that is the startup "
        "wrapper's job")
