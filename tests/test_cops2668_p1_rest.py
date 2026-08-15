"""Audit remediation, batch 3: the remaining ways to be wrong (COPS-2668 P1).

Five defects that share a shape with the P0 batch — each one lets the service
state something it does not know — plus one log-redaction gap.

1. `post_build_status` is the only Bitbucket write COPS-2654 did not gate on
   `_still_leader()`. Comment writes, artifact writes and PR entry are all
   gated; the merge gate itself is not. A demoted leader finishing an
   in-flight PR therefore writes a status the new leader will not overwrite
   (it has its own view of the world), leaving a sticky verdict from a pod
   that no longer speaks for the fleet.

2. `_hash_chart_tree` substitutes a fixed `b"unreadable"` sentinel when a
   chart file cannot be read. Two different unreadable trees hash the same,
   and an unreadable tree hashes the same as one whose file literally
   contains that sentinel — so an I/O blip mints a confident, collidable
   render-cache key. `render_cache.py`'s own docstring says the opposite:
   "Default to cache-miss on any doubt".

3. The durable cache key omits the helm binary version, and the salt bump is
   documented only in a README table row that RELEASING.md never mentions —
   `git log -S cops2631-v1` shows it has never been bumped. A routine helm
   upgrade therefore serves renders produced by the OLD binary to every PR
   until the entries age out.

4. `_rename_identity_confirmed` caches a verdict derived from a FETCH
   FAILURE. The degrade-to-trust is deliberate and fine; remembering it is
   not. One transient blip pins "this rename is genuine" for the pod's
   lifetime, and that verdict is what suppresses a decommission warning.

5. The decommission inventory `continue`s past any app whose main-side render
   raised, so the summary counts resources from the apps that rendered and
   silently omits the rest — an undercount presented as the inventory of what
   is about to be orphaned.

6. Raw helm stderr reaches Cloud Logging unredacted. `_redact_error_detail`
   exists and protects the comment; the logging path never calls it.
"""
import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
import chart_identity
import diff_preview as m
import render_cache


# ── 1. the merge gate must respect leadership ────────────────────────────

def test_post_build_status_is_gated_on_leadership(monkeypatch):
    """A demoted leader must not write the merge gate."""
    monkeypatch.setattr(m, "_still_leader", lambda: False)
    calls = []
    monkeypatch.setattr(m, "http", lambda *a, **k: calls.append(a) or {})
    m.post_build_status("deadbeef01234567", "SUCCESSFUL", "x", pr_id=1)
    assert calls == [], (
        "a pod that has lost the lease must not write a build status the "
        "current leader did not decide")


def test_post_build_status_still_writes_while_leading(monkeypatch):
    monkeypatch.setattr(m, "_still_leader", lambda: True)
    calls = []
    monkeypatch.setattr(m, "http", lambda *a, **k: calls.append(a) or {})
    m.post_build_status("deadbeef01234567", "SUCCESSFUL", "x", pr_id=1)
    assert calls, "the leader must still post normally"


# ── 2. an unreadable chart file is a cache miss, not a sentinel ──────────

def test_unreadable_chart_file_refuses_to_produce_a_key(tmp_path):
    """A confident key over content we could not read is how a wrong diff
    gets served from cache."""
    chart = tmp_path / "chart"
    chart.mkdir()
    (chart / "Chart.yaml").write_text("name: x\n")
    bad = chart / "values.yaml"
    bad.write_text("a: 1\n")
    bad.chmod(0o000)
    try:
        with pytest.raises(chart_identity.ChartTreeUnreadable):
            chart_identity._hash_chart_tree(str(chart))
    finally:
        bad.chmod(0o644)


def test_readable_chart_tree_still_hashes(tmp_path):
    chart = tmp_path / "chart"
    chart.mkdir()
    (chart / "Chart.yaml").write_text("name: x\n")
    assert chart_identity._hash_chart_tree(str(chart))


def test_cache_get_and_put_are_noops_without_a_key():
    """The bypass has to be safe at both ends."""
    assert render_cache._main_render_cache_get(None) == (None, None, "miss")
    render_cache._main_render_cache_put(None, "raw", {})   # must not raise


# ── 3. the durable key must move when the renderer moves ─────────────────

def test_content_key_includes_the_helm_version(monkeypatch, tmp_path):
    chart = tmp_path / "chart"
    chart.mkdir()
    (chart / "Chart.yaml").write_text("name: x\n")

    monkeypatch.setattr(render_cache, "_helm_binary_version", lambda: "v3.21.2")
    a = render_cache._main_render_content_key(str(chart), "r", "ns", {})
    monkeypatch.setattr(render_cache, "_helm_binary_version", lambda: "v3.22.0")
    b = render_cache._main_render_content_key(str(chart), "r", "ns", {})
    assert a != b, (
        "a helm upgrade changes render output, so it must change the key; "
        "otherwise every pod serves old-binary renders until they age out")


def test_releasing_documents_the_salt():
    """The one manual guard on the durable tier must appear in the checklist
    that people actually follow, not only in a README table row."""
    with open(os.path.join(os.path.dirname(__file__), "..", "RELEASING.md")) as f:
        text = f.read()
    assert "MAIN_RENDER_CACHE_SALT" in text


# ── 4. never cache a verdict derived from a failure ──────────────────────

def test_rename_verdict_from_a_fetch_failure_is_not_cached(monkeypatch):
    """The degrade-to-trust is fine. Remembering it is not: it suppresses a
    decommission warning for the life of the pod."""
    with m._identity_rename_verdict_lock:
        m._identity_rename_verdict_cache.clear()

    def _boom(path, sha):
        raise RuntimeError("bitbucket down")
    monkeypatch.setattr(m, "_bb_fetch_cached", _boom)

    m._rename_identity_confirmed("old/customer.yaml", "new/customer.yaml",
                                 "a" * 12, "b" * 12)
    with m._identity_rename_verdict_lock:
        assert not m._identity_rename_verdict_cache, (
            "a verdict computed from a failed fetch must not be remembered")


def test_rename_verdict_from_a_real_answer_is_cached(monkeypatch):
    """Caching the genuine answer is the point of the cache; keep it."""
    with m._identity_rename_verdict_lock:
        m._identity_rename_verdict_cache.clear()
    monkeypatch.setattr(m, "_bb_fetch_cached",
                        lambda path, sha: ("appspace:\n  name: env-a\n", m.BB_OK))
    m._rename_identity_confirmed("old/customer.yaml", "new/customer.yaml",
                                 "a" * 12, "b" * 12)
    with m._identity_rename_verdict_lock:
        assert m._identity_rename_verdict_cache, \
            "a verdict from real content must still be cached"


# ── 5. an inventory that skipped apps is not an inventory ────────────────

def test_decommission_inventory_reports_apps_it_could_not_render(monkeypatch):
    """Counting only the apps that rendered, and presenting that as what the
    cascade will orphan, understates the blast radius."""
    import inspect
    src = inspect.getsource(m._evaluate_env_decommissions)
    assert "unrendered" in src or "could not be rendered" in src, (
        "the decommission path must account for apps whose main-side render "
        "failed, not silently continue past them")


# ── 6. the log sink deserves the same redaction as the comment ───────────

def test_helm_stderr_is_redacted_before_logging(monkeypatch):
    """R2 protects the comment; Cloud Logging was getting the raw bytes."""
    seen = []
    monkeypatch.setattr(m.logsink, "debug",
                        lambda msg, **k: seen.append(str(k.get("detail", "")) + str(msg)))
    monkeypatch.setattr(m.logsink, "log", lambda msg, *a, **k: seen.append(str(msg)))

    secret = "password: hunter2supersecret"
    redacted = m._redact_error_detail(secret)
    assert "hunter2supersecret" not in redacted, (
        "sanity: the redactor must actually mask this shape")

    import inspect
    src = inspect.getsource(m.argocd_diff)
    assert "_redact_error_detail(detail" in src or "_safe_detail" in src, (
        "the diff-step logging path must redact before emitting")
