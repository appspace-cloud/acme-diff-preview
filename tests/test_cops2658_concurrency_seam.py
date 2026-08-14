"""COPS-2658: the shared sub-task pool becomes a state family of its own.

`_subtask_pool` is a lazily-created ThreadPoolExecutor reached through one
accessor. It is process-wide state, and while it lived in the hub it was in
the closure of every cluster that submits background work -- the main render
cache among them, which is what made that cluster look entangled with
process lifecycle when it is not.

The rule for a mutable state family is that it moves atomically or not at
all: the global, its size and its only accessor travel together, so there is
never a second source of truth for "which pool is this".

The size is derived from DIFF_WORKERS, which stays in the hub because it is
a documented capacity knob with its own tests. This module reads the same
env var rather than importing the hub's value; the resulting duplicated
default is pinned below so it cannot drift silently.
"""
import ast
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import concurrency  # noqa: E402
import diff_preview  # noqa: E402

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
NAMES = ("_subtask_pool", "_get_subtask_pool", "_SUBTASK_POOL_WORKERS")


def test_the_pool_is_created_lazily_and_then_reused(monkeypatch):
    """One pool per process, created on first use.

    A fresh executor per call would spawn hundreds of threads per PR, which
    is the whole reason this indirection exists.
    """
    monkeypatch.setattr(concurrency, "_subtask_pool", None)
    first = concurrency._get_subtask_pool()
    try:
        assert first is not None
        assert concurrency._get_subtask_pool() is first, "pool was recreated"
        assert first.submit(lambda: 7).result(timeout=10) == 7
    finally:
        first.shutdown(wait=False)


def test_the_pool_size_cannot_drift_from_DIFF_WORKERS():
    """The one duplicated default in this split, pinned so it cannot drift.

    DIFF_WORKERS stays in the hub: it is a documented capacity knob whose
    exact source line and env fallback are both pinned by other tests. This
    module therefore reads the same env var instead of importing the hub's
    value, which is the only way to honour that and the no-back-import rule
    at once. The cost is one duplicated default, and this is the assertion
    that makes the duplication safe.
    """
    assert concurrency._SUBTASK_POOL_WORKERS == max(8, diff_preview.DIFF_WORKERS * 2)


@pytest.mark.parametrize("name", NAMES)
def test_the_hub_keeps_no_binding_for_the_pool_family(name):
    """A leftover binding would be a second, unpatched way in.

    The suite patches `_subtask_pool` and `_get_subtask_pool`; a hub-level
    alias would let callers resolve past whatever the test replaced.
    """
    assert not hasattr(diff_preview, name), (
        f"diff_preview still carries `{name}`; a caller resolving through it "
        "would escape a patch applied to concurrency"
    )


@pytest.mark.parametrize("name", NAMES)
def test_no_module_reads_the_pool_family_as_a_bare_global(name):
    """Every reader goes through the module object the suite patches."""
    offenders = {}
    for fn in sorted(os.listdir(SRC)):
        if not fn.endswith(".py") or fn == "concurrency.py":
            continue
        tree = ast.parse(open(os.path.join(SRC, fn), encoding="utf-8").read())
        bare = [n.lineno for n in ast.walk(tree)
                if isinstance(n, ast.Name) and n.id == name
                and isinstance(n.ctx, ast.Load)]
        if bare:
            offenders[fn] = bare
    assert not offenders, f"bare `{name}` reads outside concurrency.py: {offenders}"


def test_the_family_lives_in_exactly_one_module():
    """Split state and accessor and you get two answers to one question."""
    tree = ast.parse(open(os.path.join(SRC, "concurrency.py"), encoding="utf-8").read())
    defined = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            defined.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined.add(node.target.id)
    assert set(NAMES) <= defined, f"missing from concurrency.py: {set(NAMES) - defined}"
