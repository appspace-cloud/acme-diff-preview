"""Refactor safety net: prove that monkeypatch seams still connect.

WHY THIS EXISTS

The test suite patches the service module about a thousand times, over
roughly 140 distinct names: `monkeypatch.setattr(m, "_bb_fetch_status", ...)`,
`monkeypatch.setattr(m, "generate_ai_summary", ...)` and so on. Each of
those is a seam: the test replaces a name in the module namespace, and
every caller that resolves that name through the same namespace picks up
the replacement.

Splitting the service into modules can cut a seam WITHOUT failing anything.
If `format_comment` moves to `comment_render.py` and it calls
`generate_ai_summary`, that call now resolves in `comment_render`'s
namespace. A test doing `monkeypatch.setattr(m, "generate_ai_summary", fake)`
still runs green -- and no longer patches the real call. Sixty-odd tests
would start reaching for Vertex for real while reporting success. Green
while lying is the worst failure mode this codebase can produce, because
CI stops being evidence.

WHAT IT CHECKS

For every name N patched on module M by the suite, a reference to N in the
source tree has to resolve through M's namespace. There are two ways for it
not to, and the audit checks both:

1. A bare read of N from another module. `format_comment` moves to
   `comment_render.py`, calls `generate_ai_summary`, and that call now
   resolves in comment_render's namespace instead of M's.

2. A qualified read `X.N` where X is some source module other than M. This
   is the same escape wearing a different hat: reaching the function through
   a module object the patch never touched is exactly as effective at
   dodging the patch as reading a bare global in the wrong namespace.

Re-exporting is fine and expected: if `_redact_sensitive` moves to
`redact.py` and the hub does `from redact import _redact_sensitive`, the
hub keeps its own binding, hub callers resolve through it, and the seam
holds. What breaks a seam is moving the CALLER out, not the callee.

Note that a re-export makes case 2 look especially healthy. The name is
present on the patched module, `hasattr` says yes, the surface probe in
tests/test_module_surface.py is satisfied, and the patch still applies
cleanly -- to a binding nobody reads.

Run standalone for a readable report:

    python3 scripts/audit_seams.py
"""
import ast
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "src")
TESTS = os.path.join(REPO, "tests")

# Aliases are learned from the imports in each test file, so no list of
# nicknames has to be kept in sync here.


def _src_modules(src=SRC):
    """Every module the service ships, keyed by module name."""
    out = {}
    for fn in sorted(os.listdir(src)):
        if fn.endswith(".py"):
            out[fn[:-3]] = os.path.join(src, fn)
    return out


def _aliases(tree, src=SRC):
    """Map local name -> source module name, for `import X [as Y]` forms.

    Used on test files to learn what `m` refers to, and on source files to
    learn which qualifiers name a module rather than an ordinary object.
    `from X import y` deliberately does not count: it binds y, not X, so it
    creates no qualifier.
    """
    known = set(_src_modules(src))
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in known:
                    out[a.asname or a.name] = a.name
    return out


def patched_names(src=SRC, tests=TESTS):
    """(module, name) pairs the suite replaces via monkeypatch.setattr."""
    found = set()
    for root, _dirs, files in os.walk(tests):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            try:
                tree = ast.parse(open(path, encoding="utf-8").read())
            except SyntaxError:
                continue
            alias = _aliases(tree, src)
            if not alias:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                if not (isinstance(f, ast.Attribute) and f.attr == "setattr"):
                    continue
                if len(node.args) < 2:
                    continue
                target, name = node.args[0], node.args[1]
                # Only the two-arg form patches a module namespace.
                # `setattr(m.time, "sleep", ...)` patches the stdlib module
                # object instead and is unaffected by where our code lives.
                if not (isinstance(target, ast.Name) and target.id in alias):
                    continue
                if not (isinstance(name, ast.Constant)
                        and isinstance(name.value, str)):
                    continue
                found.add((alias[target.id], name.value))
    return found


def _referencing_modules(name, src=SRC):
    """Modules whose code reads the bare global `name`.

    Only unqualified reads count here. Qualified reads are a separate
    question with a separate answer, handled by _qualified_reads below:
    `redact._redact_sensitive(...)` honours a patch applied to redact, but
    only because redact is the module being patched. Whether a qualified
    read is safe depends on WHICH module it goes through, so it cannot be
    judged -- or dismissed -- without knowing the patch target.
    """
    hits = set()
    for mod, path in _src_modules(src).items():
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except SyntaxError:
            continue
        # Names bound by the module's own import statements are re-exports,
        # not reads of someone else's global.
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for a in node.names:
                    imported.add(a.asname or a.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == name:
                if node.id in imported and isinstance(node.ctx, ast.Store):
                    continue
                hits.add(mod)
                break
    return hits


def _qualified_reads(src=SRC):
    """Reads of the form `X.attr` where X names a source module.

    Returns {(module_X, attr): {modules containing such a read}}.

    The qualifier is resolved through the reading file's OWN import
    statements, never against the bare list of module names in src/. The
    difference is not academic: the hub has a `version_fold` dict parameter
    and calls `version_fold.get("label")` on it, with a real version_fold
    module sitting right next to it in src/. Name-matching would read that
    dict lookup as a module access. Since the hub imports that module as
    `from version_fold import ...`, no module qualifier is bound and the
    lookup is correctly ignored.
    """
    out = {}
    for mod, path in _src_modules(src).items():
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except SyntaxError:
            continue
        bound = _aliases(tree, src)
        if not bound:
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.ctx, ast.Load)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in bound):
                out.setdefault((bound[node.value.id], node.attr),
                               set()).add(mod)
    return out


def broken_seams(src=SRC, tests=TESTS):
    """Seams where a bare-name read escapes the patch.

    Half the audit. bypassed_seams() covers the qualified-read half; both
    have to be clean for a seam to hold, and main() runs both.
    """
    problems = []
    for mod, name in sorted(patched_names(src, tests)):
        refs = _referencing_modules(name, src)
        strangers = refs - {mod}
        if strangers:
            problems.append((mod, name, sorted(strangers)))
    return problems


def bypassed_seams(src=SRC, tests=TESTS):
    """Seams a qualified read routes around.

    The suite patches M.N, but somewhere the code says X.N with X a
    different source module, so the call resolves through a namespace the
    patch never touched. Yields (M, N, X, [modules doing the read]).

    A read through M itself is the legitimate case and stays silent: that is
    the module whose attribute the patch replaced.
    """
    targets = {}
    for mod, name in patched_names(src, tests):
        targets.setdefault(name, set()).add(mod)
    problems = []
    for (qualifier, name), readers in _qualified_reads(src).items():
        for mod in targets.get(name, ()):
            if qualifier != mod:
                problems.append((mod, name, qualifier, sorted(readers)))
    return sorted(problems)


def main():
    seams = patched_names()
    problems = broken_seams()
    bypassed = bypassed_seams()
    print(f"seams checked: {len(seams)} (module, name) pairs")
    if not problems and not bypassed:
        print("all seams intact")
        return 0
    if problems:
        print(f"BROKEN SEAMS: {len(problems)}")
        for mod, name, strangers in problems:
            print(f"  tests patch {mod}.{name}, but {', '.join(strangers)} "
                  f"read it in their own namespace")
    if bypassed:
        print(f"SEAMS ROUTED AROUND: {len(bypassed)}")
        for mod, name, qualifier, readers in bypassed:
            print(f"  tests patch {mod}.{name}, but the read in "
                  f"{', '.join(readers)} goes through {qualifier}.{name}, "
                  f"which that patch never reaches")
    return 1


if __name__ == "__main__":
    sys.exit(main())
