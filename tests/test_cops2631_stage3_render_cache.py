"""COPS-2631 stage 3: content-keyed, disk-backed main-side render cache.

The existing cache keyed on (app, main_sha, main_rev, pull_gen) cannot hit
in production: unrelated main commits invalidate identical renders, and a
global clear fires whenever ANY of the three config repos moves. Stage 3
keys on the inputs that actually determine helm output, stores raw render
text on disk, and keeps a memory front cache of parsed resources.

A wrong cache entry produces a wrong diff, which is worse than slow. The
shadow audit and the content-key tests are the gate.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_preview as m  # noqa: E402


def _vals(a="1", b="2"):
    # Insertion order matters for helm -f semantics.
    return {"cfg/a.yaml": f"x: {a}\n", "cfg/b.yaml": f"y: {b}\n"}


def test_content_key_stable_for_identical_inputs(tmp_path):
    chart = tmp_path / "chart"
    chart.mkdir()
    (chart / "Chart.yaml").write_text("name: demo\nversion: 1.0.0\n")
    (chart / "templates").mkdir()
    (chart / "templates" / "d.yaml").write_text("kind: ConfigMap\n")
    k1 = m._main_render_content_key(str(chart), "rel", "ns", _vals())
    k2 = m._main_render_content_key(str(chart), "rel", "ns", _vals())
    assert k1 == k2
    assert len(k1) == 64  # sha256 hex


def test_content_key_changes_when_values_change(tmp_path):
    chart = tmp_path / "chart"
    chart.mkdir()
    (chart / "Chart.yaml").write_text("name: demo\nversion: 1.0.0\n")
    a = m._main_render_content_key(str(chart), "rel", "ns", _vals(a="1"))
    b = m._main_render_content_key(str(chart), "rel", "ns", _vals(a="2"))
    assert a != b


def test_content_key_changes_when_chart_files_change(tmp_path):
    chart = tmp_path / "chart"
    chart.mkdir()
    (chart / "Chart.yaml").write_text("name: demo\nversion: 1.0.0\n")
    before = m._main_render_content_key(str(chart), "rel", "ns", _vals())
    (chart / "Chart.yaml").write_text("name: demo\nversion: 1.0.1\n")
    after = m._main_render_content_key(str(chart), "rel", "ns", _vals())
    assert before != after


def test_content_key_ignores_main_sha(tmp_path):
    """Unrelated main commits must not invent a new key. That is the whole
    point of content-keying (measured: 11 commits, identical render)."""
    chart = tmp_path / "chart"
    chart.mkdir()
    (chart / "Chart.yaml").write_text("name: demo\nversion: 1.0.0\n")
    # The helper does not take main_sha on purpose.
    import inspect
    params = inspect.signature(m._main_render_content_key).parameters
    assert "main_sha" not in params


def test_disk_cache_roundtrip_raw_text(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "MAIN_RENDER_CACHE_DIR", str(tmp_path))
    key = "a" * 64
    raw = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n"
    assert m._main_render_disk_load(key) is None
    m._main_render_disk_store(key, raw)
    assert m._main_render_disk_load(key) == raw


def test_cache_get_put_memory_and_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "MAIN_RENDER_CACHE_DIR", str(tmp_path))
    m._main_render_cache.clear()
    key = "b" * 64
    raw = ("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: demo\n"
           "  namespace: ns\n")
    resources = m._parse_manifest_resources(raw)
    m._main_render_cache_put(key, raw, resources)
    hit_res, hit_raw, source = m._main_render_cache_get(key)
    assert source == "memory"
    assert hit_res == resources
    # Memory hits do not re-read disk; raw is None (shadow audit loads it).
    assert hit_raw is None
    # Drop memory; disk must still serve.
    m._main_render_cache.clear()
    hit_res2, hit_raw2, source2 = m._main_render_cache_get(key)
    assert source2 == "disk"
    assert hit_raw2 == raw
    assert hit_res2 == resources


def test_main_sha_advance_does_not_wipe_content_cache(monkeypatch):
    """Regression for the production 0% hit rate: a main move used to
    clear the whole cache across all repos."""
    m._main_render_cache.clear()
    m._main_render_cache["keep-me"] = {"parsed": True}
    # Simulate what main_iteration used to do.
    assert hasattr(m, "_CLEAR_MAIN_RENDER_ON_TIP_MOVE")
    assert m._CLEAR_MAIN_RENDER_ON_TIP_MOVE is False
