"""Regression tests for the v2.5.0 hardening round (deep analysis round 2).

Each test encodes one finding from FINDINGS_ROUND2.md and must FAIL against
the pre-fix code, then PASS once the fix lands. Pure-function level, no network.
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m


# ── H3: block-scalar secret must be redacted ──────────────────────────
def test_redact_block_scalar_pem_secret():
    body = ("         - name: TLS_PRIVATE_KEY\n"
            "           value: |-\n"
            "             -----BEGIN RSA PRIVATE KEY-----\n"
            "             MIIEowIBAAKCAQEAsecretmaterial\n"
            "             -----END RSA PRIVATE KEY-----\n")
    out = m._redact_for_display("apps/Deployment default/x", body)
    assert "MIIEowIBAAKCAQEAsecretmaterial" not in out
    assert "BEGIN RSA PRIVATE KEY" not in out
    assert "[REDACTED" in out


def test_redact_block_scalar_stops_at_next_key():
    # The block value is masked, but an unrelated following non-sensitive
    # key at the value indent level must stay visible.
    body = ("         - name: appspace_privateKey\n"
            "           value: |\n"
            "             line-one-secret\n"
            "             line-two-secret\n"
            "         - name: appspace_publicUrl\n"
            "           value: https://public.example.com\n")
    out = m._redact_for_display("apps/Deployment default/x", body)
    assert "line-one-secret" not in out
    assert "line-two-secret" not in out
    assert "https://public.example.com" in out  # non-sensitive stays


# ── H4: flow-style env secret must be redacted ────────────────────────
def test_redact_flow_style_env_secret():
    body = "         - {name: appspace_dbPassword, value: SUPERSECRET123}\n"
    out = m._redact_for_display("apps/Deployment default/x", body)
    assert "SUPERSECRET123" not in out
    assert "[REDACTED]" in out


def test_redact_flow_style_non_sensitive_kept():
    body = "         - {name: appspace_publicUrl, value: https://ok.example.com}\n"
    out = m._redact_for_display("apps/Deployment default/x", body)
    assert "https://ok.example.com" in out


# ── H1: quote-only name change is not a phantom add+delete ────────────
def test_parse_name_quote_normalized():
    unq = m._parse_manifest_resources(
        "apiVersion: v1\nkind: Secret\nmetadata:\n  name: mysecret\n")
    q = m._parse_manifest_resources(
        'apiVersion: v1\nkind: Secret\nmetadata:\n  name: "mysecret"\n')
    assert list(unq.keys()) == list(q.keys()), (list(unq.keys()), list(q.keys()))


def test_diff_quote_only_name_is_single_modification():
    main = 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: s\nstringData:\n  a: "1"\n'
    pr = 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: "s"\nstringData:\n  a: "2"\n'
    d = m._diff_manifests(main, pr)
    # exactly one resource block, not an add + a delete
    assert d.count("=====") <= 2, d  # one header uses 2 '=====' markers


# ── H2: kind: List is expanded into its items ─────────────────────────
def test_parse_list_kind_expands_items():
    doc = ("apiVersion: v1\nkind: List\nitems:\n"
           "- apiVersion: v1\n  kind: ConfigMap\n  metadata:\n    name: a\n"
           "- apiVersion: v1\n  kind: ConfigMap\n  metadata:\n    name: b\n")
    r = m._parse_manifest_resources(doc)
    names = sorted(k[2] for k in r)
    assert names == ["a", "b"], names


# ── H5: build-metadata version is accepted ────────────────────────────
def test_valid_chart_version_allows_build_metadata():
    assert m._is_valid_chart_version("1.0.0+build") is True
    assert m._is_valid_chart_version("2603.0.1-dev+abc") is True


def test_valid_chart_version_still_rejects_unsafe():
    assert m._is_valid_chart_version("../../etc") is False
    assert m._is_valid_chart_version("-dev") is False
    assert m._is_valid_chart_version("a b") is False
    assert m._is_valid_chart_version("x;rm") is False


# ── H7: invalid YAML is a permanent reason ────────────────────────────
def test_invalid_yaml_is_permanent():
    assert m.REASON_INVALID_YAML in m.PERMANENT_REASONS
    assert m.REASON_INVALID_YAML not in m.RETRYABLE_REASONS


def test_invalid_yaml_token_is_permanent():
    ar = {"appX": m.DiffResult("", [], 0, False, "bad yaml",
                               m.OUT_INDETERMINATE, m.REASON_INVALID_YAML)}
    c = m.format_comment("abcdef1234567890", ar, base_sha="ba5eba11")
    assert m._extract_status_token(c) == "permanent"


# ── H10: new-env invalid YAML is FAILED, not green ────────────────────
def test_new_env_invalid_yaml_is_failed():
    state, expected = m._new_env_status(
        "Error: error converting YAML to JSON: yaml: line 3: did not find expected key")
    assert state == "FAILED"
    assert expected is False


def test_new_env_missing_credentials_still_green():
    state, expected = m._new_env_status(
        "helm template failed: execution error: Missing required value")
    assert state == "SUCCESSFUL"
    assert expected is True
