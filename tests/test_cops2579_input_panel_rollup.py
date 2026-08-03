"""COPS-2579 item 3: the input-change panel listed one bullet per service
for a shared ancestor-file change (acme-config-prod PR #3837: 67 services
losing the identical Spot compute-class override rendered as 67 near-
identical bullets, capped at 25 with "+110 more"). _rollup_by_service
collapses any group of INPUT_ROLLUP_MIN_SERVICES or more services sharing
the same (rest-of-key, value) into one summary line; smaller groups and
non-microservice keys render individually, unchanged from before.
"""
import os
import sys

os.environ.setdefault("BB_USER", "test-user")
os.environ.setdefault("BB_TOKEN", "test-token")
os.environ.setdefault("ARGOCD_PASS", "test-pass")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m


def _svc_key(service, rest):
    return f"appspace.microservices.definitions.{service}.{rest}"


def test_service_and_rest_splits_microservice_key():
    service, rest = m._service_and_rest(
        "appspace.microservices.definitions.analytics.nodeSelector.x")
    assert service == "analytics"
    assert rest == "nodeSelector.x"


def test_service_and_rest_returns_none_for_non_microservice_key():
    service, rest = m._service_and_rest("appspace.redis.resources.requests.cpu")
    assert service is None
    assert rest == "appspace.redis.resources.requests.cpu"


def test_rollup_collapses_large_identical_group():
    services = [f"svc{i}" for i in range(67)]
    keys = sorted(_svc_key(s, "nodeSelector.compute-class") for s in services)
    lines = m._rollup_by_service(
        keys,
        sig_fn=lambda k: "spot",
        render_group=lambda rest, val, svcs: f"GROUP:{rest}:{val}:{len(svcs)}",
        render_single=lambda k: f"SINGLE:{k}",
    )
    assert lines == ["GROUP:nodeSelector.compute-class:spot:67"]


def test_rollup_keeps_small_group_individual():
    """Below INPUT_ROLLUP_MIN_SERVICES, rendering must stay per-key,
    identical to the pre-rollup behavior."""
    services = ["svc-a", "svc-b"]
    keys = sorted(_svc_key(s, "replicas") for s in services)
    lines = m._rollup_by_service(
        keys,
        sig_fn=lambda k: "1",
        render_group=lambda rest, val, svcs: f"GROUP:{rest}",
        render_single=lambda k: f"SINGLE:{k}",
    )
    assert len(lines) == 2
    assert all(line.startswith("SINGLE:") for line in lines)


def test_rollup_keeps_non_microservice_keys_individual():
    keys = ["appspace.redis.resources.requests.cpu",
            "appspace.global.replicaCount"]
    lines = m._rollup_by_service(
        keys,
        sig_fn=lambda k: "x",
        render_group=lambda rest, val, svcs: f"GROUP:{rest}",
        render_single=lambda k: f"SINGLE:{k}",
    )
    assert sorted(lines) == sorted(f"SINGLE:{k}" for k in keys)


def test_rollup_separates_different_values_into_different_groups():
    """Two different values must never merge into one group even if the
    field name (rest) is the same across all services."""
    keys_a = [_svc_key(s, "resources.requests.cpu") for s in ("a1", "a2", "a3")]
    keys_b = [_svc_key(s, "resources.requests.cpu") for s in ("b1", "b2", "b3")]
    values = {**{k: "10m" for k in keys_a}, **{k: "50m" for k in keys_b}}
    lines = m._rollup_by_service(
        sorted(keys_a + keys_b),
        sig_fn=lambda k: values[k],
        render_group=lambda rest, val, svcs: f"GROUP:{rest}:{val}:{sorted(svcs)}",
        render_single=lambda k: f"SINGLE:{k}",
    )
    assert len(lines) == 2
    assert any("10m" in line and "a1" in line for line in lines)
    assert any("50m" in line and "b1" in line for line in lines)


def test_fmt_service_list_truncates_and_counts_remainder():
    services = sorted(f"svc{i}" for i in range(10))
    out = m._fmt_service_list(services, shown=8)
    assert out.count(",") == 7  # 8 shown -> 7 commas
    assert "(+2 more)" in out
