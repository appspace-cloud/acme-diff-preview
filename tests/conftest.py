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
