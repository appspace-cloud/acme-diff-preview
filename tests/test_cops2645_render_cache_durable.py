"""COPS-2645: the render cache has to outlive the pod.

COPS-2631 stage 3 fixed a cache that could not hit (wrong key, global
invalidation). It still reported 0 hits in production for a different
reason: it lives in the /tmp emptyDir, which dies with the pod, and the
pod is replaced often -- three times in ninety minutes during the
2026-08-11 audit (a deploy plus an autoscaler zone move). The standby
holds nothing at all, because only the leader renders, so a lease
handover also starts from zero.

So the chain gains a durable tier: memory -> disk -> GCS -> render. Two
capacity defects found in the same code are fixed here too, because all
three reduce how much cache actually survives:

- memory eviction deleted the DISK entry as well, so disk could never
  hold more than memory had recently held;
- eviction was FIFO (insertion order, no move-on-hit), so hot keys could
  be dropped before cold ones.

A wrong entry produces a wrong diff, which is worse than slow. The
shadow audit therefore extends to GCS hits, and a mismatch must remove
the poisoned object from the BUCKET too -- otherwise it re-infects every
fresh pod that warms from it.
"""
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_preview as m  # noqa: E402
import diff_ui  # noqa: E402


def _reset(tmp_path, monkeypatch, bucket=""):
    """Isolate every cache tier onto tmp_path and a fake bucket."""
    monkeypatch.setattr(m, "MAIN_RENDER_CACHE_DIR", str(tmp_path / "renders"))
    monkeypatch.setattr(m, "MAIN_RENDER_GCS_BUCKET", bucket, raising=False)
    with m._main_render_lock:
        m._main_render_cache.clear()
    return {}


class _FakeBucket:
    """Stands in for GCS at the diff_ui._gcs_* boundary. No network."""

    def __init__(self, fail_upload=False):
        self.objects = {}
        self.fail_upload = fail_upload
        self.uploads = []
        self.downloads = []
        self.deletes = []

    def install(self, monkeypatch):
        def up(bucket, name, data):
            self.uploads.append(name)
            if self.fail_upload:
                return False
            self.objects[name] = data
            return True

        def down(bucket, name):
            self.downloads.append(name)
            return self.objects.get(name)

        def dele(bucket, name):
            self.deletes.append(name)
            self.objects.pop(name, None)
            return True

        monkeypatch.setattr(diff_ui, "_gcs_upload", up)
        monkeypatch.setattr(diff_ui, "_gcs_download", down)
        monkeypatch.setattr(diff_ui, "_gcs_delete", dele)
        return self


RAW = "kind: ConfigMap\nmetadata:\n  name: demo\n"


def _parsed():
    return m._parse_manifest_resources(RAW)


# --- 1. memory eviction must not take the disk entry with it --------------

def test_memory_eviction_keeps_the_disk_entry(tmp_path, monkeypatch):
    """Disk is the primary store; memory is a front cache.

    Deleting the disk file when a key falls out of memory capped the disk
    tier at "whatever memory recently held", which is the opposite of the
    stage-3 design. Disk has its own count and byte caps and prunes itself.
    """
    _reset(tmp_path, monkeypatch)
    monkeypatch.setattr(m, "MAIN_RENDER_CACHE_MAX", 4)
    keys = [f"k{i:02d}" for i in range(12)]
    for k in keys:
        m._main_render_cache_put(k, RAW + k, _parsed())
    with m._main_render_lock:
        in_memory = set(m._main_render_cache)
    assert len(in_memory) <= 4, "memory cap must still be enforced"
    evicted = [k for k in keys if k not in in_memory]
    assert evicted, "the test needs at least one evicted key to be meaningful"
    for k in evicted:
        assert os.path.exists(m._main_render_disk_path(k)), (
            f"{k} was evicted from memory and lost from disk too")


def test_an_evicted_key_is_still_a_hit_from_disk(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    monkeypatch.setattr(m, "MAIN_RENDER_CACHE_MAX", 2)
    for k in ("a", "b", "c", "d", "e"):
        m._main_render_cache_put(k, RAW + k, _parsed())
    resources, raw, source = m._main_render_cache_get("a")
    assert resources is not None, "an evicted key must still hit from disk"
    assert source == "disk"


# --- 2. eviction order is LRU, not insertion order ------------------------

def test_a_hit_protects_a_key_from_eviction(tmp_path, monkeypatch):
    """FIFO eviction drops the oldest INSERT even if it is the hottest key.

    On a mixed dev/stage/prod workload that evicts the keys being reused
    and keeps the ones nobody asked for again.
    """
    _reset(tmp_path, monkeypatch)
    monkeypatch.setattr(m, "MAIN_RENDER_CACHE_MAX", 3)
    for k in ("old", "mid", "new"):
        m._main_render_cache_put(k, RAW + k, _parsed())
    # "old" is the hot key: touch it, then push past the cap.
    m._main_render_cache_get("old")
    m._main_render_cache_put("newest", RAW + "newest", _parsed())
    with m._main_render_lock:
        in_memory = set(m._main_render_cache)
    assert "old" in in_memory, "a key that was just hit must survive eviction"


# --- 3. the durable tier -------------------------------------------------

def test_a_cold_pod_warms_from_the_bucket(tmp_path, monkeypatch):
    """The whole point: a replacement pod must not re-render the fleet."""
    fake = _FakeBucket().install(monkeypatch)
    _reset(tmp_path, monkeypatch, bucket="b")
    m._main_render_cache_put("key1", RAW, _parsed())
    m._main_render_gcs_flush()          # uploads are off the diff path
    assert fake.objects, "put must mirror the entry to the bucket"

    # A brand-new pod: empty memory, empty disk, same bucket.
    _reset(tmp_path / "fresh", monkeypatch, bucket="b")
    resources, raw, source = m._main_render_cache_get("key1")
    assert resources is not None, "cold pod must warm from the bucket"
    assert source == "gcs"
    assert raw == RAW, "the shadow audit needs the exact bytes that were served"


def test_a_gcs_hit_warms_the_local_tiers(tmp_path, monkeypatch):
    fake = _FakeBucket().install(monkeypatch)
    _reset(tmp_path, monkeypatch, bucket="b")
    m._main_render_cache_put("key1", RAW, _parsed())
    m._main_render_gcs_flush()
    _reset(tmp_path / "fresh", monkeypatch, bucket="b")
    m._main_render_cache_get("key1")
    before = len(fake.downloads)
    _r, _raw, source = m._main_render_cache_get("key1")
    assert source == "memory", "a warmed key must not go back to the network"
    assert len(fake.downloads) == before


def test_a_salt_bump_does_not_serve_the_old_object(tmp_path, monkeypatch):
    """The salt is the CacheVersion equivalent: a render-affecting code
    change must orphan every existing object, not reuse it."""
    fake = _FakeBucket().install(monkeypatch)
    _reset(tmp_path, monkeypatch, bucket="b")
    monkeypatch.setattr(m, "MAIN_RENDER_CACHE_SALT", "salt-v1")
    m._main_render_cache_put("key1", RAW, _parsed())
    m._main_render_gcs_flush()
    old_names = set(fake.objects)

    _reset(tmp_path / "fresh", monkeypatch, bucket="b")
    monkeypatch.setattr(m, "MAIN_RENDER_CACHE_SALT", "salt-v2")
    resources, _raw, source = m._main_render_cache_get("key1")
    assert resources is None and source == "miss", (
        "a salt bump must not resolve to an object written under the old salt")
    assert old_names.isdisjoint(set(fake.downloads)), (
        "the object name must carry the salt, not just the content key")


def test_no_bucket_configured_is_a_plain_local_cache(tmp_path, monkeypatch):
    fake = _FakeBucket().install(monkeypatch)
    _reset(tmp_path, monkeypatch, bucket="")
    m._main_render_cache_put("key1", RAW, _parsed())
    m._main_render_gcs_flush()
    assert not fake.uploads, "no bucket means no network calls at all"
    _reset(tmp_path / "fresh", monkeypatch, bucket="")
    resources, _raw, source = m._main_render_cache_get("key1")
    assert resources is None and source == "miss"
    assert not fake.downloads


def test_a_failing_bucket_is_never_fatal(tmp_path, monkeypatch):
    """A bucket outage must slow nothing and fail nothing: the diff run
    keeps working, it just loses durability until the bucket returns."""
    fake = _FakeBucket(fail_upload=True).install(monkeypatch)
    _reset(tmp_path, monkeypatch, bucket="b")
    m._main_render_cache_put("key1", RAW, _parsed())
    m._main_render_gcs_flush()
    assert fake.uploads, "an upload was attempted"
    assert not fake.objects, "and it failed"
    resources, _raw, source = m._main_render_cache_get("key1")
    assert resources is not None and source == "memory", (
        "local tiers keep serving when the bucket is unavailable")


def test_a_bucket_read_error_falls_through_to_a_miss(tmp_path, monkeypatch):
    """Corrupt or undecodable object must be a miss, never an exception and
    never a half-parsed entry. Any doubt defaults to re-rendering."""
    fake = _FakeBucket().install(monkeypatch)
    _reset(tmp_path, monkeypatch, bucket="b")
    m._main_render_cache_put("key1", RAW, _parsed())
    m._main_render_gcs_flush()
    for name in list(fake.objects):
        fake.objects[name] = b"\x28\xb5\x2f\xfd-not-a-valid-frame"
    _reset(tmp_path / "fresh", monkeypatch, bucket="b")
    resources, _raw, source = m._main_render_cache_get("key1")
    assert resources is None and source == "miss"


# --- 4. correctness: a poisoned object must not survive the audit ---------

def test_shadow_discard_removes_the_object_from_the_bucket(tmp_path,
                                                           monkeypatch):
    """A wrong entry in a store that dies in an hour is bad. A wrong object
    in a durable store re-infects every fresh pod that warms from it, which
    is why the discard has to reach the bucket."""
    fake = _FakeBucket().install(monkeypatch)
    _reset(tmp_path, monkeypatch, bucket="b")
    m._main_render_cache_put("key1", RAW, _parsed())
    m._main_render_gcs_flush()
    assert fake.objects

    m._main_render_cache_discard("key1")

    with m._main_render_lock:
        assert "key1" not in m._main_render_cache
    assert not os.path.exists(m._main_render_disk_path("key1"))
    assert fake.deletes, "the bucket copy must be deleted too"
    assert not fake.objects


# --- 5. the counters have to distinguish the tiers ------------------------

def test_hits_are_counted_by_tier(tmp_path, monkeypatch):
    """One hit counter cannot tell "the cache works" from "the cache works
    only inside one pod's life", which is the confusion COPS-2645 exists
    to end."""
    for key in ("main_render_cache_hits_memory",
                "main_render_cache_hits_disk",
                "main_render_cache_hits_gcs"):
        assert key in m._diff_stats, f"{key} must be a declared counter"
        names = [row[0] for row in m._PROM_REGISTRY]
        assert key in names, f"{key} must be exported at /metrics"


def test_tier_counters_move_with_the_source(tmp_path, monkeypatch):
    fake = _FakeBucket().install(monkeypatch)
    _reset(tmp_path, monkeypatch, bucket="b")
    for k in ("main_render_cache_hits_memory", "main_render_cache_hits_disk",
              "main_render_cache_hits_gcs"):
        m._diff_stats[k] = 0
    m._main_render_cache_put("key1", RAW, _parsed())
    m._main_render_gcs_flush()

    m._main_render_cache_get("key1")
    assert m._diff_stats["main_render_cache_hits_memory"] == 1

    with m._main_render_lock:
        m._main_render_cache.clear()
    m._main_render_cache_get("key1")
    assert m._diff_stats["main_render_cache_hits_disk"] == 1

    _reset(tmp_path / "fresh", monkeypatch, bucket="b")
    m._main_render_cache_get("key1")
    assert m._diff_stats["main_render_cache_hits_gcs"] == 1


# --- 6. concurrency ------------------------------------------------------

def test_racing_threads_never_see_a_torn_entry(tmp_path, monkeypatch):
    """DIFF_WORKERS is 16 in production. A duplicate computation under a
    race is harmless; a half-written entry is a wrong diff."""
    _FakeBucket().install(monkeypatch)
    _reset(tmp_path, monkeypatch, bucket="b")
    monkeypatch.setattr(m, "MAIN_RENDER_CACHE_MAX", 8)
    errors = []

    def worker(n):
        try:
            for i in range(40):
                key = f"k{(n + i) % 12}"
                m._main_render_cache_put(key, RAW + key, _parsed())
                res, raw, src = m._main_render_cache_get(key)
                if res is not None and raw is not None and raw != RAW + key:
                    errors.append(f"{key}: served {raw!r}")
        except Exception as e:  # a raise here is the failure
            errors.append(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors[:5]


# --- 7. COPS-2631 invariants that must not regress ------------------------

def test_content_key_inputs_are_unchanged(tmp_path, monkeypatch):
    """COPS-2645 must not add or remove a key input. Same inputs, same key."""
    chart = tmp_path / "chart"
    chart.mkdir()
    (chart / "Chart.yaml").write_text("name: demo\nversion: 1.0.0\n")
    vals = {"cfg/a.yaml": "x: 1\n"}
    k1 = m._main_render_content_key(str(chart), "rel", "ns", vals)
    k2 = m._main_render_content_key(str(chart), "rel", "ns", vals)
    assert k1 == k2 and len(k1) == 64


def test_tip_move_still_does_not_clear_the_cache():
    assert m._CLEAR_MAIN_RENDER_ON_TIP_MOVE is False
