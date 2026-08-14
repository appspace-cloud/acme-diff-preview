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


def test_no_seam_is_routed_around_by_a_qualified_read():
    """The other half of the guard: `X.name` where the patch targets `M.name`.

    A bare-name read is not the only way to escape a patch. Reaching the
    function through a module object that is NOT the patched one escapes it
    just as completely, and just as quietly.
    """
    problems = audit_seams.bypassed_seams(src=SRC, tests=TESTS)
    assert not problems, "\n".join(
        f"tests patch {mod}.{name} but the read in {', '.join(readers)} "
        f"goes through {qualifier}.{name}, which that patch never reaches"
        for mod, name, qualifier, readers in problems
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


def test_seam_audit_detects_a_seam_reached_through_the_wrong_module(tmp_path):
    """Prove the detector fires on the shape the log decision would create.

    `log` moves to its own module, the hub re-exports it so every existing
    `m.log` reference keeps resolving, and a leaf calls `logsink.log(...)`.
    The suite still patches the hub. The re-export makes the seam LOOK
    intact -- the name is there, the attribute exists, nothing raises -- but
    the leaf's call resolves through logsink, which no patch touched.
    """
    src = tmp_path / "src"
    tests = tmp_path / "tests"
    src.mkdir()
    tests.mkdir()

    (src / "hub.py").write_text(
        "import logsink\n"
        "from logsink import log\n"
    )
    (src / "logsink.py").write_text(
        "def log(msg):\n"
        "    print(msg)\n"
    )
    # The leaf reaches log through the logsink module object, so
    # monkeypatch.setattr(hub, "log", ...) rebinds a name this call never
    # consults.
    (src / "leaf.py").write_text(
        "import logsink\n"
        "\n"
        "def render():\n"
        "    logsink.log('rendering')\n"
        "    return 'html'\n"
    )
    (tests / "test_thing.py").write_text(
        "import hub as m\n"
        "\n"
        "def test_render(monkeypatch):\n"
        "    monkeypatch.setattr(m, 'log', lambda msg: None)\n"
    )

    # The bare-name half of the audit is blind here, and that blindness is
    # the whole reason this check exists: no module reads `log` as a bare
    # global, so there is nothing for it to report.
    assert audit_seams.broken_seams(src=str(src), tests=str(tests)) == []

    problems = audit_seams.bypassed_seams(src=str(src), tests=str(tests))
    assert problems == [("hub", "log", "logsink", ["leaf"])], problems


def test_a_qualified_read_through_the_patched_module_is_not_a_break(tmp_path):
    """The legitimate case the original docstring was right about.

    `diff_ui._gcs_upload(...)` in the hub is fine, because the suite patches
    _gcs_upload on diff_ui: the call resolves through the very module object
    the patch modified. Flagging this would make the audit cry wolf on three
    real call sites.
    """
    src = tmp_path / "src"
    tests = tmp_path / "tests"
    src.mkdir()
    tests.mkdir()

    (src / "hub.py").write_text(
        "import store\n"
        "\n"
        "def save(blob):\n"
        "    return store.upload(blob)\n"
    )
    (src / "store.py").write_text(
        "def upload(blob):\n"
        "    return 'gs://' + blob\n"
    )
    (tests / "test_thing.py").write_text(
        "import store as s\n"
        "\n"
        "def test_save(monkeypatch):\n"
        "    monkeypatch.setattr(s, 'upload', lambda b: 'fake')\n"
    )

    assert audit_seams.bypassed_seams(src=str(src), tests=str(tests)) == []


def test_the_audit_reads_SRC_and_TESTS_at_call_time(tmp_path, monkeypatch):
    """Pointing the module at another tree must actually redirect the audit.

    `def broken_seams(src=SRC, ...)` binds SRC once, when the function is
    defined. Reassigning `audit_seams.SRC` afterwards therefore changed
    nothing, and the audit went on reporting the real tree while looking
    like it had been redirected -- a clean tree answering a question about
    a dirty one, which is the failure mode this whole module exists to
    prevent. Anything scripting against the module hits it; the guards in
    this file were only immune because they pass src=/tests= explicitly.
    """
    src = tmp_path / "src"
    tests = tmp_path / "tests"
    src.mkdir()
    tests.mkdir()

    (src / "hub.py").write_text("from helper import fetch, render\n")
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
    )

    monkeypatch.setattr(audit_seams, "SRC", str(src))
    monkeypatch.setattr(audit_seams, "TESTS", str(tests))

    assert audit_seams.patched_names() == {("hub", "fetch")}
    assert audit_seams.broken_seams() == [("hub", "fetch", ["helper"])]


def test_a_local_that_shares_a_module_name_is_not_a_qualified_read(tmp_path):
    """Do not mistake `local.get(...)` for a read through a module.

    This is not hypothetical: the hub takes a `version_fold` dict parameter
    and calls `version_fold.get("label")` on it, while a real version_fold
    module sits next to it in src/. Resolving the qualifier against the bare
    list of module names would report that dict lookup as a cut seam. The
    qualifier is therefore resolved only through the reading file's own
    `import X` statements, and the hub imports this module with
    `from version_fold import ...`, which binds no module object.
    """
    src = tmp_path / "src"
    tests = tmp_path / "tests"
    src.mkdir()
    tests.mkdir()

    (src / "hub.py").write_text(
        "from version_fold import fold\n"
        "\n"
        "def render(version_fold):\n"
        "    return fold(version_fold.get('label'))\n"
    )
    (src / "version_fold.py").write_text(
        "def fold(x):\n"
        "    return x\n"
        "\n"
        "def get(key):\n"
        "    return None\n"
    )
    (tests / "test_thing.py").write_text(
        "import version_fold as vf\n"
        "\n"
        "def test_get(monkeypatch):\n"
        "    monkeypatch.setattr(vf, 'get', lambda k: 'x')\n"
    )

    assert audit_seams.bypassed_seams(src=str(src), tests=str(tests)) == []


# ── dependency direction ────────────────────────────────────────────────────

# Modules extracted out of the hub. The arrow points one way only: the hub
# imports them, they never import the hub. A back-import would create a
# cycle, and worse, it would let a leaf reach into service state and undo
# the reason for extracting it.
EXTRACTED_LEAVES = ("redact", "vocabulary", "comment_render", "version_fold",
                    "grouping", "vm_analysis", "decommission", "manifest",
                    "identity", "envcfg", "schema_errors", "chart_identity",
                    "app_meta", "ai_summary", "logsink", "render_profile",
                    "concurrency")

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
