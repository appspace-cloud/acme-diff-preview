"""Suite-wide test configuration.

v2.5.20 (E1): HTTP connection pooling is DISABLED for the whole suite.

Rationale: every network-touching test in this suite mocks at the
urllib.request.urlopen seam (the transport used since v1). The pooled
path (_pooled_urlopen -> http.client.HTTPSConnection) sits IN FRONT of
that seam, so with pooling on, a test that mocks urlopen would first
attempt a REAL TLS connection to api.bitbucket.org before falling back
to the mock — a live network call inside the test suite, the exact
failure class fixed in acme-mcp v4.16.6 (tests must be zero-network by
assertion, not by accident).

Setting DIFF_HTTP_POOLING=off here (before diff_preview is imported by
any test module) makes _pooled_urlopen defer straight to urlopen,
restoring the exact pre-E1 transport that all existing mocks assume.

Tests that exercise the pool itself (tests/test_v2520_http_pooling.py)
opt back in by monkeypatching HTTP_POOLING_ENABLED and stubbing
http.client.HTTPSConnection with fakes — still zero-network.
"""
import os

os.environ["DIFF_HTTP_POOLING"] = "off"


# ── COPS-2546: hermetic fetch cache between tests ───────────────────────────
#
# _vf_cache is keyed by (commit sha, path) and lives for the whole process,
# which is correct in production: content at a git sha is immutable, so a hit
# is always valid and the cache is what keeps Bitbucket API usage down.
#
# In tests it is a trap. Test modules share short fake shas ("prsha",
# "cafe0001", ...) and the same fixture paths, so a value cached by one test
# silently answers the next one and its monkeypatched fetcher is never called.
# That produces failures that only appear in a full-suite run and vanish when
# the test is run alone, which is the worst possible signal for a suite that
# gates releases.
#
# Clearing before and after every test makes each one hermetic regardless of
# ordering. Tests that populate the cache on purpose do so inside their own
# body, after this fixture has run.
import pytest


@pytest.fixture(autouse=True)
def _clear_sha_fetch_cache():
    import diff_preview as _m
    # _retry_backoff is module-level too: a transient failure registered by one
    # test would otherwise make the next test's PR be skipped before it does
    # any work, which surfaces as an empty result list far from the cause.
    # _yaml_cache (COPS-2562) shares the (sha, path) keying of _vf_cache and
    # therefore the same cross-test poisoning risk: two tests faking the same
    # sha with different content must never see each other's parse.
    for d in (_m._vf_cache, _m._vf_inflight, _m._retry_backoff, _m._yaml_cache):
        d.clear()
    yield
    for d in (_m._vf_cache, _m._vf_inflight, _m._retry_backoff, _m._yaml_cache):
        d.clear()


# ── COPS-2595: no real waiting in the test suite ────────────────────────────
#
# Measured before this fixture existed: the full suite took ~20 minutes, and
# the 40 slowest tests accounted for ~1,087s of the 1,224s total. Profiling a
# single 29.4s test showed 27.0s of it inside time.sleep, with real TLS
# handshakes alongside it.
#
# Tracing every sleep to its call site found the cause: those tests escape the
# urlopen mock seam, reach the REAL Bitbucket and GCP endpoints, fail, and then
# retry with hardcoded backoff:
#
#     _bb_fetch_cached -> _bb_fetch_status  18.0s   ((attempt + 1) * 2)
#     _pr_chart_revision_checked -> same    12.0s
#     _gcp_access_token -> http             3.0s    (2 ** attempt)
#
# Those waits are hardcoded, not env-tunable, so they cannot be turned down
# from the outside. Neutralising sleep took that one test from 29.40s to 2.40s.
#
# Scope, stated precisely: diff_preview does `import time`, so _m.time IS the
# stdlib time module and this patch is process-wide for the DURATION OF ONE
# TEST. monkeypatch reverts it at teardown, so it never leaks between tests.
#
# The retry loops still execute in full: same number of attempts, same
# branches, same log lines, same assertions. Nothing is skipped and no test is
# weakened -- the only thing removed is dead wall-clock waiting for a network
# the test never meant to touch. It also stops a genuinely non-hermetic test
# from hiding behind a slow retry instead of failing visibly.
#
# Tests that need real elapsed time -- real threads, leader-election races,
# anything where another thread must actually make progress during the wait --
# opt out with @pytest.mark.realtime and get the untouched time.sleep.
@pytest.fixture(autouse=True)
def _no_real_sleep(request, monkeypatch):
    if request.node.get_closest_marker("realtime"):
        yield          # genuine timing test: leave time.sleep alone
        return
    import diff_preview as _m
    monkeypatch.setattr(_m.time, "sleep", lambda _s: None)
    yield
