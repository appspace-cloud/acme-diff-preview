"""Identifying a chart tree, so an unchanged render can be reused.

Sliced out of diff_preview.py unchanged (COPS-2658 phase 6).

A digest over a chart directory and its value files, used as the cache key
for a rendered manifest. `_hash_chart_tree` walks the tree; the memo in front
of it exists because COPS-2646 measured the same tree being hashed repeatedly
within one run.

That memo, its size cap and its lock move together with their only two
accessors. Splitting mutable state from the code that reads it creates two
sources of truth, and the symptom -- a cache that answers differently
depending on what ran before it -- is close to impossible to reproduce. Pure
stdlib, no repo dependencies.
"""
import collections
import hashlib
import os
import threading


class ChartTreeUnreadable(OSError):
    """A chart file could not be read, so no honest content key exists.

    COPS-2668. Raised rather than hashing a placeholder: a key is a claim
    about content, and a claim made over bytes we failed to read is exactly
    the kind of confident-but-wrong answer the render cache must never give.
    Callers treat it as a cache bypass and render fresh.
    """


# COPS-2646: memo for the chart tree digest. _main_render_content_key runs
# it twice per app and it walks and reads the whole chart every time, which
# measured ~22ms per call on an appspace-micro-services-sized tree -- about
# 15s of pure re-hashing on a 345-app fleet bump, on the GIL-bound side of
# the workload. Charts are immutable once pulled, which is why
# _helm_chart_cache and the per-chart pull locks exist at all.
#
# The key is a STAT fingerprint of the whole tree: every relative path with
# its mtime and size, but without reading a single file body. That keeps the
# memo self-invalidating -- there is no separate invalidation path to keep in
# sync with the four places that evict the chart cache, and therefore no way
# to forget one -- while still noticing every way a chart can change:
#
#   * a dev registry republishing under the same tag (_ensure_chart parks the
#     stale tree aside and lands a fresh pull at the SAME path);
#   * a file edited in place, which changes neither the directory inode nor
#     its mtime. An earlier version of this memo keyed on the directory inode
#     alone and missed exactly that -- caught by the COPS-2631 stage 3 test
#     that pins "chart files change => content key changes".
#
# Serving a stale digest would key a fresh render to an old cache entry: a
# wrong diff, the worst failure this service has. Statting is cheap next to
# reading and hashing several MB of chart bodies, so the fingerprint keeps
# most of the win and gives up none of the correctness.
_CHART_TREE_MEMO_MAX = 256
_chart_tree_digest_memo: "collections.OrderedDict" = collections.OrderedDict()
_chart_tree_memo_lock = threading.Lock()


def _chart_tree_identity(chart_path: str):
    """Stat fingerprint of the tree: paths + mtimes + sizes, no file reads.

    Returns None when the tree cannot be walked, which forces the caller to
    fall through to a full hash rather than trust a partial fingerprint.
    """
    h = hashlib.sha256()
    try:
        for root, dirs, files in os.walk(chart_path):
            dirs.sort()
            for name in sorted(files):
                path = os.path.join(root, name)
                rel = os.path.relpath(path, chart_path).replace(os.sep, "/")
                st = os.stat(path)
                h.update(rel.encode("utf-8", "surrogateescape"))
                h.update(f"\0{st.st_mtime_ns}\0{st.st_size}\0".encode())
    except OSError:
        return None
    return (chart_path, h.hexdigest())


def _hash_chart_tree(chart_path: str) -> bytes:
    """Digest of the on-disk chart tree (files, relative paths, contents).

    Empty / missing tree hashes to a fixed sentinel so a missing chart becomes
    a cache miss rather than a KeyError. That sentinel is deliberately NOT
    memoized: an in-flight pull is about to populate the path, and pinning
    "missing" for it would outlive the pull.
    """
    h = hashlib.sha256()
    if not chart_path or not os.path.isdir(chart_path):
        h.update(b"missing-chart")
        return h.digest()

    ident = _chart_tree_identity(chart_path)
    if ident is not None:
        with _chart_tree_memo_lock:
            hit = _chart_tree_digest_memo.get(ident)
            if hit is not None:
                _chart_tree_digest_memo.move_to_end(ident)
                return hit
    for root, dirs, files in os.walk(chart_path):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(root, name)
            rel = os.path.relpath(path, chart_path).replace(os.sep, "/")
            h.update(rel.encode("utf-8", "surrogateescape"))
            h.update(b"\0")
            try:
                with open(path, "rb") as f:
                    while True:
                        chunk = f.read(1024 * 1024)
                        if not chunk:
                            break
                        h.update(chunk)
            except OSError as e:
                # COPS-2668: a fixed sentinel made two different unreadable
                # trees hash identically -- and made an unreadable tree hash
                # like one whose file literally contains the sentinel. That
                # mints a confident, collidable render-cache key out of an I/O
                # blip, which is the one thing this module's own docstring
                # forbids: "Default to cache-miss on any doubt". Refuse to
                # produce a key instead; the caller renders fresh.
                raise ChartTreeUnreadable(
                    f"cannot hash chart tree at {chart_path}: {rel}: {e}") from e
            h.update(b"\0")
    digest = h.digest()

    # Re-stat before storing. If the directory changed identity while the
    # walk was running (a re-pull landing underneath us), the digest we just
    # computed may mix two trees, so it is not safe to memoize against
    # either identity. Dropping it costs one re-hash; keeping it could cost
    # a wrong diff. A duplicate computation under a race is harmless.
    if ident is not None and _chart_tree_identity(chart_path) == ident:
        with _chart_tree_memo_lock:
            _chart_tree_digest_memo[ident] = digest
            _chart_tree_digest_memo.move_to_end(ident)
            while len(_chart_tree_digest_memo) > _CHART_TREE_MEMO_MAX:
                _chart_tree_digest_memo.popitem(last=False)
    return digest


def _hash_value_files(vals: dict) -> bytes:
    """Digest of resolved value-file contents in helm -f order."""
    h = hashlib.sha256()
    for path, body in (vals or {}).items():
        h.update(str(path).encode("utf-8", "surrogateescape"))
        h.update(b"\0")
        h.update((body or "").encode("utf-8", "surrogateescape"))
        h.update(b"\0")
    return h.digest()


def _find_chart_subdir(chart_dir: str) -> str:
    """Return the chart directory inside chart_dir (helm --untar creates a subdir).

    Prefers the subdirectory that contains a Chart.yaml to avoid picking an
    arbitrary one when untaring produces multiple dirs (e.g. chart + dependency).
    """
    try:
        subdirs = [d for d in os.listdir(chart_dir)
                   if os.path.isdir(os.path.join(chart_dir, d))]
        if not subdirs:
            return chart_dir
        # Pick the subdir that contains Chart.yaml (the chart root)
        for d in subdirs:
            if os.path.isfile(os.path.join(chart_dir, d, "Chart.yaml")):
                return os.path.join(chart_dir, d)
        # Fallback: first subdir (as before)
        return os.path.join(chart_dir, subdirs[0])
    except OSError:
        return chart_dir
