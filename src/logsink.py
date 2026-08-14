"""Structured logging, extracted so the patch seam has one canonical home.

`log` is the most-read name the service has: 182 call sites in the hub
alone, and the suite replaces it at 31 places. While it lived in
`diff_preview`, every def that logs was pinned there too -- moving one
would have resolved `log` in the new module's namespace and quietly
escaped the patch, which is why COPS-2658 deferred this across six phases.

The rule that keeps the seam honest is simple: nothing reads `log` as a
bare global. Every caller goes through this module object, which is the
one the suite patches, so a patch reaches the hub and every leaf alike.
scripts/audit_seams.py enforces both halves of that -- a bare read from
another namespace, and a qualified read routed through some *other*
module -- and tests/test_cops2658_log_seam.py pins the end state.
"""
import os
import json
from datetime import datetime, timezone


def log(msg: str, severity: str = "INFO", **labels) -> None:
    """Emit a structured JSON log line in GCP Cloud Logging format."""
    entry: dict = {
        "severity":  severity,
        "message":   msg,
        "time":      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "component": "acme-diff-preview",
    }
    if labels:
        entry["labels"] = {k: str(v) for k, v in labels.items()}
    print(json.dumps(entry), flush=True)


# Verbose per-app / full-stderr logging. Set LOG_LEVEL=DEBUG to enable.
LOG_LEVEL          = os.environ.get("LOG_LEVEL", "INFO").upper()
DEBUG              = LOG_LEVEL == "DEBUG"


def debug(msg: str, **labels) -> None:
    """Emit a DEBUG log line only when LOG_LEVEL=DEBUG.

    Used for the verbose diagnostics that help explain *why* a diff failed:
    full ArgoCD stderr, per-attempt classification, repo-server error category,
    etc. Kept off by default so normal INFO logs stay readable.
    """
    if DEBUG:
        log(msg, "DEBUG", **labels)
