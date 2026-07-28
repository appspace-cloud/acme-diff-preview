"""v2.13.0: shared Bitbucket rate-limit gate + stage in the cron hard-refresh.

Two production bugs, both found while chasing "acme-diff-preview is failing
on normal PRs" (COPS-2543).

1. `_bb_fetch_status()` is the hottest Bitbucket path (one call per value
   file, ~14 per app per PR) and it was the ONE 429 path that never learned
   the lesson bughunt F4 taught `http()`: Bitbucket rate-limit windows run
   ~60s, so retrying after 2s then 4s just burns both attempts inside the
   same window and returns BB_ERROR. An empty value file makes `helm
   template` fail with "missing required value", which surfaces as "diff
   indeterminate" on the PR. Production saw 162 retries and 7 hard failures
   in 24h. Two things were missing: `Retry-After` was ignored here (while
   `http()` has parsed it since v2.5.19), and the pause was per-thread, so
   all BB_API_CONCURRENCY threads had to each discover the same 429 and burn
   their own retries doing it. A 429 is a property of the token, not of one
   request, so the pause has to be shared.

2. `dev_hard_refresh.py` hardcoded `PROJECTS = ["appspace-dev",
   "appspace-qa"]`, leaving `appspace-stage` with no cron safety net behind
   the JFrog webhook. Stage needs it exactly as much as dev/qa: 35 of its 38
   apps track MUTABLE `-dev` chart tags, so ArgoCD will not re-pull the .tgz
   without a hard refresh. Prod stays out on purpose, it pins immutable
   `-rev1` tags.
"""
import importlib
import io
import os
import sys
import threading
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import dev_hard_refresh as dhr  # noqa: E402
import diff_preview as m  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────

def _http_error(code, headers=None):
    return urllib.error.HTTPError(
        "https://api.bitbucket.org/x", code, "boom", headers or {}, io.BytesIO(b""))


@pytest.fixture(autouse=True)
def _clear_gate():
    """The rate-limit gate is module-global; never leak a pause between tests."""
    m._bb_ratelimit_clear()
    yield
    m._bb_ratelimit_clear()


@pytest.fixture
def raising_urlopen(monkeypatch):
    """Make urlopen raise a scripted sequence, and record every sleep."""
    def _install(*errors):
        seq = list(errors)
        calls = {"n": 0}
        sleeps = []

        def fake_urlopen(*a, **k):
            calls["n"] += 1
            exc = seq.pop(0) if seq else _http_error(429)
            raise exc

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(m.time, "sleep", lambda s: sleeps.append(s))
        return calls, sleeps
    return _install


# ── the shared gate helpers (pure) ───────────────────────────────────────

def test_ratelimit_hold_extends_but_never_shortens_an_active_pause():
    # Thread A learns "wait 45s". Thread B then sees a 429 whose header only
    # says 5s. Shortening the pause to 5s would let the whole pool back into
    # the same window that just rejected it, so max() wins.
    m._bb_ratelimit_hold(45)
    long_pause = m._bb_ratelimit_remaining()
    m._bb_ratelimit_hold(5)
    assert m._bb_ratelimit_remaining() == pytest.approx(long_pause, abs=1.0)


def test_ratelimit_wait_returns_immediately_when_no_pause_is_active(monkeypatch):
    slept = []
    monkeypatch.setattr(m.time, "sleep", lambda s: slept.append(s))
    m._bb_ratelimit_wait()
    assert slept == [], "no active pause must not cost a single sleep"


def test_ratelimit_wait_sleeps_out_an_active_pause(monkeypatch):
    slept = []
    monkeypatch.setattr(m.time, "sleep", lambda s: slept.append(s))
    m._bb_ratelimit_hold(30)
    m._bb_ratelimit_wait()
    assert slept, "an active pause must be slept out"
    assert slept[0] == pytest.approx(30, abs=1.0)


def test_ratelimit_wait_is_bounded_and_cannot_spin_forever(monkeypatch):
    # Defensive: the loop must not depend on time actually advancing (a
    # no-op sleep, a frozen clock). Bounded slices, not a while-True.
    monkeypatch.setattr(m.time, "sleep", lambda s: None)   # time never moves
    m._bb_ratelimit_hold(600)
    m._bb_ratelimit_wait()   # must return, not hang the whole pool


# ── _bb_fetch_status: 429 handling ───────────────────────────────────────

def test_429_with_retry_after_header_waits_the_server_mandated_window(raising_urlopen):
    # THE bug: Bitbucket says "come back in 30s", we came back in 2s.
    calls, sleeps = raising_urlopen(
        _http_error(429, {"Retry-After": "30"}),
        _http_error(429, {"Retry-After": "30"}),
        _http_error(429, {"Retry-After": "30"}),
    )
    content, status = m._bb_fetch_status("gcp/dev/x/config.yaml", "a" * 40)
    assert (content, status) == (None, m.BB_ERROR)
    # approx: the pause is published as a monotonic deadline, so the sleep is
    # the remainder after the microseconds spent getting there.
    assert max(sleeps) == pytest.approx(30, abs=1.0), (
        f"Retry-After: 30 must be honored, slept {sleeps} instead")


def test_429_retry_after_http_date_form_is_honored(raising_urlopen):
    # RFC 7231 allows the HTTP-date form; _parse_retry_after handles both and
    # this path must go through it rather than parsing the header itself.
    from email.utils import format_datetime
    from datetime import datetime, timedelta, timezone
    when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=40))
    calls, sleeps = raising_urlopen(
        _http_error(429, {"Retry-After": when}),
        _http_error(429, {"Retry-After": when}),
        _http_error(429, {"Retry-After": when}),
    )
    m._bb_fetch_status("gcp/dev/x/config.yaml", "a" * 40)
    assert max(sleeps) >= 30, f"date-form Retry-After must be parsed, slept {sleeps}"


def test_429_retry_after_is_capped_so_a_broken_header_cannot_stall_a_pr(raising_urlopen):
    calls, sleeps = raising_urlopen(
        _http_error(429, {"Retry-After": "99999"}),
        _http_error(429, {"Retry-After": "99999"}),
        _http_error(429, {"Retry-After": "99999"}),
    )
    m._bb_fetch_status("gcp/dev/x/config.yaml", "a" * 40)
    assert max(sleeps) <= m.BB_RATELIMIT_MAX_PAUSE, (
        f"a hostile Retry-After must be capped at {m.BB_RATELIMIT_MAX_PAUSE}s, "
        f"slept {sleeps}")


def test_429_without_retry_after_still_waits_longer_than_the_old_2s(raising_urlopen):
    # Bitbucket does not always send the header on /src. The fallback still
    # has to be window-sized, otherwise both retries land inside the same
    # rejected window exactly like before.
    calls, sleeps = raising_urlopen(
        _http_error(429), _http_error(429), _http_error(429))
    m._bb_fetch_status("gcp/dev/x/config.yaml", "a" * 40)
    assert m.BB_RATELIMIT_FALLBACK > 2, "2s was the bug; the fallback must beat it"
    assert max(sleeps) == pytest.approx(m.BB_RATELIMIT_FALLBACK, abs=1.0), (
        f"the no-header fallback must be window-sized, slept {sleeps}")


def test_429_publishes_the_pause_for_every_other_thread(raising_urlopen):
    # The whole point: one thread discovering the 429 must brake the entire
    # pool, so the other 29 do not each burn their own retries learning it.
    raising_urlopen(_http_error(429, {"Retry-After": "30"}))
    m._bb_fetch_status("gcp/dev/x/config.yaml", "a" * 40)
    assert m._bb_ratelimit_remaining() > 0, (
        "a 429 seen by one thread must publish a shared pause")


def test_a_paused_thread_does_not_hold_a_concurrency_slot(monkeypatch):
    # Sleeping while holding _bb_api_sem would let a 429 storm pin all
    # BB_API_CONCURRENCY slots on threads that are doing nothing but waiting.
    seen = []

    def fake_urlopen(*a, **k):
        # Snapshot how many slots are free at the moment of the call.
        free = 0
        while m._bb_api_sem.acquire(blocking=False):
            free += 1
        for _ in range(free):
            m._bb_api_sem.release()
        seen.append(free)
        raise _http_error(429, {"Retry-After": "30"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    m._bb_fetch_status("gcp/dev/x/config.yaml", "a" * 40)
    # Attempt 2+ happens after a pause; by then the slot taken for attempt 1
    # must be back in the pool, so the free count never drops attempt over
    # attempt.
    assert len(seen) >= 2
    assert seen[1] >= seen[0], (
        f"a paused retry leaked a concurrency slot: free slots per attempt {seen}")


def test_429_is_visible_at_warning_not_only_in_debug(raising_urlopen, capsys):
    # Production only ever showed the aggregate "Bitbucket API error" line,
    # because the per-call 429 was logged at debug() and DIFF_DEBUG is off.
    # Rate limiting is an operational signal, it has to be visible.
    raising_urlopen(_http_error(429, {"Retry-After": "30"}))
    m._bb_fetch_status("gcp/dev/x/config.yaml", "a" * 40)
    out = capsys.readouterr().out
    assert "429" in out and "WARNING" in out, f"429 must be logged at WARNING: {out}"


def test_a_5xx_still_uses_plain_backoff_and_no_shared_pause(raising_urlopen):
    # Only 429 means "the token is over budget". A 503 is one sick request;
    # braking every other thread for it would be a self-inflicted outage.
    calls, sleeps = raising_urlopen(
        _http_error(503), _http_error(503), _http_error(503))
    content, status = m._bb_fetch_status("gcp/dev/x/config.yaml", "a" * 40)
    assert (content, status) == (None, m.BB_ERROR)
    assert calls["n"] == 3
    assert m._bb_ratelimit_remaining() == 0, (
        "a 5xx must not brake the whole pool, only 429 means over-budget")


def test_404_still_short_circuits_without_any_pause(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(_http_error(404)))
    content, status = m._bb_fetch_status("gcp/dev/gone.yaml", "a" * 40)
    assert (content, status) == (None, m.BB_NOT_FOUND)
    assert m._bb_ratelimit_remaining() == 0


def test_a_successful_fetch_is_unchanged(monkeypatch):
    class _R:
        def read(self):
            return b"appspace:\n  version: 2603.1.3-dev\n"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _R())
    content, status = m._bb_fetch_status("gcp/dev/x/config.yaml", "a" * 40)
    assert status == m.BB_OK and "2603.1.3-dev" in content


# ── dev_hard_refresh: stage ──────────────────────────────────────────────

def test_the_chart_ships_the_cron_disabled(): 
    # COPS-2543: the webhook already refreshes exactly the apps tracking a
    # published chart. A full cron sweep drove enough traffic through the
    # argocd-agent principal that spoke-hosted apps got cut off by the 30s LB
    # timeout, so the sweep is opt-in now. Encoded as a test because "it is
    # off" is a deliberate decision, not an accident of the default file.
    import yaml
    values = yaml.safe_load(open(os.path.join(
        os.path.dirname(__file__), "..", "charts", "acme-diff-preview", "values.yaml")))
    assert values["hardRefresh"]["enabled"] is False
    # The tunables must still be present and sane, so re-enabling it is a
    # one-line change and not a re-litigation of the whole thing.
    assert values["hardRefresh"]["workers"] <= 4
    assert "appspace-stage" in values["hardRefresh"]["projects"]
    assert "appspace-prod" not in values["hardRefresh"]["projects"]


def test_default_projects_include_stage_but_never_prod(monkeypatch):
    monkeypatch.delenv("HARD_REFRESH_PROJECTS", raising=False)
    reloaded = importlib.reload(dhr)
    try:
        assert "appspace-stage" in reloaded.PROJECTS, (
            "35 of 38 stage apps track mutable -dev tags; stage needs the cron "
            "safety net exactly like dev/qa")
        assert "appspace-dev" in reloaded.PROJECTS
        assert "appspace-qa" in reloaded.PROJECTS
        assert "appspace-prod" not in reloaded.PROJECTS, (
            "prod pins immutable -rev1 tags: hard-refreshing 761 prod apps "
            "daily would hammer the hub for nothing")
    finally:
        importlib.reload(dhr)


def test_projects_are_env_overridable_and_tolerate_sloppy_lists(monkeypatch):
    monkeypatch.setenv("HARD_REFRESH_PROJECTS", " appspace-dev , ,appspace-stage ")
    reloaded = importlib.reload(dhr)
    try:
        assert reloaded.PROJECTS == ["appspace-dev", "appspace-stage"], (
            "the chart passes a comma-joined string; whitespace and empty "
            "entries must not become bogus --project flags")
    finally:
        monkeypatch.delenv("HARD_REFRESH_PROJECTS", raising=False)
        importlib.reload(dhr)


def test_default_concurrency_leaves_headroom_under_the_30s_lb_timeout(monkeypatch):
    # A single `app get --hard-refresh` measures ~19s against an idle hub and
    # the GCP LB backend in front of argocd.appspace.com cuts at 30s, so there
    # is ~11s of headroom to spend on queueing. 8 workers spent more than that
    # and lost 20 of 120 apps to LB cuts, all of them spoke-hosted (`pv-*`)
    # apps going through the principal's ~9.5 events/s per spoke ceiling.
    monkeypatch.delenv("HARD_REFRESH_WORKERS", raising=False)
    reloaded = importlib.reload(dhr)
    try:
        assert reloaded.WORKERS <= 4, (
            "this is a once-a-day safety net; wall-clock time is worth nothing "
            "and not overwhelming the hub is worth a lot")
        assert reloaded.ATTEMPTS >= 2, "an LB cut is transient and must be retried"
    finally:
        importlib.reload(dhr)


def test_workers_pace_and_attempts_are_env_tunable(monkeypatch):
    monkeypatch.setenv("HARD_REFRESH_WORKERS", "6")
    monkeypatch.setenv("HARD_REFRESH_ATTEMPTS", "3")
    monkeypatch.setenv("HARD_REFRESH_PACE", "0")
    reloaded = importlib.reload(dhr)
    try:
        assert (reloaded.WORKERS, reloaded.ATTEMPTS, reloaded.PACE) == (6, 3, 0.0)
    finally:
        for k in ("HARD_REFRESH_WORKERS", "HARD_REFRESH_ATTEMPTS", "HARD_REFRESH_PACE"):
            monkeypatch.delenv(k, raising=False)
        importlib.reload(dhr)


@pytest.mark.parametrize("bad", ["", "0", "-4", "three", "  "])
def test_a_bogus_worker_count_falls_back_instead_of_crashing_the_job(monkeypatch, bad):
    # A typo in the chart value must not turn the nightly safety net into a
    # CrashLoop or a max_workers=0 ThreadPoolExecutor ValueError.
    monkeypatch.setenv("HARD_REFRESH_WORKERS", bad)
    reloaded = importlib.reload(dhr)
    try:
        assert reloaded.WORKERS >= 1
    finally:
        monkeypatch.delenv("HARD_REFRESH_WORKERS", raising=False)
        importlib.reload(dhr)


def test_a_transient_failure_is_retried_and_can_still_succeed(tmp_path, monkeypatch):
    import subprocess as sp
    calls = {"n": 0}

    def fake_run(cmd, **kw):
        calls["n"] += 1

        class R:
            # First attempt is cut off by the LB, second one gets through.
            returncode = 0 if calls["n"] > 1 else 1
            stdout = ""
            stderr = '{"level":"fatal","msg":"rpc error: code = Unknown desc = POST https://argocd.appspace.com/... EOF"}'
        return R()

    monkeypatch.setattr(sp, "run", fake_run)
    monkeypatch.setattr(dhr, "PACE", 0)
    monkeypatch.setattr(dhr, "ATTEMPTS", 2)
    app, ok, _ = dhr.hard_refresh("pv-qa11-a-glb")
    assert ok is True and calls["n"] == 2, (
        "an LB cut on the first attempt must not cost the app its daily refresh")


def test_the_full_cli_error_survives_truncation(tmp_path, monkeypatch, capsys):
    # The old stderr[:80] cut exactly at "...POST https://argocd.app", which is
    # why production logs never showed whether it was a timeout or a 502.
    import subprocess as sp
    err = ('{"level":"fatal","msg":"rpc error: code = Unknown desc = POST '
           'https://argocd.appspace.com/application.ApplicationService/Get '
           'failed: EOF","time":"2026-07-28T06:00:00Z"}')

    def fake_run(cmd, **kw):
        class R:
            returncode = 1
            stdout = ""
            stderr = err
        return R()

    monkeypatch.setattr(sp, "run", fake_run)
    monkeypatch.setattr(dhr, "PACE", 0)
    app, ok, _ = dhr.hard_refresh("pv-qa11-a-glb")
    out = capsys.readouterr().out
    assert ok is False
    assert "failed: EOF" in out, (
        f"the useful half of the error must survive truncation: {out}")


def test_pace_staggers_each_attempt(monkeypatch):
    import subprocess as sp
    sleeps = []

    def fake_run(cmd, **kw):
        class R:
            returncode = 1
            stdout = ""
            stderr = "boom"
        return R()

    monkeypatch.setattr(sp, "run", fake_run)
    monkeypatch.setattr(dhr.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(dhr, "PACE", 0.5)
    monkeypatch.setattr(dhr, "ATTEMPTS", 2)
    dhr.hard_refresh("pv-x-a-ms")
    assert sleeps == [0.5, 0.5]


def test_list_apps_passes_one_project_flag_per_configured_project(tmp_path, monkeypatch):
    import subprocess as sp
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd

        class R:
            returncode = 0
            stdout = "argocd/app-one\n"
            stderr = ""
        return R()

    monkeypatch.setattr(sp, "run", fake_run)
    monkeypatch.setattr(dhr, "PROJECTS", ["appspace-dev", "appspace-stage"])
    dhr.list_apps()
    cmd = captured["cmd"]
    pairs = [(cmd[i], cmd[i + 1]) for i, a in enumerate(cmd) if a == "--project"]
    assert pairs == [("--project", "appspace-dev"), ("--project", "appspace-stage")]
