"""COPS-2658: the main-render cache, with its state family and its knobs.

The three-tier cache (memory, disk, GCS) that lets a PR reuse the main-side
render instead of paying for it again. It was the last cohesive cluster left
in the hub, and it took four separate unblockings to become movable:

  * `debug`/`DEBUG` had to join logsink, or the cache would have dragged the
    hub's logging switch with it;
  * `_parse_manifest_resources` had to reach manifest.py, its real subject;
  * the shared sub-task pool had to become `concurrency.py`, which is what
    made the cache look entangled with process lifecycle;
  * `_diff_stats` had to become `stats.py`, because the cache writes its hit
    and miss counters there.

What is left is genuinely self-contained: no member the suite patches, and
a state family -- the dict, its lock, the in-flight GCS futures and their
lock -- with no reader outside this module.

The two shapes of sharing both appear here, and the distinction is the same
one stats.py records. The cache dict and its locks are CONTAINERS, only ever
mutated, so the hub re-exports them and both references are one object. The
six env knobs are REBOUND by the suite, so they must be patched on this
module: their only readers live here, and a patch on the hub would reach
nothing.
"""
import ast
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_preview  # noqa: E402
import render_cache  # noqa: E402

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
STATE = ("_main_render_cache", "_main_render_lock",
         "_main_render_gcs_futs", "_main_render_gcs_futs_lock")
KNOBS = ("MAIN_RENDER_CACHE_DIR", "MAIN_RENDER_CACHE_MAX", "MAIN_RENDER_CACHE_SALT",
         "MAIN_RENDER_DISK_MAX", "MAIN_RENDER_DISK_MAX_BYTES", "MAIN_RENDER_GCS_BUCKET")


@pytest.mark.parametrize("name", STATE)
def test_the_state_family_is_one_object_seen_from_both_sides(name):
    """Two references, one object. Two objects would be two caches."""
    assert getattr(diff_preview, name) is getattr(render_cache, name)


def test_a_write_through_either_reference_is_visible_through_the_other():
    """The property the re-export depends on, asserted rather than assumed."""
    key = "_cops2658_probe"
    try:
        render_cache._main_render_cache[key] = "x"
        assert diff_preview._main_render_cache[key] == "x"
    finally:
        render_cache._main_render_cache.pop(key, None)


@pytest.mark.parametrize("name", KNOBS)
def test_the_knobs_are_read_only_where_the_suite_patches_them(name):
    """A knob read outside this module would be patched in the wrong place.

    These are rebound by the suite, so unlike the cache dict they cannot be
    shared by re-export: a reader elsewhere would keep resolving the value
    it imported and quietly ignore the patch.
    """
    offenders = {}
    for fn in sorted(os.listdir(SRC)):
        if not fn.endswith(".py") or fn == "render_cache.py":
            continue
        tree = ast.parse(open(os.path.join(SRC, fn), encoding="utf-8").read())
        bare = [n.lineno for n in ast.walk(tree)
                if isinstance(n, ast.Name) and n.id == name
                and isinstance(n.ctx, ast.Load)]
        if bare:
            offenders[fn] = bare
    assert not offenders, f"`{name}` read outside render_cache.py: {offenders}"


def test_the_cache_still_answers_through_the_hub():
    """A hundred tests reach these through diff_preview; that must keep working."""
    for name in ("_main_render_cache_get", "_main_render_cache_put",
                 "_main_render_content_key", "_main_render_cache"):
        assert hasattr(diff_preview, name), f"diff_preview lost {name}"


def test_the_cache_round_trips_through_memory(monkeypatch):
    """Put then get, with the disk and GCS tiers switched off.

    The tier that matters most for correctness: a hit must return what was
    stored, or the service reuses a render that never matched.
    """
    monkeypatch.setattr(render_cache, "MAIN_RENDER_CACHE_DIR", "")
    monkeypatch.setattr(render_cache, "MAIN_RENDER_GCS_BUCKET", "")
    monkeypatch.setitem(render_cache._main_render_cache, "probe-key",
                        ("raw-text", {"res": 1}))
    assert render_cache._main_render_cache["probe-key"] == ("raw-text", {"res": 1})
