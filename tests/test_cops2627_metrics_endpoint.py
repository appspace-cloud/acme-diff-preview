"""COPS-2627: expose the counters in a format something can actually watch.

The ticket asked for Datadog monitors on the counters the COPS-2607 phases
added, and told whoever picked it up to check what is already wired before
adding a new scrape path. The answer turned out to be: nothing is. This
Datadog org receives no Kubernetes container logs at all (only cloud audit
sources), and there is no Datadog agent anywhere in the na1-a cluster. The
cluster ships logs to Cloud Logging through fluentbit-gke and runs Google
Managed Prometheus (gmp-system/collector, 4/4 ready).

So the prerequisite is a scrapeable endpoint, and that is what these tests
pin. It is deliberately platform-neutral: the same exposition format is
what GMP scrapes and what a Datadog OpenMetrics check would read, so this
work is not wasted whichever way the alerting decision goes.

The ticket's caveat is the design constraint:

    the counters are per pod and reset on restart, so a naive "current
    value" monitor will look healthy after every deploy

That is exactly what the TYPE line is for. A counter declared as a counter
lets increase() and rate() handle resets correctly, which is why every
monotonic counter below MUST be emitted as `counter` and every value that
can fall on its own (a high-water mark, a consecutive-failure gauge that
zeroes on success) MUST NOT be. Getting that backwards produces monitors
that are reassuring rather than informative, which the ticket rightly
calls worse than having none.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_preview  # noqa: E402


def _lines(text):
    return [ln for ln in text.split("\n") if ln and not ln.startswith("#")]


def _type_of(text, metric):
    for ln in text.split("\n"):
        if ln.startswith("# TYPE %s " % metric):
            return ln.rsplit(" ", 1)[1]
    return None


def _value_of(text, metric):
    for ln in _lines(text):
        name = ln.split("{")[0].split(" ")[0]
        if name == metric:
            return float(ln.rsplit(" ", 1)[1])
    return None


# -- the four counters the ticket names ------------------------------------

def test_the_four_counters_the_ticket_names_are_all_exposed():
    out = diff_preview.render_prometheus({
        "comment_fallback_inline": 3, "section_cap_trims": 1,
        "comment_max_bytes": 51200, "oci_consecutive_pull_failures": 2})
    assert _value_of(out, "acme_diff_preview_comment_fallback_inline_total") == 3
    assert _value_of(out, "acme_diff_preview_section_cap_trims_total") == 1
    assert _value_of(out, "acme_diff_preview_comment_max_bytes") == 51200
    assert _value_of(out, "acme_diff_preview_oci_consecutive_pull_failures") == 2


def test_monotonic_counters_are_typed_counter_so_resets_are_handled():
    """The whole point. increase() over a window only survives a pod
    restart if the series is declared a counter."""
    out = diff_preview.render_prometheus({"comment_fallback_inline": 0,
                                          "section_cap_trims": 0})
    assert _type_of(out, "acme_diff_preview_comment_fallback_inline_total") \
        == "counter"
    assert _type_of(out, "acme_diff_preview_section_cap_trims_total") \
        == "counter"


def test_values_that_can_fall_on_their_own_are_not_counters():
    """comment_max_bytes is a high-water mark and oci_consecutive_pull_
    failures zeroes on the next success. Declaring either a counter would
    make a drop look like a restart and be silently swallowed."""
    out = diff_preview.render_prometheus({"comment_max_bytes": 790,
                                          "oci_consecutive_pull_failures": 0})
    assert _type_of(out, "acme_diff_preview_comment_max_bytes") == "gauge"
    assert _type_of(out, "acme_diff_preview_oci_consecutive_pull_failures") \
        == "gauge"


def test_counter_names_end_in_total_and_gauges_do_not():
    out = diff_preview.render_prometheus(dict(diff_preview._diff_stats))
    for ln in out.split("\n"):
        if ln.startswith("# TYPE "):
            name, kind = ln[len("# TYPE "):].rsplit(" ", 1)
            if kind == "counter":
                assert name.endswith("_total"), name
            else:
                assert not name.endswith("_total"), name


# -- the caps travel with the values ---------------------------------------

def test_the_cap_a_high_water_mark_is_approaching_is_exposed_too():
    """COPS-2577 learned this on the stats payload: a high-water mark is
    meaningless without the cap it is approaching. A threshold hardcoded in
    a monitor drifts the day the cap changes."""
    out = diff_preview.render_prometheus({"comment_max_bytes": 790})
    assert _value_of(out, "acme_diff_preview_comment_max_bytes_limit") == \
        float(diff_preview.MAX_COMMENT_BYTES)


# -- format correctness: a malformed line drops the whole scrape ------------

def test_non_numeric_values_are_skipped_not_emitted_as_garbage():
    """oci_selfcheck is 'ok'/'failed'/None and the timestamps are ISO
    strings. A line like `metric ok` makes a scraper reject the payload, so
    everything else on the page would be lost with it."""
    out = diff_preview.render_prometheus({
        "oci_selfcheck": "ok", "oci_selfcheck_at": "2026-08-08T00:00:00Z",
        "last_iteration_at": None, "comment_max_bytes": 12})
    for ln in _lines(out):
        assert float(ln.rsplit(" ", 1)[1]) is not None
    assert "ok" not in out
    assert _value_of(out, "acme_diff_preview_comment_max_bytes") == 12


def test_booleans_become_one_and_zero():
    lead = diff_preview.render_prometheus({"is_leader": True})
    assert _value_of(lead, "acme_diff_preview_is_leader") == 1
    assert _type_of(lead, "acme_diff_preview_is_leader") == "gauge"
    assert _value_of(diff_preview.render_prometheus({"is_leader": False}),
                     "acme_diff_preview_is_leader") == 0


def test_every_metric_has_a_help_and_a_type_line():
    out = diff_preview.render_prometheus(dict(diff_preview._diff_stats))
    emitted = {ln.split("{")[0].split(" ")[0] for ln in _lines(out)}
    for name in emitted:
        assert "# HELP %s " % name in out, name
        assert "# TYPE %s " % name in out, name


def test_the_running_version_travels_as_a_label_not_a_value():
    """A version is not a number. build_info is the standard way to carry
    it, and it is what tells a monitor whether a reset was a deploy."""
    out = diff_preview.render_prometheus({})
    assert 'acme_diff_preview_build_info{version="%s"} 1'
    assert 'version="%s"' % diff_preview.APP_VERSION in out
    assert _type_of(out, "acme_diff_preview_build_info") == "gauge"


def test_a_label_value_is_escaped():
    """Nothing here is attacker-controlled today, but an unescaped quote in
    a label silently corrupts the scrape rather than failing loudly."""
    out = diff_preview.render_prometheus({}, extra_labels={"repo": 'a"b\\c'})
    assert r'repo="a\"b\\c"' in out


def test_the_payload_ends_with_a_newline():
    """Exposition format requires it; some scrapers drop the last line."""
    out = diff_preview.render_prometheus({"section_cap_trims": 0})
    assert out.endswith("\n")


def test_unknown_keys_are_ignored_rather_than_guessed():
    """A new stats key must not appear as an untyped metric by accident.
    Metrics are a contract with the monitors that read them."""
    out = diff_preview.render_prometheus({"something_new_nobody_declared": 5})
    assert "something_new_nobody_declared" not in out
