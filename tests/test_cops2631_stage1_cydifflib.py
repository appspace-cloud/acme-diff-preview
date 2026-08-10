"""COPS-2631 stage 1: CyDifflib for the diff engine, byte-identical to stdlib.

_diff_resources imports difflib inside the function and builds
SequenceMatcher via unified_diff. The preferred swap is a module-scope
monkeypatch of difflib.SequenceMatcher onto cydifflib.SequenceMatcher so
stdlib grouping/formatting stay untouched.

Gate: output must be byte-identical to stdlib on Kubernetes-shaped
manifests, including resources over 200 lines (the golden comment corpus
tops out at 178 lines and cannot exercise that alone).
"""
import os
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_preview  # noqa: E402


def _k8s_doc(kind, name, n_lines, tag="v1"):
    """Synthetic Deployment-shaped YAML with n_lines of body."""
    body = "\n".join(
        "  line-%03d: value-%s-%d" % (i, tag, i) for i in range(n_lines))
    return textwrap.dedent("""\
        apiVersion: apps/v1
        kind: {kind}
        metadata:
          name: {name}
          namespace: demo
        spec:
        {body}
        """).format(kind=kind, name=name, body=body)


def _resource_dict(docs):
    text = "\n---\n".join(docs)
    return diff_preview._parse_manifest_resources(text)


def _diff_with_matcher(matcher_cls, main_res, pr_res):
    """Run _diff_resources under an explicit SequenceMatcher class."""
    import difflib
    saved = difflib.SequenceMatcher
    try:
        difflib.SequenceMatcher = matcher_cls
        return diff_preview._diff_resources(main_res, pr_res)
    finally:
        difflib.SequenceMatcher = saved


def test_cydifflib_engine_is_active():
    """Production image installs cydifflib; the monkeypatch must engage."""
    cydifflib = pytest.importorskip("cydifflib")
    assert diff_preview._DIFFLIB_ENGINE == "cydifflib"
    import difflib
    assert difflib.SequenceMatcher is cydifflib.SequenceMatcher


def test_diff_resources_byte_identical_to_stdlib_on_large_resources():
    """Byte-identity gate for resources over 200 lines (ticket stage 1)."""
    cydifflib = pytest.importorskip("cydifflib")

    main_docs = [
        _k8s_doc("Deployment", "api", 220, tag="main"),
        _k8s_doc("Service", "api", 40, tag="main"),
        _k8s_doc("ConfigMap", "cfg", 80, tag="same"),
    ]
    pr_docs = [
        _k8s_doc("Deployment", "api", 220, tag="pr"),
        _k8s_doc("Service", "api", 40, tag="pr"),
        _k8s_doc("ConfigMap", "cfg", 80, tag="same"),
    ]
    main_res = _resource_dict(main_docs)
    pr_res = _resource_dict(pr_docs)

    stdlib_out = _diff_with_matcher(
        diff_preview._STDLIB_SEQUENCE_MATCHER, main_res, pr_res)
    cy_out = _diff_with_matcher(cydifflib.SequenceMatcher, main_res, pr_res)

    assert cy_out == stdlib_out
    assert "Deployment" in cy_out
    assert "ConfigMap" not in cy_out
    assert cy_out.count("\n") > 200

    # Leave the production monkeypatch in place for later tests in this
    # process (the helper restores whatever was set when it entered).
    import difflib
    difflib.SequenceMatcher = cydifflib.SequenceMatcher


def test_autojunk_does_not_diverge_on_k8s_manifests():
    """Ticket measured autojunk True/False identical on this data shape."""
    pytest.importorskip("cydifflib")

    a = _k8s_doc("Deployment", "api", 250, tag="a").splitlines(keepends=True)
    b = _k8s_doc("Deployment", "api", 250, tag="b").splitlines(keepends=True)
    stdlib_cls = diff_preview._STDLIB_SEQUENCE_MATCHER
    assert (stdlib_cls(None, a, b, autojunk=True).get_opcodes()
            == stdlib_cls(None, a, b, autojunk=False).get_opcodes())
