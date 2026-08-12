"""COPS-2652: everything this service writes to stdout must be JSON.

`log()` ends in `print(json.dumps(entry), flush=True)`, so stdout is a
JSON stream. 31 plain `print()` calls wrote human-formatted text into
that same stream. Measured on the leader pod at 2.60.0: 426 of 684 lines
over 20 minutes were not valid JSON, 62% of the output.

Two consequences, and the second is worse than it looks:

1. A `severity>=WARNING` query cannot see any of them, and none are
   queryable by field. `Skipping: transient-failure backoff active for
   SHA X` is literally the answer to "why does this PR have no
   comment?", and it was plain text with nothing to filter on.

2. GKE's troubleshooting guide for MISSING logs names this exact
   pattern: the agent's parser expects one consistent format per
   stream, and a plain-text line in a JSON stream can break it, causing
   entries to be dropped or ingested incorrectly. A diagnostic channel
   that fails silently is the worst kind.

The three bootstrap helpers (`_env_int`, `_env_float`,
`_require_env`) stay on `print()` deliberately: they run at import time,
before `log()` is defined at line 1222, and they write to STDERR, which
carries nothing else. That stream is internally consistent, so it does
not have the mixing problem.
"""
import ast
import io
import json
import os
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_preview as dp  # noqa: E402

SRC = os.path.join(os.path.dirname(__file__), "..", "src", "diff_preview.py")

# The only print() calls allowed to remain, by the function that owns them.
_ALLOWED_IN = {
    "log",           # the JSON emitter itself
    "_env_int",      # bootstrap, pre-log(), stderr
    "_env_float",    # bootstrap, pre-log(), stderr
    "_require_env",  # bootstrap, pre-log(), stderr
}


def _print_calls():
    """Every print() call in the module, with its enclosing function."""
    tree = ast.parse(open(SRC).read())
    owner = {}

    class Walker(ast.NodeVisitor):
        def __init__(self):
            self.stack = []

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            if isinstance(node.func, ast.Name) and node.func.id == "print":
                owner[node.lineno] = self.stack[-1] if self.stack else "<module>"
            self.generic_visit(node)

    Walker().visit(tree)
    return owner


def test_no_stray_print_calls_remain():
    """The gate. A new print() is how this regresses."""
    stray = {ln: fn for ln, fn in _print_calls().items()
             if fn not in _ALLOWED_IN}
    assert not stray, (
        "print() writes plain text into the JSON stdout stream. Use log(). "
        "Offenders (line: function): " + ", ".join(
            f"{ln}: {fn}" for ln, fn in sorted(stray.items())))


def test_the_bootstrap_helpers_still_use_stderr():
    """They are exempt because they predate log() AND write to stderr, a
    stream that carries nothing else. Lose either half and the exemption
    stops being valid."""
    src = open(SRC).read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (isinstance(node, ast.FunctionDef)
                and node.name in ("_env_int", "_env_float", "_require_env")):
            body = ast.get_source_segment(src, node) or ""
            if "print(" in body:
                assert "file=sys.stderr" in body, (
                    f"{node.name} prints without stderr, which puts plain "
                    f"text into the JSON stdout stream")


def test_every_line_a_comment_run_writes_to_stdout_is_json(monkeypatch):
    """Behavioural half: format_comment emits the `[comment] mode=...`
    trace, which was one of the offenders.

    monkeypatch, not direct assignment: assigning to dp.generate_ai_summary
    leaks the stub into every test that runs afterwards in the same
    session, which broke three unrelated AI tests when this file was first
    written.
    """
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    results = {"pv-x-a-glb": dp.DiffResult(
        "--- /apps/Deployment d0", [("/apps/Deployment d0", "  image: a:1")],
        1, True, None, dp.OUT_DIFF, None)}
    buf = io.StringIO()
    with redirect_stdout(buf):
        dp.format_comment("a" * 40, results, base_sha="b" * 40,
                          artifact_url="https://x/diff/r/1/s")
    bad = []
    for line in buf.getvalue().splitlines():
        if not line.strip():
            continue
        try:
            json.loads(line)
        except ValueError:
            bad.append(line)
    assert not bad, (
        f"{len(bad)} non-JSON line(s) on stdout, first: {bad[0]!r}")


def test_the_diagnostic_lines_carry_an_event_field():
    """These are the ones an operator actually queries: why is there no
    comment on this PR, and was this PR's app list truncated."""
    src = open(SRC).read()
    for needle, event in (
            ("transient-failure backoff active", "backoff_skip"),
            ("Capped to", "app_cap_applied")):
        i = src.find(needle)
        assert i > 0, f"log line for {needle!r} not found"
        window = src[max(0, i - 400):i + 400]
        assert f'event="{event}"' in window or f"event='{event}'" in window, (
            f"the {needle!r} line must carry event={event} so it can be "
            f"counted and alerted on")
