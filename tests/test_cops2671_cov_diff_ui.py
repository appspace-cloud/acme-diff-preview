"""COPS-2671: the dark corners of the full-diff artifact store and page.

Every line pinned here is a fallback: the branch diff_ui takes when the
happy path it was written for does not happen. They stayed dark for four
different reasons, and each reason is worth naming because it says what
kind of regression could ship unnoticed.

  * The wheel-less encode path (`_encode_artifact_bytes` -> `.json`) is
    dark because CI installs `zstandard`. Production installs it too, but
    the fallback exists precisely for the image that does not, and if it
    ever produced a `.json.zst` name holding raw JSON, every reader would
    reject it.

  * `_artifact_paths_for_read` / `_artifact_zst_path` are dark because
    `load_artifact` open-codes the same two names inline (deliberately --
    CodeQL loses the sanitiser barrier across a call boundary, see the
    comment there). Two spellings of one naming rule is exactly the pair
    that drifts, so the helper is never compared to a restated format
    string: every claim about a name is made by writing through
    `save_artifact` and reading back through `load_artifact`, which is the
    only way the inline spelling and its load ORDER get a vote.

  * The soft-failure paths -- a stat hook that throws, a
    cleanup DELETE that 404s or 500s, a legacy sibling that will not
    unlink, a corrupt object -- are dark because nothing in the suite
    made those things fail. Every one of them exists so that a bonus tier
    (durability, observability) can break without taking a diff run with
    it, which is a property no green happy-path test can demonstrate.

  * The pending-upload eviction at `_PENDING_UPLOAD_MAX` was dark by
    accident: `test_pending_is_bounded` scripts a bucket that fails its
    first 99 ATTEMPTS, and at three attempts per save it starts
    succeeding after ~33 saves, so the map never reaches 128 and the
    assertion `<= 128` passes without the bound ever being applied.

Two page-render fallbacks join them: the anchor that has to survive a
markdown table collapsing many source lines into one row, and the NUL
guard in `_render_inline` (rule 3: an unrecognised line renders exactly
as it did before, never decorated with a half-applied transform).
"""
import json
import os
import sys
import urllib.error

import pytest

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import diff_ui  # noqa: E402

REPO = "acme-config-dev"
PR = 7063
SHA = "ab12cd3"
BODY = "## Diff\n" + ("===== /Deployment ns/app ======\n- a\n+ b\n" * 20)


def _legacy_artifact(body=BODY, sha=SHA):
    """A pre-stage-4 artifact dict, as raw UTF-8 JSON bytes."""
    return json.dumps({
        "repo": REPO, "pr_id": PR, "sha": sha, "pr_url": "",
        "base_sha": "", "outcome_counts": {}, "app_count": None,
        "created_utc": "2026-01-01 00:00:00 UTC", "body": body,
    }, ensure_ascii=False).encode("utf-8")


@pytest.fixture(autouse=True)
def _isolated_module_state():
    """`_pending_uploads` is a process-wide map; keep it out of other files."""
    diff_ui.reset_pending_uploads()
    yield
    diff_ui.reset_pending_uploads()


class _Resp:
    def read(self):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Bucket:
    """Fake GCS at the urlopen boundary, with a scriptable DELETE outcome.

    Uploads always succeed here: these tests are about what happens
    AFTER a successful upload, when the cleanup of the other encoding
    goes wrong.
    """

    def __init__(self, delete_exc=None):
        self.uploaded = {}
        self.deleted = []
        self.delete_exc = delete_exc

    def install(self, monkeypatch):
        monkeypatch.setattr(diff_ui, "_gcs_token", lambda: "fake-token")
        monkeypatch.setattr(diff_ui, "_GCS_RETRY_SLEEP", 0.0, raising=False)
        bucket = self

        def fake_urlopen(req, *a, **kw):
            url = req.full_url
            if "/upload/" in url:
                bucket.uploaded[url.split("&name=")[-1]] = req.data
                return _Resp()
            if req.get_method() == "DELETE":
                name = url.rsplit("/o/", 1)[-1]
                bucket.deleted.append(name)
                if bucket.delete_exc is not None:
                    raise bucket.delete_exc
                return _Resp()
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        monkeypatch.setattr(diff_ui.urllib.request, "urlopen", fake_urlopen)
        return self


@pytest.fixture
def warnings(monkeypatch):
    """Capture the host's warning hook (diff_preview wires it in production)."""
    seen = []
    monkeypatch.setattr(diff_ui, "on_warning", seen.append)
    return seen


# ── the wheel-less encode fallback ──────────────────────────────────────────

def test_without_the_zstd_wheel_the_artifact_is_plain_json_under_a_json_name(
        tmp_path, monkeypatch):
    """Name and encoding must agree. A `.json.zst` holding raw JSON (or a
    `.json` holding a frame) is unreadable by every reader in the fleet."""
    monkeypatch.setattr(diff_ui, "_zstd_available", lambda: False)
    path = diff_ui.save_artifact(str(tmp_path), REPO, PR, SHA, BODY)

    assert path.endswith(".json") and not path.endswith(".zst"), (
        f"no wheel means no compressed name, got {path!r}")
    with open(path, "rb") as f:
        raw = f.read()
    assert not raw.startswith(diff_ui._ZSTD_MAGIC), (
        "the payload was compressed after the wheel was declared missing")
    assert json.loads(raw.decode("utf-8"))["body"] == BODY


def test_the_wheel_less_artifact_is_still_loadable(tmp_path, monkeypatch):
    """The fallback is only useful if the read path accepts what it wrote."""
    monkeypatch.setattr(diff_ui, "_zstd_available", lambda: False)
    diff_ui.save_artifact(str(tmp_path), REPO, PR, SHA, BODY)
    art = diff_ui.load_artifact(str(tmp_path), REPO, PR, SHA)
    assert art is not None and art["body"] == BODY


# ── the read-path name helper ───────────────────────────────────────────────

def test_read_paths_are_the_names_save_actually_writes(tmp_path, monkeypatch):
    """Pinned against real writes AND against a real read, never against a
    restated format string.

    The helper and load_artifact's inline copy of the same rule are the
    pair that drifts, and a drift means a permanent 404 for every permalink
    written by the other spelling. Nothing calls the helper in production,
    so comparing it only to itself would prove nothing: each half here
    writes through save_artifact and then reads back through load_artifact,
    which is the only way the inline spelling gets a vote.
    """
    zst_path, json_path = diff_ui._artifact_paths_for_read(
        str(tmp_path), REPO, PR, SHA)

    written_zst = diff_ui.save_artifact(str(tmp_path), REPO, PR, SHA, BODY)
    assert written_zst == zst_path, (
        f"save wrote {written_zst!r}, the read path looks for {zst_path!r}")
    art = diff_ui.load_artifact(str(tmp_path), REPO, PR, SHA)
    assert art is not None and art["body"] == BODY, (
        "load_artifact's inline names no longer include the compressed name "
        f"save_artifact just wrote at {written_zst!r}")

    monkeypatch.setattr(diff_ui, "_zstd_available", lambda: False)
    written_json = diff_ui.save_artifact(str(tmp_path), REPO, PR, SHA, BODY)
    assert written_json == json_path, (
        f"save wrote {written_json!r}, the read path looks for {json_path!r}")
    art = diff_ui.load_artifact(str(tmp_path), REPO, PR, SHA)
    assert art is not None and art["body"] == BODY, (
        "load_artifact's inline names no longer include the legacy name "
        f"save_artifact just wrote at {written_json!r}")


def test_the_compressed_name_is_tried_before_the_legacy_one(tmp_path):
    """Order is load order. Legacy first would serve a stale `.json` that a
    newer `.json.zst` write was supposed to supersede.

    The helper's tuple order is asserted, but the claim that matters is
    made against load_artifact with BOTH encodings on disk holding
    different bodies: load_artifact open-codes its own names tuple (the
    CodeQL barrier comment says why), so only a real read can tell which
    order production actually uses.
    """
    zst_path, json_path = diff_ui._artifact_paths_for_read(
        str(tmp_path), REPO, PR, SHA)
    assert zst_path.endswith(".json.zst")
    assert json_path.endswith(".json") and not json_path.endswith(".zst")

    diff_ui.save_artifact(str(tmp_path), REPO, PR, SHA, "the newer compressed write")
    with open(json_path, "wb") as f:
        f.write(_legacy_artifact(body="the superseded legacy write"))
    assert os.path.isfile(zst_path) and os.path.isfile(json_path), (
        "the test needs both encodings present to say anything about order")

    art = diff_ui.load_artifact(str(tmp_path), REPO, PR, SHA)
    assert art["body"] == "the newer compressed write", (
        "the legacy sibling won the read: load order is reversed and every "
        "PR with a leftover `.json` serves a stale diff")


def test_read_paths_refuse_a_repo_slug_that_is_not_a_repo_slug(tmp_path):
    """COPS-2580 layer one: the filename segments are rejected by regex
    before anything is joined.

    Matching the message is the whole point. `../etc` would also be caught
    by the joined-path normalize-then-check further down, so a bare
    `raises(ValueError)` stays green with the regex layer deleted outright
    and proves nothing about which layer fired.
    """
    with pytest.raises(ValueError, match="bad repo slug"):
        diff_ui._artifact_paths_for_read(str(tmp_path), "../etc", PR, SHA)
    with pytest.raises(ValueError, match="bad pr id"):
        diff_ui._artifact_paths_for_read(str(tmp_path), REPO, "7 8", SHA)
    with pytest.raises(ValueError, match="bad sha"):
        diff_ui._artifact_paths_for_read(str(tmp_path), REPO, PR, "AB12CD3")


def test_the_name_helpers_still_refuse_to_escape_with_a_loosened_validator(
        tmp_path, monkeypatch):
    """COPS-2580 layer two, on the write-side name helpers.

    The sibling test above pins the regex; this one pins the independent
    normalize-then-check, which is the layer that has to hold if the
    regexes are ever loosened. A loosened validator is the only way to
    reach it, exactly as test_the_filename_guard_holds_when_the_slug_
    validator_is_loosened does for load_artifact.
    """
    base = tmp_path / "artifacts"
    base.mkdir()
    monkeypatch.setattr(diff_ui, "_validate",
                        lambda repo, pr_id, sha: ("../outside", "7", SHA))

    with pytest.raises(ValueError, match="escapes base_dir"):
        diff_ui._artifact_path(str(base), REPO, 7, SHA)
    # And the compressed spelling must not be the one that slips through.
    with pytest.raises(ValueError, match="escapes base_dir"):
        diff_ui._artifact_zst_path(str(base), REPO, 7, SHA)


# ── the stat hook is optional, and allowed to be broken ─────────────────────

def test_a_stat_hook_that_throws_never_breaks_the_save(tmp_path, monkeypatch):
    """Observability must never break the thing it observes."""
    calls = []

    def _exploding_stat(key, n=1):
        calls.append(key)
        raise RuntimeError("the host's stats plumbing is down")

    bucket = _Bucket().install(monkeypatch)
    monkeypatch.setattr(diff_ui, "on_stat", _exploding_stat)

    path = diff_ui.save_artifact(str(tmp_path), REPO, PR, SHA, BODY,
                                 bucket="b")
    assert calls, "the hook must actually be called, not skipped"
    assert os.path.isfile(path), "the artifact must survive a broken hook"
    assert f"{REPO}__{PR}.json.zst" in bucket.uploaded
    assert diff_ui.load_artifact(str(tmp_path), REPO, PR, SHA)["body"] == BODY


# ── the pending-upload map is bounded, oldest first ─────────────────────────

def test_the_pending_map_drops_the_OLDEST_pr_when_it_is_full(
        tmp_path, monkeypatch):
    """A long bucket outage must not grow the map without limit, and the
    entry it sheds has to be the stalest one: the newest commits are the
    ones whose absence from the bucket splits the replicas right now."""
    monkeypatch.setattr(diff_ui, "_gcs_upload", lambda b, n, d: False)
    over = diff_ui._PENDING_UPLOAD_MAX + 1
    for i in range(1, over + 1):
        diff_ui.save_artifact(str(tmp_path), REPO, i, "%07x" % (0x1000000 + i),
                              BODY, bucket="b")

    assert diff_ui.pending_upload_count() == diff_ui._PENDING_UPLOAD_MAX, (
        f"pending held {diff_ui.pending_upload_count()} entries, cap is "
        f"{diff_ui._PENDING_UPLOAD_MAX}")

    attempted = []

    def _recording_upload(bucket, name, data):
        attempted.append(name)
        return True

    monkeypatch.setattr(diff_ui, "_gcs_upload", _recording_upload)
    healed = diff_ui.retry_pending_uploads()

    assert healed == diff_ui._PENDING_UPLOAD_MAX
    assert f"{REPO}__{over}.json.zst" in attempted, (
        "the newest pending upload was evicted instead of the oldest")
    assert f"{REPO}__1.json.zst" not in attempted, (
        "the oldest entry should have been shed at the cap")
    assert diff_ui.pending_upload_count() == 0


# ── the sibling cleanup is best-effort ──────────────────────────────────────

def test_a_legacy_sibling_that_will_not_unlink_never_fails_the_save(
        tmp_path, monkeypatch):
    """One live object per PR is the goal, not a precondition. A locked or
    vanishing `.json` sibling must cost a stale file, never the save that
    the whole diff run is waiting on."""
    legacy = os.path.join(str(tmp_path), f"{REPO}__{PR}.json")
    with open(legacy, "wb") as f:
        f.write(_legacy_artifact(body="the previous encoding"))

    real_remove = os.remove
    blocked = []

    def _stubborn_remove(path, *a, **kw):
        if os.path.abspath(path) == os.path.abspath(legacy):
            blocked.append(path)
            raise OSError(16, "Device or resource busy")
        return real_remove(path, *a, **kw)

    monkeypatch.setattr(diff_ui.os, "remove", _stubborn_remove)

    path = diff_ui.save_artifact(str(tmp_path), REPO, PR, SHA, BODY)

    assert blocked, "the test did not exercise the sibling cleanup at all"
    assert path.endswith(".json.zst") and os.path.isfile(path)
    art = diff_ui.load_artifact(str(tmp_path), REPO, PR, SHA)
    assert art["body"] == BODY, (
        "the compressed write must win the read even with the sibling left "
        "behind")


# ── the cleanup DELETE in the bucket ────────────────────────────────────────

def test_a_404_on_the_cleanup_delete_is_success_and_not_worth_a_warning(
        tmp_path, monkeypatch, warnings):
    """The object being absent IS the desired end state. Warning about it
    would put a line in the log on every PR that never had a legacy copy."""
    bucket = _Bucket(delete_exc=urllib.error.HTTPError(
        "u", 404, "Not Found", {}, None)).install(monkeypatch)

    diff_ui.save_artifact(str(tmp_path), REPO, PR, SHA, BODY, bucket="b")

    assert bucket.deleted == [f"{REPO}__{PR}.json"], (
        "the other encoding must be the object we tried to delete")
    assert warnings == [], f"a 404 delete should be silent, got {warnings}"
    # The return value is the contract the caller may one day read.
    assert diff_ui._gcs_delete("b", f"{REPO}__{PR}.json") is True


def test_a_5xx_on_the_cleanup_delete_is_reported_and_still_not_fatal(
        tmp_path, monkeypatch, warnings):
    """A stale legacy object left in the bucket can be preferred on a later
    miss, so this one is worth a human's attention -- but not a failed run."""
    _Bucket(delete_exc=urllib.error.HTTPError(
        "u", 503, "Service Unavailable", {}, None)).install(monkeypatch)

    path = diff_ui.save_artifact(str(tmp_path), REPO, PR, SHA, BODY,
                                 bucket="b")

    assert os.path.isfile(path), "a failed cleanup must not fail the save"
    assert len(warnings) == 1, f"expected exactly one warning, got {warnings}"
    assert f"{REPO}__{PR}.json" in warnings[0]
    assert "503" in warnings[0], (
        f"the status code is the actionable part: {warnings[0]!r}")
    assert diff_ui._gcs_delete("b", f"{REPO}__{PR}.json") is False


def test_a_transport_failure_on_the_cleanup_delete_is_reported_the_same_way(
        tmp_path, monkeypatch, warnings):
    """Not every failure arrives as an HTTPError: a timeout or a token blip
    reaches this code as something else entirely."""
    _Bucket(delete_exc=TimeoutError("read timed out")).install(monkeypatch)

    path = diff_ui.save_artifact(str(tmp_path), REPO, PR, SHA, BODY,
                                 bucket="b")

    assert os.path.isfile(path)
    assert len(warnings) == 1, f"expected exactly one warning, got {warnings}"
    assert f"{REPO}__{PR}.json" in warnings[0]
    assert "read timed out" in warnings[0], (
        f"the cause must survive into the log line: {warnings[0]!r}")
    assert diff_ui._gcs_delete("b", f"{REPO}__{PR}.json") is False


# ── corrupt payloads: fall through, but never swallow a surprise ────────────

def test_a_corrupt_local_zst_falls_through_to_the_legacy_sibling(tmp_path):
    """zstandard.ZstdError is NOT a ValueError subclass, so a truncated
    frame would escape the ordinary handler and 500 the page instead of
    serving the perfectly good `.json` next to it."""
    with open(os.path.join(str(tmp_path), f"{REPO}__{PR}.json.zst"), "wb") as f:
        f.write(diff_ui._ZSTD_MAGIC + b"\x00truncated-frame")
    with open(os.path.join(str(tmp_path), f"{REPO}__{PR}.json"), "wb") as f:
        f.write(_legacy_artifact(body="served from the legacy sibling"))

    art = diff_ui.load_artifact(str(tmp_path), REPO, PR, SHA)

    assert art is not None, "a corrupt preferred file must not hide the good one"
    assert art["body"] == "served from the legacy sibling"


def test_an_unexpected_local_decode_failure_is_not_swallowed_into_a_404(
        tmp_path, monkeypatch):
    """The fall-through is scoped to decode failures we understand. A
    MemoryError from a decompression bomb is not a cache miss, and turning
    it into `None` would render an empty page and lose the incident."""
    diff_ui.save_artifact(str(tmp_path), REPO, PR, SHA, BODY)

    def _explode(data):
        raise MemoryError("decompressed payload does not fit")

    monkeypatch.setattr(diff_ui, "_decode_artifact_bytes", _explode)

    with pytest.raises(MemoryError):
        diff_ui.load_artifact(str(tmp_path), REPO, PR, SHA)


def test_a_corrupt_object_in_the_bucket_falls_through_to_the_legacy_object(
        tmp_path, monkeypatch):
    """Same fall-through on the durable tier: a fresh replica whose only
    copy is a truncated `.json.zst` in GCS must still find the `.json`."""
    objects = {
        f"{REPO}__{PR}.json.zst": diff_ui._ZSTD_MAGIC + b"\x00truncated",
        f"{REPO}__{PR}.json": _legacy_artifact(body="served from the bucket"),
    }
    monkeypatch.setattr(diff_ui, "_gcs_download",
                        lambda bucket, name: objects.get(name))

    art = diff_ui.load_artifact(str(tmp_path), REPO, PR, SHA, bucket="b")

    assert art is not None and art["body"] == "served from the bucket"
    warmed = os.path.join(str(tmp_path), f"{REPO}__{PR}.json")
    assert os.path.isfile(warmed), "the usable object should warm the cache"


def test_an_unexpected_bucket_decode_failure_is_not_swallowed_either(
        tmp_path, monkeypatch):
    """The bucket branch keeps the same contract as the local one: only the
    understood failures become a miss."""
    monkeypatch.setattr(
        diff_ui, "_gcs_download",
        lambda bucket, name: _legacy_artifact() if name.endswith(".zst")
        else None)

    def _explode(data):
        raise MemoryError("decompressed payload does not fit")

    monkeypatch.setattr(diff_ui, "_decode_artifact_bytes", _explode)

    with pytest.raises(MemoryError):
        diff_ui.load_artifact(str(tmp_path), REPO, PR, SHA, bucket="b")


# ── the second layer of the path guard ──────────────────────────────────────

def test_the_filename_guard_holds_when_the_slug_validator_is_loosened(
        tmp_path, monkeypatch):
    """COPS-2580 built this as two independent layers on purpose: the
    regexes, and a normalize-then-check on the joined path. Simulating a
    loosened validator is the only way to show the second layer is load
    bearing rather than decorative -- without it, a repo slug carrying a
    separator would read a file outside the artifact directory."""
    base = tmp_path / "artifacts"
    base.mkdir()
    outside = tmp_path / "outside__7.json"
    outside.write_bytes(_legacy_artifact(body="SECRETS FROM OUTSIDE THE DIR"))

    monkeypatch.setattr(diff_ui, "_validate",
                        lambda repo, pr_id, sha: ("../outside", "7", SHA))

    with pytest.raises(ValueError) as excinfo:
        diff_ui.load_artifact(str(base), REPO, 7, SHA)
    assert "escapes base_dir" in str(excinfo.value)


# ── page anchors ────────────────────────────────────────────────────────────

APP_LINE = "⚠️ **`%s`** — %d resource(s) changed"


def test_two_application_names_that_sanitise_alike_get_distinct_anchors():
    """Anchor ids are sanitised to [a-z0-9-], so two different application
    names can collapse onto the same id. Without the numeric suffix every
    deep link to the second one would land on the first."""
    body = "\n".join([APP_LINE % ("pv-alpha", 1), APP_LINE % ("PV.ALPHA", 2)])
    outline = diff_ui.build_outline(body)

    ids = [app["id"] for app in outline]
    assert ids == ["app-pv-alpha", "app-pv-alpha-2"], ids
    assert outline[1]["name"] == "PV.ALPHA", "the label must stay verbatim"


def test_colliding_anchors_stay_consistent_between_the_index_and_the_body():
    """The suffix is only worth anything if the index links to the id the
    body actually renders."""
    body = "\n".join([APP_LINE % ("pv-alpha", 1), APP_LINE % ("PV.ALPHA", 2)])
    page = diff_ui.render_html({"repo": REPO, "pr_id": PR, "sha": SHA,
                                "body": body})
    assert 'id="app-pv-alpha-2"' in page, "the second app has no anchor row"
    assert 'href="#app-pv-alpha-2"' in page, "the index does not link to it"


def test_an_anchor_survives_a_markdown_table_collapsing_many_lines_to_one():
    """Rule 4 (one row per source line) is broken exactly once, for pipe
    tables. A pending anchor whose name appears inside the collapsed block
    has to land on the single row that replaced those lines, or the deep
    link 404s silently.

    The outline sees the resource header inside the ```diff fence (it does
    not track fences); the renderer does not consume anchors inside one, so
    the match is still pending when the table arrives.
    """
    body = "\n".join([
        APP_LINE % ("pv-alpha-a-ms", 1),
        "",
        "```diff",
        "**`/apps/Deployment ghost`**",
        "```",
        "",
        "| resource | note |",
        "| --- | --- |",
        "| `/apps/Deployment ghost` | moved |",
    ])
    outline = diff_ui.build_outline(body)
    res_id = outline[0]["resources"][0]["id"]

    page = diff_ui.render_html({"repo": REPO, "pr_id": PR, "sha": SHA,
                                "body": body})

    assert f'<tr class="row mdt" id="{res_id}">' in page, (
        f"the collapsed table row carries no anchor for {res_id!r}")
    assert f'href="#{res_id}"' in page, "the index links at a missing anchor"
    assert page.count(f'id="{res_id}"') == 1, "the anchor must be unique"


# ── the NUL guard in the inline renderer ────────────────────────────────────

def test_a_line_containing_NUL_renders_literally_instead_of_half_decorated():
    """NUL is the placeholder delimiter for code spans and links. A body
    that already contains one could make a restore land in the wrong place,
    so such a line is returned exactly as escaped -- rule 3: an unrenderable
    line is a cosmetic miss, a corrupted one is lost information."""
    body = "clean `iscode` **isbold**\n\x00 dirty `notcode` **notbold**"
    page = diff_ui.render_html({"repo": REPO, "pr_id": PR, "sha": SHA,
                                "body": body})

    assert "<code>iscode</code>" in page and "<strong>isbold</strong>" in page, (
        "the ordinary line must still be decorated")
    assert "<code>notcode</code>" not in page, (
        "inline markup was applied to a line holding the placeholder delimiter")
    assert "<strong>notbold</strong>" not in page
    assert "`notcode` **notbold**" in page, (
        "the NUL line must survive verbatim, not be dropped or mangled")
