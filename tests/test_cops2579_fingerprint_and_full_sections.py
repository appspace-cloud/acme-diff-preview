"""COPS-2579: DiffResult.sections was hard-capped to AI_MAX_SECTIONS_PER_APP
(10) at diff time, discarding every resource past #10 before the comment or
the diff-UI page could ever show it. Fixed by storing up to the much more
generous FULL_SECTIONS_MAX_PER_APP, and by computing a fingerprint of the
FULL section list so identical-diff apps can be grouped later in
format_comment (COPS-2579 item 2).
"""
import os
import sys

os.environ.setdefault("BB_USER", "test-user")
os.environ.setdefault("BB_TOKEN", "test-token")
os.environ.setdefault("ARGOCD_PASS", "test-pass")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m


def _fake_sections(n, prefix="svc"):
    return [(f"/apps/Deployment {prefix}{i}", f"-old{i}\n+new{i}\n")
            for i in range(n)]


def test_package_sections_stores_more_than_ten():
    """The historical bug: only 10 of 67 sections used to survive into
    DiffResult.sections. Now all 67 must survive (well under the memory
    cap)."""
    sections = _fake_sections(67)
    _, stored, _, _, _ = m._package_sections(sections)
    assert len(stored) == 67


def test_package_sections_respects_memory_cap():
    """Storage is generous but not unbounded: past
    FULL_SECTIONS_MAX_PER_APP, stored sections are still capped."""
    cap = m.FULL_SECTIONS_MAX_PER_APP
    sections = _fake_sections(cap + 50)
    _, stored, _, _, _ = m._package_sections(sections)
    assert len(stored) == cap


def test_fingerprint_identical_content_same_hash_regardless_of_order():
    a = _fake_sections(5, prefix="svc")
    b = list(reversed(a))
    assert m._fingerprint_sections(a) == m._fingerprint_sections(b)


def test_fingerprint_different_content_different_hash():
    a = _fake_sections(5, prefix="svc")
    b = _fake_sections(5, prefix="other")
    assert m._fingerprint_sections(a) != m._fingerprint_sections(b)


def test_package_sections_returns_fingerprint():
    sections = _fake_sections(3)
    _, _, _, _, fp = m._package_sections(sections)
    assert isinstance(fp, str) and len(fp) == 64  # sha256 hex digest


def test_package_sections_fingerprint_stable_across_two_apps_same_change():
    """Two different apps whose diff is byte-for-byte the same change
    (the acme-config-prod PR #3837 shape) must fingerprint identically,
    independent of section order."""
    app_a_sections = _fake_sections(67)
    app_b_sections = list(reversed(_fake_sections(67)))
    _, _, _, _, fp_a = m._package_sections(app_a_sections)
    _, _, _, _, fp_b = m._package_sections(app_b_sections)
    assert fp_a == fp_b


def test_package_sections_fingerprint_differs_for_different_apps():
    app_a_sections = _fake_sections(67, prefix="svc")
    app_c_sections = _fake_sections(3, prefix="tiny")
    _, _, _, _, fp_a = m._package_sections(app_a_sections)
    _, _, _, _, fp_c = m._package_sections(app_c_sections)
    assert fp_a != fp_c


# ── Property-based test (COPS-2579 scope: grouping must be order-independent) ──
import random

from hypothesis import given, settings, strategies as st

settings.register_profile("suite", deadline=None)
settings.load_profile("suite")

_header_st = st.text(min_size=1, max_size=40).map(lambda s: f"/apps/Deployment {s}")
_body_st = st.text(min_size=0, max_size=200)
_section_st = st.tuples(_header_st, _body_st)
_sections_list_st = st.lists(_section_st, min_size=0, max_size=20, unique_by=lambda s: s[0])


@given(sections=_sections_list_st, seed=st.integers(min_value=0, max_value=10_000))
def test_fingerprint_is_order_independent(sections, seed):
    """Any set of (header, body) pairs must fingerprint the same regardless
    of the order they arrive in -- this is the invariant format_comment's
    grouping relies on: two apps whose diff workers finished in a
    different order, but produced the identical change, must land in the
    same group."""
    shuffled = list(sections)
    random.Random(seed).shuffle(shuffled)
    assert m._fingerprint_sections(sections) == m._fingerprint_sections(shuffled)


@given(sections=_sections_list_st, extra=_section_st)
def test_fingerprint_changes_when_content_differs(sections, extra):
    """Adding one more distinguishing section must never collide with the
    original fingerprint (the grouping must not silently merge apps whose
    diffs actually differ)."""
    if extra in sections:
        return  # not a real difference, skip
    with_extra = sections + [extra]
    assert m._fingerprint_sections(sections) != m._fingerprint_sections(with_extra)
