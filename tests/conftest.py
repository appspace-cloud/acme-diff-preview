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
    for d in (_m._vf_cache, _m._vf_inflight, _m._retry_backoff):
        d.clear()
    yield
    for d in (_m._vf_cache, _m._vf_inflight, _m._retry_backoff):
        d.clear()
