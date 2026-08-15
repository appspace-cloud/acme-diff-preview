"""COPS-2671: the failure arms of the main-render cache, which nothing drove.

Every tier of this cache (memory -> disk -> GCS) is written best-effort: the
cache is an optimisation, and the module's own rule is that losing it must
never fail a diff, while serving a wrong entry is the worst thing the service
can do. That combination produces a lot of `except ... : log and carry on`,
and those arms were the dark half of render_cache.py -- the half that only
executes on the bad day, which is exactly when nobody wants to discover it
was never exercised.

What is pinned here, one incident per section:

  * an unresolvable `helm` binary must key DIFFERENTLY from a resolved one
    and from an empty answer, or a pod that cannot run helm silently reuses
    renders produced by a binary it cannot identify (COPS-2668);
  * `_main_render_disk_prune` runs against a live emptyDir shared with helm
    scratch: files vanish mid-prune and files refuse to be removed. It must
    abort rather than guess in the first case, and keep going in the second;
  * the byte cap must actually evict past the count cap, or the 1Gi emptyDir
    fills with orphaned renders;
  * bucket store / load / delete failures must be counted and logged, never
    raised -- including the "no pool" path, where the mirror runs inline
    rather than being dropped;
  * a discard has to reach all three tiers even when one of them was never
    written, because a poisoned object left in the bucket re-infects every
    fresh pod that warms from it (COPS-2645);
  * a disk failure must not cost the entry: a put still lands in memory and
    the bucket, and a GCS hit is still served when the local warm fails.

The assertions are on consequences -- counters, bucket contents, what a
later lookup returns, which WARNING the operator gets -- never on the fact
that a call returned.
"""
import concurrent.futures as _cf
import os
import subprocess
import sys

import pytest

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import concurrency          # noqa: E402
import diff_ui              # noqa: E402
import logsink              # noqa: E402
import render_cache         # noqa: E402
from manifest import _parse_manifest_resources   # noqa: E402
from stats import _diff_stats                    # noqa: E402

RAW = "kind: ConfigMap\napiVersion: v1\nmetadata:\n  name: demo\n  namespace: ns\n"


def _parsed():
    return _parse_manifest_resources(RAW)


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def tiers(tmp_path, monkeypatch):
    """Every tier isolated: own disk dir, durable tier off, memory emptied."""
    d = tmp_path / "renders"
    d.mkdir()
    monkeypatch.setattr(render_cache, "MAIN_RENDER_CACHE_DIR", str(d))
    monkeypatch.setattr(render_cache, "MAIN_RENDER_GCS_BUCKET", "")
    with render_cache._main_render_lock:
        render_cache._main_render_cache.clear()
    yield d
    render_cache._main_render_gcs_flush(5.0)
    with render_cache._main_render_lock:
        render_cache._main_render_cache.clear()


@pytest.fixture
def logged(monkeypatch):
    """Capture the structured log at the seam the whole repo patches."""
    rows = []

    def _log(msg, severity="INFO", **labels):
        rows.append((severity, msg))

    monkeypatch.setattr(logsink, "log", _log)
    return rows


def _warnings(rows, needle):
    return [m for sev, m in rows if sev == "WARNING" and needle in m]


class _FakeBucket:
    """Stands in for GCS at the diff_ui._gcs_* boundary. No network."""

    def __init__(self):
        self.objects = {}
        self.deletes = []

    def install(self, monkeypatch):
        monkeypatch.setattr(diff_ui, "_gcs_upload", self._up)
        monkeypatch.setattr(diff_ui, "_gcs_download", self._down)
        monkeypatch.setattr(diff_ui, "_gcs_delete", self._del)
        return self

    def _up(self, bucket, name, data):
        self.objects[name] = data
        return True

    def _down(self, bucket, name):
        return self.objects.get(name)

    def _del(self, bucket, name):
        self.deletes.append(name)
        self.objects.pop(name, None)
        return True


def _seed(bucket, key, raw=RAW):
    bucket.objects[render_cache._main_render_gcs_name(key)] = \
        render_cache._main_render_gcs_encode(raw)


def _counter(name):
    return _diff_stats[name]


def _mk_chart(tmp_path):
    d = tmp_path / "chart"
    d.mkdir()
    (d / "Chart.yaml").write_text("name: demo\nversion: 1.0.0\n")
    return str(d)


def _files(dirpath, count, size=100):
    """count render files, oldest first, with pinned mtimes."""
    paths = []
    for i in range(count):
        p = os.path.join(dirpath, f"e{i:02d}.yaml")
        with open(p, "w") as f:
            f.write("x" * size)
        os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))
        paths.append(p)
    return paths


# ── 1. the renderer's own version is a render input (145, 146) ───────────────

def _fake_helm(monkeypatch, stdout=None, exc=None):
    monkeypatch.setattr(render_cache, "_helm_version_cache", [])

    class _Out:
        def __init__(self, s):
            self.stdout = s
            self.stderr = ""
            self.returncode = 0

    def _run(*a, **kw):
        if exc is not None:
            raise exc
        return _Out(stdout)

    monkeypatch.setattr(subprocess, "run", _run)


def test_an_unrunnable_helm_reports_unresolved_not_a_blank(monkeypatch):
    """"We could not tell" must never silently equal "same as before"."""
    _fake_helm(monkeypatch, exc=FileNotFoundError(2, "No such file: helm"))
    assert render_cache._helm_binary_version() == "unresolved"


def test_the_unresolved_marker_is_memoised_like_a_real_version(monkeypatch):
    """One subprocess per pod, failure included: a broken binary must not cost
    a fork on every content key."""
    calls = []

    def _run(*a, **kw):
        calls.append(a)
        raise OSError("helm: permission denied")

    monkeypatch.setattr(render_cache, "_helm_version_cache", [])
    monkeypatch.setattr(subprocess, "run", _run)
    assert render_cache._helm_binary_version() == "unresolved"
    assert render_cache._helm_binary_version() == "unresolved"
    assert len(calls) == 1, "the failed probe must be memoised, not retried"


def test_an_unresolvable_helm_keys_apart_from_a_known_and_an_empty_one(
        tmp_path, monkeypatch):
    """The whole point of line 176: the binary is part of the content key.

    Three distinguishable states -- resolved, resolved-but-blank, and
    unresolvable -- must produce three distinguishable keys, or a pod that
    cannot identify helm reuses renders it has no business reusing.
    """
    chart = _mk_chart(tmp_path)
    vals = {"values.yaml": "replicas: 2\n"}

    _fake_helm(monkeypatch, stdout="v3.14.0+g1234567\n")
    known = render_cache._main_render_content_key(chart, "rel", "ns", vals)
    _fake_helm(monkeypatch, stdout="   \n")          # -> "unknown"
    blank = render_cache._main_render_content_key(chart, "rel", "ns", vals)
    _fake_helm(monkeypatch, exc=RuntimeError("no binary"))
    broken = render_cache._main_render_content_key(chart, "rel", "ns", vals)

    assert len({known, blank, broken}) == 3, (
        "an unresolvable helm must not share a cache key with a resolved one")


# ── 2. the disk prune, on a volume that fights back (202/203, 212/213, 217/219)

def test_the_byte_cap_evicts_oldest_first_even_under_the_count_cap(
        tiers, monkeypatch):
    """Count and bytes are independent caps. Renders are ~850KB, so a fleet
    well under MAIN_RENDER_DISK_MAX can still fill the 1Gi emptyDir."""
    paths = _files(str(tiers), 5, size=100)
    monkeypatch.setattr(render_cache, "MAIN_RENDER_DISK_MAX", 1000)
    monkeypatch.setattr(render_cache, "MAIN_RENDER_DISK_MAX_BYTES", 250)

    render_cache._main_render_disk_prune()

    survivors = sorted(os.path.basename(p) for p in paths if os.path.exists(p))
    assert survivors == ["e03.yaml", "e04.yaml"], (
        "the two newest fit inside 250 bytes; the three oldest had to go")


def test_the_byte_cap_leaves_a_fleet_that_already_fits_alone(tiers, monkeypatch):
    """The counterpart: under budget, nothing is evicted."""
    paths = _files(str(tiers), 5, size=100)
    monkeypatch.setattr(render_cache, "MAIN_RENDER_DISK_MAX", 1000)
    monkeypatch.setattr(render_cache, "MAIN_RENDER_DISK_MAX_BYTES", 10_000)

    render_cache._main_render_disk_prune()

    assert all(os.path.exists(p) for p in paths)


def test_a_file_vanishing_mid_prune_aborts_it_instead_of_guessing(
        tiers, monkeypatch):
    """helm scratch shares this emptyDir, so an entry can disappear between
    the listdir and the stat. Sizing is then incomplete, and deleting on an
    incomplete picture is how a prune evicts the wrong renders."""
    paths = _files(str(tiers), 6)
    monkeypatch.setattr(render_cache, "MAIN_RENDER_DISK_MAX", 2)
    real_getsize = os.path.getsize

    def _getsize(p):
        if p.endswith("e03.yaml"):
            raise FileNotFoundError(2, "vanished", p)
        return real_getsize(p)

    monkeypatch.setattr(os.path, "getsize", _getsize)
    render_cache._main_render_disk_prune()

    assert all(os.path.exists(p) for p in paths if not p.endswith("e03.yaml")), (
        "an incomplete listing must abort the prune, not evict on a guess")


def test_the_same_pruner_does_evict_when_the_listing_is_sound(tiers, monkeypatch):
    """Control for the test above: without the race, four entries go."""
    paths = _files(str(tiers), 6)
    monkeypatch.setattr(render_cache, "MAIN_RENDER_DISK_MAX", 2)

    render_cache._main_render_disk_prune()

    assert [os.path.basename(p) for p in paths if os.path.exists(p)] == \
        ["e04.yaml", "e05.yaml"]


def test_one_unremovable_file_does_not_strand_the_rest(tiers, monkeypatch):
    """A locked or already-collected file must not break a render, and must
    not abandon the eviction the cap actually asked for."""
    paths = _files(str(tiers), 6)
    monkeypatch.setattr(render_cache, "MAIN_RENDER_DISK_MAX", 2)
    real_remove = os.remove

    def _remove(p):
        if p.endswith("e00.yaml"):
            raise PermissionError(13, "file is busy", p)
        return real_remove(p)

    monkeypatch.setattr(os, "remove", _remove)
    render_cache._main_render_disk_prune()

    assert os.path.exists(paths[0]), "the test needs this removal to truly fail"
    assert not any(os.path.exists(p) for p in paths[1:4]), (
        "the remaining doomed entries must still be evicted")
    assert all(os.path.exists(p) for p in paths[4:])


# ── 3. mirroring to the bucket (305/307/308/309, 323/325, 335/336) ───────────

def test_a_raising_bucket_upload_counts_a_failure_and_warns(
        tiers, monkeypatch, logged):
    """Durability is a bonus tier. An exception out of the client (a 503, a
    credential refresh blowing up) must be counted and logged, not raised."""
    monkeypatch.setattr(render_cache, "MAIN_RENDER_GCS_BUCKET", "durable-renders")

    def _boom(bucket, name, data):
        raise RuntimeError("503 backend error")

    monkeypatch.setattr(diff_ui, "_gcs_upload", _boom)
    before = _counter("main_render_cache_gcs_store_failures")

    render_cache._main_render_gcs_store("cops2671-store-exc", RAW)
    render_cache._main_render_gcs_flush(10.0)

    assert _counter("main_render_cache_gcs_store_failures") == before + 1
    assert _warnings(logged, "bucket store failed"), (
        "an exception must be distinguishable from an upload that returned "
        f"False, which does not log: {logged}")


def test_without_a_pool_the_mirror_runs_inline_rather_than_being_lost(
        tiers, monkeypatch):
    """At shutdown -- and in any process whose pool is gone -- submit() raises.
    The entry must still reach the bucket, synchronously, before the call
    returns."""
    bucket = _FakeBucket().install(monkeypatch)
    monkeypatch.setattr(render_cache, "MAIN_RENDER_GCS_BUCKET", "durable-renders")
    dead = _cf.ThreadPoolExecutor(max_workers=1)
    dead.shutdown()
    monkeypatch.setattr(concurrency, "_get_subtask_pool", lambda: dead)
    key = "cops2671-inline"
    before = _counter("main_render_cache_gcs_stores")

    render_cache._main_render_gcs_store(key, RAW)   # deliberately no flush

    assert render_cache._main_render_gcs_name(key) in bucket.objects, (
        "a lost pool must not silently drop the durable copy")
    assert _counter("main_render_cache_gcs_stores") == before + 1
    with render_cache._main_render_gcs_futs_lock:
        assert render_cache._main_render_gcs_futs == [], (
            "nothing was submitted, so nothing may be queued for the flush")


def test_the_flush_survives_a_stuck_upload_and_still_waits_on_the_rest(tiers):
    """One upload that never settles must not abandon the ones behind it:
    fut.result(timeout) raises on the stuck future, and the loop goes on."""

    class _Recording(_cf.Future):
        def __init__(self):
            super().__init__()
            self.waited_with = "never asked"

        def result(self, timeout=None):
            self.waited_with = timeout
            return super().result(timeout=timeout)

    stuck = _cf.Future()          # pending forever -> TimeoutError
    behind = _Recording()
    behind.set_result(True)
    with render_cache._main_render_gcs_futs_lock:
        render_cache._main_render_gcs_futs[:] = [stuck, behind]

    render_cache._main_render_gcs_flush(timeout=0.05)

    assert behind.waited_with == 0.05, (
        "the flush stopped at the stuck future instead of draining the queue")
    with render_cache._main_render_gcs_futs_lock:
        assert render_cache._main_render_gcs_futs == []


# ── 4. reading the bucket back (351/352/354) ─────────────────────────────────

def test_a_bucket_read_that_raises_is_a_miss_and_not_a_crash(
        tiers, monkeypatch, logged):
    """Any doubt is a miss: the caller re-renders. The alternative is a diff
    that fails because a bonus tier had a bad minute."""
    monkeypatch.setattr(render_cache, "MAIN_RENDER_GCS_BUCKET", "durable-renders")

    def _boom(bucket, name):
        raise OSError("connection reset by peer")

    monkeypatch.setattr(diff_ui, "_gcs_download", _boom)
    before = _counter("main_render_cache_hits_gcs")

    got = render_cache._main_render_cache_get("cops2671-load-exc")

    assert got == (None, None, "miss")
    assert _counter("main_render_cache_hits_gcs") == before, (
        "a failed read must not be counted as a hit")
    assert _warnings(logged, "bucket load failed"), logged


# ── 5. discarding a poisoned entry from every tier (367, 370/371, 386/387) ───

def test_discard_makes_no_bucket_call_when_the_durable_tier_is_off(
        tiers, monkeypatch):
    """MAIN_RENDER_GCS_BUCKET unset means OFF (COPS-2668). A delete against
    the empty bucket name is a request nobody asked for."""
    bucket = _FakeBucket().install(monkeypatch)
    key = "cops2671-discard-nobucket"
    render_cache._main_render_cache_put(key, RAW, _parsed())

    render_cache._main_render_cache_discard(key)

    assert bucket.deletes == [], "the durable tier is off; do not call it"
    assert render_cache._main_render_cache_get(key) == (None, None, "miss"), (
        "the local tiers must still be purged")


def test_a_missing_disk_file_does_not_stop_the_bucket_discard(tiers, monkeypatch):
    """A key that only ever reached memory and the bucket -- disk write failed,
    or the pod restarted -- must still have its bucket object removed, or the
    poisoned render re-infects the next pod that warms from it."""
    bucket = _FakeBucket().install(monkeypatch)
    monkeypatch.setattr(render_cache, "MAIN_RENDER_GCS_BUCKET", "durable-renders")
    key = "cops2671-discard-nodisk"
    render_cache._main_render_memory_put(key, _parsed())
    _seed(bucket, key)
    assert not os.path.exists(render_cache._main_render_disk_path(key))

    render_cache._main_render_cache_discard(key)

    assert bucket.deletes == [render_cache._main_render_gcs_name(key)], (
        "the missing disk file swallowed the rest of the discard")
    assert render_cache._main_render_gcs_name(key) not in bucket.objects
    with render_cache._main_render_lock:
        assert key not in render_cache._main_render_cache


def test_a_bucket_delete_failure_still_lets_the_audit_rewrite_the_entry(
        tiers, monkeypatch, logged):
    """The shadow audit discards then re-puts the corrected render. A bucket
    delete that raises must be logged and swallowed, so the repair completes."""
    _FakeBucket().install(monkeypatch)
    monkeypatch.setattr(render_cache, "MAIN_RENDER_GCS_BUCKET", "durable-renders")

    def _boom(b, name):
        raise RuntimeError("403 storage.objects.delete denied")

    monkeypatch.setattr(diff_ui, "_gcs_delete", _boom)
    key = "cops2671-discard-exc"
    render_cache._main_render_cache_put(key, "kind: Wrong\nmetadata:\n  name: bad\n",
                                        _parse_manifest_resources("kind: Wrong\n"))
    render_cache._main_render_gcs_flush(10.0)

    render_cache._main_render_cache_discard(key)
    render_cache._main_render_cache_put(key, RAW, _parsed())

    assert _warnings(logged, "bucket delete failed"), logged
    resources, _raw, source = render_cache._main_render_cache_get(key)
    assert source == "memory"
    assert resources == _parsed(), (
        "the corrected render must survive the failed bucket delete")


# ── 6. a disk tier that cannot be written (417/418, 456/457) ─────────────────

@pytest.fixture
def broken_disk(tmp_path, monkeypatch):
    """MAIN_RENDER_CACHE_DIR pointing at a FILE: makedirs and open both fail
    with OSError, exactly like a full or read-only emptyDir."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("this is a file, not the cache dir")
    monkeypatch.setattr(render_cache, "MAIN_RENDER_CACHE_DIR", str(blocker))
    monkeypatch.setattr(render_cache, "MAIN_RENDER_GCS_BUCKET", "")
    with render_cache._main_render_lock:
        render_cache._main_render_cache.clear()
    yield blocker
    render_cache._main_render_gcs_flush(5.0)
    with render_cache._main_render_lock:
        render_cache._main_render_cache.clear()


def test_a_disk_write_failure_costs_neither_the_memory_nor_the_bucket_tier(
        broken_disk, monkeypatch, logged):
    """A put is a write-through. If the first tier is unwritable the other two
    must still get the entry, or a full emptyDir turns into a total cache
    outage instead of a degraded one."""
    bucket = _FakeBucket().install(monkeypatch)
    monkeypatch.setattr(render_cache, "MAIN_RENDER_GCS_BUCKET", "durable-renders")
    key = "cops2671-put-nodisk"

    render_cache._main_render_cache_put(key, RAW, _parsed())
    render_cache._main_render_gcs_flush(10.0)

    assert _warnings(logged, "disk store failed"), logged
    resources, _raw, source = render_cache._main_render_cache_get(key)
    assert (source, resources) == ("memory", _parsed())
    assert render_cache._main_render_gcs_name(key) in bucket.objects, (
        "the durable tier must still be written when disk is unavailable")


def test_a_gcs_hit_is_served_even_when_warming_the_disk_fails(
        broken_disk, monkeypatch, logged):
    """The cold-tier read already cost the network. Failing to cache it
    locally is a warning, not a reason to throw the answer away."""
    bucket = _FakeBucket().install(monkeypatch)
    monkeypatch.setattr(render_cache, "MAIN_RENDER_GCS_BUCKET", "durable-renders")
    key = "cops2671-warm-nodisk"
    _seed(bucket, key)
    before = _counter("main_render_cache_hits_gcs")

    resources, raw, source = render_cache._main_render_cache_get(key)

    assert source == "gcs"
    assert raw == RAW, "the shadow audit byte-compares exactly what was served"
    assert resources == _parsed()
    assert _counter("main_render_cache_hits_gcs") == before + 1
    assert _warnings(logged, "disk warm failed"), logged


def test_the_gcs_hit_still_warms_memory_when_disk_is_unavailable(
        broken_disk, monkeypatch):
    """The network must be paid at most once per key per pod, even with no
    usable disk tier: the second lookup comes out of memory."""
    bucket = _FakeBucket().install(monkeypatch)
    monkeypatch.setattr(render_cache, "MAIN_RENDER_GCS_BUCKET", "durable-renders")
    key = "cops2671-warm-memory"
    _seed(bucket, key)

    assert render_cache._main_render_cache_get(key)[2] == "gcs"
    bucket.objects.clear()          # the bucket is now unreachable for this key
    assert render_cache._main_render_cache_get(key)[2] == "memory"
