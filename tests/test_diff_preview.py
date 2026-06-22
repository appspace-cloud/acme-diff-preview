"""Basic unit tests for diff_preview.py — syntax and key function checks."""
import ast
import os
import sys

SRC = os.path.join(os.path.dirname(__file__), "..", "src", "diff_preview.py")


def test_syntax():
    """diff_preview.py must parse without syntax errors."""
    with open(SRC) as f:
        source = f.read()
    tree = ast.parse(source)
    assert tree is not None


def test_key_functions_defined():
    """Core functions must be present in the source."""
    with open(SRC) as f:
        source = f.read()
    tree = ast.parse(source)
    func_names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for required in [
        "process_pr",
        "main_iteration",
        "argocd_diff",
        "get_open_prs",
        "upsert_comment",
        "generate_ai_summary",
        "discover_path_app_map",
    ]:
        assert required in func_names, f"Missing function: {required}"


def test_wake_event_defined():
    """_wake threading.Event must be present (COPS-2497 webhook support)."""
    with open(SRC) as f:
        source = f.read()
    assert "_wake" in source
    assert "threading.Event" in source


def test_seen_lock_defined():
    """_seen_lock must be present (thread-safety for concurrent PR processing)."""
    with open(SRC) as f:
        source = f.read()
    assert "_seen_lock" in source


def test_no_gcloud_calls():
    """diff_preview.py must not call gcloud (credentials come from ESO)."""
    with open(SRC) as f:
        source = f.read()
    assert "gcloud" not in source
