"""COPS-2631 stage 2: CSafeLoader for values-file YAML (hygiene, not hot path).

Rendered manifests never touch PyYAML (_parse_manifest_resources is a line
scanner). All yaml.safe_load sites parse values files. Use CSafeLoader when
libyaml is present, with a SafeLoader fallback so a libyaml-less environment
still boots. Call sites must keep catching yaml.YAMLError.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import yaml  # noqa: E402
import diff_preview  # noqa: E402


def test_yaml_safe_loader_prefers_csafe_when_available():
    if hasattr(yaml, "CSafeLoader"):
        assert diff_preview._YAML_SAFE_LOADER is yaml.CSafeLoader
    else:  # pragma: no cover - production image has libyaml
        assert diff_preview._YAML_SAFE_LOADER is yaml.SafeLoader


def test_yaml_safe_load_helper_parses_nested_values():
    doc = diff_preview._yaml_safe_load("appspace:\n  version: 1.2.3\n")
    assert doc == {"appspace": {"version": "1.2.3"}}


def test_yaml_safe_load_helper_raises_yaml_error_on_garbage():
    try:
        diff_preview._yaml_safe_load(":\n  - [")
        assert False, "expected YAMLError"
    except yaml.YAMLError:
        pass


def test_no_direct_safe_load_left_in_diff_preview():
    """Every former yaml.safe_load site must go through the helper so the
    loader choice stays in one place."""
    import inspect
    src = inspect.getsource(diff_preview)
    # The helper itself may mention safe_load in a comment; the call form
    # yaml.safe_load( must be gone.
    assert "yaml.safe_load(" not in src
