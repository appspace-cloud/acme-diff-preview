"""Regression tests for the v2.5.3 hardening round (adversarial campaign round 3).

Each test encodes one finding from the July 2026 real-PR campaign against
acme-config-dev (PR #6637, #6638) and the local fuzzing pass. Must FAIL
against the pre-fix code, then PASS once the fix lands. Pure-function level,
no network, except the dedicated live-server test for the HMAC fix.
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m


# ── CRIT-1: duplicate appspace.version key must resolve to the LAST
#    occurrence (YAML/Helm last-key-wins), not the first ─────────────────
def test_duplicate_version_key_last_wins():
    # Confirmed live on PR #6637: first occurrence unchanged (2603.0.0-dev),
    # second (real, per `helm template` on a real chart) bumps to
    # 2603.0.1-ap-65990-dev. The extractor must report the SECOND value.
    content = (
        "argoId: dev11\n"
        "appspace:\n"
        "  customerName: dev11\n"
        "  version: 2603.0.0-dev\n"
        "  description: whatever\n"
        "  version: 2603.0.1-ap-65990-dev\n"
        "  registeredEnv: dev11\n"
    )
    version, status = m._extract_chart_version_checked(content)
    assert status == "ok"
    assert version == "2603.0.1-ap-65990-dev"


def test_duplicate_version_key_last_wins_three_occurrences():
    # Not just first-vs-second: must always take the LAST of any count.
    content = (
        "appspace:\n"
        "  version: 1.0.0-a\n"
        "  version: 1.0.0-b\n"
        "  version: 1.0.0-c\n"
    )
    version, status = m._extract_chart_version_checked(content)
    assert (version, status) == ("1.0.0-c", "ok")


def test_single_version_key_unaffected():
    # The common case (one version key) must keep working exactly as before.
    content = "appspace:\n  customerName: x\n  version: 2603.0.1-dev\n"
    assert m._extract_chart_version_checked(content) == ("2603.0.1-dev", "ok")


def test_duplicate_version_last_occurrence_invalid_still_rejected():
    # If the LAST occurrence is unsafe, it must still be rejected as
    # "invalid" (not silently fall back to the earlier, safe-looking one) --
    # that would just move the false-green bug rather than fix it.
    content = (
        "appspace:\n"
        "  version: 2603.0.0-dev\n"
        "  version: ../../etc/passwd\n"
    )
    version, status = m._extract_chart_version_checked(content)
    assert status == "invalid"
    assert version is None


def test_deeper_nested_version_key_not_confused_with_duplicate():
    # Regression guard: appspace.elastic.version must never be picked up,
    # duplicate-key handling must not break the existing indent-tracking.
    content = (
        "appspace:\n"
        "  version: 2603.0.1-dev\n"
        "  elastic:\n"
        "    version: 8.15.1\n"
    )
    assert m._extract_chart_version_checked(content) == ("2603.0.1-dev", "ok")


# ── CRIT-2: non-ASCII HMAC signature header must be rejected cleanly,
#    never raise TypeError out of the verify function ────────────────────
def test_jfrog_hmac_non_ascii_header_rejected_not_raised():
    m.JFROG_WEBHOOK_SECRET = "topsecret"
    body = b'{"event_type":"pushed"}'
    # Must return False, must NOT raise.
    assert m._verify_jfrog_hmac(body, "\u00f1x") is False


def test_bb_hmac_non_ascii_header_rejected_not_raised():
    m.BB_WEBHOOK_SECRET = "topsecret"
    body = b'{"some":"payload"}'
    assert m._verify_bb_hmac(body, "sha256=\u00f1") is False


def test_jfrog_hmac_still_accepts_valid_signature():
    # The fix must not break the legitimate path.
    import hmac, hashlib
    m.JFROG_WEBHOOK_SECRET = "topsecret"
    body = b'{"event_type":"pushed"}'
    good = hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()
    assert m._verify_jfrog_hmac(body, good) is True


def test_bb_hmac_still_accepts_valid_signature():
    import hmac, hashlib
    m.BB_WEBHOOK_SECRET = "topsecret"
    body = b'{"some":"payload"}'
    good = "sha256=" + hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()
    assert m._verify_bb_hmac(body, good) is True


def test_jfrog_hmac_still_rejects_wrong_ascii_signature():
    m.JFROG_WEBHOOK_SECRET = "topsecret"
    body = b'{"event_type":"pushed"}'
    assert m._verify_jfrog_hmac(body, "deadbeef" * 8) is False


# ── DEFENSIVE: secret redaction must catch common pwd/pass abbreviations ──
def test_redact_pwd_abbreviation():
    body = "         - name: db_pwd\n           value: SUPERSECRET123\n"
    out = m._redact_for_display("apps/Deployment default/x", body)
    assert "SUPERSECRET123" not in out
    assert "[REDACTED]" in out


def test_redact_pass_abbreviation():
    body = "         - name: redisPass\n           value: SUPERSECRET123\n"
    out = m._redact_for_display("apps/Deployment default/x", body)
    assert "SUPERSECRET123" not in out


def test_redact_existing_full_words_unaffected():
    # Guard: the extension must not stop matching the words it already caught.
    for name in ("db_password", "api_token", "auth_secret"):
        body = f"         - name: {name}\n           value: XYZ123\n"
        out = m._redact_for_display("apps/Deployment default/x", body)
        assert "XYZ123" not in out, name


def test_redact_non_sensitive_name_still_kept():
    # Guard: must not become over-broad and start redacting everything.
    body = "         - name: SESSION_COOKIE\n           value: not-a-secret-marker\n"
    out = m._redact_for_display("apps/Deployment default/x", body)
    assert "not-a-secret-marker" in out


# ── DEFENSIVE: manifest parser must not depend on an exact 2-space
#    metadata indent (helm always emits 2-space, but must not be fragile) ─
def test_parser_handles_four_space_metadata_indent():
    y = "apiVersion: v1\nkind: Service\nmetadata:\n    name: svc-weird-indent\n"
    keys = set(m._parse_manifest_resources(y).keys())
    assert ("Service", "", "svc-weird-indent") in keys


def test_parser_strips_trailing_comment_from_name():
    y = "apiVersion: v1\nkind: Service\nmetadata:\n  name: mysvc # primary\n"
    keys = set(m._parse_manifest_resources(y).keys())
    assert ("Service", "", "mysvc") in keys


def test_parser_two_space_indent_still_works():
    # Guard: the normal (helm-emitted) case must keep working.
    y = "apiVersion: v1\nkind: Service\nmetadata:\n  name: normalsvc\n"
    keys = set(m._parse_manifest_resources(y).keys())
    assert ("Service", "", "normalsvc") in keys
