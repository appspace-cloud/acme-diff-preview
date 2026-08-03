"""COPS-2580: CodeQL flagged py/path-injection twice in diff_ui.py's
load_artifact (both alerts trace to the same _artifact_path() call).

The data flow is real: CodeQL's source is self.path in the health server's
do_GET handler, genuinely attacker-controlled. But two layers of anchored
regex validation already sit between that source and the filesystem sink
(parse_request_path's per-segment check, then _validate() inside
_artifact_path), and neither _REPO_RE, _PR_RE nor _SHA_RE ever allows a "/"
or a leading "." in any matched string, so no traversal is actually
possible today.

This is a false positive caused by a known CodeQL limitation: manual
regex.match() validation is not recognized as a sanitizer barrier by the
default py/path-injection query. Fixed by adding the exact idiom CodeQL's
own documentation recommends (normalize, then check the result stays under
the root folder) as a second, independent guard -- redundant today, but a
real protection if the regexes above are ever loosened by a future change,
and it makes the sanitizer visible to static analysis so the alert closes
legitimately instead of needing a manual dismissal.
"""
import os
import sys

os.environ.setdefault("BB_USER", "test-user")
os.environ.setdefault("BB_TOKEN", "test-token")
os.environ.setdefault("ARGOCD_PASS", "test-pass")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_ui as m


def test_assert_within_base_dir_allows_normal_path(tmp_path):
    base = str(tmp_path)
    path = os.path.join(base, "acme-config-prod__3837.json")
    assert m._assert_within_base_dir(path, base) == path


def test_assert_within_base_dir_blocks_direct_traversal(tmp_path):
    """Exercises the guard in isolation, bypassing _validate entirely, so
    it is proven correct on its own merits -- not just as a side effect of
    the regex validation upstream."""
    base = str(tmp_path)
    evil = os.path.join(base, "..", "escaped.json")
    try:
        m._assert_within_base_dir(evil, base)
        assert False, "expected ValueError for a path escaping base_dir"
    except ValueError:
        pass


def test_assert_within_base_dir_blocks_absolute_escape(tmp_path):
    base = str(tmp_path)
    try:
        m._assert_within_base_dir("/etc/passwd", base)
        assert False, "expected ValueError for an absolute path outside base_dir"
    except ValueError:
        pass


def test_assert_within_base_dir_allows_base_dir_itself(tmp_path):
    base = str(tmp_path)
    assert m._assert_within_base_dir(base, base) == base
