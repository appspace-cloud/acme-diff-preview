"""The main-side render cache: memory, then disk, then GCS.

A PR diff renders both sides. The main side is identical across every PR
that shares a base sha, so rendering it once and reusing it is the single
biggest saving this service has -- and getting a stale answer from it is
the single worst thing it could do, which is why the shadow audit exists.

Two kinds of sharing meet here, and the difference is rebinding. The cache
dict, its lock and the in-flight GCS futures are CONTAINERS: only ever
mutated, so the hub re-exports them and both references are one object. The
six env knobs are REBOUND by the suite, so they must be patched on this
module -- every reader of them lives here, and a patch applied to the hub
would reach nothing at all.
"""
import collections
import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor  # noqa: F401

import concurrency
import diff_ui
import logsink
from chart_identity import _hash_chart_tree, _hash_value_files
from envcfg import DIFF_UI_GCS_BUCKET, KUBE_VERSION, _env_int
from manifest import _parse_manifest_resources
from stats import _diff_stats, _diff_stats_lock


# ── Main-side render cache (#3 / COPS-2631 stage 3) ──────────────────────────
# Pre-COPS-2631 the key was (app, main_sha, main_rev, pull_gen) and the cache
# was wiped whenever ANY configured repo's main moved. Measured hit rate on
# the live leader: 0 / 275. The inputs that actually determine helm output are
# the chart bytes + the resolved value-file contents + release/namespace +
# render flags. Key on those, store the RAW render text on disk (so a shadow
# audit can byte-compare), and keep the parsed dict only as a memory front
# cache. main_sha is deliberately NOT in the key.
#
# A wrong entry produces a wrong diff, which is worse than slow. Default to
# cache-miss on any doubt; bump MAIN_RENDER_CACHE_SALT on any render-affecting
# code change; the optional shadow audit re-renders a sampled hit.
# OrderedDict, not dict: eviction has to drop the LEAST RECENTLY USED key,
# and a plain dict only knows insertion order. On a mixed dev/stage/prod
# workload that evicted the keys being reused and kept the ones nobody
# asked for again (COPS-2645).
_main_render_cache: "collections.OrderedDict" = collections.OrderedDict()
_main_render_lock        = threading.Lock()


# Memory front-cache cap. Disk is the durable store; memory is an LRU-ish
# front. Default raised from 200 (could not hold prod's ~900 apps) to 2048.
MAIN_RENDER_CACHE_MAX = _env_int("MAIN_RENDER_CACHE_MAX", 2048)
MAIN_RENDER_CACHE_DIR = os.environ.get(
    "MAIN_RENDER_CACHE_DIR",
    os.path.join(os.environ.get("HELM_CACHE_DIR", "/tmp/acme-helm-cache"),
                 "main-renders"))
# Disk entries can outlive the memory front cache. Cap both count and bytes
# so the 1Gi emptyDir is never filled by orphaned {key}.yaml files (the
# tip-move wipe is deliberately off for content-keyed keys).
MAIN_RENDER_DISK_MAX = _env_int("MAIN_RENDER_DISK_MAX", MAIN_RENDER_CACHE_MAX * 2)
MAIN_RENDER_DISK_MAX_BYTES = _env_int(
    "MAIN_RENDER_DISK_MAX_BYTES", 400 * 1024 * 1024)
# COPS-2676: prune is O(entries on disk), not O(this write). Running it after
# every single store turned a large PR's flood of cache-miss writes into a
# flood of full-directory rescans of the SAME up-to-4096-entry cache -- a live
# profile of a 405-app render caught getmtime/getsize among the hottest leaf
# frames, and a local replay at the production disk cap measured ~23ms per
# scan. One store every 25 calls keeps the on-disk count within
# MAIN_RENDER_DISK_PRUNE_EVERY - 1 of the cap between prunes (a bounded,
# harmless overshoot on an already best-effort cap) while cutting a
# large-PR's prune overhead by the same factor.
#
# The counter is in-process state; the disk it gates is not -- an emptyDir
# outlives a container restart within the same pod (only pod deletion wipes
# it), and this cache shares that emptyDir with helm scratch, which the
# kubelet enforces its own sizeLimit on by EVICTING THE POD. A restart loop
# that lands fewer than MAIN_RENDER_DISK_PRUNE_EVERY stores per cycle (an
# OOMKill, a liveness-probe bounce) would otherwise never reach the
# threshold in any single process lifetime and defer cap enforcement
# indefinitely, which the unconditional pre-throttle code could never do (it
# re-derived the real directory state on every call). _main_render_disk_store
# forces the FIRST store of every process lifetime through unthrottled, so
# every restart gets at least one real reconciliation against the actual
# on-disk state no matter how short-lived the process is.
MAIN_RENDER_DISK_PRUNE_EVERY = _env_int("MAIN_RENDER_DISK_PRUNE_EVERY", 25)
_main_render_disk_prune_counter = 0
_main_render_disk_prune_started = False
_main_render_disk_prune_lock = threading.Lock()
# Salt bumped when render-affecting code changes (same idea as ArgoCD
# CacheVersion). Part of every content key AND of every bucket object name,
# so a bump orphans the durable copies instead of serving them.
MAIN_RENDER_CACHE_SALT = os.environ.get("MAIN_RENDER_CACHE_SALT", "cops2718-v1")
# COPS-2645: durable tier. memory -> disk -> GCS -> render. Both local tiers
# live in the /tmp emptyDir, which dies with the pod; the pod was replaced
# three times in ninety minutes during the 2026-08-11 audit (a deploy plus an
# autoscaler zone move), and the standby holds nothing at all because only
# the leader renders. A bucket read of a ~850KB object costs tens of ms
# against a ~500ms render, so even a slow read is an order of magnitude
# cheaper. Defaults to the artifact bucket; empty disables the tier entirely.
def _resolve_render_cache_bucket(configured: str, artifact_bucket: str) -> str:
    """Where durable main-side renders may be persisted, or "" for nowhere.

    COPS-2668. This tier stores the RAW `helm template` output — redaction is
    display-time only by design (redact.py: "the diff engine compares the real
    values"), the shadow audit byte-compares persisted bytes against a fresh
    render, and a cold-tier hit rebuilds diff inputs from the stored text. So
    redacting before persist is not available as a fix: it would poison every
    cached main side and fabricate a diff on every Secret. The control has to
    be WHERE the bytes go.

    It used to default to DIFF_UI_GCS_BUCKET, whose documented contract is
    "only ever holds already-redacted content". Any deployment setting
    `diffUi.gcsBucket` therefore opted, silently and undocumented, into
    shipping plaintext Secret values to a bucket other people reasonably treat
    as safe to grant read on. Measured in production before this change: 1,748
    such objects, in a bucket where `projectViewer` already carried
    `legacyObjectReader`.

    Two rules, both enforced here rather than in a comment someone can miss:

    1. No inheritance. Unset means the durable tier is OFF. Persisting
       unredacted fleet-wide renders is a decision an operator makes by naming
       a bucket, never one they arrive at by configuring something else.
    2. The artifact bucket is refused outright, because that is precisely the
       configuration this removes.

    The memory and disk tiers are unaffected; only cross-pod warming is lost
    when this returns "".
    """
    configured = (configured or "").strip()
    artifact_bucket = (artifact_bucket or "").strip()
    if not configured:
        return ""
    if configured == artifact_bucket:
        logsink.log(
            f"MAIN_RENDER_GCS_BUCKET is set to the diff-UI artifact bucket "
            f"({configured}). That bucket is documented as holding "
            f"already-redacted content, and durable renders are NOT redacted "
            f"— refusing, durable render cache disabled. Provision a separate "
            f"bucket with its own IAM (COPS-2668).", "ERROR")
        return ""
    return configured


MAIN_RENDER_GCS_BUCKET = _resolve_render_cache_bucket(
    os.environ.get("MAIN_RENDER_GCS_BUCKET", ""), DIFF_UI_GCS_BUCKET)
MAIN_RENDER_GCS_PREFIX = os.environ.get(
    "MAIN_RENDER_GCS_PREFIX", "render-cache").strip("/")


_helm_version_cache: list = []


def _helm_binary_version() -> str:
    """`helm version --short`, resolved once per process.

    Part of the content key (COPS-2668). Resolved lazily and memoised because
    it costs a subprocess and never changes within a pod's life; an
    unresolvable binary yields a distinct marker rather than an empty string,
    so "we could not tell" never silently equals "same as before".
    """
    if _helm_version_cache:
        return _helm_version_cache[0]
    import subprocess as _sp
    try:
        out = _sp.run([os.environ.get("HELM_BIN", "helm"), "version", "--short"],
                      capture_output=True, text=True, timeout=15)
        ver = (out.stdout or "").strip() or "unknown"
    except Exception:
        ver = "unresolved"
    _helm_version_cache.append(ver)
    return ver


def _main_render_content_key(chart_path, release, namespace, vals) -> str:
    """Content key for a main-side helm render (COPS-2631).

    Inputs: chart tree digest, release, namespace, value-file contents,
    kube version, include-crds flag, salt. main_sha is intentionally absent.
    """
    h = hashlib.sha256()
    h.update(MAIN_RENDER_CACHE_SALT.encode("utf-8"))
    h.update(b"\0")
    h.update(_hash_chart_tree(chart_path))
    h.update(b"\0")
    h.update(str(release or "").encode("utf-8"))
    h.update(b"\0")
    h.update(str(namespace or "").encode("utf-8"))
    h.update(b"\0")
    h.update(str(KUBE_VERSION).encode("utf-8"))
    h.update(b"\0")
    # COPS-2668: the renderer itself is a render input. Everything else here
    # can be identical across a helm upgrade whose output differs (ordering,
    # label emission, a template-function fix), and the durable tier outlives
    # pods by design — so without this a routine Dockerfile bump serves
    # old-binary renders to every PR until the entries age out, with the 1%
    # shadow audit healing them one at a time after the wrong comments are
    # already posted. The salt would also cover it, but only if someone
    # remembers; this cannot be forgotten.
    h.update(_helm_binary_version().encode("utf-8"))
    h.update(b"\0")
    h.update(b"include-crds=1")  # _helm_template always passes --include-crds
    h.update(b"\0")
    h.update(_hash_value_files(vals))
    return h.hexdigest()


def _main_render_disk_path(key: str) -> str:
    return os.path.join(MAIN_RENDER_CACHE_DIR, f"{key}.yaml")


def _main_render_stat(path: str) -> os.stat_result:
    """Indirection point for _main_render_disk_prune's per-file stat.

    A test simulating a file vanishing mid-scan has to replace this call
    with something that raises. Patching os.stat itself works too, but it
    patches the process-wide os module -- pytest's own traceback formatter
    calls os.stat via pathlib, so a raising fake collides with it and turns
    a clean assertion failure into an opaque INTERNALERROR (COPS-2676).
    A private, single-purpose seam has no other caller to collide with.
    Named with the module's _main_render_* prefix, not the generic _stat --
    diff_ui.py already has an unrelated _stat() (a metrics counter), and the
    seam audit (test_module_surface.py) flags same-named symbols across
    modules on sight.
    """
    return os.stat(path)


def _main_render_disk_prune() -> None:
    """Drop oldest on-disk render entries past count/byte caps. Best-effort.

    Memory eviction alone is not enough: disk keys survive tip moves and
    republish, and live under the same 1Gi emptyDir as helm scratch.
    """
    try:
        names = [
            os.path.join(MAIN_RENDER_CACHE_DIR, n)
            for n in os.listdir(MAIN_RENDER_CACHE_DIR)
            if n.endswith(".yaml") and not n.endswith(".tmp")
        ]
        # One stat() per file, not two (getmtime then getsize each re-stat
        # the same path): halves the syscalls a full-directory scan costs.
        stats = {p: _main_render_stat(p) for p in names}
        entries = sorted(stats, key=lambda p: stats[p].st_mtime)
        sizes = {p: stats[p].st_size for p in entries}
    except OSError:
        return
    total = sum(sizes.values())
    over_count = max(0, len(entries) - MAIN_RENDER_DISK_MAX)
    doomed = entries[:over_count]
    total -= sum(sizes[p] for p in doomed)
    if MAIN_RENDER_DISK_MAX_BYTES:
        for path in entries[over_count:]:
            if total <= MAIN_RENDER_DISK_MAX_BYTES:
                break
            doomed.append(path)
            total -= sizes[path]
    for path in doomed:
        try:
            os.remove(path)
        except OSError:
            # Best-effort: a locked/vanished file must not break a render.
            pass


def _main_render_disk_load(key: str):
    path = _main_render_disk_path(key)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _main_render_disk_store(key: str, raw: str) -> None:
    import tempfile as _tf
    os.makedirs(MAIN_RENDER_CACHE_DIR, exist_ok=True)
    path = _main_render_disk_path(key)
    fd, tmp = _tf.mkstemp(dir=MAIN_RENDER_CACHE_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(raw)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):  # pragma: no cover
            try:
                os.remove(tmp)
            except OSError as e:
                # Best-effort: a leftover tmp after a successful replace is
                # harmless; a failed cleanup must not poison the cache write.
                logsink.log(f"[main-render-cache] tmp cleanup failed (non-fatal): {e}",
                            "WARNING")
    global _main_render_disk_prune_counter, _main_render_disk_prune_started
    with _main_render_disk_prune_lock:
        _main_render_disk_prune_counter += 1
        due = (not _main_render_disk_prune_started
               or _main_render_disk_prune_counter >= MAIN_RENDER_DISK_PRUNE_EVERY)
        if due:
            _main_render_disk_prune_counter = 0
            _main_render_disk_prune_started = True
    if due:
        _main_render_disk_prune()


_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
# Uploads run off the diff path: a bucket blip must never slow a render.
_main_render_gcs_futs: list = []
_main_render_gcs_futs_lock = threading.Lock()


def _main_render_gcs_name(key: str) -> str:
    """Object name for one cached render.

    The SALT is part of the name, not only of the content key, so bumping
    it orphans every durable copy instead of resolving to one written by
    render-affecting code that no longer exists.
    """
    return f"{MAIN_RENDER_GCS_PREFIX}/{MAIN_RENDER_CACHE_SALT}/{key}.yaml.zst"


def _main_render_gcs_encode(raw: str) -> bytes:
    """zstd level 3 when the wheel is there, plain UTF-8 otherwise.

    Renders are highly compressible, and the wheel is already a runtime
    dependency (COPS-2631 stage 4), so this is bandwidth for free. The
    decoder sniffs the magic bytes, so a mixed bucket stays readable.
    """
    data = raw.encode("utf-8", "surrogateescape")
    try:
        import zstandard as zstd
    except ImportError:  # pragma: no cover - production image has the wheel
        return data
    return zstd.ZstdCompressor(level=3).compress(data)


def _main_render_gcs_decode(data: bytes) -> str:
    if data.startswith(_ZSTD_MAGIC):
        import zstandard as zstd
        # COPS-2673 (DOS-1): bounded stream-decompress, shared with diff_ui, so a
        # crafted render object cannot OOM the pod. See _zstd_decompress_capped.
        data = diff_ui._zstd_decompress_capped(zstd, data)
    return data.decode("utf-8", "surrogateescape")


def _main_render_gcs_store(key: str, raw: str) -> None:
    """Mirror one entry to the bucket. Best-effort, off the diff path."""
    if not MAIN_RENDER_GCS_BUCKET:
        return
    payload = _main_render_gcs_encode(raw)
    name = _main_render_gcs_name(key)

    def _upload():
        try:
            if diff_ui._gcs_upload(MAIN_RENDER_GCS_BUCKET, name, payload):
                with _diff_stats_lock:
                    _diff_stats["main_render_cache_gcs_stores"] += 1
            else:
                with _diff_stats_lock:
                    _diff_stats["main_render_cache_gcs_store_failures"] += 1
        except Exception as e:
            # Durability is a bonus tier: losing it must never fail a diff.
            with _diff_stats_lock:
                _diff_stats["main_render_cache_gcs_store_failures"] += 1
            logsink.log(f"[main-render-cache] bucket store failed (non-fatal): {e}",
                        "WARNING")

    try:
        fut = concurrency._get_subtask_pool().submit(_upload)
        with _main_render_gcs_futs_lock:
            # Drop settled futures on every append. Without this the list
            # grows by one entry per cache write for the life of the pod,
            # and each closure pins the compressed render bytes it was
            # about to upload -- a slow leak measured in hundreds of MB.
            # Only tests and shutdown ever call the flush.
            _main_render_gcs_futs[:] = [
                f for f in _main_render_gcs_futs if not f.done()]
            _main_render_gcs_futs.append(fut)
    except Exception:
        # No pool (tests, shutdown): do it inline rather than lose the entry.
        _upload()


def _main_render_gcs_flush(timeout: float = 30.0) -> None:
    """Wait for in-flight mirror uploads. Only tests and shutdown need this."""
    with _main_render_gcs_futs_lock:
        futs, _main_render_gcs_futs[:] = list(_main_render_gcs_futs), []
    for fut in futs:
        try:
            fut.result(timeout=timeout)
        except Exception:
            pass


def _main_render_gcs_load(key: str):
    """Raw render text from the bucket, or None. Never raises.

    Any doubt -- missing object, wheel-less environment, corrupt frame,
    undecodable bytes -- returns None so the caller re-renders. A wrong
    entry is worse than a slow one.
    """
    if not MAIN_RENDER_GCS_BUCKET:
        return None
    try:
        data = diff_ui._gcs_download(MAIN_RENDER_GCS_BUCKET,
                                     _main_render_gcs_name(key))
    except Exception as e:
        logsink.log(f"[main-render-cache] bucket load failed (non-fatal): {e}",
                    "WARNING")
        return None
    if not data:
        return None
    try:
        return _main_render_gcs_decode(data)
    except Exception as e:
        logsink.log(f"[main-render-cache] bucket object undecodable, treating as a "
                    f"miss (non-fatal): {e}", "WARNING")
        return None


def _main_render_gcs_delete(key: str) -> None:
    if not MAIN_RENDER_GCS_BUCKET:
        return
    try:
        diff_ui._gcs_delete(MAIN_RENDER_GCS_BUCKET, _main_render_gcs_name(key))
    except Exception as e:
        logsink.log(f"[main-render-cache] bucket delete failed (non-fatal): {e}",
                    "WARNING")


def _main_render_cache_discard(key: str) -> None:
    """Remove one entry from EVERY tier.

    Used by the shadow audit. A wrong entry in a store that dies with the
    pod is bad; a wrong object in the bucket re-infects every fresh pod
    that warms from it, so the discard has to reach all three.
    """
    with _main_render_lock:
        _main_render_cache.pop(key, None)
    try:
        os.remove(_main_render_disk_path(key))
    except OSError:
        pass          # already gone, or never written: nothing to undo
    _main_render_gcs_delete(key)


def _main_render_memory_put(key: str, resources: dict) -> None:
    """Insert into the memory front cache and evict LRU past the cap.

    Eviction drops the MEMORY entry only. Deleting the disk file here
    capped the disk tier at whatever memory had recently held, which is
    the opposite of the stage-3 design; disk owns its own count and byte
    caps through _main_render_disk_prune (COPS-2645).
    """
    with _main_render_lock:
        _main_render_cache[key] = resources
        _main_render_cache.move_to_end(key)
        while len(_main_render_cache) > MAIN_RENDER_CACHE_MAX:
            _main_render_cache.popitem(last=False)


def _main_render_cache_put(key: str, raw: str, resources: dict) -> None:
    """Write through every tier: disk, memory front cache, and the bucket.

    A None key means the content key could not be computed honestly (an
    unreadable chart file, COPS-2668). Storing under a fabricated key is how
    a wrong render gets served later, so there is nothing to write.
    """
    if not key:
        return
    try:
        _main_render_disk_store(key, raw)
    except OSError as e:
        logsink.log(f"[main-render-cache] disk store failed (non-fatal): {e}", "WARNING")
    _main_render_memory_put(key, resources)
    _main_render_gcs_store(key, raw)


def _main_render_cache_get(key: str):
    """Return (resources, raw_or_None, source) with source memory|disk|gcs|miss.

    Lookup order is cheapest-first: memory, then the disk file, then the
    bucket. A hit in a colder tier warms the warmer ones, so the network
    is paid at most once per key per pod.

    raw is returned whenever this call actually read the bytes, because
    the shadow audit byte-compares against exactly what was served.

    A None key (COPS-2668: the chart tree could not be hashed honestly) is a
    guaranteed miss — there is no key to look anything up by.
    """
    if not key:
        return None, None, "miss"
    with _main_render_lock:
        cached = _main_render_cache.get(key)
        if cached is not None:
            _main_render_cache.move_to_end(key)   # LRU: a hit is a use
    if cached is not None:
        with _diff_stats_lock:
            _diff_stats["main_render_cache_hits_memory"] += 1
        return cached, None, "memory"

    raw = _main_render_disk_load(key)
    source = "disk"
    if raw is None:
        raw = _main_render_gcs_load(key)
        source = "gcs"
        if raw is not None:
            # Warm the local disk tier so the next pod-local read is free.
            try:
                _main_render_disk_store(key, raw)
            except OSError as e:
                logsink.log(f"[main-render-cache] disk warm failed (non-fatal): {e}",
                            "WARNING")
    if raw is None:
        return None, None, "miss"

    resources = _parse_manifest_resources(raw)
    _main_render_memory_put(key, resources)
    with _diff_stats_lock:
        _diff_stats[f"main_render_cache_hits_{source}"] += 1
    return resources, raw, source
