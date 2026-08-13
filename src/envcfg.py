"""Environment configuration readers.

Sliced out of diff_preview.py unchanged (COPS-2658 phase 6).

Three readers that turn an environment variable into a typed value with a
default, and one that refuses to start without a required variable. Pure
stdlib, no repo dependencies -- the most foundational leaf in the tree, and
deliberately so: nearly every module-level constant in the service is built
from one of these, so anything that needs a constant can import this without
dragging a domain module along.

Extracting it also clears the way for the RenderProfile decision. That
cluster's closure reaches `_env_int` because its constants are built from it,
which would otherwise have forced either a back-import or a duplicate.
"""
import os
import sys


def _env_int(name: str, default: int) -> int:
    """Parse an integer env var, falling back to default on any bad value.

    A typo in ANY numeric env var (e.g. DIFF_WORKERS=sixteen) used to crash
    the pod at import time with a raw traceback and no hint which variable
    was at fault (bughunt N3). Now it logs a WARNING naming the variable,
    the bad value, and the default used, and the pod starts normally.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f'WARNING: env var {name}="{raw}" is not a valid integer; '
              f'using default {default}', file=sys.stderr, flush=True)
        return default


def _env_float(name: str, default: float) -> float:
    """Parse a float env var, falling back to default on any bad value."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f'WARNING: env var {name}="{raw}" is not a valid float; '
              f'using default {default}', file=sys.stderr, flush=True)
        return default


def _require_env(*names):
    """Exit with ONE clear message listing every missing required env var.

    v2.5.19 (F2): the bare os.environ["X"] reads below fail-fast correctly but
    greet a misconfigured deployment with a raw KeyError for a single var at a
    time — fix one, redeploy, hit the next. This reports them all at once.
    Returns None when all are present (also used by tests).
    """
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        msg = ("FATAL: missing required environment variable(s): "
               + ", ".join(missing))
        print(msg, file=sys.stderr, flush=True)
        raise SystemExit(msg)
    return None
