"""v2.5.16: block-scalar openers with a trailing comment leaked secret bodies.

Both display-time redactors decided whether a `key: value` value opened a
YAML block scalar with an exact set-membership test
(val in ("|", "|-", "|+", ">", ">-", ">+")). YAML allows a comment after the
block indicator, so a value like `tls.crt: |- # PEM cert` produced the value
string "|- # PEM cert", which was NOT in the set. The opener line was masked,
but in_block was never entered, so the indented continuation lines -- the
actual secret bytes -- fell through verbatim into the Bitbucket PR comment.

Same leak class and severity as FIX D (v2.4.9), v2.5.0 H3 and v2.5.14, all
confirmed live. Fixed by _is_block_scalar_opener, which parses the indicator
grammar ([|>] + optional chomping/indentation indicators + optional comment)
instead of matching a fixed set of strings.

Each test asserts the CORRECT behavior, so it fails against the pre-fix code.
"""
import os
import sys

os.environ.setdefault("BB_USER", "test-user")
os.environ.setdefault("BB_TOKEN", "test-token")
os.environ.setdefault("ARGOCD_PASS", "test-pass")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m


# ── _is_block_scalar_opener grammar ──────────────────────────────────────

def test_block_scalar_opener_plain_indicators():
    for v in ("|", "|-", "|+", ">", ">-", ">+", "|2", ">8", "|2-", "|-2"):
        assert m._is_block_scalar_opener(v), f"{v!r} should be an opener"


def test_block_scalar_opener_with_trailing_comment():
    for v in ("| # note", "|-  # PEM cert", ">2 # folded", "|+ #keep"):
        assert m._is_block_scalar_opener(v), f"{v!r} should be an opener"


def test_block_scalar_opener_rejects_inline_values():
    for v in ("hunter2", "mongodb://u:p@h", "| pipe in text", "true", "12"):
        assert not m._is_block_scalar_opener(v), f"{v!r} is not an opener"


# ── Secret whole-mask path ───────────────────────────────────────────────

def test_secret_block_scalar_with_comment_body_masked():
    """A Secret block-scalar value whose opener carries a trailing comment
    must still have its continuation lines masked, not leaked."""
    section = (
        " kind: Secret\n"
        " data:\n"
        "   tls.crt: |- # PEM cert\n"
        "     LS0tLS1CRUdJTiBDRVJUSUZJQ0FURQ==\n"
        "     bSECRETCERTBYTESdddddddddddddddd\n"
    )
    out = m._redact_secret_section(section)
    assert "LS0tLS1CRUdJTiBDRVJUSUZJQ0FURQ==" not in out
    assert "bSECRETCERTBYTESdddddddddddddddd" not in out
    assert "tls.crt:" in out  # key kept so the reviewer sees what changed


def test_secret_folded_scalar_indent_indicator_comment_masked():
    section = (
        "+kind: Secret\n"
        "+data:\n"
        "+  blob: >2 # folded blob\n"
        "+    SECRETFOLDEDBYTESaaaaaaaaaaaa\n"
    )
    out = m._redact_secret_section(section)
    assert "SECRETFOLDEDBYTESaaaaaaaaaaaa" not in out


# ── k8s env-var path ─────────────────────────────────────────────────────

def test_env_block_scalar_with_comment_body_masked():
    """`- name: <sensitive>` / `value: | # note` must mask the block body."""
    section = (
        "     - name: appspace_password\n"
        "       value: | # inline note\n"
        "         SUPERSECRETdbpassword123\n"
    )
    out = m._redact_k8s_env_pairs(section)
    assert "SUPERSECRETdbpassword123" not in out
    assert "value:" in out


def test_env_block_keep_chomp_with_comment_masked():
    section = (
        "     - name: appspace_token\n"
        "       value: |+ # keep\n"
        "         PRIVATEKEYBYTESxxxxxxxxxx\n"
    )
    out = m._redact_k8s_env_pairs(section)
    assert "PRIVATEKEYBYTESxxxxxxxxxx" not in out


def test_end_to_end_via_redact_for_display():
    """The full display path (kind-aware dispatch) must not leak either shape."""
    secret = (
        " kind: Secret\n"
        " data:\n"
        "   key: |- # comment\n"
        "     LEAKYSECRETVALUEaaaaaaaa\n"
    )
    assert "LEAKYSECRETVALUEaaaaaaaa" not in m._redact_for_display("app/Secret ", secret)

    deploy = (
        " kind: Deployment\n"
        " - name: db_password\n"
        "   value: > # folded\n"
        "     LEAKYENVVALUEbbbbbbbb\n"
    )
    assert "LEAKYENVVALUEbbbbbbbb" not in m._redact_for_display("app/Deployment ", deploy)


# ── _redact_sensitive block-tracking (leak 2) ────────────────────────────
#
# A top-level sensitive key rendered as a YAML block scalar in a resource
# that is neither a Secret nor a k8s env-var pair (e.g. a ConfigMap or CRD
# holding a PEM key or token) leaked its body: _redact_sensitive only ever
# matched the opener line, and _redact_k8s_env_pairs only covers the
# `- name:` / `value:` shape, so neither pass masked the continuation lines.
# Confirmed live via probe_redact2.py before this fix landed.

def test_redact_sensitive_configmap_block_scalar_masked():
    section = (
        " kind: ConfigMap\n"
        " data:\n"
        "   ssl-private-key: |\n"
        "     -----BEGIN PRIVATE KEY-----\n"
        "     MIIELEAKEDPRIVATEKEYBYTES\n"
        "     -----END PRIVATE KEY-----\n"
    )
    out = m._redact_sensitive(section)
    assert "MIIELEAKEDPRIVATEKEYBYTES" not in out
    assert "ssl-private-key:" in out  # key kept so the reviewer sees what changed


def test_redact_sensitive_crd_api_token_block_scalar_masked():
    section = (
        " kind: MyCRD\n"
        " spec:\n"
        "   apiToken: |\n"
        "     tok-LEAKEDTOKEN-abcdef\n"
    )
    out = m._redact_sensitive(section)
    assert "tok-LEAKEDTOKEN-abcdef" not in out


def test_redact_sensitive_block_scalar_blank_line_preserved():
    """A blank line inside the block body must stay blank, not be read as
    the end of the block (which would let the next real line leak)."""
    section = (
        "   apiToken: |\n"
        "     tok-LEAKEDTOKEN-part1\n"
        "\n"
        "     tok-LEAKEDTOKEN-part2\n"
    )
    out = m._redact_sensitive(section)
    assert "tok-LEAKEDTOKEN-part1" not in out
    assert "tok-LEAKEDTOKEN-part2" not in out


def test_redact_sensitive_dedent_exits_block_and_resumes_normal_handling():
    """Once a line dedents back to the opener's indent, the block must end
    and normal per-line handling must resume, including redacting a second
    sensitive key that follows the block."""
    section = (
        "   apiToken: |\n"
        "     tok-LEAKEDTOKEN-abcdef\n"
        "   password: hunter2LEAK\n"
    )
    out = m._redact_sensitive(section)
    assert "tok-LEAKEDTOKEN-abcdef" not in out
    assert "hunter2LEAK" not in out
    assert "password:" in out


def test_end_to_end_configmap_and_crd_block_scalar_via_redact_for_display():
    """Same two leaks, through the actual kind-aware dispatch used when
    posting the Bitbucket PR comment."""
    cm = (
        " kind: ConfigMap\n"
        " data:\n"
        "   ssl-private-key: |\n"
        "     MIIELEAKEDPRIVATEKEYBYTES\n"
    )
    assert "MIIELEAKEDPRIVATEKEYBYTES" not in m._redact_for_display("app/ConfigMap ", cm)

    crd = (
        " kind: MyCRD\n"
        " spec:\n"
        "   apiToken: |\n"
        "     tok-LEAKEDTOKEN-abcdef\n"
    )
    assert "tok-LEAKEDTOKEN-abcdef" not in m._redact_for_display("app/MyCRD ", crd)
