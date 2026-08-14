"""COPS-2658: the diff counters become a module, shared by object not by name.

`_diff_stats` is the service's metrics dict, read in 19 hub defs and reached
by the suite at 74 places. It is also what kept the main render cache in the
hub: the cache writes hit/miss counters into it, so the cache could not leave
while the dict stayed behind.

The distinction that makes this cheap is worth stating, because it decides
the shape of every move like it:

  * A name that gets REBOUND -- `log`, `_subtask_pool` -- must be reached
    through the module object, because a patch replaces the binding and only
    readers resolving through that namespace see the replacement.
  * A CONTAINER that is only ever mutated -- this dict, its lock -- can be
    re-exported. Both modules then hold a reference to the same object, and
    a mutation through either is visible through both.

`_diff_stats` is never reassigned (nothing declares `global _diff_stats`),
so the second case applies and the hub's existing reads keep working
untouched. Should anyone ever patch it, the name becomes a patched name and
scripts/audit_seams.py starts failing on the split -- which is the point.
"""
import ast
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_preview  # noqa: E402
import stats  # noqa: E402

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")


def test_the_hub_and_stats_share_one_counter_dict():
    """Two references, one object. Two objects would be two truths."""
    assert diff_preview._diff_stats is stats._diff_stats
    assert diff_preview._diff_stats_lock is stats._diff_stats_lock


def test_a_mutation_through_either_reference_is_visible_through_the_other():
    """The property the re-export depends on, asserted rather than assumed."""
    key = "_cops2658_probe"
    try:
        stats._diff_stats[key] = 41
        assert diff_preview._diff_stats[key] == 41
        diff_preview._diff_stats[key] += 1
        assert stats._diff_stats[key] == 42
    finally:
        stats._diff_stats.pop(key, None)


def test_the_counters_are_defined_only_in_stats():
    """A second definition would silently split the metrics in half."""
    homes = []
    for fn in sorted(os.listdir(SRC)):
        if not fn.endswith(".py"):
            continue
        tree = ast.parse(open(os.path.join(SRC, fn), encoding="utf-8").read())
        for node in tree.body:
            target = None
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                target = node.target.id
            elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name):
                target = node.targets[0].id
            if target == "_diff_stats":
                homes.append(fn)
    assert homes == ["stats.py"], homes


def test_nothing_rebinds_the_counter_dict():
    """The re-export is only safe while the dict is mutated, never replaced."""
    offenders = {}
    for fn in sorted(os.listdir(SRC)):
        if not fn.endswith(".py") or fn == "stats.py":
            continue
        tree = ast.parse(open(os.path.join(SRC, fn), encoding="utf-8").read())
        rebinds = [n.lineno for n in ast.walk(tree)
                   if isinstance(n, ast.Global) and "_diff_stats" in n.names]
        if rebinds:
            offenders[fn] = rebinds
    assert not offenders, (
        f"`global _diff_stats` found in {offenders}; rebinding it would give "
        "the hub and stats.py two different dicts"
    )
