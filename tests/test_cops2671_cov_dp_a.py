"""COPS-2671 (coverage pass A): the observability paths nothing ever drove.

Every line pinned here belongs to code that only runs when something else
has already gone wrong, or to a surface a monitoring system reads rather
than a human. That is exactly why none of it had a test, and exactly why it
needs one: these are the lines that decide whether the next incident is
visible or silent.

  _record_stage, input hygiene (678-681)
      A stage sample is `time.perf_counter() - t0`. A call site that passes
      None (a t0 that was never taken) or a negative delta must have its
      sample DROPPED, because stage_*_seconds and stage_*_count are exported
      as Prometheus counters: a counter that moves backwards -- or a total
      that grows while its count does not -- is read as a pod restart by
      rate()/increase() and silently swallowed. The guard existed from
      COPS-2631 stage 0; only the happy path was ever exercised.

  GET /metrics (1268-1277)
      render_prometheus() is well covered (test_cops2627_metrics_endpoint),
      but nothing had ever asked the health server for the page. The
      snapshot of the live counters, the per-replica leadership stamp, the
      exposition Content-Type and the Content-Length were all dark. A scrape
      target that answers with the wrong content type or a short body loses
      EVERY metric on the page at once, and that failure looks on a
      dashboard exactly like "all the counters are zero", i.e. healthy.

  _base_superseded_by, the COPS-2633 ordering guard (1083-1084)
      Belt-and-braces for the stale base hint. _note_base_observed retires a
      hint the poller has already moved past, and the peek side refuses to
      act on one anyway. The braces normally hide the belt, so the belt was
      never executed: the only way to reach it is for the retirement to fail,
      which _note_base_observed swallows by design ("must never raise for any
      reason"). That failure is injected here, because a swallowed error must
      not resurrect the bug that cost every PR on a repo three iterations.

  _oci_selfcheck, the COPS-2650 fallback probe's own failure (3306-3308)
      When the pinned self-check reference is stale, the check re-probes with
      a chart this pod really pulled. That second opinion must never become a
      new failure mode of its own: if the re-probe explodes, the original
      verdict stands and the reason is logged at DEBUG, not raised into the
      self-check loop.

  _is_transient_exception, subprocess timeouts (3736)
      A `helm template` / `argocd` call that outruns its timeout is
      infrastructure, not a broken PR. COPS-2668 made the catch-all classify
      the exception it caught; every branch of that classifier had a test
      except this one -- and it is the one that fires on a slow repo-server,
      the most common real hiccup of the lot.
"""
import os
import subprocess
import sys
import urllib.error
import urllib.request

import pytest

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import diff_preview as m  # noqa: E402
import logsink  # noqa: E402

from test_coverage_orchestration import world, _mk_pr, PATH_MAP, BASE_SHA  # noqa: E402,F401


STAGES = ("pull", "render", "parse", "diff", "store")


def _stage(stage):
    with m._diff_stats_lock:
        return (m._diff_stats["stage_%s_seconds" % stage],
                m._diff_stats["stage_%s_count" % stage])


@pytest.fixture()
def clean_stage_stats():
    """Stage counters are process-global; leave them exactly as found."""
    with m._diff_stats_lock:
        backup = {k: m._diff_stats[k] for k in m._diff_stats
                  if k.startswith("stage_")}
        for stage in STAGES:
            m._diff_stats["stage_%s_seconds" % stage] = 0.0
            m._diff_stats["stage_%s_count" % stage] = 0
    yield
    with m._diff_stats_lock:
        m._diff_stats.update(backup)


# ── 1. _record_stage refuses samples it cannot trust ─────────────────────

@pytest.mark.parametrize("bogus", [None, "n/a", object()])
def test_a_non_numeric_stage_sample_is_dropped(clean_stage_stats, bogus):
    """float(None) is a TypeError and float("n/a") a ValueError. Either one
    escaping would crash a diff worker on the hot path; recording it would
    be worse still, because `count` would climb without `seconds` and every
    derived average would be wrong for the life of the pod."""
    m._record_stage("pull", 1.5)
    m._record_stage("pull", bogus)
    assert _stage("pull") == (1.5, 1)


def test_a_negative_stage_sample_is_dropped(clean_stage_stats):
    """A negative delta means the two clock reads came from different
    places, not that the stage took less than no time. Adding it would move
    a Prometheus counter backwards, which every scraper reads as a restart."""
    m._record_stage("render", 2.0)
    m._record_stage("render", -0.75)
    assert _stage("render") == (2.0, 1)


def test_a_dropped_sample_does_not_stop_the_next_good_one(clean_stage_stats):
    """The guards must be a filter, not a fuse: after bad input the stage
    keeps accumulating normally."""
    m._record_stage("diff", None)
    m._record_stage("diff", -3.0)
    m._record_stage("diff", 0.25)
    assert _stage("diff") == (0.25, 1)


def test_zero_seconds_is_a_real_sample_not_a_bad_one(clean_stage_stats):
    """The boundary the `< 0` test draws. A cache hit legitimately measures
    ~0s, and dropping those would make the render cache look like it never
    ran (which is the very thing stage timing exists to prove)."""
    m._record_stage("store", 0.0)
    assert _stage("store") == (0.0, 1)


# ── 2. GET /metrics: the page a scraper actually reads ───────────────────

@pytest.fixture()
def health():
    srv = m._start_health_server(0)
    yield "http://127.0.0.1:%d" % srv.server_address[1]
    srv.shutdown()


@pytest.fixture()
def scrapeable_counter():
    """Put a recognisable value into the live counters and restore it."""
    with m._diff_stats_lock:
        before = m._diff_stats["section_cap_trims"]
        m._diff_stats["section_cap_trims"] = 4242
    yield 4242
    with m._diff_stats_lock:
        m._diff_stats["section_cap_trims"] = before


class _FakeElector:
    def __init__(self, leading):
        self._leading = leading

    def is_leader(self):
        return self._leading


def _get(url):
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _metric(text, name):
    for ln in text.split("\n"):
        if ln and not ln.startswith("#") and ln.split("{")[0].split(" ")[0] == name:
            return float(ln.rsplit(" ", 1)[1])
    return None


def test_metrics_serves_the_live_counters_not_a_blank_page(health,
                                                           scrapeable_counter):
    """The endpoint must snapshot the counters this pod is really keeping.
    Serving a fresh/empty dict would render a full, well-formed page of
    zeroes -- the most convincing way possible to hide an incident."""
    code, _headers, body = _get(health + "/metrics")
    assert code == 200
    text = body.decode()
    assert _metric(text, "acme_diff_preview_section_cap_trims_total") == \
        scrapeable_counter


def test_metrics_is_served_as_prometheus_exposition(health):
    """Not application/json, not text/html. GMP and a Datadog OpenMetrics
    check both key off this header; get it wrong and the target is up, the
    scrape succeeds, and no series is ever created."""
    _code, headers, _body = _get(health + "/metrics")
    assert headers["Content-Type"] == "text/plain; version=0.0.4; charset=utf-8"


def test_metrics_declares_the_length_of_what_it_sent(health):
    """A Content-Length shorter than the body truncates the page: the client
    stops reading where the header told it to, and the last metric arrives as
    a fragment. One malformed line makes a scraper reject the entire scrape,
    so the page has to arrive whole or not at all."""
    _code, headers, body = _get(health + "/metrics")
    text = body.decode()
    assert int(headers["Content-Length"]) == len(body)
    assert text.endswith("\n")
    values = [ln for ln in text.split("\n") if ln and not ln.startswith("#")]
    types = [ln for ln in text.split("\n") if ln.startswith("# TYPE ")]
    assert values and len(values) == len(types), \
        "the page is cut short: %d declared metrics, %d values" % (
            len(types), len(values))
    for ln in values:
        float(ln.rsplit(" ", 1)[1])   # every line arrived complete


def test_metrics_stamps_which_replica_is_leading(health, monkeypatch):
    """is_leader is NOT one of the stats counters -- the handler computes it
    per request. Both replicas are scraped, and without this stamp a fleet
    of counters cannot be told apart from a standby's idle zeroes."""
    monkeypatch.setattr(m, "_leader", _FakeElector(True))
    _c, _h, body = _get(health + "/metrics")
    assert _metric(body.decode(), "acme_diff_preview_is_leader") == 1

    monkeypatch.setattr(m, "_leader", _FakeElector(False))
    _c, _h, body = _get(health + "/metrics")
    assert _metric(body.decode(), "acme_diff_preview_is_leader") == 0


def test_metrics_reflects_a_counter_that_moves_between_scrapes(health):
    """Two scrapes of the same live counter must differ by what happened in
    between. This is the property increase() depends on, and the one a
    cached or pre-rendered page would quietly break."""
    with m._diff_stats_lock:
        before = m._diff_stats["diff_retries"]
    try:
        _c, _h, first = _get(health + "/metrics")
        with m._diff_stats_lock:
            m._diff_stats["diff_retries"] = before + 7
        _c, _h, second = _get(health + "/metrics")
        name = "acme_diff_preview_diff_retries_total"
        assert _metric(second.decode(), name) - _metric(first.decode(), name) == 7
    finally:
        with m._diff_stats_lock:
            m._diff_stats["diff_retries"] = before


# ── 3. the COPS-2633 ordering guard on the peek side ─────────────────────

REPO, BASE = "acme-config-prod", "main"
MERGED = "bb12eea854a3"      # merge commit of an earlier PR
TIP = "96145380874c"         # where main actually is, per the poller
OTHER = "0000dead0000"


class _UndeletableHints(dict):
    """_base_superseded whose retirement fails.

    _note_base_observed records the observation first and retires the
    overtaken hint second, with the whole body inside a swallow-everything
    block because the poll loop must never break on hint bookkeeping. So a
    failure in between is silent and leaves a hint the poller has already
    moved past -- the pre-COPS-2633 state, which cost PR #2802 three skipped
    iterations. The peek side has to hold on its own.
    """
    def __delitem__(self, key):
        raise RuntimeError("simulated failure while retiring the hint")


@pytest.fixture()
def surviving_stale_hint(monkeypatch):
    monkeypatch.setattr(m, "_base_superseded", _UndeletableHints())
    monkeypatch.setattr(m, "_base_observed", {})
    m._record_base_hint(REPO, BASE, MERGED)      # merge webhook
    m._note_base_observed(REPO, BASE, TIP)       # poller reads the real tip
    assert (REPO, BASE) in m._base_superseded, \
        "fixture precondition: the hint must have survived retirement"
    return MERGED


def test_a_hint_the_poller_already_overtook_cannot_abort_this_snapshot(
        surviving_stale_hint):
    """base_sha IS the tip the poller read, and the hint predates that read,
    so it describes a move this snapshot already contains. Aborting on it is
    the COPS-2633 bug: `main advanced (96145380 -> bb12eea8)` -- backwards --
    repeated until the livelock guard gave up."""
    assert m._base_superseded_by(REPO, BASE, TIP) is None


def test_the_ordering_guard_is_scoped_to_the_observed_snapshot(
        surviving_stale_hint):
    """It must not degrade into 'a hint is never a supersede'. For a
    snapshot the poller has NOT confirmed, the pre-COPS-2633 answer stands
    and the hint still supersedes."""
    assert m._base_superseded_by(REPO, BASE, OTHER) == MERGED


def test_a_hint_recorded_after_the_observation_still_aborts(monkeypatch):
    """The other side of the ordering test, on the same surviving-hint
    state: a merge that lands after the poll is genuine news."""
    monkeypatch.setattr(m, "_base_superseded", _UndeletableHints())
    monkeypatch.setattr(m, "_base_observed", {})
    m._note_base_observed(REPO, BASE, TIP)
    m._record_base_hint(REPO, BASE, OTHER)
    assert m._base_superseded_by(REPO, BASE, TIP) == OTHER


# ── 4. the OCI self-check fallback probe is only ever a second opinion ───

@pytest.fixture()
def selfcheck_state():
    with m._oci_health_lock:
        backup = {k: m._diff_stats[k] for k in
                  ("oci_selfcheck", "oci_selfcheck_at",
                   "oci_consecutive_pull_failures")}
    yield
    with m._oci_health_lock:
        m._diff_stats.update(backup)


def test_a_fallback_probe_that_explodes_leaves_the_original_verdict(
        selfcheck_state, monkeypatch):
    """The configured reference failed, so the check re-probes with a chart
    this pod really pulled. If that second probe raises -- no space for its
    temp helm home, the helm binary gone, a fork that fails under memory
    pressure -- the self-check must still return the FAILED verdict it had.
    It runs on a daemon timer whose caller logs nothing, so an escaping
    exception would end the self-check loop for the life of the pod."""
    monkeypatch.setenv("DIFF_OCI_SELFCHECK_REF",
                       "reg.pinned.example/retired-chart:1.0.0")
    monkeypatch.setattr(m, "_last_pull_ok_ref",
                        ("reg.real.example", "appspace-micro-services", "9.9.9"))
    monkeypatch.setattr(m, "_helm_login", lambda registry: True)
    logged = []
    monkeypatch.setattr(logsink, "DEBUG", True)
    monkeypatch.setattr(logsink, "log",
                        lambda msg, sev="INFO", **kw: logged.append((sev, msg)))

    probes = []

    def fake_run(cmd, **kw):
        probes.append(cmd)
        if any("retired-chart" in str(c) for c in cmd):
            class R:
                returncode = 1
                stdout = ""
                stderr = "chart not found"
            return R()
        raise OSError("Cannot allocate memory")

    monkeypatch.setattr(m.subprocess, "run", fake_run)

    assert m._oci_selfcheck() is False, "a broken second opinion is not a pass"
    assert m._diff_stats["oci_selfcheck"] == "failed"
    assert len(probes) == 2, "the fallback probe must have been attempted"

    assert any(sev == "DEBUG" and "fallback probe failed" in msg
               and "Cannot allocate memory" in msg for sev, msg in logged), \
        "the reason the second opinion was unavailable must be recoverable " \
        "from the logs, not swallowed silently: %r" % (logged,)


def test_the_failure_reported_is_the_configured_reference(selfcheck_state,
                                                          monkeypatch):
    """The ERROR line is what pages someone. When the fallback never
    produced an answer, that line must still name the reference that
    actually failed, not the chart the probe was going to try."""
    monkeypatch.setenv("DIFF_OCI_SELFCHECK_REF",
                       "reg.pinned.example/retired-chart:1.0.0")
    monkeypatch.setattr(m, "_last_pull_ok_ref",
                        ("reg.real.example", "appspace-micro-services", "9.9.9"))
    monkeypatch.setattr(m, "_helm_login", lambda registry: True)
    logged = []
    monkeypatch.setattr(logsink, "log",
                        lambda msg, sev="INFO", **kw: logged.append((sev, msg)))

    def fake_run(cmd, **kw):
        if any("retired-chart" in str(c) for c in cmd):
            class R:
                returncode = 1
                stdout = ""
                stderr = "chart not found"
            return R()
        raise OSError("Cannot allocate memory")

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    m._oci_selfcheck()

    errors = [msg for sev, msg in logged if sev == "ERROR"]
    assert errors, logged
    assert any("retired-chart:1.0.0" in msg for msg in errors), errors
    assert not any("appspace-micro-services:9.9.9" in msg for msg in errors), \
        "the unfinished fallback must not be reported as the thing that failed"


# ── 5. a subprocess timeout is infrastructure, not a broken PR ───────────

def test_a_helm_timeout_is_published_as_transient(world, monkeypatch):
    """A `helm template` or `argocd` call that outruns its timeout is the
    single most common real hiccup here (a busy repo-server). COPS-2668:
    the token in the comment is durable and cross-replica, so classifying
    this as permanent would freeze the PR's verdict until someone pushes."""
    sinks, _plan = world

    def _raise(*a, **k):
        raise subprocess.TimeoutExpired(cmd=["helm", "template", "."],
                                        timeout=300)
    monkeypatch.setattr(m, "get_pr_changed_files", _raise)
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)

    assert sinks.upserts, "the catch-all must still post a comment"
    body = sinks.upserts[-1]
    assert "[transient]" in body, body[-400:]
    assert "[permanent]" not in body


def test_a_subprocess_that_failed_fast_stays_permanent():
    """The contrast that keeps the timeout branch honest: it is about
    running out of time, not about 'anything subprocess raised'. A helm
    template that exits non-zero is a real, stable problem with the PR."""
    assert m._is_transient_exception(
        subprocess.TimeoutExpired(cmd=["helm", "template", "."], timeout=300)) \
        is True
    assert m._is_transient_exception(
        subprocess.CalledProcessError(1, ["helm", "template", "."])) is False
