"""COPS-2651: an app kept for being risky, then rendered with no evidence,
leaves a naked header that repeats its own table row.

Observed on acme-config-prod PR 4115 (sha f6e34781, 3 apps, 191 resources).
The comment renders the Changeset overview table naming all three apps with
counts and deep links, then immediately below:

    warn **`pv-ubp-a-glb`** -- 38 resource(s) changed


    warn **`pv-ubp-a-ss`** -- 10 resource(s) changed

Two facts each, app name and count, both already in the row above -- which
additionally carries a deep link the header does not have. No fold summary,
no evidence, no hunks. `pv-ubp-a-ms` (143 resources) has no such header,
so the shape was not even consistent.

## Why the existing skip missed them

COPS-2635/2636 added the skip, gated on `not _is_risky_result(rep_r)`. That
predicate is true when an app has vm_changes -- and in 4115 the glb and ss
apps carry the KCC Compute* resources listed in the VM panel. So they were
kept as risky.

Then `_format_app_diff_block` rendered them with COMMENT_INLINE_EVIDENCE_LINES
at its default of 0, so the evidence loop never ran, and with row_pointer
False (COPS-2640) so the pointer was gone too. Being risky bought a header
and nothing else.

The gate was a PROXY -- "risky apps have something to show" -- and the
proxy is false whenever the profile renders no evidence. This pins the
direct property instead: a block whose body is empty is dropped, because
its header states nothing the table does not already state better.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_preview as dp  # noqa: E402
import render_profile

URL = "https://argocd.appspace.com/diff/acme-config-prod/4115/f6e34781d10e"


def _changed(name, n, vm=None, deleted=None, fold=None):
    """DiffResult is a namedtuple, so every field goes in at construction."""
    hdrs = ["/apps/Deployment d%d" % i for i in range(n)]
    secs = [(h, "  image: acme/%s:1" % name) for h in hdrs]
    return dp.DiffResult(
        "\n".join("--- %s" % h for h in hdrs), secs, n, True, None,
        dp.OUT_DIFF, None,
        None,                 # version_change
        deleted or None,      # deleted_resources
        None,                 # replicas_zeroed
        None,                 # fingerprint
        None,                 # renamed_resources
        vm or None,           # vm_changes
        fold or None,         # version_fold
    )


def _pr4115():
    """The real shape: three apps, two of them carrying VM changes."""
    vm = [{"header": "/kcc/ComputeInstance pv-ubp-svc-a",
           "kind": "ComputeInstance", "name": "pv-ubp-svc-a",
           "env": "pv-ubp-a", "dangerous": False,
           "notes": ["other field(s) changed: aan"]}]
    return {
        "pv-ubp-a-glb": _changed("glb", 38, vm=vm),
        "pv-ubp-a-ms": _changed("ms", 143),
        "pv-ubp-a-ss": _changed("ss", 10, vm=vm),
    }


def _comment(results=None, **kw):
    return dp.format_comment("f" * 40, results if results is not None
                             else _pr4115(), base_sha="d" * 40,
                             artifact_url=URL, **kw)


def _header_lines(text):
    return [ln for ln in text.splitlines()
            if "resource(s) changed" in ln and ln.lstrip().startswith("\u26a0")]


# -- the defect -------------------------------------------------------------

def test_no_app_header_survives_with_an_empty_body(monkeypatch):
    """THE gate. Every remaining per-app header must be followed by
    something; a header with nothing under it is the defect."""
    monkeypatch.setattr(render_profile, "COMMENT_INLINE_DIFFS", False)
    monkeypatch.setattr(render_profile, "COMMENT_INLINE_EVIDENCE_LINES", 0)
    out = _comment()
    lines = out.splitlines()
    for i, ln in enumerate(lines):
        if "resource(s) changed" not in ln or not ln.lstrip().startswith("\u26a0"):
            continue
        rest = [x for x in lines[i + 1:i + 6] if x.strip()]
        assert rest and not rest[0].lstrip().startswith("\u26a0"), (
            f"orphan header with no body: {ln!r}")
        assert not rest[0].startswith("\U0001f50e"), (
            f"header followed only by the global full-diff pointer: {ln!r}")


def test_the_pr4115_shape_emits_no_orphan_headers(monkeypatch):
    """The table already names all three apps with counts and links."""
    monkeypatch.setattr(render_profile, "COMMENT_INLINE_DIFFS", False)
    monkeypatch.setattr(render_profile, "COMMENT_INLINE_EVIDENCE_LINES", 0)
    out = _comment()
    assert "Changeset overview" in out, "precondition: the table renders"
    for app in ("pv-ubp-a-glb", "pv-ubp-a-ms", "pv-ubp-a-ss"):
        assert app in out, f"{app} must still appear (in the table row)"
    assert not _header_lines(out), (
        "no per-app header should survive when every block is empty:\n"
        + "\n".join(_header_lines(out)))


def test_risky_no_longer_buys_a_bare_header(monkeypatch):
    """vm_changes made these apps 'risky', which kept the block, but with
    zero evidence lines the block had nothing to keep."""
    monkeypatch.setattr(render_profile, "COMMENT_INLINE_DIFFS", False)
    monkeypatch.setattr(render_profile, "COMMENT_INLINE_EVIDENCE_LINES", 0)
    out = _comment()
    for app in ("pv-ubp-a-glb", "pv-ubp-a-ss"):
        assert f"**`{app}`** \u2014 " not in out, (
            f"{app} is risky but its block is empty, so the header is "
            f"pure duplication of its table row")


# -- what must NOT change ---------------------------------------------------

def test_a_block_with_evidence_keeps_its_header(monkeypatch):
    """The header is what evidence hangs from. Never drop it when the
    block actually says something."""
    monkeypatch.setattr(render_profile, "COMMENT_INLINE_DIFFS", False)
    monkeypatch.setattr(render_profile, "COMMENT_INLINE_EVIDENCE_LINES", 3)
    results = _pr4115()
    results["pv-ubp-a-ss"] = _changed("ss", 10, vm=results["pv-ubp-a-ss"].vm_changes,
                                      deleted=["/apps/Deployment d0"])
    out = _comment(results)
    assert "**`pv-ubp-a-ss`** \u2014 " in out, (
        "an app rendering evidence must keep its header")


def test_a_version_fold_conclusion_keeps_its_header(monkeypatch):
    """COPS-2612 kept the fold sentence in the comment on purpose: it is a
    conclusion no table cell states."""
    monkeypatch.setattr(render_profile, "COMMENT_INLINE_DIFFS", False)
    monkeypatch.setattr(render_profile, "COMMENT_INLINE_EVIDENCE_LINES", 0)
    results = _pr4115()
    _r = results["pv-ubp-a-glb"]
    results["pv-ubp-a-glb"] = _changed(
        "glb", 38, vm=_r.vm_changes,
        fold={"n_foldable": 30, "label": "2602 -> 2603",
              "headers": [h for h, _ in _r.sections[:30]],
              "classes": ["image tag"]})
    out = _comment(results)
    assert "**`pv-ubp-a-glb`** \u2014 " in out
    assert "version transition" in out


def test_inline_diffs_on_keeps_every_header(monkeypatch):
    """The rollback shape (COMMENT_INLINE_DIFFS=true) renders hunks inside
    each block, so every block has a body and every header stays."""
    monkeypatch.setattr(render_profile, "COMMENT_INLINE_DIFFS", True)
    out = _comment()
    assert len(_header_lines(out)) >= 1, (
        "with inline diffs the blocks carry hunks and keep their headers")


def test_the_page_still_renders_every_block():
    """FULL_PROFILE is the complete record and pins inline_diffs True; the
    page must never lose an app block."""
    out = dp.format_comment("f" * 40, _pr4115(), base_sha="d" * 40,
                            artifact_url=URL, profile=dp.FULL_PROFILE)
    for app in ("pv-ubp-a-glb", "pv-ubp-a-ms", "pv-ubp-a-ss"):
        assert f"**`{app}`**" in out, f"the page dropped {app}"


def test_without_a_table_every_app_is_still_named(monkeypatch):
    """The whole argument for dropping a header is that the table states
    the same facts. With no table there is no such argument."""
    monkeypatch.setattr(render_profile, "COMMENT_INLINE_DIFFS", False)
    monkeypatch.setattr(render_profile, "COMMENT_INLINE_EVIDENCE_LINES", 0)
    out = dp.format_comment("f" * 40, _pr4115(), base_sha="d" * 40,
                            artifact_url="")
    for app in ("pv-ubp-a-glb", "pv-ubp-a-ms", "pv-ubp-a-ss"):
        assert app in out, f"{app} vanished with no table to name it"
