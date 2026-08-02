"""COPS-2577 - remaining headroom under MAX_APPS_PER_RUN must be observable.

The app cap is a hard merge block once crossed: the over-cap apps are not
evaluated and the build status is FAILED by design, because a partial
evaluation must never look like full coverage. That is correct, but it
means the first sign of an undersized cap is a blocked production PR.

The fleet grows with every new customer environment, so this will recur at
whatever number the cap is set to. These tests defend the early warning:
the largest affected-app count ever seen is tracked as a high-water mark
and published, so the squeeze is visible before it bites.
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m  # noqa: E402


def _reset_high_water():
    with m._diff_stats_lock:
        m._diff_stats["max_affected_apps_seen"] = 0


def test_high_water_mark_records_the_largest_run():
    _reset_high_water()
    m._record_affected_apps(120)
    m._record_affected_apps(43)
    m._record_affected_apps(views := 861)
    m._record_affected_apps(7)
    with m._diff_stats_lock:
        assert m._diff_stats["max_affected_apps_seen"] == views, (
            "the high-water mark must keep the largest count, not the last")


def test_headroom_is_published_alongside_the_cap():
    _reset_high_water()
    m._record_affected_apps(700)
    srv = m._start_health_server(0)
    try:
        port = srv.server_address[1]
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/diff-preview/stats",
                timeout=10) as r:
            payload = json.loads(r.read())
    finally:
        srv.shutdown()
    assert payload["max_affected_apps_seen"] == 700
    assert payload["max_apps_per_run"] == m.MAX_APPS_PER_RUN, (
        "the cap must be published next to the high-water mark, otherwise "
        "the number alone says nothing about remaining headroom")


def test_a_run_at_the_cap_is_recorded_even_though_nothing_was_skipped():
    """The last safe run is the most valuable warning, so it must count."""
    _reset_high_water()
    m._record_affected_apps(m.MAX_APPS_PER_RUN)
    with m._diff_stats_lock:
        assert m._diff_stats["max_affected_apps_seen"] == m.MAX_APPS_PER_RUN


def test_the_count_recorded_is_the_demand_not_the_truncated_batch():
    """A capped run must record what the PR actually asked for.

    Recording the post-cap length would peg the high-water mark at the cap
    forever and hide exactly the overflow the mark exists to reveal.
    """
    _reset_high_water()
    m._record_affected_apps(m.MAX_APPS_PER_RUN + 63)
    with m._diff_stats_lock:
        seen = m._diff_stats["max_affected_apps_seen"]
    assert seen == m.MAX_APPS_PER_RUN + 63, (
        f"expected the pre-cap demand, got {seen}")
