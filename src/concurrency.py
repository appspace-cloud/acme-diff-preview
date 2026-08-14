"""How much this service does at once, and the pool that does it.

The sub-task pool is process-wide mutable state reached through a single
accessor. While it lived in the hub it sat in the closure of every cluster
that submits background work, which made those clusters look entangled with
process lifecycle when the only thing they actually shared was this pool.

State families move atomically or not at all: the global, its size and its
one accessor travel together, so "which pool is this" has exactly one
answer. The size is derived from DIFF_WORKERS, which deliberately stays in the hub:
it is a documented capacity knob, and a test pins both its exact source line
and its fallback when the env var is a typo. Reading the same env var here
rather than importing the hub's value is the only way to keep that contract
and the no-back-import rule at once -- and the duplicated default is pinned
by a test that compares the two, so it cannot drift silently.
"""
from concurrent.futures import ThreadPoolExecutor

from envcfg import _env_int  # environment configuration readers (stdlib only)




# ── Shared thread pool for sub-tasks inside _run_one_diff (#6) ───────────────
# Creating/destroying a ThreadPoolExecutor per diff call (3× per call) causes
# hundreds of thread spawns per PR. A module-level pool is cheaper: workers are
# reused and the pool lives for the pod lifetime.
# Size: enough for concurrent (pull PR + pull main + fetch PR vf + fetch main vf
# + render PR + render main) across DIFF_WORKERS parallel diffs.
_SUBTASK_POOL_WORKERS = max(8, _env_int("DIFF_WORKERS", 16) * 2)  # default 32
_subtask_pool: ThreadPoolExecutor = None           # created lazily in main()

def _get_subtask_pool() -> ThreadPoolExecutor:
    """Return (or create) the module-level sub-task pool."""
    global _subtask_pool
    if _subtask_pool is None:
        _subtask_pool = ThreadPoolExecutor(
            max_workers=_SUBTASK_POOL_WORKERS,
            thread_name_prefix="diff-subtask")
    return _subtask_pool
