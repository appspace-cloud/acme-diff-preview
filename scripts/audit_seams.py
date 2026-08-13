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

For every name N patched on module M by the suite, every reference to N in
the source tree must live in M itself. A reference from any other module is
a call the patch cannot reach.

Re-exporting is fine and expected: if `_redact_sensitive` moves to
`redact.py` and the hub does `from redact import _redact_sensitive`, the
hub keeps its own binding, hub callers resolve through it, and the seam
holds. What breaks a seam is moving the CALLER out, not the callee.

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
    """Map local alias -> source module name, for `import X as Y` forms."""
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

    Only unqualified reads count. `redact._redact_sensitive(...)` resolves
    through the redact module object at call time and therefore honours a
    patch applied to that module, so a qualified access is not a broken
    seam and is skipped here.
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


def broken_seams(src=SRC, tests=TESTS):
    """Seams where a patch can no longer reach every caller."""
    problems = []
    for mod, name in sorted(patched_names(src, tests)):
        refs = _referencing_modules(name, src)
        strangers = refs - {mod}
        if strangers:
            problems.append((mod, name, sorted(strangers)))
    return problems


def main():
    seams = patched_names()
    problems = broken_seams()
    print(f"seams checked: {len(seams)} (module, name) pairs")
    if not problems:
        print("all seams intact")
        return 0
    print(f"BROKEN SEAMS: {len(problems)}")
    for mod, name, strangers in problems:
        print(f"  tests patch {mod}.{name}, but {', '.join(strangers)} "
              f"read it in their own namespace")
    return 1


if __name__ == "__main__":
    sys.exit(main())
