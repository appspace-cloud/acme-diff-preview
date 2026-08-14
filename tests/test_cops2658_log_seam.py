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

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_preview  # noqa: E402
import logsink  # noqa: E402

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")


def test_patching_logsink_intercepts_the_hub_own_logging(monkeypatch):
    """The seam a hundred tests depend on, exercised end to end.

    `_record_affected_apps` lives in the hub and logs when the app cap is
    exceeded. If the hub resolved `log` through its own namespace, this
    patch would not reach it and the capture would stay empty while the
    test still passed -- the exact silent green this phase had to avoid.
    """
    captured = []
    monkeypatch.setattr(logsink, "log",
                        lambda msg, severity="INFO", **kw: captured.append(msg))

    diff_preview._record_affected_apps(diff_preview.MAX_APPS_PER_RUN + 1)

    assert captured, "the hub's log call did not route through logsink"
    assert "app cap exceeded" in captured[0], captured


def test_debug_routes_through_the_same_seam(monkeypatch):
    """`debug` is a logging function, so it lives with `log` and its switch.

    It used to sit in the hub reading a hub-level DEBUG, which put both
    names in the closure of every cluster that logs verbosely -- the render
    cache among them. Moving the pair here removes that from every closure
    and leaves one place where "should this be emitted" is decided.
    """
    captured = []
    monkeypatch.setattr(logsink, "log",
                        lambda msg, severity="INFO", **kw: captured.append((msg, severity)))

    monkeypatch.setattr(logsink, "DEBUG", False)
    logsink.debug("suppressed")
    assert captured == [], "DEBUG=False must emit nothing"

    monkeypatch.setattr(logsink, "DEBUG", True)
    logsink.debug("emitted", pr="1")
    assert captured == [("emitted", "DEBUG")], captured


def test_the_hub_keeps_no_second_binding_for_log():
    """A leftover `log` in the hub namespace would be a second, unpatched seam.

    Dropping the name rather than re-exporting it is deliberate: a stray
    bare `log(...)` in the hub then fails loudly with NameError instead of
    quietly writing past whatever the suite patched.
    """
    for name in ("log", "debug", "DEBUG"):
        assert not hasattr(diff_preview, name), (
            f"diff_preview still carries a `{name}` binding; callers "
            "resolving through it would escape a patch applied to logsink"
        )


@pytest.mark.parametrize("name", ["log", "debug"])
def test_defined_exactly_once_across_the_service(name):
    """One definition, one seam. A duplicate is how this class of bug returns."""
    definitions = []
    for fn in sorted(os.listdir(SRC)):
        if not fn.endswith(".py"):
            continue
        tree = ast.parse(open(os.path.join(SRC, fn), encoding="utf-8").read())
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                definitions.append(fn)
    assert definitions == ["logsink.py"], definitions


@pytest.mark.parametrize("name", ["log", "debug", "DEBUG"])
def test_no_module_reads_the_logging_names_as_bare_globals(name):
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
                if isinstance(n, ast.Name) and n.id == name
                and isinstance(n.ctx, ast.Load)]
        if bare:
            offenders[fn] = bare
    assert not offenders, f"bare `{name}` reads outside logsink.py: {offenders}"
