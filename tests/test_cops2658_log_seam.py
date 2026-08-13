"""COPS-2658 phase 7: `log` has one canonical home, and one seam.

`log` was the most-read name in the hub -- 182 call sites -- and the suite
patched it on `diff_preview` at 31 places. That made it the single biggest
obstacle to moving anything else: a def that logs could not leave the hub
without its logging escaping the patch.

Moving it is only safe because the escape is now detectable. Two shapes
would have cut the seam silently:

  * a leaf doing `from logsink import log` and calling it bare -- caught by
    the bare-name half of scripts/audit_seams.py;
  * a leaf calling `logsink.log(...)` while the suite still patched
    `diff_preview.log` -- invisible until the qualified-read half landed.

So every reader now goes through the module object that the suite patches,
and these tests pin that end state: the hub's own logging is interceptable
by patching `logsink.log`, and no second definition of `log` has grown back.
"""
import ast
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_preview  # noqa: E402
import logsink  # noqa: E402

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")


def test_patching_logsink_intercepts_the_hub_own_logging(monkeypatch):
    """The seam a hundred tests depend on, exercised end to end.

    `debug()` lives in the hub and logs. If the hub resolved `log` through
    its own namespace, this patch would not reach it and the capture would
    stay empty while the test still passed -- the exact silent green this
    phase had to avoid.
    """
    captured = []
    monkeypatch.setattr(logsink, "log",
                        lambda msg, severity="INFO", **kw: captured.append((msg, severity)))
    monkeypatch.setattr(diff_preview, "DEBUG", True)

    diff_preview.debug("phase 7 seam check", pr="1")

    assert captured == [("phase 7 seam check", "DEBUG")], captured


def test_the_hub_keeps_no_second_binding_for_log():
    """A leftover `log` in the hub namespace would be a second, unpatched seam.

    Dropping the name rather than re-exporting it is deliberate: a stray
    bare `log(...)` in the hub then fails loudly with NameError instead of
    quietly writing past whatever the suite patched.
    """
    assert not hasattr(diff_preview, "log"), (
        "diff_preview still carries a `log` binding; callers resolving "
        "through it would escape a patch applied to logsink"
    )


def test_log_is_defined_exactly_once_across_the_service():
    """One definition, one seam. A duplicate is how this class of bug returns."""
    definitions = []
    for fn in sorted(os.listdir(SRC)):
        if not fn.endswith(".py"):
            continue
        tree = ast.parse(open(os.path.join(SRC, fn), encoding="utf-8").read())
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "log":
                definitions.append(fn)
    assert definitions == ["logsink.py"], definitions


def test_no_module_reads_log_as_a_bare_global():
    """Every reader must go through the module object the suite patches.

    This is the invariant the qualified-read guard protects from the other
    side: the audit proves no patch is routed around, and this proves the
    hub did not simply re-import the name to dodge the rewrite.
    """
    offenders = {}
    for fn in sorted(os.listdir(SRC)):
        if not fn.endswith(".py") or fn == "logsink.py":
            continue
        tree = ast.parse(open(os.path.join(SRC, fn), encoding="utf-8").read())
        bare = [n.lineno for n in ast.walk(tree)
                if isinstance(n, ast.Name) and n.id == "log"
                and isinstance(n.ctx, ast.Load)]
        if bare:
            offenders[fn] = bare
    assert not offenders, f"bare `log` reads outside logsink.py: {offenders}"
