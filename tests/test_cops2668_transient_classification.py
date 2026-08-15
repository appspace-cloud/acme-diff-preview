"""A transient failure must never be published as a permanent verdict (COPS-2668).

`process_pr` wraps its whole body in one catch-all, and that handler hardcoded
`[permanent]` into the comment it posts for ANY exception. So a Bitbucket 429
or a 502 that outlived its retries — an infrastructure hiccup with nothing to
do with the PR — reached the author as:

    ❌ Error processing diff: HTTP Error 429: Too Many Requests
    Status: ❌ Error running diff        [permanent]

Two things follow from that token, and both are wrong for a 429:

1. It blames the author for an outage. The comment reads as "your PR is
   broken", and the build status goes FAILED next to it.
2. It suppresses the retry. Since `_extract_status_token` learned to read the
   token properly, `[permanent]` makes the next iteration skip the PR — and
   not only on this pod: the token lives in the durable comment, so every
   replica and every future pod reads the same verdict. A one-minute rate
   limit becomes a verdict that never re-evaluates itself until someone
   pushes a new commit.

The service already distinguishes these two classes everywhere else
(RETRYABLE_REASONS vs PERMANENT_REASONS, `_backoff_register_transient`). The
catch-all was the one place that did not.
"""
import os
import sys
import urllib.error

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m

from test_coverage_orchestration import world, _mk_pr, PATH_MAP, BASE_SHA  # noqa: E402,F401


def _http_error(code):
    return urllib.error.HTTPError("https://api.bitbucket.org/x", code,
                                  "boom", {}, None)


def _run_with_failure(world, monkeypatch, exc):
    """Drive the real process_pr into its catch-all with a chosen exception."""
    sinks, _plan = world

    def _raise(*a, **k):
        raise exc
    monkeypatch.setattr(m, "get_pr_changed_files", _raise)
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    assert sinks.upserts, "the catch-all must still post a comment"
    return sinks.upserts[-1]


# ── the transport failures that must stay retryable ──────────────────────

def test_rate_limit_is_transient(world, monkeypatch):
    body = _run_with_failure(world, monkeypatch, _http_error(429))
    assert "[transient]" in body, "a 429 is an outage, not a broken PR"
    assert "[permanent]" not in body


def test_bad_gateway_is_transient(world, monkeypatch):
    body = _run_with_failure(world, monkeypatch, _http_error(502))
    assert "[transient]" in body


def test_service_unavailable_is_transient(world, monkeypatch):
    body = _run_with_failure(world, monkeypatch, _http_error(503))
    assert "[transient]" in body


def test_connection_failure_is_transient(world, monkeypatch):
    body = _run_with_failure(world, monkeypatch,
                             urllib.error.URLError("connection refused"))
    assert "[transient]" in body


def test_timeout_is_transient(world, monkeypatch):
    body = _run_with_failure(world, monkeypatch, TimeoutError("read timed out"))
    assert "[transient]" in body


# ── the failures that genuinely are the PR's fault ───────────────────────

def test_not_found_stays_permanent(world, monkeypatch):
    """A 404 is a real, stable problem — the PR or repo is wrong."""
    body = _run_with_failure(world, monkeypatch, _http_error(404))
    assert "[permanent]" in body
    assert "[transient]" not in body


def test_programming_error_stays_permanent(world, monkeypatch):
    """A bug in our own code must not be retried forever."""
    body = _run_with_failure(world, monkeypatch, ValueError("bad value"))
    assert "[permanent]" in body


def test_forbidden_stays_permanent(world, monkeypatch):
    """403 is a credential/permission problem: retrying will not fix it."""
    body = _run_with_failure(world, monkeypatch, _http_error(403))
    assert "[permanent]" in body


# ── the comment must stay honest about which it is ───────────────────────

def test_transient_comment_says_it_will_retry(world, monkeypatch):
    """The human half of the comment must match the machine token, or the
    author reads 'error' and starts debugging an outage."""
    body = _run_with_failure(world, monkeypatch, _http_error(429))
    assert "retry" in body.lower() or "transient" in body.lower(), (
        "a transient error must tell the reader it will be retried "
        "automatically: %r" % body[-400:])
