"""COPS-2579: _SENSITIVE_KEYS matched bare "key" as a substring anywhere in
a YAML key name, so structural Kubernetes scheduling fields got wrongly
redacted.

Found on acme-config-prod PR #3837 (Spot compute-class removal): the
toleration `key:` field and the topologySpreadConstraints `topologyKey:`
field both got replaced with `[REDACTED]` in the posted comment, hiding the
exact line the PR was changing (`cloud.google.com/compute-class`).

Bare "pass" and bare "auth" are deliberately left untouched: they are
proven, load-bearing behavior (test_redact_pass_abbreviation pins that a
field literally named "redisPass" must still be redacted), and this ticket
only has concrete evidence for the "key" over-match, not for those two.

kind: Secret bodies are a separate, intentionally blunt code path
(_redact_secret_section) that whole-masks every key regardless of name --
unaffected by this fix, and pinned here so the two paths are not confused.
"""
import os
import sys

os.environ.setdefault("BB_USER", "test-user")
os.environ.setdefault("BB_TOKEN", "test-token")
os.environ.setdefault("ARGOCD_PASS", "test-pass")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m


def test_toleration_key_field_not_redacted():
    body = (
        "       tolerations:\n"
        "-        - key: cloud.google.com/compute-class\n"
        "-          operator: Equal\n"
        "-          value: spot-preferred-standard-fallback\n"
        "-          effect: NoSchedule\n"
    )
    out = m._redact_sensitive(body)
    assert "cloud.google.com/compute-class" in out
    assert "[REDACTED]" not in out


def test_topology_key_field_not_redacted():
    body = (
        "       topologySpreadConstraints:\n"
        "         maxSkew: 1\n"
        "-        topologyKey: kubernetes.io/hostname\n"
        "         whenUnsatisfiable: ScheduleAnyway\n"
    )
    out = m._redact_sensitive(body)
    assert "kubernetes.io/hostname" in out
    assert "[REDACTED]" not in out


def test_node_affinity_match_expressions_key_not_redacted():
    body = (
        "       matchExpressions:\n"
        "-        - key: gke_node_type\n"
        "-          operator: In\n"
    )
    out = m._redact_sensitive(body)
    assert "gke_node_type" in out
    assert "[REDACTED]" not in out


def test_real_password_field_still_redacted():
    body = "-  password: hunter2\n"
    out = m._redact_sensitive(body)
    assert "hunter2" not in out
    assert "[REDACTED]" in out


def test_real_api_key_field_still_redacted():
    body = "-  apiKey: sk-abc123\n"
    out = m._redact_sensitive(body)
    assert "sk-abc123" not in out
    assert "[REDACTED]" in out


def test_real_bearer_token_field_still_redacted():
    body = "-  authToken: eyJraWQ\n"
    out = m._redact_sensitive(body)
    assert "eyJraWQ" not in out
    assert "[REDACTED]" in out


def test_redis_pass_abbreviation_still_redacted():
    """Regression guard: this is the exact shape the full suite's
    test_redact_pass_abbreviation pins. Bare "pass" stays untouched by
    this fix, so this must keep passing."""
    body = "-  redisPass: SUPERSECRET123\n"
    out = m._redact_sensitive(body)
    assert "SUPERSECRET123" not in out
    assert "[REDACTED]" in out


def test_generic_custom_key_field_still_redacted():
    """A custom, non-Kubernetes-structural field literally named e.g.
    sshKey must still be caught -- only the known scheduling field names
    are exempt, not every "*key" identifier."""
    body = "-  sshKey: AAAAB3NzaC1yc2E\n"
    out = m._redact_sensitive(body)
    assert "AAAAB3NzaC1yc2E" not in out
    assert "[REDACTED]" in out


def test_secret_kind_still_whole_masks_field_named_key():
    """The Secret-specific redactor is unaffected by the scheduling
    exemption: inside an actual Secret, a data key literally named 'key'
    must still be masked, since Secret data keys are arbitrary and the
    key name carries no scheduling meaning there."""
    body = "-  key: c29tZS1zZWNyZXQtYnl0ZXM=\n"
    out = m._redact_secret_section(body)
    assert "c29tZS1zZWNyZXQtYnl0ZXM=" not in out
    assert "[REDACTED]" in out
