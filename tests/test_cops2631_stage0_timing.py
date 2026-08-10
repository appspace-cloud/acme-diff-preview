"""COPS-2631 stage 0: per-stage timing for the diff hot path.

There is no stage timing in the service today: grep perf_counter finds
nothing, and the only timed operations are coarse (iteration wall clock,
per-app elapsed in logs). /metrics already exists (COPS-2627); this stage
is wiring, not new infrastructure.

Stages recorded (matching the ticket's measured cost model):

  pull   - chart OCI ensure + value-file fetch
  render - helm template (wall clock of the parallel wait)
  parse  - _parse_manifest_resources
  diff   - _diff_resources
  store  - full-diff artifact save

Each stage accumulates seconds_total + count so /metrics and
/diff-preview/stats can prove stage 3 (render cache) actually moved the
needle, instead of guessing from end-to-end PR duration.
"""
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_preview  # noqa: E402


STAGES = ("pull", "render", "parse", "diff", "store")


def _reset_stage_stats():
    with diff_preview._diff_stats_lock:
        for stage in STAGES:
            diff_preview._diff_stats["stage_%s_seconds" % stage] = 0.0
            diff_preview._diff_stats["stage_%s_count" % stage] = 0


def test_stage_timing_keys_exist_on_diff_stats():
    """The five stages must be first-class counters, not ad-hoc log scrapes."""
    for stage in STAGES:
        assert "stage_%s_seconds" % stage in diff_preview._diff_stats
        assert "stage_%s_count" % stage in diff_preview._diff_stats


def test_record_stage_accumulates_seconds_and_count():
    _reset_stage_stats()
    diff_preview._record_stage("render", 0.5)
    diff_preview._record_stage("render", 0.25)
    with diff_preview._diff_stats_lock:
        assert diff_preview._diff_stats["stage_render_seconds"] == 0.75
        assert diff_preview._diff_stats["stage_render_count"] == 2


def test_record_stage_is_thread_safe_under_fanout():
    """DIFF_WORKERS=16 record concurrently; a lost increment would make
    stage 3 look quieter than it is."""
    _reset_stage_stats()
    n_threads = 32
    per_thread = 50

    def worker():
        for _ in range(per_thread):
            diff_preview._record_stage("parse", 0.001)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with diff_preview._diff_stats_lock:
        assert diff_preview._diff_stats["stage_parse_count"] == n_threads * per_thread
        # floating-point sum of 0.001 * 1600 is exact in binary here
        assert abs(diff_preview._diff_stats["stage_parse_seconds"] - 1.6) < 1e-9


def test_stage_timings_are_prometheus_counters():
    """Seconds and counts are monotonic and must be typed counter so
    increase()/rate() survive a pod restart (same rule as COPS-2627)."""
    out = diff_preview.render_prometheus({
        "stage_pull_seconds": 12.5,
        "stage_pull_count": 40,
        "stage_render_seconds": 200.0,
        "stage_render_count": 40,
        "stage_parse_seconds": 2.0,
        "stage_parse_count": 40,
        "stage_diff_seconds": 0.8,
        "stage_diff_count": 40,
        "stage_store_seconds": 0.1,
        "stage_store_count": 3,
    })
    for stage in STAGES:
        sec = "acme_diff_preview_stage_%s_seconds_total" % stage
        cnt = "acme_diff_preview_stage_%s_count_total" % stage
        assert "# TYPE %s counter" % sec in out, stage
        assert "# TYPE %s counter" % cnt in out, stage
        assert any(ln.startswith(sec + " ") or ln.startswith(sec + "{")
                   for ln in out.splitlines())
        assert any(ln.startswith(cnt + " ") or ln.startswith(cnt + "{")
                   for ln in out.splitlines())


def test_unknown_stage_is_ignored_not_crash():
    """A typo in a call site must not invent a new stats key by accident
    (COPS-2627: a metric is a contract)."""
    _reset_stage_stats()
    before = dict(diff_preview._diff_stats)
    diff_preview._record_stage("nope", 1.0)
    with diff_preview._diff_stats_lock:
        assert diff_preview._diff_stats == before
