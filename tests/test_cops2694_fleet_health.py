"""COPS-2694: fleet health gauges that back the mass-degradation alert.

The Cloud Monitoring policies in acme-infrastructure reference these metric
names and label keys verbatim. A rename here silently blinds paging, so the
exposition format itself is under test, not just the arithmetic.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import fleet_health


def _app(dest_name=None, server=None, health="Healthy", sync="Synced"):
    app = {"metadata": {"name": "x"},
           "spec": {"destination": {}},
           "status": {}}
    if dest_name:
        app["spec"]["destination"]["name"] = dest_name
    if server:
        app["spec"]["destination"]["server"] = server
    if health is not None:
        app["status"]["health"] = {"status": health}
    if sync is not None:
        app["status"]["sync"] = {"status": sync}
    return app


def test_counts_by_destination_and_status():
    apps = ([_app("gcp-prod-pv-eu1-b", health="Degraded")] * 3
            + [_app("gcp-prod-pv-eu1-b")] * 5
            + [_app("az-prod-pv-na1-a", sync="OutOfSync")] * 2)
    assert fleet_health.collect(apps) == 10
    body = fleet_health.render_prometheus(is_leader=True)
    assert ('acme_diff_preview_argocd_app_health{dest_cluster='
            '"gcp-prod-pv-eu1-b",health_status="Degraded"} 3') in body
    assert ('acme_diff_preview_argocd_app_health{dest_cluster='
            '"gcp-prod-pv-eu1-b",health_status="Healthy"} 5') in body
    assert ('acme_diff_preview_argocd_app_sync{dest_cluster='
            '"az-prod-pv-na1-a",sync_status="OutOfSync"} 2') in body
    # TYPE lines present exactly once per family: a malformed exposition
    # makes the scraper reject the entire payload.
    assert body.count("# TYPE acme_diff_preview_argocd_app_health gauge") == 1
    assert body.count("# TYPE acme_diff_preview_argocd_app_sync gauge") == 1


def test_missing_status_buckets_as_unknown_never_healthy():
    """An absent status must degrade the picture, not embellish it: a
    freshly-created app with no health yet counted as Healthy would dilute
    the Degraded ratio the alert divides by."""
    fleet_health.collect([_app("gcp-prod-pv-na4-a", health=None, sync=None)])
    body = fleet_health.render_prometheus(is_leader=True)
    assert ('acme_diff_preview_argocd_app_health{dest_cluster='
            '"gcp-prod-pv-na4-a",health_status="Unknown"} 1') in body
    assert ('acme_diff_preview_argocd_app_sync{dest_cluster='
            '"gcp-prod-pv-na4-a",sync_status="Unknown"} 1') in body


def test_destination_fallbacks():
    fleet_health.collect([
        _app(server="https://kubernetes.default.svc"),
        _app(server="https://10.1.2.3:443"),
        _app(),   # neither name nor server
    ])
    body = fleet_health.render_prometheus(is_leader=True)
    assert 'dest_cluster="in-cluster"' in body
    assert 'dest_cluster="https://10.1.2.3:443"' in body
    assert 'dest_cluster="Unknown"' in body


def test_malformed_entries_are_skipped_not_fatal():
    """collect() sits inside discovery; one bad app must never break the
    path map build (the P0-6 discovery contract)."""
    apps = [_app("gcp-prod-pv-na2-a"), "not-a-dict", None,
            {"metadata": None, "spec": None, "status": None}]
    counted = fleet_health.collect(apps)
    # The two dict-shaped entries count (the degenerate one as Unknown);
    # the two non-dicts are skipped.
    assert counted == 2
    body = fleet_health.render_prometheus(is_leader=True)
    assert 'dest_cluster="gcp-prod-pv-na2-a"' in body


def test_standby_emits_nothing():
    """A standby emitting zeros would double-count under sum() the moment
    both replicas answer a scrape; absence is the contract."""
    fleet_health.collect([_app("gcp-prod-pv-na2-a")])
    assert fleet_health.render_prometheus(is_leader=False) == ""


def test_never_collected_emits_nothing_even_on_leader():
    """Before the first successful app list there is nothing truthful to
    say; an empty exposition lets the alert-side absent() guard see it."""
    fleet_health._collected_monotonic = 0.0
    fleet_health._health_counts = {}
    fleet_health._sync_counts = {}
    assert fleet_health.render_prometheus(is_leader=True) == ""


def test_snapshot_age_reflects_collection_time():
    fleet_health.collect([_app("gcp-prod-pv-na2-a")])
    base = fleet_health._collected_monotonic
    body = fleet_health.render_prometheus(is_leader=True,
                                          now=lambda: base + 120.0)
    assert "acme_diff_preview_fleet_snapshot_age_seconds 120" in body


def test_label_values_are_escaped():
    fleet_health.collect([_app('we"ird\\name')])
    body = fleet_health.render_prometheus(is_leader=True)
    assert 'dest_cluster="we\\"ird\\\\name"' in body


def test_collect_replaces_rather_than_accumulates():
    """A destination that disappears from the app list must drop out of the
    exposition: stale series would keep satisfying (or breaking) the alert
    for a spoke that no longer exists."""
    fleet_health.collect([_app("gcp-prod-pv-nachaos-a", health="Degraded")])
    fleet_health.collect([_app("gcp-prod-pv-na2-a")])
    body = fleet_health.render_prometheus(is_leader=True)
    assert "nachaos" not in body
    assert 'dest_cluster="gcp-prod-pv-na2-a"' in body


def test_metrics_endpoint_appends_fleet_block():
    """The wiring in diff_preview: /metrics = stats block + fleet block."""
    import diff_preview as m
    fleet_health.collect([_app("gcp-prod-pv-eu1-b", health="Degraded")])
    with m._diff_stats_lock:
        snapshot = dict(m._diff_stats)
    snapshot["is_leader"] = True
    combined = (m.render_prometheus(snapshot)
                + fleet_health.render_prometheus(True))
    assert "acme_diff_preview_build_info" in combined
    assert "acme_diff_preview_argocd_app_health" in combined
    # Exposition stays parseable line-by-line: every non-comment line is
    # "name{labels} value" or "name value".
    for line in combined.strip().splitlines():
        if line.startswith("#"):
            continue
        assert " " in line, line
        float(line.rsplit(" ", 1)[1])
