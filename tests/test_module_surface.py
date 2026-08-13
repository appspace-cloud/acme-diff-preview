"""The refactor safety net (COPS-2658 phase 0).

Two guards that must stay green through every extraction step:

1. Every symbol any test reaches for on `diff_preview` still exists there.
   Moving code out of the hub is fine; making it unreachable from the hub
   is not, because a hundred test files and the operator tooling address
   the service through that one module.

2. Every monkeypatch seam still connects. See scripts/audit_seams.py for
   why a cut seam is silent and therefore dangerous.

Both guards are computed from the tree itself rather than a hand-written
list, so they cannot drift out of date.
"""
import ast
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "src")
TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SRC)
sys.path.insert(0, os.path.join(REPO, "scripts"))

import diff_preview  # noqa: E402
import audit_seams  # noqa: E402


def _module_aliases(tree):
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "diff_preview":
                    out[a.asname or a.name] = True
    return set(out)


def _optional_attrs(tree, alias):
    """Names the file itself declares optional via hasattr(m, "name").

    A test written as `real_open = m.open if hasattr(m, "open") else open`
    is stating that the attribute may be absent. Demanding it would turn
    the file's own tolerance into a hard requirement on the module.
    """
    out = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "hasattr"
                and len(node.args) == 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in alias
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)):
            out.add(node.args[1].value)
    return out


def _attributes_tests_reach_for():
    """Every `m.<attr>` the suite touches, with the file that wants it."""
    wanted = {}
    for root, _dirs, files in os.walk(TESTS):
        for fn in sorted(files):
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            try:
                tree = ast.parse(open(path, encoding="utf-8").read())
            except SyntaxError:
                continue
            alias = _module_aliases(tree)
            if not alias:
                continue
            optional = _optional_attrs(tree, alias)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)
                        and node.value.id in alias
                        and node.attr not in optional):
                    wanted.setdefault(node.attr, set()).add(fn)
    return wanted


def _missing_from(namespace, wanted):
    """Names in `wanted` that `namespace` does not carry."""
    return {
        name: sorted(files)
        for name, files in wanted.items()
        if not hasattr(namespace, name)
    }


# The suite also reaches stdlib and sibling modules through the hub
# (m.time, m.diff_ui, m.subprocess). Those are attributes of the module
# namespace like any other, so they are covered here as well -- and they
# should be: an extraction that drops the `import diff_ui` line from the
# hub would break real callers, not just tests.
def test_every_symbol_the_suite_reaches_for_still_exists():
    wanted = _attributes_tests_reach_for()
    assert wanted, "surface probe found nothing: the AST walk is broken"
    missing = _missing_from(diff_preview, wanted)
    assert not missing, (
        "these names left the diff_preview namespace but the suite still "
        f"addresses them there: {missing}"
    )


def test_surface_probe_detects_a_symbol_that_went_missing():
    """A guard that cannot fail is decoration, not evidence.

    Take the real set of names the suite wants and hold it against a
    namespace that is missing one of them. The probe has to say so.
    """
    wanted = _attributes_tests_reach_for()
    victim = "format_comment"
    assert victim in wanted, "probe no longer sees a name the suite clearly uses"

    class _HubMinusOneSymbol:
        def __getattr__(self, name):
            if name == victim:
                raise AttributeError(name)
            return getattr(diff_preview, name)

    missing = _missing_from(_HubMinusOneSymbol(), wanted)
    assert victim in missing
    assert missing[victim], "the report must name the files that would break"


# ── seam guard ──────────────────────────────────────────────────────────────

def test_every_monkeypatch_seam_still_connects():
    problems = audit_seams.broken_seams(src=SRC, tests=TESTS)
    assert not problems, "\n".join(
        f"tests patch {mod}.{name} but it is read inside "
        f"{', '.join(strangers)}, where the patch cannot reach"
        for mod, name, strangers in problems
    )


def test_the_suite_actually_has_seams_to_check():
    """If the collector silently returns nothing, the guard above is a no-op."""
    seams = audit_seams.patched_names(src=SRC, tests=TESTS)
    assert len(seams) > 100, f"only {len(seams)} seams found; collector broken?"


def test_seam_audit_detects_a_cut_seam(tmp_path):
    """Prove the detector fires on the exact shape this refactor risks.

    A helper moves to a new module, its caller moves with it, and the test
    keeps patching the old home. Nothing raises; the patch simply stops
    applying. That is what has to be caught.
    """
    src = tmp_path / "src"
    tests = tmp_path / "tests"
    src.mkdir()
    tests.mkdir()

    (src / "hub.py").write_text(
        "import helper\n"
        "from helper import fetch, render\n"
    )
    # render() calls fetch() in helper's namespace, so patching hub.fetch
    # no longer reaches it.
    (src / "helper.py").write_text(
        "def fetch():\n"
        "    return 'real'\n"
        "\n"
        "def render():\n"
        "    return fetch()\n"
    )
    (tests / "test_thing.py").write_text(
        "import hub as m\n"
        "\n"
        "def test_render(monkeypatch):\n"
        "    monkeypatch.setattr(m, 'fetch', lambda: 'fake')\n"
        "    assert m.render() == 'fake'\n"
    )

    problems = audit_seams.broken_seams(src=str(src), tests=str(tests))
    assert problems == [("hub", "fetch", ["helper"])], problems


# ── dependency direction ────────────────────────────────────────────────────

# Modules extracted out of the hub. The arrow points one way only: the hub
# imports them, they never import the hub. A back-import would create a
# cycle, and worse, it would let a leaf reach into service state and undo
# the reason for extracting it.
EXTRACTED_LEAVES = ("redact", "vocabulary", "comment_render", "version_fold",
                    "grouping", "vm_analysis", "decommission", "manifest",
                    "identity")

# Modules that predate the split and are allowed to be reached from the hub
# without being leaves themselves.
SERVICE_MODULES = ("diff_preview",)


@pytest.mark.parametrize("leaf", EXTRACTED_LEAVES)
def test_extracted_module_never_imports_the_service_back(leaf):
    tree = ast.parse(open(os.path.join(SRC, leaf + ".py"), encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    offenders = imported.intersection(SERVICE_MODULES)
    assert not offenders, (
        f"{leaf}.py imports {sorted(offenders)}; extracted modules must stay "
        "leaves so the dependency arrow only points out of the hub"
    )
