"""COPS-2647: a failed artifact upload must not silently split the replicas.

2.46.0 fixed the two hub replicas serving different pages for the same
URL: the standby had warmed from GCS while an older commit was current,
so `load_artifact` now treats a local sha mismatch as a miss when a
bucket is configured and goes to the bucket instead.

That fix assumes the bucket holds the current artifact. Nothing
guaranteed it did. `_gcs_upload` was a single attempt that logged a
warning and returned False, so one timeout, one 503 or one token blip
left the previous commit's artifact in the bucket and the same defect
came back through a different door -- with no counter and no alert, only
a warning line in whichever pod happened to be the leader.

Three things here: bounded retries on TRANSIENT failures only, counters
so the condition is visible at all, and a reconcile pass so a failed
upload heals itself on a later iteration instead of waiting for the next
commit to overwrite it.

The reconcile deliberately re-reads the payload from the local file
rather than holding the bytes. COPS-2645 shipped a leak by pinning
compressed render bytes in a closure; not repeating it.
"""
import os
import sys
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_preview as m  # noqa: E402
import diff_ui  # noqa: E402


class _Resp:
    def __init__(self, body=b"{}"):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Bucket:
    """Fake GCS at the HTTP boundary, with scriptable failures."""

    def __init__(self, fail_times=0, exc=None):
        self.objects = {}
        self.attempts = []
        self.fail_times = fail_times
        self.exc = exc or TimeoutError("simulated transient failure")

    def install(self, monkeypatch):
        monkeypatch.setattr(diff_ui, "_gcs_token", lambda: "fake-token")
        monkeypatch.setattr(diff_ui, "_GCS_RETRY_SLEEP", 0.0, raising=False)
        bucket = self

        def fake_urlopen(req, *a, **kw):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "/upload/" in url:
                name = url.split("&name=")[-1]
                bucket.attempts.append(name)
                if len(bucket.attempts) <= bucket.fail_times:
                    raise bucket.exc
                bucket.objects[name] = req.data
                return _Resp()
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        monkeypatch.setattr(diff_ui.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(diff_ui, "_gcs_delete", lambda b, n: True)
        return self


def _reset():
    for k in ("artifact_gcs_upload_ok", "artifact_gcs_upload_failed",
              "artifact_gcs_upload_retries", "artifact_gcs_pending",
              "artifact_gcs_download_failed"):
        if k in m._diff_stats:
            m._diff_stats[k] = 0
    diff_ui.reset_pending_uploads()


def _save(tmp_path, sha="abc1234", body="# diff", pr_id=42):
    return diff_ui.save_artifact(str(tmp_path), "acme-config-dev", pr_id, sha,
                                 body, bucket="b")


# --- retries -------------------------------------------------------------

def test_a_transient_failure_is_retried_and_succeeds(tmp_path, monkeypatch):
    """Most bucket failures are transient. A single attempt threw away the
    cheapest possible recovery."""
    bucket = _Bucket(fail_times=2).install(monkeypatch)
    _reset()
    _save(tmp_path)
    assert len(bucket.attempts) == 3, "expected two retries then a success"
    assert bucket.objects, "the object must land"
    assert m._diff_stats["artifact_gcs_upload_ok"] == 1
    assert m._diff_stats["artifact_gcs_upload_failed"] == 0


def test_retries_are_bounded(tmp_path, monkeypatch):
    """Bounded and off the critical path: a bucket outage must not turn one
    save into an unbounded stall."""
    bucket = _Bucket(fail_times=99).install(monkeypatch)
    _reset()
    _save(tmp_path)
    assert len(bucket.attempts) <= 4, (
        f"too many attempts ({len(bucket.attempts)}) for one save")
    assert m._diff_stats["artifact_gcs_upload_failed"] == 1


def test_a_permanent_error_is_not_retried(tmp_path, monkeypatch):
    """A 403 will still be a 403 in 200ms. Retrying it wastes the diff run's
    time and hammers a bucket that is already telling us something."""
    err = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
    bucket = _Bucket(fail_times=99, exc=err).install(monkeypatch)
    _reset()
    _save(tmp_path)
    assert len(bucket.attempts) == 1, (
        f"a permanent error must not be retried, got {len(bucket.attempts)}")
    assert m._diff_stats["artifact_gcs_upload_failed"] == 1


def test_a_failed_upload_never_fails_the_save(tmp_path, monkeypatch):
    """Durability is a bonus tier. A bucket outage must never block a PR
    comment or fail a diff run."""
    _Bucket(fail_times=99).install(monkeypatch)
    _reset()
    path = _save(tmp_path)
    assert path and os.path.exists(path), "the local artifact must still exist"
    assert diff_ui.load_artifact(str(tmp_path), "acme-config-dev", 42,
                                 "abc1234") is not None


# --- reconcile -----------------------------------------------------------

def test_a_failed_upload_is_retried_on_the_next_pass(tmp_path, monkeypatch):
    """A failed upload should heal within a poll cycle rather than waiting
    for the next commit to overwrite it."""
    bucket = _Bucket(fail_times=99).install(monkeypatch)
    _reset()
    _save(tmp_path)
    assert diff_ui.pending_upload_count() == 1

    bucket.fail_times = 0          # the bucket comes back
    assert diff_ui.retry_pending_uploads() == 1
    assert bucket.objects, "the object must land on the reconcile pass"
    assert diff_ui.pending_upload_count() == 0


def test_the_reconcile_rereads_from_disk(tmp_path, monkeypatch):
    """Deliberately not holding the bytes: COPS-2645 shipped a leak by
    pinning compressed payloads in a closure. A vanished local file is
    dropped rather than retried forever."""
    bucket = _Bucket(fail_times=99).install(monkeypatch)
    _reset()
    path = _save(tmp_path)
    os.remove(path)
    bucket.fail_times = 0
    assert diff_ui.retry_pending_uploads() == 0
    assert diff_ui.pending_upload_count() == 0


def test_pending_is_bounded(tmp_path, monkeypatch):
    """An unbounded pending map during a long outage is a leak."""
    _Bucket(fail_times=99).install(monkeypatch)
    _reset()
    for i in range(300):
        _save(tmp_path, sha="%07x" % (i + 0x1000000), pr_id=i + 1)
    assert diff_ui.pending_upload_count() <= 128, (
        f"pending grew to {diff_ui.pending_upload_count()}")


def test_a_superseded_pr_replaces_its_pending_entry(tmp_path, monkeypatch):
    """A newer commit for the same PR makes the older pending upload
    pointless -- uploading it would put a stale artifact in the bucket."""
    _Bucket(fail_times=99).install(monkeypatch)
    _reset()
    _save(tmp_path, sha="01dabc1")
    _save(tmp_path, sha="0e0abc2")
    assert diff_ui.pending_upload_count() == 1, (
        "the same PR must hold one pending upload, the newest")


# --- observability -------------------------------------------------------

def test_the_counters_exist_and_are_exported():
    """The counter has to exist before COPS-2648 can alert on it."""
    names = [row[0] for row in m._PROM_REGISTRY]
    for key in ("artifact_gcs_upload_ok", "artifact_gcs_upload_failed",
                "artifact_gcs_pending"):
        assert key in m._diff_stats, f"{key} must be a declared counter"
        assert key in names, f"{key} must be exported at /metrics"


def test_a_404_download_is_a_miss_not_a_failure(tmp_path, monkeypatch):
    """A missing object is the normal cold path, not an operational
    problem. Conflating them would make the failure counter useless."""
    _Bucket().install(monkeypatch)
    _reset()
    before = m._diff_stats.get("artifact_gcs_download_failed", 0)
    assert diff_ui._gcs_download("b", "nope") is None
    assert m._diff_stats.get("artifact_gcs_download_failed", 0) == before


def test_the_render_cache_shares_the_hardened_upload(tmp_path, monkeypatch):
    """COPS-2645 mirrors renders through the same helper, so it inherits
    the retries. Pinning it so a future refactor cannot split them."""
    bucket = _Bucket(fail_times=2).install(monkeypatch)
    _reset()
    assert diff_ui._gcs_upload("b", "render-cache/salt/key.yaml.zst", b"data")
    assert len(bucket.attempts) == 3
