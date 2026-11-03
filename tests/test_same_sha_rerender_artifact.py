"""A replica must not serve an older render of the same commit.

A PR that asks for a chart version not yet in the registry renders twice:
first "chart not found", then a clean diff once the chart is pullable. Same
commit, two artifacts. The leader served the clean one. The standby kept
serving "chart not found" for the same URL, because its local copy carried
the requested sha and a matching sha was treated as proof of freshness.

A commit is rendered again whenever its last result was not final (a chart
not published yet, a transient error) or its base moved, so the sha alone
cannot say whether the local copy is current. The bucket object generation
can: every upload gets a new one.

Each pod is its own process with its own generation map. `_pod` switches the
map so two base dirs in one test behave like two replicas.
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

REPO, PR, SHA = "acme-config-prod", 1, "abc1234def56"


class _Resp:
    def __init__(self, data=b"{}", headers=None):
        self._data = data
        self.headers = headers or {}

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _gcs(monkeypatch, calls):
    """Fake GCS that bumps an object's generation on every upload."""
    objects = {}

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        calls.append(url)
        if "metadata.google.internal" in url:
            return _Resp(json.dumps(
                {"access_token": "tok", "expires_in": 3600}).encode())
        if "/upload/storage/v1/b/" in url:
            name = urllib.parse.unquote(url.split("name=")[1])
            gen = str(int(objects.get(name, (b"", "0"))[1]) + 1)
            objects[name] = (req.data, gen)
            return _Resp(json.dumps({"name": name, "generation": gen}).encode())
        if "/storage/v1/b/" in url:
            name = urllib.parse.unquote(url.split("/o/")[1].split("?")[0])
            if req.get_method() == "DELETE":
                objects.pop(name, None)
                return _Resp(b"")
            if name not in objects:
                raise urllib.error.HTTPError(url, 404, "gone", {}, None)
            data, gen = objects[name]
            if "alt=media" in url:
                return _Resp(data, {"x-goog-generation": gen})
            return _Resp(json.dumps({"generation": gen}).encode())
        raise AssertionError(url)  # pragma: no cover

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(diff_ui, "_token_cache", {"token": "", "exp": 0.0})
    monkeypatch.setattr(diff_ui, "on_warning", None)


def _pod(monkeypatch, generations):
    monkeypatch.setattr(diff_ui, "_local_generation", generations,
                        raising=False)


def _downloads(calls):
    return [u for u in calls if "alt=media" in u]


def test_standby_serves_the_newer_render_of_the_same_commit(tmp_path,
                                                            monkeypatch):
    calls = []
    _gcs(monkeypatch, calls)
    leader, standby = str(tmp_path / "leader"), str(tmp_path / "standby")
    leader_gens, standby_gens = {}, {}

    _pod(monkeypatch, leader_gens)
    diff_ui.save_artifact(leader, REPO, PR, SHA, "chart not found", bucket="b")
    _pod(monkeypatch, standby_gens)
    assert diff_ui.load_artifact(standby, REPO, PR, SHA,
                                 bucket="b")["body"] == "chart not found"

    _pod(monkeypatch, leader_gens)
    diff_ui.save_artifact(leader, REPO, PR, SHA, "94 resources", bucket="b")

    _pod(monkeypatch, standby_gens)
    art = diff_ui.load_artifact(standby, REPO, PR, SHA, bucket="b")
    assert art["body"] == "94 resources", "standby served the older render"


def test_former_leader_serves_the_newer_render_of_the_same_commit(
        tmp_path, monkeypatch):
    """Leadership moves: the pod that wrote the first render is the one
    holding the stale copy."""
    calls = []
    _gcs(monkeypatch, calls)
    old_leader, new_leader = str(tmp_path / "a"), str(tmp_path / "b")
    old_gens, new_gens = {}, {}

    _pod(monkeypatch, old_gens)
    diff_ui.save_artifact(old_leader, REPO, PR, SHA, "chart not found",
                          bucket="b")
    _pod(monkeypatch, new_gens)
    diff_ui.save_artifact(new_leader, REPO, PR, SHA, "94 resources",
                          bucket="b")

    _pod(monkeypatch, old_gens)
    art = diff_ui.load_artifact(old_leader, REPO, PR, SHA, bucket="b")
    assert art["body"] == "94 resources"


def test_unchanged_object_is_served_locally_without_a_download(tmp_path,
                                                               monkeypatch):
    calls = []
    _gcs(monkeypatch, calls)
    leader, standby = str(tmp_path / "leader"), str(tmp_path / "standby")
    leader_gens, standby_gens = {}, {}
    _pod(monkeypatch, leader_gens)
    diff_ui.save_artifact(leader, REPO, PR, SHA, "94 resources", bucket="b")
    _pod(monkeypatch, standby_gens)
    diff_ui.load_artifact(standby, REPO, PR, SHA, bucket="b")

    before = len(calls)
    for base, gens in ((leader, leader_gens), (standby, standby_gens)):
        _pod(monkeypatch, gens)
        assert diff_ui.load_artifact(base, REPO, PR, SHA,
                                     bucket="b")["body"] == "94 resources"
    assert _downloads(calls[before:]) == [], "a current copy was downloaded"


def test_unreachable_bucket_still_serves_the_local_copy(tmp_path,
                                                        monkeypatch):
    calls = []
    _gcs(monkeypatch, calls)
    _pod(monkeypatch, {})
    base = str(tmp_path)
    diff_ui.save_artifact(base, REPO, PR, SHA, "94 resources", bucket="b")

    def boom(req, timeout=None):
        raise urllib.error.URLError("gcs down")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(diff_ui, "_token_cache", {"token": "", "exp": 0.0})
    art = diff_ui.load_artifact(base, REPO, PR, SHA, bucket="b")
    assert art["body"] == "94 resources"
