"""BB API call visibility and clearer schema failures (COPS-2564).

Three problems found while investigating acme-config-prod PR 3837
("Disable Spot compute class for GCP microservices"):

1. Nobody can answer "how many Bitbucket API calls did this PR cost?".
   Every file read goes through _bb_fetch_status and every REST call
   through bb(), but neither is counted, so the only evidence of API
   pressure was 429s after the fact.

2. The PR comment truncated the schema failure mid-token:
   "at '/appspace/microservices/definitions/a". helm's stderr is capped
   at 400 characters before anything parses it, and that PR produced 53
   violations, so the reader could not see which services were broken.

3. The 53 violations all said "got null, want object", caused by
   commenting out the only body of a definitions entry. The generic
   "correct each value listed above" gives no clue that the fix is
   `service: {}`, nor that deleting the key would delete the microservice.
"""
import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m

SCHEMA_ERR = (
    "Error: values don't meet the specifications of the schema(s) in the "
    "following chart(s):\nappspace-micro-services:\n"
    + "\n".join(
        f"- at '/appspace/microservices/definitions/service-{i:02d}': "
        f"got null, want object" for i in range(53))
    + "\n"
)


# ── 1. BB API call counting ─────────────────────────────────────────────────

def test_bb_file_fetches_are_counted(monkeypatch):
    """The hot path (one call per value file) must be counted, so the cost of
    a mass PR is a number in the log instead of a guess."""
    m.reset_bb_call_stats()

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"x: 1"
    monkeypatch.setattr(m.urllib.request, "urlopen", lambda *a, **k: FakeResp())

    m._bb_fetch_status("gcp/config.yaml", "sha1")
    m._bb_fetch_status("gcp/other.yaml", "sha1")
    assert m.bb_call_stats()["file_fetches"] == 2


def test_cached_reads_do_not_count_as_api_calls(monkeypatch):
    """A cache hit must not increment the counter, or the number stops
    meaning "calls we actually made to Bitbucket"."""
    m.reset_bb_call_stats()
    calls = []

    def fake(p, s, repo=None):
        calls.append(1)
        return ("x: 1", m.BB_OK)
    monkeypatch.setattr(m, "_bb_fetch_status", fake)
    m._vf_cache.clear()
    m._bb_fetch_cached("gcp/config.yaml", "sha1")
    m._bb_fetch_cached("gcp/config.yaml", "sha1")
    assert len(calls) == 1, "second read must come from the cache"


def test_rate_limited_calls_are_counted_separately(monkeypatch):
    """429s are the signal that matters, so they need their own counter and a
    ratio against total calls without grepping logs."""
    m.reset_bb_call_stats()
    err = m.urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)

    def boom(*a, **k):
        raise err
    monkeypatch.setattr(m.urllib.request, "urlopen", boom)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    m._bb_fetch_status("gcp/config.yaml", "sha1")
    s = m.bb_call_stats()
    assert s["rate_limited"] >= 1
    assert s["file_fetches"] >= 1


def test_stats_survive_concurrent_increments():
    """16 diff workers touch these counters at once; a lost update would
    understate the number we are trying to trust."""
    import threading
    m.reset_bb_call_stats()

    def bump():
        for _ in range(500):
            m._count_bb_call("file_fetches")
    ts = [threading.Thread(target=bump) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert m.bb_call_stats()["file_fetches"] == 4000


# ── 2. schema failures must not be truncated mid-token ──────────────────────

def test_schema_error_is_not_cut_mid_token():
    out = "\n".join(m._explain_schema_error(SCHEMA_ERR))
    assert "definitions/service-00" in out
    for line in out.splitlines():
        assert not line.rstrip().endswith("definitions/"), f"cut mid path: {line}"


def test_long_violation_list_is_capped_by_count_with_a_remainder():
    lines = m._explain_schema_error(SCHEMA_ERR)
    shown = [l for l in lines if "got null" in l]
    assert len(shown) <= 12, f"{len(shown)} violation lines is a wall of text"
    assert any("more" in l for l in lines), "must say how many were not shown"


def test_short_violation_list_is_shown_in_full():
    err = ("Error: values don't meet the specifications:\n"
           "- at '/appspace/x': got null, want object\n"
           "- at '/appspace/y': got string, want integer\n")
    lines = m._explain_schema_error(err)
    assert sum("at '/appspace" in l for l in lines) == 2
    assert not any("more" in l for l in lines)


def test_helm_stderr_cap_keeps_the_whole_violation_list():
    """The 400-char cap on helm stderr is what cut the list mid-token, so a
    schema failure must survive it intact."""
    kept = m._cap_helm_error(SCHEMA_ERR)
    assert kept.count("got null") == 53, kept.count("got null")


def test_helm_stderr_cap_still_bounds_ordinary_errors():
    long_err = "boom " * 500
    assert len(m._cap_helm_error(long_err)) <= 600


# ── 3. the null-collapse class needs its own actionable hint ────────────────

def test_null_violations_get_a_specific_fix_hint():
    hint = "\n".join(m._schema_fix_hints(SCHEMA_ERR))
    assert "{}" in hint, "must show the empty-map fix"
    assert "comment" in hint.lower() or "removed" in hint.lower()
    assert "delete" in hint.lower() or "deleting" in hint.lower()


def test_definitions_nulls_warn_about_deleting_the_microservice():
    hint = "\n".join(m._schema_fix_hints(SCHEMA_ERR)).lower()
    assert "microservice" in hint


def test_non_null_schema_errors_do_not_get_the_null_hint():
    err = "- at '/appspace/version': got integer, want string\n"
    assert "{}" not in "\n".join(m._schema_fix_hints(err))


# ── 4. the constant collision introduced in COPS-2562 ───────────────────────

def test_identity_basenames_is_defined_once():
    src = open(os.path.join(os.path.dirname(__file__), "..", "src",
                            "diff_preview.py")).read()
    assert src.count("\n_IDENTITY_BASENAMES") == 1, "shadowed constant"


def test_identity_basename_matching_is_still_exact():
    assert m._changed_files_with_bad_names(
        ["gcp/x/mycustomer.yaml", "gcp/x/app-config.yaml"], "pr", "base") == {}


def test_per_pr_cost_is_measured_as_a_delta():
    """Per-PR attribution must be a delta of the shared counters, so a PR is
    charged for the calls it caused even though the cache is global."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "src",
                            "diff_preview.py")).read()
    body = src.split("def process_pr", 1)[1]
    assert "_bb_at_pr_start = bb_call_stats()" in body
    assert '- _bb_at_pr_start["file_fetches"]' in body


def test_iteration_resets_and_reports_the_counters():
    src = open(os.path.join(os.path.dirname(__file__), "..", "src",
                            "diff_preview.py")).read()
    assert "reset_bb_call_stats()" in src
    assert "bb_calls=bb_total" in src
