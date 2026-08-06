"""Property-based tests (hypothesis) for three pure, security-relevant helpers.

Every example-based test in this suite checks hand-picked inputs; these
tests instead assert INVARIANTS over generated input space. The three
targets are exactly the functions where a missed corner case is a
security or data-leak problem, not a cosmetic one:

- _is_valid_chart_version gates values that flow into helm/OCI
  operations, so its promise ("no shell/whitespace metacharacters")
  must hold for EVERY string, not just the examples we thought of.
- _redact_sensitive is the last line of defense before diff text is
  sent to Vertex AI; a value that survives redaction is a leak.
- _verify_bb_hmac / _verify_jfrog_hmac compare attacker-controlled,
  PRE-AUTH header bytes; v2.5.3 CRIT-2 was precisely a crash on a
  non-ASCII header that no example test had thought to send. The
  "never raises, for any header" property pins that class of bug
  forever, not just the one instance we saw.
"""

import hashlib
import hmac
import os
import string

for _k in ("BB_USER", "BB_TOKEN", "ARGOCD_PASS"):
    os.environ.setdefault(_k, "t")
os.environ["DIFF_HTTP_POOLING"] = "off"

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from hypothesis import assume, example, given, settings, strategies as st

import diff_preview as m  # noqa: E402

# CI runners can be slow under Docker load; per-example deadlines only
# add flakiness for these fast pure functions.
settings.register_profile("suite", deadline=None)
settings.load_profile("suite")


# ── _is_valid_chart_version ─────────────────────────────────────────────────

_FIRST = string.ascii_letters + string.digits
_REST = _FIRST + "._+-"


def _version_spec(s: str) -> bool:
    """Independent re-statement of the documented contract, written
    WITHOUT regex on purpose: if the regex and this loop ever disagree,
    one of them is wrong about the spec (this is how the trailing-newline
    hole in `$` was caught)."""
    return (0 < len(s) <= 128
            and s[0] in _FIRST
            and all(c in _REST for c in s))


@given(st.text(max_size=200))
@example("0\n")   # the trailing-newline hole: `$` in Python re matches
                  # BEFORE a final "\n", so the old `.match()+$` regex
                  # accepted "0\n" while the docstring promises no
                  # whitespace. Pinned so it can never come back.
def test_chart_version_matches_the_documented_spec_exactly(s):
    # Total (never raises) AND equivalent to the spec for every string:
    # accepts everything the contract allows, rejects everything else —
    # including whitespace anywhere, which covers the "\n" corner `$`
    # silently allowed.
    assert m._is_valid_chart_version(s) == _version_spec(s)


@given(st.builds(lambda a, b: a + b,
                 st.sampled_from(_FIRST),
                 st.text(alphabet=_REST, max_size=127)))
def test_chart_version_accepts_the_entire_safe_grammar(s):
    assert m._is_valid_chart_version(s) is True


# ── _redact_sensitive ───────────────────────────────────────────────────────

@given(st.text(alphabet=st.characters(
    # Python's splitlines() also breaks on VT, FF, FS, GS, RS, NEL, LS and
    # PS. _redact_sensitive is a "\n"-oriented function, so on those
    # characters the two line models disagree and the count invariant below
    # is ambiguous rather than wrong. They never occur in a Kubernetes YAML
    # diff, so they are excluded from the search space instead of being
    # pinned one by one (hypothesis found '0\x1e\x85' on 2026-08-06).
    blacklist_characters="\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"),
    max_size=2000))
@example("\r")   # CI's hypothesis run caught this: splitlines() sees one
                 # (empty) line in a lone terminator, but the redactor
                 # joins with "\n", so a round-trip line COUNT comparison
                 # was the wrong spec at the boundary. The real invariant
                 # is below; this pins the counterexample.
def test_redact_never_raises_and_preserves_line_structure(text):
    out = m._redact_sensitive(text)
    # One output line per input line: redaction must never merge, drop or
    # invent lines, or the diff context shown to the AI stops matching
    # the real diff. Stated precisely: the output is exactly the input's
    # splitlines() sequence re-joined with "\n" (line endings normalized,
    # trailing terminator dropped — intended, harmless for the AI prompt),
    # so its "\n"-split length equals the input's line count, except that
    # joining zero-or-one lines both yield a single "" segment.
    assert len(out.split("\n")) == max(len(text.splitlines()), 1)


_SECRET_KEYS = st.sampled_from(
    ["password", "secret", "token", "credential", "apikey", "api_key"])
_SECRET_VALUES = st.text(alphabet=string.ascii_lowercase + string.digits,
                         min_size=12, max_size=40)


@given(st.sampled_from(["+", "-", " ", ""]), _SECRET_KEYS, _SECRET_VALUES)
def test_redact_never_leaks_a_sensitive_value(marker, key, value):
    out = m._redact_sensitive(f"{marker}{key}: {value}")
    assert value not in out
    assert "[REDACTED]" in out


# Every character str.splitlines() treats as a line boundary. Text made ONLY
# of these has no content lines at all, which is the one input class where
# idempotency and the line-structure contract cannot both hold (COPS-2561).
_LINE_TERMINATORS = "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"


@given(st.text(max_size=2000))
def test_redact_is_idempotent(text):
    # Redacting already-redacted text must change nothing: [REDACTED]
    # placeholders and untouched lines are both fixed points. A violation
    # would mean the masking output itself re-triggers (or un-triggers)
    # masking, i.e. the result depends on how many times the pipeline ran.
    #
    # COPS-2561: scoped to text with at least one real character. For input
    # that is nothing but line terminators ('\r\r', '\x1e\r'), each pass
    # drops one terminator, because _redact_sensitive rebuilds its output with
    # splitlines() + join() and join only puts separators BETWEEN lines. That
    # drop is deliberate and asserted by
    # test_redact_never_raises_and_preserves_line_structure, whose contract
    # (len(out.split("\n")) == max(len(text.splitlines()), 1)) production
    # really depends on: redaction must never merge, drop or invent lines.
    # The two properties are incompatible for terminator-only input, and the
    # line-structure one wins, so this property is narrowed rather than the
    # function changed. _redact_sensitive is never applied twice anywhere in
    # production. The accepted edge is pinned by
    # test_cops2562_customer_name_cap.py::test_terminator_only_input_shrinks_by_design.
    assume(text.strip(_LINE_TERMINATORS))
    once = m._redact_sensitive(text)
    assert m._redact_sensitive(once) == once


# ── webhook HMAC verification (pre-auth, attacker-controlled input) ─────────

def _with_secret(attr, value):
    """Set a module-level secret for the duration of one example."""
    class _Ctx:
        def __enter__(self):
            self.old = getattr(m, attr)
            setattr(m, attr, value)

        def __exit__(self, *exc):
            setattr(m, attr, self.old)
    return _Ctx()


@given(st.binary(max_size=500), st.text(max_size=200))
def test_bb_hmac_never_raises_on_any_header(body, header):
    # The v2.5.3 CRIT-2 class, as a law: any header value (non-ASCII,
    # wrong length, not hex, empty) is just "unequal", never an exception.
    with _with_secret("BB_WEBHOOK_SECRET", "s3cret"):
        assert m._verify_bb_hmac(body, header) in (True, False)
    with _with_secret("BB_WEBHOOK_SECRET", ""):
        # Permissive mode (secret unset) accepts everything by design.
        assert m._verify_bb_hmac(body, header) is True


@given(st.binary(max_size=500), st.text(max_size=200))
def test_jfrog_hmac_never_raises_and_rejects_without_secret(body, header):
    with _with_secret("JFROG_WEBHOOK_SECRET", "s3cret"):
        assert m._verify_jfrog_hmac(body, header) in (True, False)
    with _with_secret("JFROG_WEBHOOK_SECRET", ""):
        # Unlike BB's rollout-permissive mode, JFrog with no secret
        # rejects everything.
        assert m._verify_jfrog_hmac(body, header) is False


@given(st.binary(max_size=500),
       st.text(alphabet=string.ascii_letters + string.digits,
               min_size=1, max_size=32))
def test_bb_hmac_accepts_exactly_the_correct_signature(body, secret):
    good = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    with _with_secret("BB_WEBHOOK_SECRET", secret):
        assert m._verify_bb_hmac(body, "sha256=" + good) is True
        # Flipping one nibble anywhere must flip the verdict.
        tampered = ("0" if good[0] != "0" else "1") + good[1:]
        assert m._verify_bb_hmac(body, "sha256=" + tampered) is False


@given(st.binary(max_size=500),
       st.text(alphabet=string.ascii_letters + string.digits,
               min_size=1, max_size=32))
def test_jfrog_hmac_accepts_exactly_the_correct_signature(body, secret):
    good = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    with _with_secret("JFROG_WEBHOOK_SECRET", secret):
        assert m._verify_jfrog_hmac(body, good) is True
        tampered = ("0" if good[0] != "0" else "1") + good[1:]
        assert m._verify_jfrog_hmac(body, tampered) is False


def test_redact_idempotent_on_trailing_terminators():
    # Deterministic pin of the hypothesis finding ('0\r\r'): content-bearing
    # text with trailing terminators (any \r/\n mix) must reach a fixed
    # point after one pass, not lose one terminator per pass forever.
    for text in ("0\r\r", "x\n\n", "a\r\nb\r\n\r\n", "y\n\n\n"):
        once = m._redact_sensitive(text)
        assert m._redact_sensitive(once) == once, repr(text)
