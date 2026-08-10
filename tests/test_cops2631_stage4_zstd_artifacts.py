"""COPS-2631 stage 4: zstd compression for full-diff artifacts.

Artifacts are multi-app comment bodies with near-duplicate diff blocks.
Measured: zstd -3 is ~4x smaller than gzip -6 and ~20x faster to compress
on that shape. New writes use zstd level 3; the read path accepts both
raw `.json` (legacy) and `.json.zst` so existing GCS/local entries keep
serving during the transition.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_ui  # noqa: E402


BODY = "## Diff\n" + ("===== /Deployment ns/app ======\n- a\n+ b\n" * 200)


def test_save_writes_zstd_when_available(tmp_path):
    import zstandard  # noqa: F401
    path = diff_ui.save_artifact(str(tmp_path), "acme-config-dev", 42,
                                 "ab12cd3", BODY)
    assert path.endswith(".json.zst")
    assert os.path.isfile(path)
    assert not os.path.isfile(path[:-4])  # no sibling .json


def test_load_roundtrips_zstd_artifact(tmp_path):
    import zstandard  # noqa: F401
    diff_ui.save_artifact(str(tmp_path), "acme-config-dev", 42, "ab12cd3",
                          BODY)
    art = diff_ui.load_artifact(str(tmp_path), "acme-config-dev", 42,
                                "ab12cd3")
    assert art is not None
    assert art["body"] == BODY
    assert art["sha"] == "ab12cd3"


def test_load_still_reads_legacy_raw_json(tmp_path):
    """Transition: artifacts written before stage 4 must keep working."""
    legacy = {
        "repo": "acme-config-dev", "pr_id": 42, "sha": "ab12cd3",
        "pr_url": "", "base_sha": "", "outcome_counts": {},
        "app_count": None, "created_utc": "2026-01-01 00:00:00 UTC",
        "body": BODY,
    }
    path = os.path.join(str(tmp_path), "acme-config-dev__42.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(legacy, f)
    art = diff_ui.load_artifact(str(tmp_path), "acme-config-dev", 42,
                                "ab12cd3")
    assert art["body"] == BODY


def test_save_replaces_legacy_json_with_zst(tmp_path):
    import zstandard  # noqa: F401
    legacy = os.path.join(str(tmp_path), "acme-config-dev__42.json")
    with open(legacy, "w", encoding="utf-8") as f:
        f.write('{"body":"old"}')
    path = diff_ui.save_artifact(str(tmp_path), "acme-config-dev", 42,
                                 "ab12cd3", BODY)
    assert path.endswith(".json.zst")
    assert not os.path.exists(legacy)
    art = diff_ui.load_artifact(str(tmp_path), "acme-config-dev", 42,
                                "ab12cd3")
    assert art["body"] == BODY


def test_zstd_payload_is_smaller_than_raw_json(tmp_path):
    import zstandard  # noqa: F401
    # Near-duplicate blocks: the shape that made zstd win 4x over gzip.
    fat = "## Diff\n" + ("===== /Deployment ns/app ======\n- a\n+ b\n" * 5000)
    path = diff_ui.save_artifact(str(tmp_path), "repo-x", 1, "abcdef1", fat)
    raw_size = len(json.dumps({
        "repo": "repo-x", "pr_id": 1, "sha": "abcdef1", "body": fat,
    }, ensure_ascii=False).encode("utf-8"))
    assert os.path.getsize(path) < raw_size * 0.5


def test_prune_counts_both_json_and_zst(tmp_path):
    import zstandard  # noqa: F401
    for i in range(5):
        diff_ui.save_artifact(str(tmp_path), "repo-x", i + 1, "abcdef1",
                              BODY, max_artifacts=3)
    names = os.listdir(str(tmp_path))
    assert len([n for n in names if n.endswith((".json", ".json.zst"))]) <= 3


def test_decode_zstd_without_wheel_raises_valueerror(monkeypatch):
    """Wheel-less envs must fall through to legacy .json, not abort."""
    monkeypatch.setattr(diff_ui, "_zstd_available", lambda: False)
    import zstandard as zstd
    payload = zstd.ZstdCompressor(level=3).compress(b'{"body":"x"}')
    # Force import failure inside decode even though we have the wheel for
    # building the fixture: patch the import path via a fake module miss.
    import builtins
    real_import = builtins.__import__

    def _block_zstd(name, *a, **kw):
        if name == "zstandard":
            raise ImportError("blocked for test")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _block_zstd)
    try:
        diff_ui._decode_artifact_bytes(payload)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "zstandard" in str(e).lower() or "zstd" in str(e).lower()


def test_load_falls_through_to_legacy_json_when_zst_undecodable(tmp_path,
                                                                monkeypatch):
    import zstandard as zstd
    legacy = {
        "repo": "acme-config-dev", "pr_id": 42, "sha": "ab12cd3",
        "pr_url": "", "base_sha": "", "outcome_counts": {},
        "app_count": None, "created_utc": "2026-01-01 00:00:00 UTC",
        "body": BODY,
    }
    zst = os.path.join(str(tmp_path), "acme-config-dev__42.json.zst")
    with open(zst, "wb") as f:
        f.write(zstd.ZstdCompressor(level=3).compress(b"{not-json"))
    path = os.path.join(str(tmp_path), "acme-config-dev__42.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(legacy, f)
    art = diff_ui.load_artifact(str(tmp_path), "acme-config-dev", 42,
                                "ab12cd3")
    assert art["body"] == BODY
