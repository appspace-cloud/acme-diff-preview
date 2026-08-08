"""v2.5.17: _format_app_diff_block's legacy fallback under-redacted Secrets.

_format_app_diff_block has two rendering paths: the primary one (sections
populated) redacts each (hdr, body) pair through the kind-aware
_redact_for_display, which whole-masks `kind: Secret` bodies regardless of
key name. The fallback (sections empty, diff_text non-empty -- reachable
through _result()'s legacy 3-tuple coercion, which rebuilds sections with
parse_diff_sections() but without _filter_diff_sections()) instead ran only
the flat _redact_sensitive() pass. That pass is not kind-aware and only
catches keys matching _SENSITIVE_KEYS, so a Secret data key that isn't in
that list (tls.crt, ca.bundle, .dockerconfigjson, ...) leaked verbatim.

Confirmed live with a throwaway probe before this fix. Not reachable through
the real diff pipeline today (argocd_diff always keeps diff_text and
sections in lockstep), but a real landmine for the legacy coercion path or
any future refactor that breaks that invariant -- and an existing test
(test_format_app_diff_block_legacy_diff_text_without_sections) already
exercised this exact fallback without ever checking redaction, so the gap
went unnoticed.

Fixed by having the fallback re-derive (hdr, body) sections from diff_text
via parse_diff_sections() and redact each one through _redact_for_display,
same as the primary path. Only falls back further to the flat
_redact_sensitive() pass when the text has no "===== hdr =====" markers at
all to key off (truly unstructured legacy diff text).
"""
import os
import sys

os.environ.setdefault("BB_USER", "test-user")
os.environ.setdefault("BB_TOKEN", "test-token")
os.environ.setdefault("ARGOCD_PASS", "test-pass")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m

# COPS-2612: these cases exercise the INLINE diff-block rendering
# path. The comment stopped using it by default when phase E flipped
# COMMENT_INLINE_DIFFS, but it is still what the full-diff page renders
# always and what the comment renders on rollback, so the behaviour
# below (redaction, body caps, fence safety) must keep being tested.
_INLINE = m.COMMENT_PROFILE.replace(inline_diffs=True)



def test_fallback_whole_masks_secret_with_unlisted_key_name():
    """A Secret data key that _SENSITIVE_KEYS does not name (tls.crt) must
    still be whole-masked when reached through the sections=[] fallback."""
    raw = (
        "===== app/Secret my-tls =====\n"
        " kind: Secret\n"
        " data:\n"
        "   tls.crt: |-\n"
        "     LEAKEDCERTBYTESaaaaaaaaaaaaaaaa\n"
    )
    out = "\n".join(m._format_app_diff_block("my-app", [], raw, show_diff=True, n_res=1, profile=_INLINE))
    assert "LEAKEDCERTBYTES" not in out
    assert "tls.crt:" in out  # key kept so the reviewer sees what changed


def test_fallback_masks_configmap_block_scalar_sensitive_key():
    """Same fallback, a ConfigMap/CRD-shaped sensitive top-level key as a
    block scalar (the v2.5.16 leak-2 shape) must also be masked here."""
    raw = (
        "===== app/ConfigMap my-cm =====\n"
        " kind: ConfigMap\n"
        " data:\n"
        "   apiToken: |\n"
        "     tok-LEAKEDTOKEN-abcdef\n"
    )
    out = "\n".join(m._format_app_diff_block("my-app", [], raw, show_diff=True, n_res=1, profile=_INLINE))
    assert "tok-LEAKEDTOKEN-abcdef" not in out


def test_fallback_multiple_sections_each_redacted_independently():
    """Multiple resources in one legacy diff_text blob must each go through
    the kind-aware redaction, not just the first/last one."""
    raw = (
        "===== app/Secret sec-a =====\n"
        " kind: Secret\n"
        " data:\n"
        "   tls.crt: |-\n"
        "     LEAKEDCERTAaaaaaaaaaaaaaaaaaaaa\n"
        "===== app/Deployment dep-b =====\n"
        " kind: Deployment\n"
        " - name: db_password\n"
        "   value: LEAKEDENVVALUEbbbbbbbbbbbbbbbb\n"
    )
    out = "\n".join(m._format_app_diff_block("my-app", [], raw, show_diff=True, n_res=2, profile=_INLINE))
    assert "LEAKEDCERTAaaaaaaaaaaaaaaaaaaaa" not in out
    assert "LEAKEDENVVALUEbbbbbbbbbbbbbbbb" not in out


def test_fallback_still_handles_truly_headerless_text():
    """No '===== hdr =====' markers at all (arbitrary legacy diff text):
    still falls through to the flat redaction pass, unchanged behavior."""
    out = "\n".join(m._format_app_diff_block(
        "legacy-app", [], "--- a\n+++ b\n-old\n+new\n", show_diff=True, n_res=1, profile=_INLINE))
    assert "```diff" in out and "-old" in out and "+new" in out
