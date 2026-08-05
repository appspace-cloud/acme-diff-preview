"""v2.5.18 scale-hardening regression tests (bughunt/FINDINGS_SCALE.md S1-S6).

Every test here was confirmed RED against v2.5.17 before the fixes landed,
except where noted (premise checks). The findings, in one line each:

S1 - the AI prompt capped sections/app and chars/section but NOT the number
     of apps: 300 changed apps built a 4.3MB prompt (~1.08M tokens) that
     exceeds gemini-2.5-flash's context, so mass version-bump PRs silently
     lost their AI summary (and uploaded MBs to Vertex before failing).
S2 - upsert_comment truncated oversized comments from the END, destroying
     the footer's [clean|permanent|transient] and [base:...] tokens; a pod
     restart then re-diffed the whole (unchanged) mass PR from scratch.
S3 - the chart-pull `with ThreadPoolExecutor` blocked in __exit__ until the
     pulls really finished, so a "timed out" diff held its worker slot for
     minutes past DIFF_TIMEOUT exactly when the registry was degraded.
S4 - render/fetch futures abandoned on timeout were never cancel()ed on the
     SHARED subtask pool; each retry stacked more zombies (congestion
     amplification under load).
S5 - the >MAX_APPS_PER_RUN note implied a transient skip when the cut is
     deterministic and permanent for that commit.
S6 - the hand-built no-apps/error bodies wrote 'Commit `sha`' (no bold),
     invisible to _extract_comment_sha — the exact v2.4.6 bug class,
     reintroduced.
"""
import os
import re
import sys
import threading
import time
import concurrent.futures

os.environ.setdefault("BB_USER", "test-user")
os.environ.setdefault("BB_TOKEN", "test-token")
os.environ.setdefault("ARGOCD_PASS", "test-pass")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m


def _mass_results(n_apps, sections_per_app=10, body_chars=1500):
    body = ("--- a\n+++ b\n"
            + ("-  image: repo/svc:1.0.0\n+  image: repo/svc:2.0.0\n" * 40))[:body_chars]
    sections = [(f"/Deployment ns/svc-{i}", body) for i in range(sections_per_app)]
    return {
        f"pv-m{i:03d}-a-ms": m.DiffResult("t", sections, sections_per_app, True,
                                          None, m.OUT_DIFF, "changes")
        for i in range(n_apps)
    }


def _call_ai_with_capture(monkeypatch, app_results):
    """Run generate_ai_summary with Vertex mocked out, returning the prompt."""
    captured = {}
    monkeypatch.setattr(m, "_gcp_access_token", lambda: "tok")

    def fake_http(method, url, body=None, headers=None, auth=None):
        captured["prompt"] = body["contents"][0]["parts"][0]["text"]
        return {"candidates": [{"content": {"parts": [{"text": "AI OK"}]},
                                "finishReason": "STOP"}],
                "usageMetadata": {}}

    monkeypatch.setattr(m, "http", fake_http)
    out = m.generate_ai_summary(app_results)
    return out, captured.get("prompt", "")


# ── S1: AI prompt must be bounded in app count ───────────────────────────

def test_s1_ai_prompt_bounded_at_300_apps(monkeypatch):
    out, prompt = _call_ai_with_capture(monkeypatch, _mass_results(300))
    assert out is not None, "the AI call itself must succeed"
    # gemini-2.5-flash context is ~1M tokens (~4M chars). Keep a wide margin:
    # the capped prompt (40 apps x 10 sections x ~1.5K chars) is ~700KB.
    assert len(prompt) < 1_000_000, f"prompt is {len(prompt)} chars — unbounded"


def test_s1_ai_prompt_notes_omitted_apps(monkeypatch):
    _out, prompt = _call_ai_with_capture(monkeypatch, _mass_results(300))
    assert re.search(r"\d+ more app\(s\) omitted", prompt), \
        "the model must be told the prompt is a sample, not the full set"


def test_s1_ai_headline_still_counts_all_apps(monkeypatch):
    # The deterministic head line is built in code from the FULL result set;
    # capping the prompt must not shrink the numbers a reviewer reads first.
    out, _prompt = _call_ai_with_capture(monkeypatch, _mass_results(300))
    assert out.startswith("**300 app(s) updated")


def test_s1_small_pr_prompt_unchanged(monkeypatch):
    _out, prompt = _call_ai_with_capture(monkeypatch, _mass_results(5))
    for i in range(5):
        assert f"pv-m{i:03d}-a-ms" in prompt
    assert "omitted" not in prompt


# ── S2: comment truncation must preserve the footer tokens ───────────────

def _oversized_comment(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda *_a, **_k: None)
    body = ("--- a\n+++ b\n" + ("-  replicas: 3\n+  replicas: 4\n" * 400))[:7000]
    sections = [(f"/Deployment ns/app-{i}", body) for i in range(10)]
    app_results = {
        f"pv-big{i:02d}-a-ms": m.DiffResult("t", sections, 10, True, None,
                                            m.OUT_DIFF, "changes")
        for i in range(20)
    }
    return m.format_comment("deadbeefcafe1234", app_results,
                            base_sha="0123456789abcdef",
                            # readable_budget=0 = render everything, the way
                            # the persisted full-diff artifact is built. The
                            # comment path now folds ordinary bulk away long
                            # before 245KB, so this is where a body that big
                            # still comes from -- and upsert_comment must
                            # still truncate it without losing the footer.
                            readable_budget=0)


def test_s2_truncated_comment_keeps_footer_tokens(monkeypatch):
    full = _oversized_comment(monkeypatch)
    assert len(full.encode()) > m.MAX_COMMENT_BYTES, "premise: really oversized"
    posted = {}
    monkeypatch.setattr(
        m, "bb",
        lambda method, path, **kw: posted.update(raw=kw["body"]["content"]["raw"]) or {})
    m.upsert_comment(42, full, existing_id=None)
    out = posted["raw"]
    assert len(out.encode("utf-8")) <= m.MAX_COMMENT_BYTES
    assert any(mk in out for mk in m._COMMENT_MARKERS)
    assert "truncated" in out
    # The three machine-readable pieces the dedup design depends on:
    assert m._extract_comment_sha(out) == "deadbeef"
    assert m._extract_status_token(out) == "clean", \
        "footer [clean] token lost — pod restart would re-diff the whole PR"
    assert re.search(r"\[base:01234567\]", out), \
        "footer [base:] token lost — F1 main-advanced check breaks"


def test_s2_truncation_closes_open_code_fence(monkeypatch):
    # The middle cut can land inside a ```diff block; the footer must not
    # end up rendered inside a code fence.
    full = _oversized_comment(monkeypatch)
    posted = {}
    monkeypatch.setattr(
        m, "bb",
        lambda method, path, **kw: posted.update(raw=kw["body"]["content"]["raw"]) or {})
    m.upsert_comment(42, full, existing_id=None)
    assert posted["raw"].count("```") % 2 == 0, "unbalanced code fences after cut"


def test_s2_footerless_body_keeps_legacy_truncation(monkeypatch):
    # Hand-built/legacy bodies without a '---\n**Status:**' footer keep the
    # old end-cut behavior (marker preserved in the note) — same contract
    # test_upsert_comment_truncates_oversized_bodies already pins.
    recorded = []
    monkeypatch.setattr(m, "bb",
                        lambda method, path, **kw: recorded.append(kw) or {"id": 1})
    monkeypatch.setattr(m, "MAX_COMMENT_BYTES", 500)
    m.upsert_comment(10, "x" * 2000)
    sent = recorded[0]["body"]["content"]["raw"]
    assert len(sent.encode()) < 900
    assert "truncated" in sent and m.COMMENT_MARKER in sent


# ── S3: chart-pull timeout must return AT the timeout, not after the pull ─

def test_s3_chart_pull_timeout_returns_promptly(monkeypatch):
    app = "fake-s3-timeout-app"
    monkeypatch.setitem(m._app_chart_map, app, "some-chart")
    monkeypatch.setitem(m._app_chart_revision_map, app, "1.0.0")
    monkeypatch.setitem(m._app_chart_registry_map, app, "reg.example.com")
    monkeypatch.setitem(m._app_value_files_map, app, ["$config/a/customer.yaml"])
    monkeypatch.setitem(m._app_namespace_map, app, "ns")
    monkeypatch.setattr(m, "DIFF_TIMEOUT", 1)
    release = threading.Event()

    def slow_pull(_reg, _chart, _ver):
        release.wait(3.5)   # simulates a pull stuck well past DIFF_TIMEOUT
        return None

    monkeypatch.setattr(m, "_ensure_chart", slow_pull)
    t0 = time.monotonic()
    res = m._run_one_diff(app, "a" * 40, "b" * 40)
    elapsed = time.monotonic() - t0
    release.set()   # let the background pull threads exit immediately
    assert res[1] == m.REASON_TIMEOUT
    # v2.5.17 blocked in the pool's __exit__ until the pulls finished (~3.5s);
    # the DIFF_TIMEOUT contract is a ~1s return.
    assert elapsed < 2.5, f"took {elapsed:.1f}s — worker held past DIFF_TIMEOUT"


# ── S4: a timed-out diff must cancel every future it abandoned ────────────

class _RecordingFuture:
    def __init__(self, real):
        self._real = real
        self.cancel_called = False

    def cancel(self):
        self.cancel_called = True
        return self._real.cancel()

    def result(self, timeout=None):
        return self._real.result(timeout=timeout)

    def done(self):
        return self._real.done()


class _RecordingPool:
    def __init__(self, real):
        self._real = real
        self.futures = []

    def submit(self, fn, *a, **kw):
        f = _RecordingFuture(self._real.submit(fn, *a, **kw))
        self.futures.append(f)
        return f


def test_s4_render_timeout_cancels_abandoned_futures(monkeypatch):
    app = "fake-s4-render-timeout-app"
    monkeypatch.setitem(m._app_chart_map, app, "some-chart")
    monkeypatch.setitem(m._app_chart_revision_map, app, "1.0.0")
    monkeypatch.setitem(m._app_chart_registry_map, app, "reg.example.com")
    monkeypatch.setitem(m._app_value_files_map, app, ["$config/a/customer.yaml"])
    monkeypatch.setitem(m._app_namespace_map, app, "ns")
    monkeypatch.setattr(m, "DIFF_TIMEOUT", 1)
    monkeypatch.setattr(m, "_ensure_chart", lambda _r, _c, _v: "/tmp/fake-chart")
    monkeypatch.setattr(m, "_fetch_value_files",
                        lambda vfs, _sha: {vf: "x: 1\n" for vf in vfs})
    release = threading.Event()

    def slow_template(_chart, _release, _ns, _vals):
        release.wait(3.5)
        return "kind: ConfigMap\nmetadata:\n  name: a\n", None

    monkeypatch.setattr(m, "_helm_template", slow_template)
    real_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    rec = _RecordingPool(real_pool)
    monkeypatch.setattr(m, "_get_subtask_pool", lambda: rec)
    try:
        res = m._run_one_diff(app, "c" * 40, "d" * 40)
        assert res[1] == m.REASON_TIMEOUT
        pending = [f for f in rec.futures if not f.done()]
        assert pending, "premise: something must still be in flight at timeout"
        assert all(f.cancel_called for f in rec.futures), \
            "timed-out diff abandoned shared-pool futures without cancel()"
    finally:
        release.set()
        real_pool.shutdown(wait=True)


# ── S5: the over-cap note must state the permanence and the remedy ────────

def test_s5_skipped_apps_note_states_permanence_and_remedy(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda *_a, **_k: None)
    res = {"app-a": m.DiffResult("", [], 0, False, None, m.OUT_NO_DIFF, "clean")}
    body = m.format_comment("deadbeefcafe1234", res,
                            skipped_apps=["x1", "x2", "x3"],
                            base_sha="0123456789abcdef")
    assert "MAX_APPS_PER_RUN" in body, "must name the knob that fixes it"
    assert "will not be evaluated" in body, "must state the skip is permanent"


# ── S6: every comment body header must round-trip through the extractor ──

def test_s6_comment_header_roundtrips_with_extractor():
    hdr = m._comment_header("deadbeefcafe1234")
    assert m._extract_comment_sha(hdr + "\nrest of the comment") == "deadbeef"


def test_s6_format_comment_uses_the_shared_header(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda *_a, **_k: None)
    res = {"a": m.DiffResult("", [], 0, False, None, m.OUT_NO_DIFF, "clean")}
    body = m.format_comment("deadbeefcafe1234", res, base_sha="0123456789abcdef")
    assert m._comment_header("deadbeefcafe1234") in body


def test_s6_no_plain_commit_headers_left_in_source():
    """Source-grep guard: any comment body built with a plain (non-bold)
    'Commit `sha`' header silently breaks _extract_comment_sha again — the
    v2.4.6 generated-vs-parsed bug class. All bodies must go through
    _comment_header()."""
    src_path = os.path.join(os.path.dirname(__file__), "..", "src", "diff_preview.py")
    with open(src_path) as f:
        src = f.read()
    assert not re.search(r'f"Commit `\{pr_sha', src), \
        "hand-built plain 'Commit `sha`' header found — use _comment_header()"
