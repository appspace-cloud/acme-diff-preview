"""The durable render cache must not inherit the redacted-artifact bucket (COPS-2668, P1).

The disk and GCS tiers of the main-render cache persist the RAW `helm template`
output. Redaction is display-time only, and deliberately so: `redact.py` says
"the diff engine compares the real values", the shadow audit byte-compares
persisted bytes against a fresh render, and a cold-tier hit rebuilds the diff
inputs from the stored text. Redacting before persist would poison every cached
main side and fabricate a diff on every Secret, so it is not the fix.

The defect is where those raw renders go. `MAIN_RENDER_GCS_BUCKET` defaulted to
`DIFF_UI_GCS_BUCKET`, and that bucket's documented contract — in diff_ui.py's
own module docstring — is:

    the store only ever holds already-redacted content

The chart exposed no knob for the render-cache bucket, so any deployment that
set `diffUi.gcsBucket` silently opted into shipping plaintext Secret values
there. Verified in production on 2026-08-14: 1,748 objects under
`render-cache/cops2631-v1/`, in a bucket where `projectViewer` already holds
`legacyObjectReader` — so a read-only project role conferred plaintext reads of
every Secret rendered across dev/stage/prod in a rolling 14-day window.

Two rules follow, and this file pins both:

1. No silent inheritance. An unset render-cache bucket disables the durable
   tier rather than borrowing the artifact bucket. The operator names the
   bucket that will hold unredacted renders, on purpose, or there is not one.

2. Pointing it AT the artifact bucket is refused outright, not merely
   discouraged. That is the exact configuration this ticket exists to remove,
   and a comment in values.yaml would not stop someone re-creating it.
"""
import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import render_cache


R = render_cache._resolve_render_cache_bucket


# ── 1. no silent inheritance ─────────────────────────────────────────────

def test_unset_does_not_inherit_the_artifact_bucket():
    """The whole defect in one assertion."""
    assert R("", "acme-diff-preview-artifacts.appspacestorage.com") == "", (
        "an unset render-cache bucket must disable the durable tier, not "
        "borrow the bucket documented as redacted-only")


def test_unset_with_no_artifact_bucket_is_also_off():
    assert R("", "") == ""


# ── 2. an explicit, separate bucket is honoured ──────────────────────────

def test_explicit_bucket_is_used():
    assert R("acme-diff-preview-render-cache", "artifacts-bucket") == \
        "acme-diff-preview-render-cache"


def test_explicit_bucket_works_without_an_artifact_bucket():
    assert R("render-cache-only", "") == "render-cache-only"


def test_whitespace_is_stripped():
    assert R("  render-cache-only  ", "") == "render-cache-only"


# ── 3. the dangerous configuration is refused, not warned about ──────────

def test_same_bucket_as_artifacts_is_refused():
    """Re-creating the exact defect must not be possible by configuration."""
    same = "acme-diff-preview-artifacts.appspacestorage.com"
    assert R(same, same) == "", (
        "pointing the render cache at the redacted-artifact bucket is the "
        "configuration this ticket removes; it must be refused, not honoured")


def test_same_bucket_comparison_ignores_whitespace():
    assert R("  shared-bucket ", "shared-bucket") == ""


# ── 4. the module must not ship the old default ──────────────────────────

def test_module_default_is_not_the_artifact_bucket():
    """Guards the wiring, not just the helper: if someone re-points the
    module-level constant at DIFF_UI_GCS_BUCKET, this fails."""
    import inspect
    src = inspect.getsource(render_cache)
    assert 'MAIN_RENDER_GCS_BUCKET", DIFF_UI_GCS_BUCKET' not in src, (
        "the render-cache bucket must not default to the artifact bucket")


def test_docstring_contract_of_the_artifact_store_is_accurate():
    """diff_ui.py claimed the artifact store 'only ever holds already-redacted
    content'. That was true of its own writes and false about the bucket once
    the render cache started sharing it. A stale contract is what makes an IAM
    grant on that bucket look safe."""
    import diff_ui
    doc = diff_ui.__doc__ or ""
    if "only ever holds already-redacted content" in doc:
        assert "render-cache" in doc or "COPS-2668" in doc, (
            "if the redacted-only claim stays, it must say what else may "
            "share the bucket and under which prefix")
