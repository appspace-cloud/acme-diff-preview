"""Full-diff artifact store and minimal web UI (Atlantis-style).

The PR comment stays the human summary (and is truncated over
MAX_COMMENT_BYTES); the COMPLETE, untruncated diff body is persisted here per
(repo, pr_id, sha) and served by the existing health server at:

    /diff/<repo>/<pr_id>/<sha>        rendered HTML (everything escaped)
    /diff/<repo>/<pr_id>/<sha>/raw    exact plain text

This mirrors Atlantis: the Bitbucket build status "Details" link opens the
full output page instead of the truncated comment.

Standalone module on purpose (never imports diff_preview), same pattern as
the provider split: independently testable, no circular imports.
diff_preview passes configuration as arguments.

Storage v1 is a bounded flat directory (one JSON file per artifact, atomic
write, oldest-by-mtime pruned past max_artifacts). The caller passes the SAME
body it posts to Bitbucket, so everything THIS module writes is already
redacted.

That is a claim about this module's own writes, and COPS-2668 is the reminder
that it was not a claim about the bucket. The durable render cache used to
default to this same bucket and persists RAW helm output under a
`render-cache/` prefix, so the bucket held unredacted Secret values while this
docstring was read as a guarantee that it did not — which is exactly what
makes granting read on it look harmless. The render cache now refuses to share
this bucket (render_cache._resolve_render_cache_bucket). Anything else that
wants to store here must redact first, and the classification of the bucket
belongs in values.yaml where the IAM decision is actually made.

Storage v2 adds an optional durable GCS layer behind that directory: when a
bucket is configured every save is also uploaded, and a local read miss
falls back to the bucket and warms the cache. The local dir usually lives
on an emptyDir, so this is what keeps permalinks alive across pod restarts,
and it is replica-ready: any pod can serve any artifact, and because the
content for a (repo, pr) is deterministic per sha, concurrent writers
converge on the same bytes (last-writer-wins is safe). Plain GCS JSON API
over urllib keeps GCS I/O free of cloud SDKs; auth is the pod's Workload
Identity token from the GKE metadata server. Every GCS failure is soft: the
worst outcome is a 404 after a restart (exactly the old behavior), never a
broken diff run or a broken page.

COPS-2631 stage 4: new writes compress the JSON payload with zstd level 3
(optional `zstandard` wheel; falls back to raw `.json` if the wheel is
missing). The read path accepts both `.json.zst` and legacy `.json` so
existing local/GCS entries keep serving during the transition.
"""
from __future__ import annotations

import collections
import html
import io
import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# zstd magic (RFC 8878). Used to sniff GCS/local payloads that may be either
# raw UTF-8 JSON or a compressed frame during the dual-read transition.
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_ZSTD_LEVEL = 3

# Mirrors diff_preview.STATUS_NAME. Duplicated on purpose: this module stays
# standalone stdlib-only (see module docstring) and never imports
# diff_preview just to reuse one string. Every page this module renders
# names the service explicitly, so a reviewer landing here from a build
# status link never has to guess which tool they are looking at.
SERVICE_NAME = "ACME Diff Preview"

# Outcome keys mirror diff_preview.OUT_* (diff/no_diff/indeterminate/error/
# decommissioned), passed in as plain strings so this module stays decoupled
# from those constants. Unknown keys still render, just unlabeled.
_OUTCOME_LABELS = {
    "diff": "changed",
    "no_diff": "no changes",
    "indeterminate": "unavailable",
    "error": "errors",
    "decommissioned": "decommissioned",
}

# Bitbucket repo slugs are lowercase alphanumerics plus ._- ; PR ids are
# positive integers; shas are abbreviated-to-full lowercase hex. Anything
# else is rejected before it can touch the filesystem (no separators, no
# traversal, no case games on case-insensitive filesystems).
_REPO_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
_PR_RE = re.compile(r"^[1-9][0-9]{0,8}$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


def _validate(repo, pr_id, sha):
    """Return (repo, pr_id_str, sha) or raise ValueError."""
    pr_s = str(pr_id)
    if not _REPO_RE.match(str(repo)):
        raise ValueError(f"bad repo slug: {repo!r}")
    if not _PR_RE.match(pr_s):
        raise ValueError(f"bad pr id: {pr_id!r}")
    if not _SHA_RE.match(str(sha)):
        raise ValueError(f"bad sha: {sha!r}")
    return str(repo), pr_s, str(sha)


def _assert_within_base_dir(path: str, base_dir: str) -> str:
    """Confirm `path` resolves inside `base_dir` after normalization.

    COPS-2580: the regex validation in _validate() already makes traversal
    impossible for any input that reaches here (no "/" or leading "." is
    ever in the character classes _REPO_RE/_PR_RE/_SHA_RE allow), but
    CodeQL's py/path-injection query does not recognize manual
    regex.match() calls as a sanitizer barrier. This normalize-then-check
    idiom is the exact one CodeQL's own documentation recommends, so it
    makes the guarantee visible to static analysis (closing the alert
    legitimately) and adds a second, independent layer of defense that
    stays correct even if the regexes above are ever loosened.

    Returns the normalized absolute path on success (not the original
    string): CodeQL only treats the checked value as sanitized when that
    same value reaches the filesystem sink. Raises ValueError if the path
    would resolve outside base_dir.
    """
    norm_base = os.path.abspath(base_dir)
    norm_path = os.path.abspath(path)
    if norm_path != norm_base and not norm_path.startswith(norm_base + os.sep):
        raise ValueError(f"path escapes base_dir: {path!r} not under {base_dir!r}")
    return norm_path


def _artifact_path(base_dir, repo, pr_id, sha):
    # Keyed by (repo, pr) only, NOT by sha: one live artifact per PR, exactly
    # like the single PR comment that gets updated in place on every commit.
    # A new commit's save_artifact overwrites the previous one (atomic
    # os.replace), and load-by-sha resolves to whatever the PR's current diff
    # is, so the build-status link never 404s just because the tip moved. The
    # sha is still validated (below) and stored inside the artifact.
    #
    # Legacy path (pre-COPS-2631 stage 4): `.json`. New writes prefer
    # `.json.zst`; see _artifact_paths_for_read / save_artifact.
    repo, pr_s, sha = _validate(repo, pr_id, sha)
    path = os.path.join(base_dir, f"{repo}__{pr_s}.json")
    return _assert_within_base_dir(path, base_dir)


def _artifact_zst_path(base_dir, repo, pr_id, sha):
    # Re-assert after appending `.zst`: CodeQL's py/path-injection loses the
    # sanitizer barrier across string concatenation (COPS-2580 / COPS-2631).
    return _assert_within_base_dir(
        _artifact_path(base_dir, repo, pr_id, sha) + ".zst", base_dir)


def _artifact_paths_for_read(base_dir, repo, pr_id, sha):
    """Preferred-first local paths to try on load (zst, then legacy json)."""
    return (
        _artifact_zst_path(base_dir, repo, pr_id, sha),
        _artifact_path(base_dir, repo, pr_id, sha),
    )


def _zstd_available():
    try:
        import zstandard  # noqa: F401
        return True
    except ImportError:  # pragma: no cover - production image has the wheel
        return False


def _encode_artifact_bytes(payload_utf8: bytes):
    """Compress with zstd level 3 when available. Returns (bytes, path_suffix)."""
    if not _zstd_available():
        return payload_utf8, ".json"
    import zstandard as zstd
    return zstd.ZstdCompressor(level=_ZSTD_LEVEL).compress(payload_utf8), ".json.zst"


# COPS-2673 (DOS-1): a zstd frame is attacker-influenced (a GCS object), and an
# unbounded decompress OOM-kills the pod on a decompression bomb. max_output_size
# is NOT a reliable cap -- zstandard 0.25 honours a frame's embedded content size
# over it, so a bomb that advertises a large size still expands in full (verified).
# Streaming with a byte budget bounds memory in every case: embedded size, unknown
# size, or a lying header. Generous over any legitimate artifact/render; env-tunable
# for an exceptionally large fleet.
_ZSTD_MAX_DECOMPRESS_BYTES = int(
    os.environ.get("ZSTD_MAX_DECOMPRESS_BYTES", str(128 * 1024 * 1024)))


def _zstd_decompress_capped(zstd, data: bytes) -> bytes:
    """Stream-decompress `data`, aborting past _ZSTD_MAX_DECOMPRESS_BYTES."""
    dctx = zstd.ZstdDecompressor()
    out = io.BytesIO()
    with dctx.stream_reader(io.BytesIO(data)) as reader:
        while True:
            chunk = reader.read(1 << 16)
            if not chunk:
                break
            out.write(chunk)
            if out.tell() > _ZSTD_MAX_DECOMPRESS_BYTES:
                raise ValueError(
                    "zstd output exceeds %d-byte cap (decompression bomb?)"
                    % _ZSTD_MAX_DECOMPRESS_BYTES)
    return out.getvalue()


def _decode_artifact_bytes(data: bytes):
    """Decode raw JSON UTF-8 or a zstd frame into an artifact dict.

    Raises ValueError when the payload is a zstd frame but the wheel is
    missing, so callers can fall through to a legacy `.json` sibling.
    """
    if data.startswith(_ZSTD_MAGIC):
        try:
            import zstandard as zstd
        except ImportError as e:  # pragma: no cover - production has the wheel
            raise ValueError("zstd payload but zstandard wheel missing") from e
        data = _zstd_decompress_capped(zstd, data)
    return json.loads(data.decode("utf-8"))


# ── durable GCS layer ───────────────────────────────────────────────────────

_GCS_TIMEOUT = 10
_METADATA_TOKEN_URL = ("http://metadata.google.internal/computeMetadata/v1/"
                       "instance/service-accounts/default/token")
_token_cache = {"token": "", "exp": 0.0}

# Optional callable(str) the host process may set so soft GCS failures show
# up in its own logs. This module stays logging-agnostic (stdlib only, no
# assumptions about the host's log format).
on_warning = None

# COPS-2647: optional callable(str, int) so bucket outcomes become COUNTERS
# in the host, not just warning lines in whichever pod happens to be the
# leader. Same shape as on_warning for the same reason: this module keeps
# no opinion about the host's stats plumbing.
on_stat = None


def _stat(key, n=1):
    cb = on_stat
    if cb is None:
        return
    try:
        cb(key, n)
    except Exception:
        # Observability must never break the thing it observes.
        pass


def _warn(msg):
    cb = on_warning
    if cb is None:
        return
    try:
        cb(msg)
    except Exception:
        pass  # a broken log hook must never break the store


def _gcs_token():
    """Workload Identity access token, cached until shortly before expiry."""
    now = time.time()
    if _token_cache["token"] and now < _token_cache["exp"] - 60:
        return _token_cache["token"]
    req = urllib.request.Request(
        _METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=_GCS_TIMEOUT) as r:
        data = json.load(r)
    _token_cache["token"] = data["access_token"]
    _token_cache["exp"] = now + float(data.get("expires_in", 0))
    return _token_cache["token"]


# COPS-2647: bounded retries. 2.46.0 made load_artifact treat a local sha
# mismatch as a miss and go to the bucket, which assumes the bucket holds
# the CURRENT artifact. A single-attempt upload broke that assumption on
# any transient blip: the bucket kept the previous commit and the two
# replicas could serve different pages for the same URL again, silently.
_GCS_UPLOAD_ATTEMPTS = 3
_GCS_RETRY_SLEEP = 0.25          # doubled per attempt; monkeypatched to 0 in tests


def _gcs_error_is_transient(e) -> bool:
    """Retry timeouts, connection errors, 408, 429 and 5xx. Nothing else.

    A 403 will still be a 403 in 200ms: retrying it wastes the diff run's
    time and hammers a bucket that is already telling us something. Auth
    and permission failures need a human, not another attempt.
    """
    if isinstance(e, urllib.error.HTTPError):
        return e.code in (408, 429) or 500 <= e.code < 600
    return isinstance(e, (TimeoutError, urllib.error.URLError, OSError))


def _gcs_upload(bucket, name, data):
    """Upload one object, retrying transient failures. True on success.

    Still non-fatal in every case: durability is a bonus tier and a bucket
    outage must never block a PR comment or fail a diff run.
    """
    last = None
    for attempt in range(1, _GCS_UPLOAD_ATTEMPTS + 1):
        try:
            url = ("https://storage.googleapis.com/upload/storage/v1/b/"
                   f"{urllib.parse.quote(bucket, safe='')}/o?uploadType=media"
                   f"&name={urllib.parse.quote(name, safe='')}")
            req = urllib.request.Request(
                url, data=data, method="POST",
                headers={"Authorization": f"Bearer {_gcs_token()}",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=_GCS_TIMEOUT):
                pass
            _stat("artifact_gcs_upload_ok")
            return True
        except Exception as e:
            last = e
            if attempt >= _GCS_UPLOAD_ATTEMPTS or not _gcs_error_is_transient(e):
                break
            _stat("artifact_gcs_upload_retries")
            time.sleep(_GCS_RETRY_SLEEP * (2 ** (attempt - 1)))
    _stat("artifact_gcs_upload_failed")
    _warn(f"[diff-ui] GCS upload of {name} failed after {attempt} "
          f"attempt(s) (non-fatal): {last}")
    return False


# COPS-2647: uploads that failed after their retries. A failed upload is
# not just a lost copy -- it leaves the PREVIOUS commit's artifact in the
# bucket, and load_artifact sends a replica there whenever its local sha
# does not match, so the two pods can present different diffs for the same
# URL. Retrying on a later iteration heals that within a poll cycle
# instead of waiting for the next commit to overwrite it.
#
# Keyed by (repo, pr_id) so a newer commit REPLACES an older pending entry:
# uploading the superseded one would put a stale artifact in the bucket,
# which is the very failure this exists to prevent.
#
# Values hold the local PATH, never the payload. COPS-2645 shipped a leak
# by pinning compressed bytes in a closure; the bytes are re-read from disk
# at retry time, and a vanished file is dropped rather than retried forever.
_PENDING_UPLOAD_MAX = 128
_pending_uploads: "collections.OrderedDict" = collections.OrderedDict()
_pending_lock = threading.Lock()


def _note_pending_upload(bucket, name, path, repo, pr_id):
    with _pending_lock:
        _pending_uploads[(str(repo), int(pr_id))] = (bucket, name, path)
        _pending_uploads.move_to_end((str(repo), int(pr_id)))
        while len(_pending_uploads) > _PENDING_UPLOAD_MAX:
            _pending_uploads.popitem(last=False)
    # Refresh the gauge OUTSIDE the lock. The host reads it back through
    # pending_upload_count(), which takes _pending_lock, and this one is
    # not reentrant -- calling _stat while holding it deadlocked the
    # process (caught by test_retries_are_bounded hanging).
    _stat("artifact_gcs_pending", 0)


def pending_upload_count() -> int:
    with _pending_lock:
        return len(_pending_uploads)


def reset_pending_uploads() -> None:
    with _pending_lock:
        _pending_uploads.clear()


def retry_pending_uploads() -> int:
    """Re-attempt uploads that failed earlier. Returns how many landed.

    Called once per iteration by the host. Best-effort throughout: this
    runs for durability, never for correctness of the current diff.
    """
    with _pending_lock:
        items = list(_pending_uploads.items())
    healed = 0
    for key, (bucket, name, path) in items:
        try:
            with open(path, "rb") as f:
                payload = f.read()
        except OSError:
            # The local artifact was pruned or superseded. There is nothing
            # to upload and retrying forever would only grow the map.
            with _pending_lock:
                _pending_uploads.pop(key, None)
            continue
        if _gcs_upload(bucket, name, payload):
            healed += 1
            with _pending_lock:
                _pending_uploads.pop(key, None)
    _stat("artifact_gcs_pending", 0)      # outside the lock, see above
    return healed


def _gcs_download(bucket, name):
    """Return an object's bytes, or None. A 404 is a plain miss, not an
    operational problem; anything else gets surfaced through the hook."""
    try:
        url = ("https://storage.googleapis.com/storage/v1/b/"
               f"{urllib.parse.quote(bucket, safe='')}/o/"
               f"{urllib.parse.quote(name, safe='')}?alt=media")
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {_gcs_token()}"})
        with urllib.request.urlopen(req, timeout=_GCS_TIMEOUT) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code != 404:
            _warn(f"[diff-ui] GCS download of {name} failed (non-fatal): "
                  f"HTTP {e.code}")
        return None
    except Exception as e:
        _warn(f"[diff-ui] GCS download of {name} failed (non-fatal): {e}")
        return None


def _gcs_delete(bucket, name):
    """Best-effort delete of one object. Returns True on success."""
    try:
        url = ("https://storage.googleapis.com/storage/v1/b/"
               f"{urllib.parse.quote(bucket, safe='')}/o/"
               f"{urllib.parse.quote(name, safe='')}")
        req = urllib.request.Request(
            url, method="DELETE",
            headers={"Authorization": f"Bearer {_gcs_token()}"})
        with urllib.request.urlopen(req, timeout=_GCS_TIMEOUT):
            pass
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return True
        _warn(f"[diff-ui] GCS delete of {name} failed (non-fatal): HTTP {e.code}")
        return False
    except Exception as e:
        _warn(f"[diff-ui] GCS delete of {name} failed (non-fatal): {e}")
        return False


def save_artifact(base_dir, repo, pr_id, sha, body, pr_url="",
                  max_artifacts=500, base_sha="", outcome_counts=None,
                  app_count=None, bucket="", max_bytes=None):
    """Persist the full (already redacted) diff body. Atomic; then prune.

    base_sha/outcome_counts/app_count are optional PR-level context (the diff
    base commit, the per-outcome breakdown, and how many apps were
    evaluated) so the page shows more than the raw comment text: the same
    at-a-glance summary a reviewer gets from the comment header, kept even
    after the comment itself gets truncated.

    Atomic tmp+rename so a concurrent reader can never see a half-written
    file; pruning is best-effort (a locked/vanished file must never break
    the diff run that triggered the save).

    When bucket is set the exact same bytes are also uploaded to GCS
    (best-effort, soft failure) so the artifact survives pod restarts and
    is reachable from any replica.

    COPS-2631: payload is zstd-compressed (level 3) when the zstandard wheel
    is present, written as `.json.zst`. A sibling legacy `.json` for the
    same PR is removed so prune and load see one live object.
    """
    legacy_path = _artifact_path(base_dir, repo, pr_id, sha)
    os.makedirs(base_dir, exist_ok=True)
    artifact = {
        "repo": str(repo),
        "pr_id": int(pr_id),
        "sha": str(sha),
        "pr_url": pr_url,
        "base_sha": str(base_sha) if base_sha else "",
        "outcome_counts": dict(outcome_counts) if outcome_counts else {},
        "app_count": int(app_count) if app_count is not None else None,
        "created_utc": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "body": body,
    }
    raw = json.dumps(artifact, ensure_ascii=False).encode("utf-8")
    payload, suffix = _encode_artifact_bytes(raw)
    path = (legacy_path if suffix == ".json"
            else _assert_within_base_dir(legacy_path + ".zst", base_dir))
    fd, tmp = tempfile.mkstemp(dir=base_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):  # pragma: no cover - only on a failed replace
            os.remove(tmp)
    # One live object per PR: drop the other encoding if it lingered.
    other = (legacy_path if path.endswith(".zst")
             else _assert_within_base_dir(legacy_path + ".zst", base_dir))
    if other != path and os.path.exists(other):
        try:
            os.remove(other)
        except OSError:
            # Best-effort: a locked/vanished sibling must not fail the save.
            pass
    _prune(base_dir, max_artifacts, max_bytes)
    if bucket:
        name = os.path.basename(path)
        if _gcs_upload(bucket, name, payload):
            # Drop the other encoding in GCS so a failed zst upload cannot
            # leave a stale legacy object preferred on the next miss.
            other_name = (name[:-4] if name.endswith(".zst")
                          else name + ".zst")
            if other_name != name:
                _gcs_delete(bucket, other_name)
        else:
            # COPS-2647: the bucket now holds the PREVIOUS commit for this
            # PR while the leader serves the current one. Queue it so the
            # divergence closes on a later pass.
            _note_pending_upload(bucket, name, path, repo, pr_id)
    return path


def _prune(base_dir, max_artifacts, max_bytes=None):
    """Remove oldest artifacts (by mtime) beyond the caps. Best-effort.

    Two caps because they bound different failure modes. The count cap is
    the historical one. The BYTE cap (COPS-2610) is the one that matches
    how the directory actually fails: it lives on a 1Gi emptyDir and the
    kubelet EVICTS the whole pod past the sizeLimit -- and artifact sizes
    span three orders of magnitude (median ~181KB, observed worst 26.7MB
    before uncapping), so a count is measured in the wrong unit. Local
    pruning is cheap to be aggressive about: GCS is the durable copy and a
    pruned entry costs exactly one re-download on the next view.

    Counts both `.json` and `.json.zst` (COPS-2631 dual-write transition).
    """
    try:
        entries = [os.path.join(base_dir, n) for n in os.listdir(base_dir)
                   if n.endswith(".json.zst")
                   or (n.endswith(".json") and not n.endswith(".zst"))]
        entries.sort(key=lambda p: os.path.getmtime(p))
        sizes = {p: os.path.getsize(p) for p in entries}
    except OSError:  # pragma: no cover - directory vanished mid-run
        return
    total = sum(sizes.values())
    over_count = max(0, len(entries) - max_artifacts)
    doomed = entries[:over_count]
    total -= sum(sizes[p] for p in doomed)
    if max_bytes:
        for path in entries[over_count:]:
            if total <= max_bytes:
                break
            doomed.append(path)
            total -= sizes[path]
    for path in doomed:
        try:
            os.remove(path)
        except OSError:
            pass  # locked or already gone: never fail the save over pruning


def load_artifact(base_dir, repo, pr_id, sha, bucket=""):
    """Return the artifact dict, or None if missing/corrupt/bad key.

    Local cache first (`.json.zst` then legacy `.json`). On a miss (fresh
    pod after a restart, or a sibling replica wrote it) fall back to the
    GCS bucket when configured, and warm the local cache with the
    downloaded bytes so the next read is local.

    A local hit whose stored sha is NOT the one being asked for is treated as
    a miss when a bucket is configured. The artifact is keyed by (repo, pr)
    and overwritten in place, so a replica that warmed its cache from GCS
    while an older commit was current would otherwise serve that older diff
    for the rest of the PR's life, while the leader served the current one.
    Live proof, acme-config-dev PR #7063 on 2.45.0: the two hub replicas
    disagreed about whether the PR changed 110 resources or nothing at all.

    Falling back to the newest artifact that exists is still the contract
    when GCS has nothing better (the build-status link must not 404 just
    because the tip moved), and a matching sha still costs zero network.
    """
    try:
        repo, pr_s, sha = _validate(repo, pr_id, sha)
    except ValueError:
        return None
    # CodeQL py/path-injection: build the filename from validated segments
    # only, then use the exact normpath(join(base, name)) + startswith +
    # raise idiom from the CodeQL docs. The verbose inline form is
    # deliberate: extracting it into a helper does NOT clear the alert,
    # because CodeQL loses the guarantee across the call boundary. Keep it
    # inline even though it reads worse.
    base_path = os.path.abspath(base_dir)
    names = (f"{repo}__{pr_s}.json.zst", f"{repo}__{pr_s}.json")
    local = None
    for name in names:
        fullpath = os.path.normpath(os.path.join(base_path, name))
        if not fullpath.startswith(base_path):
            raise ValueError(
                f"path escapes base_dir: {fullpath!r} not under {base_path!r}")
        try:
            with open(fullpath, "rb") as f:
                data = f.read()
            local = _decode_artifact_bytes(data)
            break
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        except Exception as e:
            # zstandard.ZstdError is not always a ValueError subclass.
            if type(e).__name__ == "ZstdError":
                continue
            raise
    if local is not None and (not bucket or str(local.get("sha")) == sha):
        return local
    if not bucket:
        return local
    for name in names:
        data = _gcs_download(bucket, name)
        if data is None:
            continue
        try:
            artifact = _decode_artifact_bytes(data)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        except Exception as e:
            if type(e).__name__ == "ZstdError":
                continue
            raise
        if (local is not None
                and str(artifact.get("sha")) == str(local.get("sha"))):
            # GCS is no fresher than what we already hold: keep serving the
            # local copy rather than rewriting it for nothing.
            return local
        try:
            os.makedirs(base_path, exist_ok=True)
            fullpath = os.path.normpath(os.path.join(base_path, name))
            if not fullpath.startswith(base_path):  # pragma: no cover
                # Unreachable: the local-read loop above walks the SAME
                # `names` tuple through an identical check and raises first,
                # so nothing that escapes can ever get this far. Kept anyway
                # -- CodeQL wants the idiom at each write site, and a guard
                # that depends on a caller forty lines up is one refactor
                # away from being the only one left. COPS-2671 verified the
                # shadowing; the reachable contract is pinned by
                # test_cops2671_cov_tail.py::
                #     test_a_traversing_object_name_writes_nothing_outside_the_cache_dir
                raise ValueError(
                    f"path escapes base_dir: {fullpath!r} not under "
                    f"{base_path!r}")
            fd, tmp = tempfile.mkstemp(dir=base_path, suffix=".tmp")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, fullpath)
            # Drop the other encoding so the healed copy is the only one the
            # next local read can find.
            other = os.path.normpath(os.path.join(
                base_path, name[:-4] if name.endswith(".zst") else name + ".zst"))
            if other != fullpath and other.startswith(base_path):
                try:
                    os.remove(other)
                except OSError:
                    # Best-effort: the preferred name is written already.
                    pass
        except (OSError, ValueError):  # pragma: no cover - cache warm is best-effort
            pass
        return artifact
    # GCS had nothing usable. A local copy for an older commit is still the
    # newest diff that exists for this PR, so serve it instead of a 404.
    return local


def has_artifact(base_dir, repo, pr_id, sha, bucket=""):
    return load_artifact(base_dir, repo, pr_id, sha, bucket=bucket) is not None


def ui_url(base_url, repo, pr_id, sha):
    """Permalink for the build status URL."""
    return f"{base_url}/diff/{repo}/{pr_id}/{sha}"


def parse_request_path(path):
    """Parse /diff/<repo>/<pr>/<sha>[/raw]. Return tuple or None.

    Strict by construction: exact segment count, each segment re-validated
    with the same regexes used for filenames, query strings rejected. A None
    here becomes a 400, so nothing unvalidated ever reaches the filesystem.
    """
    if "?" in path or "#" in path:
        return None
    parts = path.split("/")
    # ["", "diff", repo, pr, sha] or ["", "diff", repo, pr, sha, "raw"]
    if len(parts) == 6 and parts[5] == "raw":
        raw = True
    elif len(parts) == 5:
        raw = False
    else:
        return None
    if parts[0] != "" or parts[1] != "diff":
        return None
    repo, pr_s, sha = parts[2], parts[3], parts[4]
    if not (_REPO_RE.match(repo) and _PR_RE.match(pr_s) and _SHA_RE.match(sha)):
        return None
    return repo, int(pr_s), sha, raw


def _format_outcome_summary(app_count, outcome_counts):
    """Chip per fact: '15 apps evaluated', '3 changed', '12 no changes'.
    Empty string if there is no metadata (e.g. an artifact saved before
    these fields existed), so the page renders no summary row at all."""
    if app_count is None and not outcome_counts:
        return ""
    parts = []
    if app_count is not None:
        parts.append(f"{app_count} app{'s' if app_count != 1 else ''} evaluated")
    for key, label in _OUTCOME_LABELS.items():
        n = outcome_counts.get(key, 0)
        if n:
            parts.append(f"{n} {label}")
    for key, n in outcome_counts.items():
        if key not in _OUTCOME_LABELS and n:
            parts.append(f"{n} {key}")
    return "".join(f"<span>{html.escape(str(p))}</span>" for p in parts)


# Fence markers as the comment renderer emits them: ``` optionally followed
# by a language tag. Only ```diff fences get diff coloring; any other fence
# is rendered as neutral code so yaml list items ("- item") are never
# painted as deletions.
_FENCE_RE = re.compile(r"^```([A-Za-z0-9_-]*)\s*$")

# Hunk headers look like "@@ -18,6 +18,8 @@": pull the old/new start lines so
# the gutters can count from the right place.
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

# Default cap on rows rendered outright. A huge multi-app diff can be many
# thousands of lines; past this the overflow is still emitted (nothing is
# dropped, /raw stays byte-exact) but hidden behind a "show full output"
# button so first paint and scrolling stay snappy. Module-level so tests and
# operators can tune it.
#
# 20,000 (COPS-2610): the old 1,500 opened routine multi-app PRs mostly
# folded. The ceiling is a measured browser-survival number, not a policy:
# acme-config-prod #3887 is 786,150 lines and renders to 113MB of HTML in
# 1.14s server-side -- ALL of it ships either way, so this constant only
# decides how many <tr> the browser must lay out on first paint. "Show
# everything by default" at that size is a hung tab, not completeness.
MAX_VISIBLE_LINES = 20000


# ── Page structure (COPS-2611, phase D) ─────────────────────────────────
# The stored body is markdown, and this module renders it line by line. To
# navigate it we need a structural model, which is derived from the markers
# format_comment already emits rather than by changing the artifact format:
# older artifacts stay readable, and /raw stays byte-exact.
#
#   app       "⚠️ **`pv-alpha-a-ms`** — 139 resource(s) changed"
#   resource  "**`/apps/Deployment apigateway`**"
#
# Sizes this has to survive, from real artifacts: 345 apps / 19,869
# resources (prod #3887), 774 apps / 11,086 resources (#3890).
#
# Defensive by construction: anything that does not match falls through and
# renders exactly as before. A parser that swallowed an unrecognised line
# would trade a navigable page for an incomplete one, which is the opposite
# of what phases C and D are for.
_APP_RE = re.compile(
    r"^.{0,4}\s*\*\*`([^`]+)`\*\*\s+\u2014\s+(\d+)\s+resource\(s\) changed\s*$")
_RES_RE = re.compile(r"^\*\*`([^`]+)`\*\*\s*$")
_UNSAFE_ID_RE = re.compile(r"[^a-z0-9]+")


def _slug(text, fallback="x"):
    """A URL-safe anchor fragment.

    Sanitised to [a-z0-9-] rather than escaped, because this value lands in
    an id= and an href="#..." attribute: escaping would still leave a quote
    to break out of. Names are PR-controlled, so the safe set is the whole
    defence here.
    """
    s = _UNSAFE_ID_RE.sub("-", str(text).lower()).strip("-")
    return (s or fallback)[:80]


def app_anchor(name):
    """The anchor id for an application block. THE single owner of that shape.

    COPS-2622: the PR comment deep-links to individual applications on this
    page. If the comment computed the id itself, the two would drift on the
    first change here and every deep link would 404 *silently* -- worse than
    the repetition it replaced. So the comment imports this.

    Deliberately order-independent, unlike the de-duplicated ids inside
    build_outline: the comment cannot know the page's application order (it
    renders its own apps in a different order after rollups and budget
    collapse), so an id that depended on position could not be reproduced.
    That makes collision-freedom a property of the input instead, which is
    safe here because application names are already `[a-z0-9-]` in the fleet
    and _slug is the identity on them. build_outline keeps its numeric
    suffix as the backstop for anything that ever is not.
    """
    return "app-" + _slug(name)


def build_outline(body):
    """Structure of the page: [{id, name, count, resources: [{id, name}]}].

    Anchors are scoped by application, because resource names repeat across
    environments constantly (`/Service web` exists in every app) and an
    unscoped anchor would send a deep link to whichever one rendered first.
    Collisions after sanitising get a numeric suffix, so two hostile names
    that sanitise identically still get one anchor each.
    """
    outline, used = [], set()

    def _unique(base):
        cand, n = base, 2
        while cand in used:
            cand, n = f"{base}-{n}", n + 1
        used.add(cand)
        return cand

    current = None
    for line in str(body).split("\n"):
        m = _APP_RE.match(line)
        if m:
            current = {"id": _unique(app_anchor(m.group(1))),
                       "name": m.group(1), "count": int(m.group(2)),
                       "resources": []}
            outline.append(current)
            continue
        m = _RES_RE.match(line)
        if m and current is not None:
            current["resources"].append(
                {"id": _unique(current["id"] + "--" + _slug(m.group(1))),
                 "name": m.group(1)})
    return outline


def _render_index(outline):
    """Table of contents. Server-rendered, collapsed per app via <details>
    so 345 applications are a list rather than a wall, and so expanding
    costs no JavaScript. Every label is escaped; every id is sanitised."""
    if not outline:
        return ""
    n_res = sum(len(a["resources"]) for a in outline)
    parts = [
        '<details class="toc" open><summary>Index: '
        f'{len(outline)} application(s), {n_res} resource(s)</summary>',
        '<input class="tocfilter" type="text" autocomplete="off" '
        'placeholder="Filter applications and resources" '
        'aria-label="Filter the index">',
        '<ul class="toclist">']
    for app in outline:
        label = html.escape(app["name"])
        parts.append(
            f'<li class="tocapp" data-k="{label.lower()}">'
            f'<details><summary><a href="#{app["id"]}">{label}</a> '
            f'<span class="tocn">{app["count"]}</span></summary><ul>')
        for res in app["resources"]:
            rlabel = html.escape(res["name"])
            parts.append(
                f'<li class="tocres" data-k="{rlabel.lower()}">'
                f'<a href="#{res["id"]}">{rlabel}</a></li>')
        parts.append("</ul></details></li>")
    parts.append("</ul></details>")
    return "".join(parts)


# ── Prose rendering (COPS-2625, phase H) ────────────────────────────────
# Phases C and D made the page complete and navigable; it still printed its
# own prose as markup, so the surface that exists to be READ was the harder
# of the two to read. These transforms fix that under four rules:
#
#   1. Outside fences only. Inside a fence, alignment and the leading +/-
#      carry meaning, so those rows are produced exactly as before.
#   2. Escape FIRST, transform the already-escaped string. Nothing below
#      ever sees raw PR-controlled text, so no transform can open an
#      injection hole; the whitelist decides only what gets *decoration*.
#   3. Anything not recognised renders exactly as it did before. Seven
#      hashes is not a heading and a values line with pipes is not a table.
#      An unrenderable line is a cosmetic miss; a swallowed line is lost
#      information, and this page exists so nothing is lost.
#   4. One row per source line, tables excepted (they collapse to one).
_MD_HEAD_RE = re.compile(r"^(#{1,6}) (.*)$")
_MD_RULE_RE = re.compile(r"^-{3,}\s*$")
_MD_LI_RE = re.compile(r"^([ \t]*)- (.*)$")
_MD_TROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_MD_TSEP_RE = re.compile(r"^\s*\|(?:\s*:?-{2,}:?\s*\|)+\s*$")
_MD_CODE_RE = re.compile(r"`([^`\n]+)`")
_MD_BOLD_RE = re.compile(r"\*\*(\S(?:[^*]*\S)?)\*\*")
# http/https only, and the closing paren must follow the url immediately.
# javascript:, data: and vbscript: are not rejected by a blocklist here --
# they simply never match, which is the only version of this that stays
# correct when someone invents a new scheme.
_MD_LINK_RE = re.compile(r"\[([^\]\n]*)\]\((https?://[^)\s]+)\)")

# The body repeats what the page chrome already states above the diff.
_MD_HDR_TITLE_RE = re.compile(r"^#{1,6} .*ACME Diff Preview\s*$")
_MD_HDR_COMMIT_RE = re.compile(r"^\*\*Commit\*\* ")


def _render_inline(esc):
    """Inline markup on an ALREADY-ESCAPED line.

    Code spans and links are lifted out into placeholders before bold runs,
    so markup inside a code span stays literal (a resource path containing
    asterisks is a path, not an emphasis) and bold can never rewrite the
    inside of an href. Placeholders are restored highest-index first,
    because a link's replacement may itself contain a code placeholder.

    NUL is the placeholder delimiter and cannot survive html.escape from
    any real body; if one is present anyway the line is returned untouched,
    per rule 3.
    """
    if "\x00" in esc:
        return esc
    toks = []

    def _hold(fragment):
        toks.append(fragment)
        return "\x00%d\x00" % (len(toks) - 1)

    out = _MD_CODE_RE.sub(
        lambda m: _hold("<code>%s</code>" % m.group(1)), esc)
    out = _MD_LINK_RE.sub(
        lambda m: _hold('<a href="%s" rel="noopener noreferrer">%s</a>'
                        % (m.group(2),
                           _MD_BOLD_RE.sub(r"<strong>\1</strong>",
                                           m.group(1)))), out)
    out = _MD_BOLD_RE.sub(r"<strong>\1</strong>", out)
    for i in range(len(toks) - 1, -1, -1):
        out = out.replace("\x00%d\x00" % i, toks[i])
    return out


def _md_cells(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _md_table(lines):
    """A pipe table as a real table. lines[1] is the separator row."""
    parts = ['<table class="mdt"><thead><tr>']
    parts += ["<th>%s</th>" % _render_inline(html.escape(c))
              for c in _md_cells(lines[0])]
    parts.append("</tr></thead><tbody>")
    for ln in lines[2:]:
        parts.append("<tr>")
        parts += ["<td>%s</td>" % _render_inline(html.escape(c))
                  for c in _md_cells(ln)]
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def _md_prose(line, esc):
    """(row class, cell html) for one prose line. esc is html.escape(line);
    every capture below is re-escaped from the raw line, which is the same
    string because escaping is per character."""
    if _MD_RULE_RE.match(line):
        return "mdrule", ""
    m = _MD_HEAD_RE.match(line)
    if m:
        return ("mdh mdh%d" % len(m.group(1)),
                _render_inline(html.escape(m.group(2))))
    m = _MD_LI_RE.match(line)
    if m:
        return "mdli", "%s&bull; %s" % (m.group(1),
                                        _render_inline(html.escape(m.group(2))))
    return "", _render_inline(esc)


def _page_header_span(lines):
    """How many leading lines the PAGE may drop because its chrome already
    states them: the generated title and the commit/base line. Returns 0
    unless the whole expected shape is present, so an unfamiliar body keeps
    every one of its rows."""
    if not lines or not _MD_HDR_TITLE_RE.match(lines[0]):
        return 0
    i = 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or not _MD_HDR_COMMIT_RE.match(lines[i]):
        return 0
    i += 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    return i


def _diff_row(cls, old_no, new_no, marker, esc_text, row_id=""):
    """One table row: two line-number gutters, a +/- marker cell, and the
    escaped code cell. esc_text is ALREADY html-escaped by the caller.
    row_id is an anchor target, already sanitised by _slug (COPS-2611)."""
    o = str(old_no) if old_no is not None else ""
    n = str(new_no) if new_no is not None else ""
    row_cls = f"row {cls}" if cls else "row"
    anchor = f' id="{row_id}"' if row_id else ""
    return (f'<tr class="{row_cls}"{anchor}>'
            f'<td class="ln-old">{o}</td>'
            f'<td class="ln-new">{n}</td>'
            f'<td class="mk">{marker}</td>'
            f'<td class="code">{esc_text}</td></tr>')


def _render_body_rows(body, outline=None, drop_header=False):
    """Render the comment body as diff table rows. Same information as the
    raw text (the /raw endpoint stays byte-exact), just readable: inside
    ```diff fences, +/-/@@ lines get GitHub-palette colors and old/new line
    numbers in the gutters; non-diff fences render as neutral code (so a yaml
    "- item" is never painted as a deletion); markdown headers outside fences
    get weight; fence markers are dimmed. Every line goes through html.escape
    BEFORE being placed in a cell, so highlighting can never open an
    injection hole. Returns a list of row strings (one per source line).

    outline (COPS-2611) pins anchor ids onto the app and resource header
    rows so the index can link into the body. It is consumed in document
    order and only OUTSIDE fences: a line inside a ```diff block that
    happens to look like a header is diff content, not structure. Anchors
    are strictly additive -- with outline=None the output is byte-identical
    to before, and one row is still emitted per source line either way.

    drop_header (COPS-2625) lets the PAGE skip the generated title and
    commit line, which its own chrome already states above the diff. It
    defaults to off, so the only caller that opts in is render_html and no
    other surface changes. The skipped lines are still walked for pending
    anchor matches, so dropping them can never shift an anchor onto the
    wrong row.
    """
    rows = []
    fence = None      # None | "diff" | "code"
    old_no = new_no = 0
    pending = []
    if outline:
        for app in outline:
            pending.append((app["name"], app["id"]))
            for res in app["resources"]:
                pending.append((res["name"], res["id"]))
    pending.reverse()   # pop() from the end walks document order

    def _take_anchor(text):
        if pending and ("`%s`" % pending[-1][0]) in text:
            return pending.pop()[1]
        return ""

    lines = str(body).split("\n")
    start = _page_header_span(lines) if drop_header else 0
    for skipped in lines[:start]:
        _take_anchor(skipped)
    i = start
    while i < len(lines):
        line = lines[i]
        i += 1
        esc = html.escape(line)
        m = _FENCE_RE.match(line)
        if m:
            fence = (None if fence is not None
                     else ("diff" if m.group(1) == "diff" else "code"))
            rows.append(_diff_row("fence", None, None, "", esc))
            continue
        if fence == "diff":
            hm = _HUNK_RE.match(line)
            if hm:
                old_no = int(hm.group(1))
                new_no = int(hm.group(2))
                rows.append(_diff_row("hunk", None, None, "", esc))
            elif line.startswith("+"):
                rows.append(_diff_row("add", None, new_no, "+", esc))
                new_no += 1
            elif line.startswith("-"):
                rows.append(_diff_row("del", old_no, None, "-", esc))
                old_no += 1
            else:
                rows.append(_diff_row("ctx", old_no, new_no, "", esc))
                old_no += 1
                new_no += 1
        elif fence == "code":
            rows.append(_diff_row("ctx", None, None, "", esc))
        elif (_MD_TROW_RE.match(line) and i < len(lines)
                and _MD_TSEP_RE.match(lines[i])):
            # A pipe row is only a table when the separator row follows it.
            # Without that check a values line containing pipes would be
            # swallowed into a table, which is rule 3 the wrong way round.
            j = i + 1
            while j < len(lines) and _MD_TROW_RE.match(lines[j]):
                j += 1
            block = lines[i - 1:j]
            # Every source line still gets its chance at a pending anchor,
            # exactly as when each was its own row; the first match lands on
            # the collapsed row. This is the one place rule 4 (one row per
            # source line) is deliberately broken, so consumption order is
            # kept identical on purpose.
            row_id = ""
            for src in block:
                got = _take_anchor(src)
                if got and not row_id:
                    row_id = got
            rows.append(_diff_row("mdt", None, None, "",
                                  _md_table(block), row_id=row_id))
            i = j
        else:
            # Anchor placement is frozen at the pre-2625 predicate on
            # purpose: headings of level 1 to 3 never took a pending match
            # and still do not, even though levels 4 to 6 now render as
            # headings too. No anchor may move, so the widened heading rule
            # is for rendering only.
            row_id = ""
            if not (line.startswith("# ") or line.startswith("## ")
                    or line.startswith("### ")):
                row_id = _take_anchor(line)
            cls, cell = _md_prose(line, esc)
            rows.append(_diff_row(cls, None, None, "", cell, row_id=row_id))
    return rows


def render_html(artifact, requested_sha=None):
    """Server-rendered Azure DevOps-style diff page. No external assets; the
    only script is a tiny theme switcher and a show-all toggle. EVERY dynamic
    value is escaped: the body is PR-controlled content, so the same
    comment-injection hardening the Bitbucket comment gets applies here.
    Colors follow the GitHub diff palette (more legible than Monaco's own),
    with Azure DevOps blue chrome. Light / Auto / Dark via a segmented
    control, persisted in localStorage; Auto follows prefers-color-scheme.

    requested_sha (COPS-2610): the sha in the URL, when the caller knows it.
    The artifact is keyed by (repo, pr) on purpose -- one live page per PR,
    exactly like the one comment -- so opening the build status of an OLDER
    commit serves the CURRENT tip. Deliberate and documented, but it must
    never be silent: a reviewer reading commit A's page as commit B's is
    reading evidence for the wrong change."""
    repo = html.escape(str(artifact.get("repo", "")))
    pr_id = html.escape(str(artifact.get("pr_id", "")))
    sha = html.escape(str(artifact.get("sha", "")))
    base_sha = html.escape(str(artifact.get("base_sha", "") or ""))
    created = html.escape(str(artifact.get("created_utc", "")))
    pr_url = str(artifact.get("pr_url", ""))
    # COPS-2673 (XSS-01): emit an href only for an http/https URL -- the same
    # positive scheme allow-list the markdown link renderer uses (_MD_LINK_RE).
    # pr_url is server-built today, so this is defence-in-depth: it keeps the
    # "no javascript:/data: href" guarantee local to this sink, surviving any
    # future change that lets pr_url become attacker-influenced.
    _pr_scheme_ok = (pr_url[:7].lower() == "http://"
                     or pr_url[:8].lower() == "https://")
    pr_link = (f'<a href="{html.escape(pr_url, quote=True)}">PR #{pr_id}</a>'
               if pr_url and _pr_scheme_ok else f"PR #{pr_id}")
    raw_href = html.escape(f"/diff/{artifact.get('repo','')}"
                           f"/{artifact.get('pr_id','')}"
                           f"/{artifact.get('sha','')}/raw", quote=True)
    base_bit = f" vs base <code>{base_sha}</code>" if base_sha else ""
    summary = _format_outcome_summary(artifact.get("app_count"),
                                      artifact.get("outcome_counts") or {})
    summary_html = f'<div class="summary">{summary}</div>' if summary else ""
    # Sha-mismatch banner. Case-normalised compare; a short-vs-long form of
    # the SAME sha is not a mismatch (both are validated hex, so prefix
    # compare on the shorter one is exact).
    mismatch_html = ""
    if requested_sha:
        req = str(requested_sha).lower()
        stored = str(artifact.get("sha", "")).lower()
        n = min(len(req), len(stored))
        if n and req[:n] != stored[:n]:
            mismatch_html = (
                f'<div class="notice">Showing the current tip '
                f'<code>{html.escape(stored[:12])}</code> of this PR '
                f'&mdash; you requested <code>{html.escape(req[:12])}</code>. '
                f'One page is kept per pull request and every new commit '
                f'replaces it, exactly like the PR comment.</div>')

    outline = build_outline(artifact.get("body", ""))
    index_html = _render_index(outline)
    rows = _render_body_rows(artifact.get("body", ""), outline=outline,
                             drop_header=True)
    visible = "".join(rows[:MAX_VISIBLE_LINES])
    overflow = rows[MAX_VISIBLE_LINES:]
    if overflow:
        rest = "".join(overflow)
        n_more = len(overflow)
        rest_html = (
            f'<tbody class="rest" hidden>{rest}</tbody>'
            f'<tbody class="show-all-row"><tr><td colspan="4">'
            f'<button type="button" class="show-all" onclick="'
            f"this.closest('table').querySelector('.rest').hidden=false;"
            f"this.closest('tbody').remove();"
            f'">show full output ({n_more} more lines)</button>'
            f'</td></tr></tbody>')
    else:
        rest_html = ""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{SERVICE_NAME} - {repo} #{pr_id} @ {sha}</title>
<style>
:root {{
  --bg: #eef1f6; --surface: #ffffff; --fg: #1f2328; --muted: #57606a;
  --border: #d5dae2;
  --panel: #f6f8fa; --link: #0969da; --accent: #0078d4;
  --gutter-bg: #fafbfc; --gutter-fg: #8b949e;
  --add-bg: #e6ffec; --add-mk: #1a7f37;
  --del-bg: #ffebe9; --del-mk: #cf222e;
  --hunk-bg: #f6f8fa; --hunk-fg: #57606a;
  --seg-bg: #e6e9ef; --seg-thumb: #ffffff; --seg-active: #0078d4;
  --mark-bg: #111418; --mark-add: #3fb950; --mark-del: #f85149;
}}
:root[data-theme="dark"] {{
  --bg: #14181f; --surface: #1a2029; --fg: #e6edf3; --muted: #8d96a0;
  --border: #363e4a;
  --panel: #20262f; --link: #4493f8; --accent: #4493f8;
  --gutter-bg: #1a2029; --gutter-fg: #6e7681;
  --add-bg: #2ea04326; --add-mk: #3fb950;
  --del-bg: #f8514926; --del-mk: #f85149;
  --hunk-bg: #20262f; --hunk-fg: #8d96a0;
  --seg-bg: #20262f; --seg-thumb: #363e4a; --seg-active: #4493f8;
  --mark-bg: #1f6feb; --mark-add: #3fb950; --mark-del: #f85149;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --bg: #14181f; --surface: #1a2029; --fg: #e6edf3; --muted: #8d96a0;
    --border: #363e4a;
    --panel: #20262f; --link: #4493f8; --accent: #4493f8;
    --gutter-bg: #1a2029; --gutter-fg: #6e7681;
    --add-bg: #2ea04326; --add-mk: #3fb950;
    --del-bg: #f8514926; --del-mk: #f85149;
    --hunk-bg: #20262f; --hunk-fg: #8d96a0;
    --seg-bg: #20262f; --seg-thumb: #363e4a; --seg-active: #4493f8;
    --mark-bg: #1f6feb; --mark-add: #3fb950; --mark-del: #f85149;
  }}
}}
* {{ box-sizing: border-box; }}
body {{ background: var(--bg); color: var(--fg); margin: 0;
       font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }}
.topbar {{ position: sticky; top: 0; z-index: 5; display: flex;
          align-items: center; justify-content: space-between;
          padding: 9px 18px; background: var(--surface);
          border-bottom: 1px solid var(--border); }}
.brandbox {{ display: flex; align-items: center; gap: 9px; }}
.mark {{ width: 26px; height: 26px; border-radius: 7px; background: var(--mark-bg);
        display: inline-flex; flex-direction: column; justify-content: center;
        gap: 3px; padding: 0 5px; flex: none; }}
.mark .ln {{ height: 3px; border-radius: 1.5px; display: block; }}
.mark .ln-a {{ background: var(--mark-add); width: 12px; }}
.mark .ln-d {{ background: var(--mark-del); width: 8px; }}
.wordmark {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
            font-size: 13px; font-weight: 700; letter-spacing: .02em; }}
.seg {{ display: inline-flex; gap: 2px; padding: 2px; border-radius: 7px;
       background: var(--seg-bg); }}
.seg button {{ border: none; background: transparent; width: 30px; height: 24px;
              border-radius: 5px; cursor: pointer; color: var(--muted);
              font-size: 13px; line-height: 1; }}
.seg button[aria-pressed="true"] {{ background: var(--seg-thumb);
              color: var(--seg-active); }}
main {{ max-width: none; margin: 0 auto; padding: 1.5rem 24px 3rem; }}
.brand {{ color: var(--muted); font-size: 12px; font-weight: 600;
         text-transform: uppercase; letter-spacing: .08em; }}
h1 {{ margin: .25rem 0 .35rem; font-size: 21px; font-weight: 600; }}
h1 .pr {{ color: var(--muted); font-weight: 400; }}
.meta {{ color: var(--muted); font-size: 13px; margin-bottom: .6rem; }}
.meta a {{ color: var(--link); text-decoration: none; }}
.meta a:hover {{ text-decoration: underline; }}
code {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
       font-size: 12px; background: var(--panel);
       border: 1px solid var(--border); border-radius: 4px; padding: 0 4px; }}
.summary {{ margin: 0 0 1rem; }}
.summary span {{ display: inline-block; background: var(--panel);
                border: 1px solid var(--border); border-radius: 999px;
                color: var(--muted); font-size: 12px;
                padding: 2px 10px; margin: 0 6px 6px 0; }}
.diffwrap {{ border: 1px solid var(--border); border-radius: 8px;
            background: var(--surface);
            overflow: auto; max-height: 78vh; }}
table.diff {{ width: 100%; border-collapse: collapse;
             font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
             font-size: 12px; line-height: 20px; }}
table.diff td {{ padding: 0 8px; vertical-align: top; }}
table.diff td.code {{ white-space: pre; width: 100%; }}
td.ln-old, td.ln-new {{ width: 1%; text-align: right; padding: 0 8px;
             color: var(--gutter-fg); background: var(--gutter-bg);
             user-select: none; border-right: 1px solid var(--border); }}
td.mk {{ width: 14px; text-align: center; user-select: none; }}
tr.add td.code {{ background: var(--add-bg); }}
tr.add td.mk {{ color: var(--add-mk); }}
tr.del td.code {{ background: var(--del-bg); }}
tr.del td.mk {{ color: var(--del-mk); }}
tr.hunk td {{ background: var(--hunk-bg); color: var(--hunk-fg); }}
tr.fence td.code {{ color: var(--muted); opacity: .55; }}
tr.mdh td.code {{ font-weight: 700; }}
/* COPS-2625: prose is rendered, not printed. The code cell stays
   white-space: pre everywhere except the collapsed table row, where the
   table does its own layout. */
tr.mdh1 td.code {{ font-size: 20px; line-height: 30px; }}
tr.mdh2 td.code {{ font-size: 17px; line-height: 26px; }}
tr.mdh3 td.code {{ font-size: 15px; line-height: 24px; }}
tr.mdh4 td.code, tr.mdh5 td.code, tr.mdh6 td.code {{ font-size: 13px; }}
tr.mdh td.code, tr.mdli td.code {{
             font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
                          Helvetica, Arial, sans-serif; }}
tr.mdrule td.code {{ padding: 0; }}
tr.mdrule td.code::after {{ content: ""; display: block;
             border-top: 1px solid var(--border); margin: 6px 0; }}
td.code strong {{ font-weight: 700; }}
td.code code {{ background: var(--panel); border: 1px solid var(--border);
             border-radius: 4px; padding: 0 4px; font-size: 11.5px; }}
td.code a {{ color: var(--link); }}
tr.mdt td.code {{ white-space: normal; padding: 6px 8px; }}
table.mdt {{ border-collapse: collapse; margin: 4px 0;
             font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
                          Helvetica, Arial, sans-serif; font-size: 12px; }}
table.mdt th, table.mdt td {{ border: 1px solid var(--border);
             padding: 4px 8px; text-align: left; vertical-align: top; }}
table.mdt th {{ background: var(--panel); font-weight: 600; }}
.show-all {{ width: 100%; border: none; background: var(--panel);
            color: var(--link); font: inherit; font-size: 12px;
            padding: 8px; cursor: pointer; }}
.show-all:hover {{ text-decoration: underline; }}
.notice {{ background: var(--hunk-bg); color: var(--hunk-fg);
          border: 1px solid var(--border); border-radius: 4px;
          padding: 8px 10px; margin: .6rem 0; font-size: 13px; }}
.toc {{ border: 1px solid var(--border); border-radius: 4px;
       background: var(--panel); padding: 8px 10px; margin: .6rem 0;
       font-size: 13px; }}
.toc > summary {{ cursor: pointer; font-weight: 600; }}
.tocfilter {{ width: 100%; box-sizing: border-box; margin: 8px 0;
             padding: 6px 8px; font: inherit; font-size: 13px;
             color: inherit; background: var(--bg);
             border: 1px solid var(--border); border-radius: 4px; }}
.toclist {{ list-style: none; margin: 0; padding: 0;
           max-height: 22rem; overflow-y: auto; }}
.toclist ul {{ list-style: none; margin: 0 0 0 1rem; padding: 0; }}
.tocapp > details > summary {{ cursor: pointer; padding: 2px 0; }}
.tocres {{ padding: 1px 0; }}
.tocres a, .tocapp a {{ color: var(--link); text-decoration: none; }}
.tocres a:hover, .tocapp a:hover {{ text-decoration: underline; }}
.tocn {{ color: var(--muted); font-size: 12px; }}
.tochide {{ display: none; }}
tr:target td {{ background: var(--hunk-bg); }}
footer {{ color: var(--muted); font-size: 12px; margin-top: 1rem; }}
</style>
</head>
<body>
<div class="topbar">
  <span class="brandbox"><span class="mark" aria-hidden="true"><span class="ln ln-a"></span><span class="ln ln-d"></span></span><span class="wordmark">acme-diff-preview</span></span>
  <div class="seg" role="group" aria-label="Appearance">
    <button type="button" data-set-theme="light" aria-label="Light" title="Light">&#9728;</button>
    <button type="button" data-set-theme="auto" aria-label="Auto" title="Auto">&#9673;</button>
    <button type="button" data-set-theme="dark" aria-label="Dark" title="Dark">&#9789;</button>
  </div>
</div>
<main>
<div class="brand">{SERVICE_NAME}</div>
<h1>{repo} <span class="pr">{pr_link}</span></h1>
<div class="meta">commit <code>{sha}</code>{base_bit}
 &middot; generated {created} &middot; <a href="{raw_href}">raw</a></div>
{mismatch_html}{summary_html}
{index_html}
<div class="diffwrap">
<table class="diff"><tbody>{visible}</tbody>{rest_html}</table>
</div>
<footer>served by acme-diff-preview &middot; full, untruncated output for this exact commit</footer>
</main>
<script>
(function(){{
  var root=document.documentElement;
  function apply(t){{
    if(t==="auto"){{root.removeAttribute("data-theme");}}
    else{{root.setAttribute("data-theme",t);}}
    var b=document.querySelectorAll("[data-set-theme]");
    for(var i=0;i<b.length;i++){{
      b[i].setAttribute("aria-pressed", b[i].getAttribute("data-set-theme")===t ? "true":"false");
    }}
  }}
  var saved="auto";
  try{{ saved=localStorage.getItem("adp-theme")||"auto"; }}catch(e){{}}
  apply(saved);
  var btns=document.querySelectorAll("[data-set-theme]");
  for(var i=0;i<btns.length;i++){{
    btns[i].addEventListener("click",function(){{
      var t=this.getAttribute("data-set-theme");
      try{{ localStorage.setItem("adp-theme",t); }}catch(e){{}}
      apply(t);
    }});
  }}
  // Index filter (COPS-2611). Narrows the INDEX, never the body: the body
  // is the evidence and must not silently hide rows. Reads a pre-escaped
  // data-k attribute and only toggles a class, so no body-derived string
  // is ever written back into the DOM. Debounced because a large page has
  // ~20k index nodes (measured: prod #3887 has 19,869 resources).
  var box=document.querySelector(".tocfilter");
  if(box){{
    var apps=document.querySelectorAll(".tocapp");
    var timer=null;
    function run(){{
      var q=box.value.toLowerCase().trim();
      for(var i=0;i<apps.length;i++){{
        var app=apps[i];
        var appHit=!q||app.getAttribute("data-k").indexOf(q)>=0;
        var kids=app.querySelectorAll(".tocres");
        var any=false;
        for(var j=0;j<kids.length;j++){{
          var hit=appHit||kids[j].getAttribute("data-k").indexOf(q)>=0;
          kids[j].classList.toggle("tochide",!hit);
          if(hit){{any=true;}}
        }}
        app.classList.toggle("tochide",!(appHit||any));
        var det=app.querySelector("details");
        if(det&&q){{det.open=true;}}
      }}
    }}
    box.addEventListener("input",function(){{
      if(timer){{clearTimeout(timer);}}
      timer=setTimeout(run,120);
    }});
  }}
  // An index entry can point at a row inside the collapsed overflow (the
  // page shows MAX_VISIBLE_LINES rows outright). Without this, clicking
  // such an entry does nothing at all -- and it is exactly the large pages
  // that need the index most. Reveal the overflow, then jump.
  function revealTarget(){{
    var h=location.hash;
    if(!h||h.length<2){{return;}}
    var el=document.getElementById(h.slice(1));
    if(!el){{return;}}
    var rest=document.querySelector(".rest");
    if(rest&&rest.hidden&&rest.contains(el)){{
      rest.hidden=false;
      var b=document.querySelector(".show-all-row");
      if(b){{b.remove();}}
      el.scrollIntoView();
    }}
  }}
  window.addEventListener("hashchange",revealTarget);
  revealTarget();
}})();
</script>
</body>
</html>
"""


def respond(path, base_dir, enabled, bucket=""):
    """Pure request handler: (status, content_type, payload bytes).

    Pure on purpose so the HTTP layer in diff_preview stays a 5-line shim
    and everything here is unit-testable without a socket.
    """
    text = "text/plain; charset=utf-8"
    if not enabled:
        return 404, text, b"diff UI disabled"
    parsed = parse_request_path(path)
    if parsed is None:
        return 400, text, b"bad request"
    repo, pr_id, sha, raw = parsed
    artifact = load_artifact(base_dir, repo, pr_id, sha, bucket=bucket)
    if artifact is None:
        # COPS-2610: a pruned or never-generated page must explain itself.
        # Once the comment stops carrying YAML (phase E), this moment is a
        # reviewer discovering that the only record of a merged PR is gone;
        # two words of text/plain are not an acceptable way to say that.
        # Everything interpolated is validated (repo/pr/sha regexes in
        # parse_request_path) and escaped anyway.
        gone = (
            "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<title>diff no longer retained</title></head><body "
            "style=\"font-family:system-ui;max-width:44rem;margin:3rem auto\">"
            f"<h1>No diff page for {html.escape(str(repo))} PR "
            f"#{html.escape(str(pr_id))}</h1>"
            "<p>The diff for this pull request is <strong>no longer "
            "retained</strong>, or was never generated (the PR may predate "
            "the diff-preview service, or the run may have been skipped)."
            "</p><p>Artifacts are kept for a fixed retention window after "
            "the PR's last diff run. For older history, the PR's file diff "
            "in Bitbucket and the rendered state in ArgoCD remain "
            "available.</p></body></html>")
        return 404, "text/html; charset=utf-8", gone.encode("utf-8")
    if raw:
        return 200, text, str(artifact.get("body", "")).encode("utf-8")
    return (200, "text/html; charset=utf-8",
            render_html(artifact, requested_sha=sha).encode("utf-8"))
