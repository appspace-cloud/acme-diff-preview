"""v2.13.1: put http() behind the shared Bitbucket gate, stop hiding BB_ERROR.

Follow-up to v2.13.0 (COPS-2543), driven by measuring what production
actually logged rather than what we assumed it logged.

Two findings.

1. **Every 429 we can see comes from `http()`, not from the path v2.13.0
   fixed.** 211 real 429s in 7 days, all of them `[http] 429 on GET`, i.e.
   the poll loop: `refs/branches/main` per repo, the PR listing, comments,
   build status. `_bb_fetch_status()` (value files) logged its 429s at
   `debug()`, and production runs at `LOG_LEVEL=INFO`, so that path has
   always been invisible.

   And `http()` carries exactly the defect v2.13.0 removed from
   `_bb_fetch_status()`: it honors `Retry-After` only when the header is
   present, and Bitbucket is not sending it on these endpoints (the logs read
   `retry 1/2 in 1s`, `retry 2/2 in 2s`). That is ~3s of total backoff against
   a ~60s window, so a rate-limited call burns both retries inside the window
   that rejected it and then raises. It also never joined the shared gate, so
   `http()` and the value-file path could not brake for each other.

   `http()` is NOT Bitbucket-only, though — it also serves the GCP metadata
   server and Vertex AI. A 429 from Vertex says nothing about our Bitbucket
   budget, so gate participation has to be host-aware, which is what
   `_is_bb_url()` is for.

2. **An unreadable value file was reported as an absent one.**
   `_fetch_value_files` collected every empty result into one `missing` list
   and logged `value files not found at sha ...` at `debug()`. A 429 that
   exhausted its retries and a genuine 404 are completely different events:
   the first means the render is about to fail for a reason that has nothing
   to do with the PR, the second is normal (new cluster not yet on main).
   Conflating them at debug level is why we could not tell, from the logs
   alone, whether the `missing_required` errors of 2026-07-28 were caused by
   rate limiting or by a real gap in the values hierarchy.
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
import diff_preview as m  # noqa: E402


BB_URL     = "https://api.bitbucket.org/2.0/repositories/ws/acme-config-dev/refs/branches/main"
VERTEX_URL = "https://us-central1-aiplatform.googleapis.com/v1/projects/p/locations/l:generateContent"
META_URL   = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"


def _http_error(code, headers=None, url=BB_URL):
    return urllib.error.HTTPError(url, code, "boom", headers or {}, io.BytesIO(b""))


@pytest.fixture(autouse=True)
def _clear_gate():
    m._bb_ratelimit_clear()
    yield
    m._bb_ratelimit_clear()


@pytest.fixture
def scripted(monkeypatch):
    """Script urlopen's failures and record every sleep the code performs."""
    def _install(*errors):
        seq = list(errors)
        sleeps = []
        calls = {"n": 0}

        def fake_urlopen(*a, **k):
            calls["n"] += 1
            raise seq.pop(0) if seq else _http_error(429)

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(m.time, "sleep", lambda s: sleeps.append(s))
        return calls, sleeps
    return _install


# ── _is_bb_url: gate participation must be host-aware ────────────────────

def test_bitbucket_api_urls_are_recognised():
    assert m._is_bb_url(BB_URL)
    assert m._is_bb_url("https://api.bitbucket.org/2.0/repositories/ws/r/pullrequests")


def test_non_bitbucket_urls_are_not_gated():
    # http() also serves Vertex AI and the GCP metadata server. Their 429s say
    # nothing about our Bitbucket budget and must not pause Bitbucket calls.
    assert not m._is_bb_url(VERTEX_URL)
    assert not m._is_bb_url(META_URL)
    # Belt and braces against a hostname that merely *contains* the real one.
    assert not m._is_bb_url("https://api.bitbucket.org.evil.test/2.0/x")


def test_a_malformed_url_is_treated_as_not_bitbucket():
    # urlsplit raises ValueError on an unclosed IPv6 bracket. Bitbucket hands us
    # the pagination `next` link verbatim (see the paged GET in _bb_paged), so a
    # malformed URL is server-supplied input, not a programming error. Failing
    # closed here keeps a bad link from tripping the shared gate for everyone.
    assert not m._is_bb_url("http://[::1")


def test_a_malformed_url_logs_as_itself():
    # Same input on the logging path: no crash, and no silent empty endpoint.
    assert m._log_endpoint("http://[::1") == "http://[::1"


# ── http() on a Bitbucket 429 ────────────────────────────────────────────

def test_bb_429_without_retry_after_waits_a_window_sized_pause(scripted):
    # THE production bug: Bitbucket does not send Retry-After on these
    # endpoints, so the old code fell back to 2**attempt = 1s then 2s. A ~60s
    # window laughs at 3s of total backoff.
    calls, sleeps = scripted(_http_error(429), _http_error(429), _http_error(429))
    with pytest.raises(urllib.error.HTTPError):
        m.http("GET", BB_URL)
    assert max(sleeps) >= m.BB_RATELIMIT_FALLBACK - 1, (
        f"a headerless 429 must wait ~{m.BB_RATELIMIT_FALLBACK}s, slept {sleeps}")
    assert 1 not in sleeps and 2 not in sleeps, (
        f"the old 1s/2s exponential backoff is still in play: {sleeps}")


def test_bb_429_honors_retry_after_when_present(scripted):
    calls, sleeps = scripted(
        _http_error(429, {"Retry-After": "37"}),
        _http_error(429, {"Retry-After": "37"}),
        _http_error(429, {"Retry-After": "37"}),
    )
    with pytest.raises(urllib.error.HTTPError):
        m.http("GET", BB_URL)
    assert max(sleeps) == pytest.approx(37, abs=1.0), (
        f"Retry-After: 37 must be honored, slept {sleeps}")


def test_bb_429_caps_a_hostile_retry_after(scripted):
    # A broken or hostile header must not be able to stall the poll loop.
    calls, sleeps = scripted(_http_error(429, {"Retry-After": "99999"}))
    with pytest.raises(urllib.error.HTTPError):
        m.http("GET", BB_URL)
    assert max(sleeps) <= m.BB_RATELIMIT_MAX_PAUSE + 1, (
        f"pause must be capped at {m.BB_RATELIMIT_MAX_PAUSE}s, slept {sleeps}")


def test_bb_429_publishes_the_shared_pause(monkeypatch):
    # A 429 is a property of the token. http() learning it must brake the
    # value-file path too, not just itself.
    def fake_urlopen(*a, **k):
        raise _http_error(429, {"Retry-After": "30"})
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    with pytest.raises(urllib.error.HTTPError):
        m.http("GET", BB_URL)
    assert m._bb_ratelimit_remaining() > 0, (
        "http() must publish its 429 to the shared gate")


def test_http_brakes_for_a_pause_published_by_the_value_file_path(scripted):
    # Symmetry: the value-file path hit the 429 first, http() must not walk
    # straight into the same closed window and spend an attempt learning it.
    calls, sleeps = scripted(None)   # never reached if the gate is honored first
    m._bb_ratelimit_hold(25)
    try:
        m.http("GET", BB_URL)
    except Exception:
        pass
    assert sleeps and max(sleeps) >= 24, (
        f"http() ignored an active shared pause, slept {sleeps}")


def test_the_429_log_line_names_the_endpoint(scripted, capsys):
    # "429 on GET" was all production ever said, so identifying which call was
    # being rejected meant correlating by timestamp against the iteration log.
    scripted(_http_error(429))
    with pytest.raises(urllib.error.HTTPError):
        m.http("GET", BB_URL)
    out = capsys.readouterr().out
    assert "refs/branches/main" in out, (
        f"the 429 line must name the endpoint, got:\n{out}")
    assert '"severity": "WARNING"' in out


# ── http() on a non-Bitbucket 429: unchanged, and no gate side effects ───

def test_vertex_429_does_not_pause_bitbucket(scripted):
    calls, sleeps = scripted(
        _http_error(429, url=VERTEX_URL),
        _http_error(429, url=VERTEX_URL),
        _http_error(429, url=VERTEX_URL),
    )
    with pytest.raises(urllib.error.HTTPError):
        m.http("POST", VERTEX_URL)
    assert m._bb_ratelimit_remaining() == 0, (
        "a Vertex 429 must not brake Bitbucket calls")
    assert sleeps == [1, 2], (
        f"non-Bitbucket backoff must stay 2**attempt, got {sleeps}")


def test_bb_5xx_keeps_per_request_backoff_and_does_not_brake_the_pool(scripted):
    # One sick request is not a spent budget.
    calls, sleeps = scripted(_http_error(503), _http_error(503), _http_error(503))
    with pytest.raises(urllib.error.HTTPError):
        m.http("GET", BB_URL)
    assert sleeps == [1, 2], f"5xx backoff must stay 2**attempt, got {sleeps}"
    assert m._bb_ratelimit_remaining() == 0, "a 5xx must not pause the whole pool"


# ── _fetch_value_files: unreadable is not the same as absent ─────────────

def _fetch_with_status(monkeypatch, status, content=None):
    """Drive _fetch_value_files with a fixed _bb_fetch_status outcome."""
    m._vf_cache.clear()
    monkeypatch.setattr(m, "_bb_fetch_status", lambda *a, **k: (content, status))
    return m._fetch_value_files(["$config/gcp/dev/x/config.yaml"], "a" * 40)


def test_unreadable_value_file_is_reported_at_warning_and_not_called_missing(
        monkeypatch, capsys):
    # A 429 that exhausted its retries is NOT "file not found". Reporting it as
    # such at debug level is why we could not tell rate limiting apart from a
    # real gap in the values hierarchy.
    _fetch_with_status(monkeypatch, m.BB_ERROR)
    out = capsys.readouterr().out
    assert '"severity": "WARNING"' in out, (
        f"an unreadable value file must be visible at INFO, got:\n{out}")
    assert "gcp/dev/x/config.yaml" in out
    assert "not found" not in out, (
        f"a transient failure must not be reported as absence:\n{out}")


def test_absent_value_file_stays_quiet(monkeypatch, capsys):
    # A 404 is normal and expected (new cluster not yet merged to main), so it
    # must not start emitting warnings just because BB_ERROR now does.
    _fetch_with_status(monkeypatch, m.BB_NOT_FOUND)
    out = capsys.readouterr().out
    assert '"severity": "WARNING"' not in out, (
        f"a genuine 404 must not warn, got:\n{out}")


def test_successful_fetch_reports_nothing(monkeypatch, capsys):
    result = _fetch_with_status(monkeypatch, m.BB_OK, content="key: value")
    assert result == {"$config/gcp/dev/x/config.yaml": "key: value"}
    assert '"severity": "WARNING"' not in capsys.readouterr().out
