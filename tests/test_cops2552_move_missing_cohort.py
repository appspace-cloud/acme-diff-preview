"""A move to a folder with no cohort config.yaml must block, not go green.

Regression found by testing v2.13.6 against live PR 3816, which reproduced
Andrew's original 3796 shape: an environment moved into
hardcoded/migration/weekly/ with NO cohort config.yaml alongside it.

The v2.13.5 identity-move fix pairs the delete and the add, which correctly
kills the false decommission. But pairing also removes the new path from the
new-environment candidate list, and the cohort guard added in v2.13.2 only
ever ran over that list. So the one case the guard exists for stopped being
checked: PR 3816 posted SUCCESSFUL with "110 resource(s) will change" and no
mention of the missing file. Merging it would have made the ApplicationSet
matrix yield zero Applications for a live production customer, silently
un-managing it.

A moved environment needs its destination cohort file exactly as much as a
brand-new one does.
"""
import sys, os
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m

OLD = "gcp/prod/private-cloud/gb1-b/weekly/pv-ukhsa-a/customer.yaml"
NEW = "gcp/prod/private-cloud/gb1-b/hardcoded/migration/weekly/pv-ukhsa-a/customer.yaml"
COHORT = "gcp/prod/private-cloud/gb1-b/hardcoded/migration/weekly/config.yaml"
PR_SHA = "beef0001"


def _fetch(monkeypatch, present):
    def fake(path, sha, repo=None):
        if path in present:
            return present[path], m.BB_OK
        return None, m.BB_NOT_FOUND
    monkeypatch.setattr(m, "_bb_fetch_status", fake)


@pytest.fixture(autouse=True)
def _clear():
    for d in (m._vf_cache, m._vf_inflight):
        d.clear()
    yield


def test_move_without_cohort_is_reported(monkeypatch):
    """The exact live shape of PR 3816."""
    _fetch(monkeypatch, {NEW: "---\nappspace:\n  customerName: ukhsa\n"})
    blocked = m._moves_missing_cohort({OLD: NEW}, PR_SHA, repo="acme-config-prod")
    assert len(blocked) == 1
    entry = blocked[0]
    assert entry["new"] == NEW
    assert entry["cohort"] == COHORT
    assert entry["env"] == "pv-ukhsa-a"


def test_move_with_cohort_present_is_fine(monkeypatch):
    _fetch(monkeypatch, {NEW: "---\n", COHORT: "---\n# placeholder\n"})
    assert m._moves_missing_cohort({OLD: NEW}, PR_SHA) == []


def test_transient_fetch_error_does_not_block(monkeypatch):
    """Only a genuine 404 is a stable fact. A blip must not block a PR."""
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda p, s, repo=None: (None, m.BB_ERROR))
    assert m._moves_missing_cohort({OLD: NEW}, PR_SHA) == []


def test_aec_paths_are_guarded_too(monkeypatch):
    """COPS-2689: the aec exemption expired. It was written when no aec
    ApplicationSet had a cohort generator; na4-a and na2-a now do, and the
    rest inherit it as they convert to the shared template. An aec move into
    a cohort folder with no config.yaml yields zero Applications exactly like
    a prod one, so it must block.

    na2-a is deliberately the example: this test previously asserted na2-a was
    exempt, and na2-a is the very spoke whose aec tree grew monthly/ and
    weekly/ cohorts and got the generator first."""
    old = "gcp/aec/private-cloud/na2-a/pv-x-a/customer.yaml"
    new = "gcp/aec/private-cloud/na2-a/moved/pv-x-a/customer.yaml"
    _fetch(monkeypatch, {new: "---\n"})
    blocked = m._moves_missing_cohort({old: new}, PR_SHA)
    assert len(blocked) == 1, blocked
    assert blocked[0]["cohort"] == "gcp/aec/private-cloud/na2-a/moved/config.yaml"


def test_aec_move_with_its_cohort_file_is_fine(monkeypatch):
    """Control for the test above: the guard blocks the MISSING file, not the
    aec tree itself. With the cohort file present the move passes."""
    old = "gcp/aec/private-cloud/na2-a/pv-x-a/customer.yaml"
    new = "gcp/aec/private-cloud/na2-a/moved/pv-x-a/customer.yaml"
    cohort = "gcp/aec/private-cloud/na2-a/moved/config.yaml"
    _fetch(monkeypatch, {new: "---\n", cohort: "---\n# placeholder\n"})
    assert m._moves_missing_cohort({old: new}, PR_SHA) == []


def test_non_identity_renames_are_ignored(monkeypatch):
    _fetch(monkeypatch, {})
    renames = {"gcp/prod/private-cloud/gb1-b/weekly/pv-x-a/notes.txt":
               "gcp/prod/private-cloud/gb1-b/hardcoded/notes.txt"}
    assert m._moves_missing_cohort(renames, PR_SHA) == []


def test_block_lines_state_cause_consequence_and_fix():
    entry = {"env": "pv-ukhsa-a", "old": OLD, "new": NEW, "cohort": COHORT}
    lines = m._moves_missing_cohort_lines([entry])
    joined = "\n".join(lines)
    assert COHORT in joined
    assert "zero Applications" in joined
    # It must name the move, so the reviewer sees this is not a new env.
    assert "pv-ukhsa-a" in joined
    # Every line must be safe to splice into the markdown comment.
    for l in lines:
        assert "\n" not in l


def test_process_pr_blocks_on_a_move_missing_its_cohort():
    """The guard must be wired into the comment/status path, not just exist."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "src",
                            "diff_preview.py")).read()
    body = src.replace("def _moves_missing_cohort(", "", 1)
    assert "_moves_missing_cohort(" in body, "guard is never called"
    assert "_moves_missing_cohort_lines(" in body.replace(
        "def _moves_missing_cohort_lines(", "", 1), "lines are never rendered"
    # It must feed the same structural flag that blocks the build status.
    i = src.index("new_env_structural=bool(")
    assert "moves_missing_cohort" in src[i - 1200:i + 200], (
        "a move missing its cohort must force the blocking status")
