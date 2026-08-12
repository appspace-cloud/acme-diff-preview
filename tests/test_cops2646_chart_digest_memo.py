"""COPS-2646: stop re-hashing an immutable chart on every cache lookup.

`_main_render_content_key` calls `_hash_chart_tree` on EVERY lookup, and
that function walks the whole chart tree and reads every file. The chart
does not change once pulled -- that is why `_helm_chart_cache` and the
per-chart pull locks exist -- so the walk is pure waste, and it is waste
on the GIL-bound side of the workload.

Measured on a tree the size of appspace-micro-services (600 template
files, ~8KB each, page cache warm): ~22ms per call, twice per app, which
is ~15s of pure re-hashing on a 345-app fleet bump. For scale, COPS-2631
measured the entire CyDifflib win at ~3.7s.

The one real risk is a STALE digest after a dev registry republishes a
chart under the same tag: that would key a fresh render to an old entry,
which is a wrong diff -- the worst failure this service has. A re-pull
parks the old directory aside and lands a fresh one at the same path, so
the directory identity changes even when the path does not, and the memo
has to notice that. test_a_republished_chart_at_the_same_path_rehashes
is the gate.
"""
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_preview as m  # noqa: E402


def _chart(root, marker="v1"):
    """A small chart tree. `marker` changes the CONTENT, not the shape."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "Chart.yaml").write_text(f"name: demo\nversion: 1.0.0\n# {marker}\n")
    tpl = root / "templates"
    tpl.mkdir(exist_ok=True)
    for i in range(5):
        (tpl / f"t{i}.yaml").write_text(f"kind: ConfigMap\ndata:\n  k: {marker}-{i}\n")
    return str(root)


class _ReadCounter:
    """Counts chart file BODY reads.

    Deliberately not a walk counter. The memo still stats the tree on every
    lookup -- that is how it notices a file edited in place, which changes
    neither the directory inode nor its mtime -- so counting walks would
    assert an implementation choice instead of the property that matters.
    The expensive half is reading and hashing several MB of chart bodies,
    and that is what must happen once.
    """

    def __init__(self, monkeypatch, chart_path):
        self.count = 0
        self.root = chart_path
        real_open = m.open if hasattr(m, "open") else open
        import builtins
        real_open = builtins.open
        counter = self

        def counting_open(file, *a, **kw):
            if isinstance(file, str) and str(file).startswith(counter.root) \
                    and "b" in (a[0] if a else kw.get("mode", "r")):
                counter.count += 1
            return real_open(file, *a, **kw)

        monkeypatch.setattr(builtins, "open", counting_open)


def _clear_memo():
    for name in ("_chart_tree_digest_memo", "_hash_chart_tree_memo"):
        memo = getattr(m, name, None)
        if memo is not None:
            memo.clear()


def test_an_unchanged_chart_is_hashed_once(tmp_path, monkeypatch):
    """The whole point: N lookups, one walk."""
    path = _chart(tmp_path / "chart")
    _clear_memo()
    counter = _ReadCounter(monkeypatch, path)
    digests = {m._hash_chart_tree(path) for _ in range(25)}
    assert len(digests) == 1, "the digest must be stable"
    # 6 files in the fixture chart: one full read of the tree, then nothing.
    assert counter.count == 6, (
        f"expected the tree to be read once across 25 lookups, got "
        f"{counter.count} file reads")


def test_the_content_key_stops_walking_per_lookup(tmp_path, monkeypatch):
    """The caller that actually matters: _main_render_content_key runs
    twice per app, so on a fleet bump this is the dominant cost."""
    path = _chart(tmp_path / "chart")
    vals = {"cfg/a.yaml": "x: 1\n"}
    _clear_memo()
    counter = _ReadCounter(monkeypatch, path)
    keys = {m._main_render_content_key(path, "rel", "ns", vals) for _ in range(20)}
    assert len(keys) == 1
    assert counter.count == 6, (
        f"expected the tree to be read once across 20 content-key builds, "
        f"got {counter.count} file reads")


def test_a_republished_chart_at_the_same_path_rehashes(tmp_path, monkeypatch):
    """THE correctness gate.

    A dev registry can republish a chart under the same tag. The re-pull
    parks the stale directory aside and lands a fresh one at the SAME
    path, so a memo keyed on the path alone would serve the previous
    tree's digest -- keying a fresh render to an old cache entry, which
    is a wrong diff.
    """
    chart_dir = tmp_path / "registry" / "demo" / "1.0.0"
    path = _chart(chart_dir, marker="v1")
    _clear_memo()
    before = m._hash_chart_tree(path)

    # Exactly what _ensure_chart does on a stale dev chart: park the old
    # tree aside, land a freshly pulled one at the same path.
    parked = tmp_path / "registry" / "demo" / "1.0.0.stale-1"
    os.rename(chart_dir, parked)
    _chart(chart_dir, marker="v2")

    after = m._hash_chart_tree(path)
    assert after != before, (
        "a republished chart at the same path must produce a new digest")


def test_a_republished_chart_changes_the_content_key(tmp_path, monkeypatch):
    """Same scenario one level up: the cache key itself must move."""
    chart_dir = tmp_path / "registry" / "demo" / "1.0.0"
    path = _chart(chart_dir, marker="v1")
    vals = {"cfg/a.yaml": "x: 1\n"}
    _clear_memo()
    before = m._main_render_content_key(path, "rel", "ns", vals)

    parked = tmp_path / "registry" / "demo" / "1.0.0.stale-1"
    os.rename(chart_dir, parked)
    _chart(chart_dir, marker="v2")

    after = m._main_render_content_key(path, "rel", "ns", vals)
    assert after != before, "a republished chart must not reuse the cache key"


def test_two_charts_at_different_paths_do_not_share_a_digest(tmp_path):
    a = _chart(tmp_path / "a", marker="aaa")
    b = _chart(tmp_path / "b", marker="bbb")
    _clear_memo()
    assert m._hash_chart_tree(a) != m._hash_chart_tree(b)


def test_identical_content_at_different_paths_digests_the_same(tmp_path):
    """The digest is of CONTENT. Two identical trees must agree, otherwise
    the cache would miss across equivalent charts."""
    a = _chart(tmp_path / "a", marker="same")
    b = _chart(tmp_path / "b", marker="same")
    _clear_memo()
    assert m._hash_chart_tree(a) == m._hash_chart_tree(b)


def test_a_missing_chart_stays_a_miss_and_is_not_memoized(tmp_path):
    """A missing tree hashes to a sentinel (COPS-2631). Memoizing that
    would pin the sentinel for a path that is about to be populated by an
    in-flight pull."""
    path = str(tmp_path / "not-yet-pulled")
    _clear_memo()
    sentinel = m._hash_chart_tree(path)
    real = _chart(tmp_path / "not-yet-pulled", marker="arrived")
    assert m._hash_chart_tree(real) != sentinel, (
        "a chart that appears after a miss must be hashed, not served from "
        "a memoized sentinel")


def test_concurrent_lookups_agree(tmp_path):
    """DIFF_WORKERS is 16 in production. A duplicate computation under a
    race is harmless; a torn or inconsistent memo entry is a wrong diff."""
    path = _chart(tmp_path / "chart")
    _clear_memo()
    seen = []
    errors = []

    def worker():
        try:
            for _ in range(30):
                seen.append(m._hash_chart_tree(path))
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors[:3]
    assert len(set(seen)) == 1, "threads disagreed on the digest"


def test_the_memo_is_bounded(tmp_path):
    """One entry per chart+version in flight is small, but an unbounded
    dict keyed on paths is how slow leaks start."""
    _clear_memo()
    for i in range(400):
        m._hash_chart_tree(_chart(tmp_path / f"c{i}", marker=str(i)))
    memo = getattr(m, "_chart_tree_digest_memo", None)
    assert memo is not None, "the memo must be introspectable to be bounded"
    assert len(memo) <= 256, f"memo grew to {len(memo)} entries"


def test_content_key_inputs_are_unchanged(tmp_path):
    """COPS-2646 must not change what goes into the key, only how often
    one of the inputs is computed."""
    path = _chart(tmp_path / "chart")
    vals = {"cfg/a.yaml": "x: 1\n"}
    k1 = m._main_render_content_key(path, "rel", "ns", vals)
    k2 = m._main_render_content_key(path, "rel", "ns", vals)
    assert k1 == k2 and len(k1) == 64
    assert m._main_render_content_key(path, "rel", "OTHER", vals) != k1
    assert m._main_render_content_key(path, "rel", "ns", {"cfg/a.yaml": "x: 2\n"}) != k1
