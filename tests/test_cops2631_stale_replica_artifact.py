"""A replica must not serve a diff it already knows is out of date.

Live case, acme-config-dev PR #7063 on hub 2.45.0: the two replicas served
different pages for the same URL. The leader had the current artifact
(sha 9d822fc4, 110 resources changed); the standby served a copy from two
minutes earlier (sha aae79e66, "no manifest changes"), so whether a reviewer
saw the change at all depended on which pod the load balancer picked.

Cause: the artifact is keyed by (repo, pr), never by sha. The standby served
one page view while the older artifact was current, warmed its local cache
from GCS, and from then on every read hit that local file. GCS was never
consulted again, so the newer artifact the leader uploaded was invisible to it.

The request URL carries the sha, so a stale copy is detectable: the artifact
stores its own sha. When they disagree and a bucket is configured, GCS is the
shared source of truth and has to be asked.

Deliberately unchanged: with no bucket, a stale sha still resolves to the
newest local artifact (test_load_by_stale_sha_returns_latest), and a matching
sha still costs zero network calls (the warm-cache win).
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_ui  # noqa: E402

REPO, PR = "acme-config-dev", 7063
OLD_SHA, NEW_SHA = "aae79e66ae58", "9d822fc4e701"


class _FakeResp:
    def __init__(self, data=b"{}"):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _gcs(monkeypatch, objects, calls):
    """Fake metadata token + GCS object store, recording every call."""
    def fake_urlopen(req, timeout=None):
        url = req.full_url
        calls.append(url)
        if "metadata.google.internal" in url:
            return _FakeResp(json.dumps(
                {"access_token": "tok", "expires_in": 3600}).encode())
        if "/upload/storage/v1/b/" in url:
            objects[urllib.parse.unquote(url.split("name=")[1])] = req.data
            return _FakeResp()
        if "/storage/v1/b/" in url:
            name = urllib.parse.unquote(url.split("/o/")[1].split("?")[0])
            if req.get_method() == "DELETE":
                objects.pop(name, None)
                return _FakeResp(b"")
            if name not in objects:
                raise urllib.error.HTTPError(url, 404, "gone", {}, None)
            return _FakeResp(objects[name])
        raise AssertionError(url)  # pragma: no cover
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(diff_ui, "_token_cache", {"token": "", "exp": 0.0})
    monkeypatch.setattr(diff_ui, "on_warning", None)


def test_standby_with_a_stale_local_copy_refetches_from_gcs(tmp_path,
                                                            monkeypatch):
    """The exact PR #7063 failure, reproduced across two base dirs."""
    objects, calls = {}, []
    _gcs(monkeypatch, objects, calls)
    leader, standby = str(tmp_path / "leader"), str(tmp_path / "standby")

    # Leader publishes the first commit's diff; standby serves a page view
    # and warms its local cache with it.
    diff_ui.save_artifact(leader, REPO, PR, OLD_SHA, "no manifest changes",
                          bucket="b")
    warmed = diff_ui.load_artifact(standby, REPO, PR, OLD_SHA, bucket="b")
    assert warmed["sha"] == OLD_SHA

    # Leader publishes the second commit's diff, in place, same PR.
    diff_ui.save_artifact(leader, REPO, PR, NEW_SHA, "110 resources changed",
                          bucket="b")

    # A reviewer opens the link for the new commit and lands on the standby.
    art = diff_ui.load_artifact(standby, REPO, PR, NEW_SHA, bucket="b")
    assert art["sha"] == NEW_SHA, "standby served a stale diff"
    assert art["body"] == "110 resources changed"


def test_refetched_artifact_replaces_the_stale_local_copy(tmp_path,
                                                          monkeypatch):
    objects, calls = {}, []
    _gcs(monkeypatch, objects, calls)
    leader, standby = str(tmp_path / "leader"), str(tmp_path / "standby")
    diff_ui.save_artifact(leader, REPO, PR, OLD_SHA, "old", bucket="b")
    diff_ui.load_artifact(standby, REPO, PR, OLD_SHA, bucket="b")
    diff_ui.save_artifact(leader, REPO, PR, NEW_SHA, "new", bucket="b")
    diff_ui.load_artifact(standby, REPO, PR, NEW_SHA, bucket="b")

    # Second read for the same sha is local-only again: the refresh healed
    # the cache instead of turning every later view into a GCS round trip.
    before = len(calls)
    art = diff_ui.load_artifact(standby, REPO, PR, NEW_SHA, bucket="b")
    assert art["sha"] == NEW_SHA
    assert len(calls) == before, "healed cache must not re-hit GCS"


def test_matching_sha_never_touches_the_network(tmp_path, monkeypatch):
    """The warm-cache win must survive this fix."""
    objects, calls = {}, []
    _gcs(monkeypatch, objects, calls)
    base = str(tmp_path / "one")
    diff_ui.save_artifact(base, REPO, PR, NEW_SHA, "body", bucket="b")
    before = len(calls)
    assert diff_ui.load_artifact(base, REPO, PR, NEW_SHA,
                                 bucket="b")["sha"] == NEW_SHA
    assert len(calls) == before


def test_stale_sha_without_bucket_still_returns_the_newest_local(tmp_path):
    """Unchanged contract: the build-status link must never 404 just because
    the tip moved (test_load_by_stale_sha_returns_latest, no bucket)."""
    base = str(tmp_path)
    diff_ui.save_artifact(base, REPO, PR, OLD_SHA, "old")
    diff_ui.save_artifact(base, REPO, PR, NEW_SHA, "new")
    art = diff_ui.load_artifact(base, REPO, PR, OLD_SHA)
    assert art["sha"] == NEW_SHA


def test_gcs_copy_no_better_than_local_falls_back_to_local(tmp_path,
                                                           monkeypatch):
    """If GCS is also behind, serve what we have rather than a 404: the
    reviewer gets the newest diff that exists, which is the old contract."""
    objects, calls = {}, []
    _gcs(monkeypatch, objects, calls)
    base = str(tmp_path)
    diff_ui.save_artifact(base, REPO, PR, OLD_SHA, "old", bucket="b")
    art = diff_ui.load_artifact(base, REPO, PR, NEW_SHA, bucket="b")
    assert art is not None and art["sha"] == OLD_SHA


def test_unreachable_gcs_does_not_break_a_stale_read(tmp_path, monkeypatch):
    base = str(tmp_path)
    diff_ui.save_artifact(base, REPO, PR, OLD_SHA, "old")

    def boom(req, timeout=None):
        raise urllib.error.URLError("gcs down")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(diff_ui, "_token_cache", {"token": "", "exp": 0.0})
    monkeypatch.setattr(diff_ui, "on_warning", None)
    art = diff_ui.load_artifact(base, REPO, PR, NEW_SHA, bucket="b")
    assert art is not None and art["sha"] == OLD_SHA
