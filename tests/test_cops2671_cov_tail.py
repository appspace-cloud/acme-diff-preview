"""The tail of the COPS-2671 coverage push, and what chasing it turned up.

The per-module fan-out closed 207 of 214 uncovered lines. Running the last
seven to ground was worth more than the seven lines: two of the three sites
turned out to be unreachable for STRUCTURAL reasons, which no amount of test
writing would have fixed and which a `pragma` added without looking would
have buried.

  * diff_ui.py:215 -- the no-callback branch of `_stat`. Genuinely reachable
    and genuinely load-bearing: observability must never break the thing it
    observes. Tested below.

  * diff_ui.py:601 -- the path-traversal guard at the GCS write site. It can
    never fire, because an IDENTICAL guard forty lines earlier (the local
    read loop, 560-563) runs over the same `names` tuple and raises first.
    CodeQL requires the check at each write site and the inline form is
    deliberate, so the right outcome is to keep the line and exclude it,
    not to contort a test around it. What IS tested here is the contract
    that actually protects the filesystem: weaken `_validate`, the way a
    real regression would, and a `../` name must still be refused with
    nothing written anywhere.

  * diff_preview.py:8775-8789 -- the capped deep-link roster. Provably dead,
    not merely untested. The branch needs a shape group AND no changeset
    table, but a shape group is built only from OUT_DIFF results and any
    OUT_DIFF result renders the table; on the complete-record page, where
    its own comment says the bullets should survive, `shape_group_for_app`
    is set to {} outright. Both arms of its condition therefore contradict.
    Left in place with an exclusion rather than deleted, because it may be
    intended behaviour that COPS-2640 lost rather than redundant code --
    that is a product call, recorded on the ticket.
"""
import json
import os
import sys

import pytest

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import diff_ui  # noqa: E402


# ── diff_ui._stat: the no-callback branch ────────────────────────────────

def test_stat_is_a_silent_no_op_when_no_host_installed_a_callback():
    """Observability must never break the thing it observes.

    Both legs are asserted in one test on purpose: a test that only proved
    "does not raise" would still pass if the None-check were deleted and the
    callback path happened to be a no-op too. The recording leg shows the
    seam really is wired, so the silence on the first leg is the guard doing
    its job rather than the whole function being dead.
    """
    seen = []
    old = diff_ui.on_stat
    try:
        diff_ui.on_stat = None
        diff_ui._stat("renders", 3)          # must evaporate
        assert seen == []

        diff_ui.on_stat = lambda key, n: seen.append((key, n))
        diff_ui._stat("renders", 3)          # must land
        assert seen == [("renders", 3)]
    finally:
        diff_ui.on_stat = old


def test_stat_swallows_a_failing_callback():
    """A broken metrics sink must not take the request down with it."""
    old = diff_ui.on_stat
    try:
        def _boom(key, n):
            raise RuntimeError("statsd is down")
        diff_ui.on_stat = _boom
        diff_ui._stat("renders")             # must not propagate
    finally:
        diff_ui.on_stat = old


# ── diff_ui.load_artifact: the path-traversal second line of defence ─────

_ART = {"sha": "b" * 40, "apps": {}}


def _artifact_bytes():
    return json.dumps(_ART).encode()


def test_a_traversing_object_name_writes_nothing_outside_the_cache_dir(
        tmp_path, monkeypatch):
    """If `_validate` ever stops rejecting separators, this guard is all
    that stands between a crafted GCS object name and a write anywhere on
    the filesystem. Weaken the first check and the second must still hold.

    Not a hypothetical shape: the names are built from the repo segment, so
    a validator regression is precisely how a `../` would arrive here.
    """
    base = tmp_path / "cache"
    base.mkdir()
    escape_target = tmp_path / "evil__9.json"

    # Weaken the FIRST line of defence, exactly as a future regression would.
    monkeypatch.setattr(diff_ui, "_validate",
                        lambda repo, pr_id, sha: ("../evil", "9", "b" * 40))
    monkeypatch.setattr(diff_ui, "_gcs_download",
                        lambda bucket, name: _artifact_bytes())

    with pytest.raises(ValueError, match="escapes base_dir"):
        diff_ui.load_artifact(str(base), "../evil", 9, "b" * 40,
                              bucket="some-bucket")

    assert not escape_target.exists(), (
        "the traversal guard did not hold: load_artifact wrote %s, outside "
        "its cache directory" % escape_target)
    assert list(base.iterdir()) == [], (
        "nothing should have been cached either: %s" % list(base.iterdir()))


def test_a_well_formed_name_is_still_cached(tmp_path, monkeypatch):
    """The control. Without it the assertions above would also pass if
    load_artifact had simply stopped writing anything at all."""
    base = tmp_path / "cache"
    base.mkdir()
    monkeypatch.setattr(diff_ui, "_validate",
                        lambda repo, pr_id, sha: ("goodrepo", "9", "b" * 40))
    monkeypatch.setattr(diff_ui, "_gcs_download",
                        lambda bucket, name: _artifact_bytes())

    got = diff_ui.load_artifact(str(base), "goodrepo", 9, "b" * 40,
                                bucket="some-bucket")

    assert got == _ART
    written = sorted(p.name for p in base.iterdir())
    assert written, "the happy path must warm the local cache"
    assert all(n.startswith("goodrepo__9.json") for n in written), written
