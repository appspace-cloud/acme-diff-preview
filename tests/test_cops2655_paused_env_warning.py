"""COPS-2655: a PR against a PAUSED environment must not read as routine.

Reproduced live on pv-qa88-a before any of this was written. With
`appspace.autosync: false` set and verified in the cluster (all three
Applications showing `automated: {enabled: false}`), a PR changing only
cicd-versions.yaml rendered:

    ✅ **Routine** — nothing dangerous detected
    | pv-qa88-a-ms | ⚠️ changed | 3 | — |
    **Status:** ⚠️ 3 resource(s) will change

Zero would have changed. The comment said nothing about the pause: no
"paused", no "autosync", no "will not be applied".

The existing warning comes from _summarize_appspace_state_changes, which
only runs for IDENTITY files (customer.yaml / config.yaml). A version bump
touches cicd-versions.yaml, so the whole path is skipped and no warning
exists anywhere else.

Why it matters more than its rarity suggests: whoever pauses an
environment knows they paused it, but whoever bumps a service version
across the fleet is usually someone else, has no idea that environment is
frozen, and gets a green comment saying the change will land. The failure
is silent and slow -- drift accumulates and surfaces weeks later as "why
is this environment on an old version?".
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_preview as dp  # noqa: E402

ENV_DIR = "gcp/qa/private-cloud/ap1/custom/pv-qa88-a"
IDENTITY = f"{ENV_DIR}/customer.yaml"
APPS = ["pv-qa88-a-glb", "pv-qa88-a-ms", "pv-qa88-a-ss"]
URL = "https://argocd.appspace.com/diff/acme-config-dev/7087/750f650dac14"

# The real repo has hierarchical defaults named config.yaml at every
# ancestor level. They appear in path_map and must never be mistaken for an
# environment's own identity file (v2.5.7 note in _decommission_candidates).
ANCESTOR = "gcp/qa/config.yaml"

PATH_MAP = {
    IDENTITY: [f"argocd/{a}" for a in APPS],
    ANCESTOR: [f"argocd/{a}" for a in APPS] + ["argocd/pv-other-b-ms"],
}


def _paused_yaml(monkeypatch, paused_paths):
    """Stub the identity-file read: {path: is_paused}."""

    def fake(path, sha, repo=None):
        return {"appspace.autosync": "false"} if path in paused_paths else {}

    monkeypatch.setattr(dp, "_flat_yaml_cached", fake)


def _results(n_changed=1):
    out = {}
    for i, a in enumerate(APPS):
        if i < n_changed:
            hdrs = [f"/apps/Deployment d{j}" for j in range(3)]
            out[a] = dp.DiffResult(
                "\n".join("--- %s" % h for h in hdrs),
                [(h, "  image: acme/ms:1.115.0") for h in hdrs],
                3, True, None, dp.OUT_DIFF, None)
        else:
            out[a] = dp.DiffResult("", [], 0, False, None, dp.OUT_NO_DIFF, None)
    return out


def _comment(monkeypatch, paused=True, results=None):
    _paused_yaml(monkeypatch, {IDENTITY} if paused else set())
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    paused_apps = dp._paused_apps_for(
        list((results or _results()).keys()), PATH_MAP, "s" * 40,
        repo="acme-config-dev")
    return dp.format_comment("f" * 40, results or _results(), base_sha="b" * 40,
                             artifact_url=URL, paused_apps=paused_apps)


# -- resolving an app to its own environment --------------------------------

def test_an_app_resolves_to_its_own_environment(monkeypatch):
    _paused_yaml(monkeypatch, {IDENTITY})
    got = dp._paused_apps_for(APPS, PATH_MAP, "s" * 40, repo="acme-config-dev")
    assert set(got) == set(APPS), f"expected all three apps paused, got {got}"


def test_an_ancestor_defaults_file_is_never_treated_as_an_identity(monkeypatch):
    """gcp/qa/config.yaml is a defaults level shared by many environments.
    Reading autosync from it would mark unrelated environments as paused --
    the v2.5.7 lesson, where an ancestor-based rule silently swallowed every
    new environment."""
    _paused_yaml(monkeypatch, {ANCESTOR})
    got = dp._paused_apps_for(APPS, PATH_MAP, "s" * 40, repo="acme-config-dev")
    assert not got, (
        f"a shared defaults file marked apps as paused: {got}")


def test_an_unpaused_environment_yields_nothing(monkeypatch):
    _paused_yaml(monkeypatch, set())
    assert not dp._paused_apps_for(APPS, PATH_MAP, "s" * 40,
                                   repo="acme-config-dev")


def test_a_read_failure_does_not_mark_an_environment_paused(monkeypatch):
    """Fail toward the current behaviour. Wrongly claiming an environment is
    frozen would send someone chasing a pause that does not exist."""

    def boom(path, sha, repo=None):
        raise RuntimeError("bitbucket down")

    monkeypatch.setattr(dp, "_flat_yaml_cached", boom)
    assert dp._paused_apps_for(APPS, PATH_MAP, "s" * 40,
                               repo="acme-config-dev") == set()


# -- what the reviewer sees -------------------------------------------------

def test_the_verdict_is_not_routine_for_a_paused_environment(monkeypatch):
    """THE gate. The live comment said 'Routine - nothing dangerous
    detected' for a change that would not be applied at all."""
    out = _comment(monkeypatch, paused=True)
    head = out.split("Changeset overview")[0]
    assert "Routine" not in head or "paused" in head.lower(), (
        "a change that will not be applied still rendered as Routine:\n"
        + head[:600])


def test_the_paused_environment_is_named_in_the_summary(monkeypatch):
    out = _comment(monkeypatch, paused=True)
    assert "paused" in out.lower(), "the comment never mentions the pause"
    assert "pv-qa88-a" in out
    head = out.split("Changeset overview")[0]
    assert "PAUSED" in head, "the verdict must name the pause before any detail"


def test_the_overview_row_is_marked(monkeypatch):
    """The row carries the resource count that will not be applied, so the
    mark belongs next to it."""
    out = _comment(monkeypatch, paused=True)
    # _results() marks the first app alphabetically as changed.
    changed = sorted(APPS)[0]
    row = [ln for ln in out.splitlines()
           if ln.startswith("|") and changed in ln]
    assert row, "no overview row for the changed app"
    assert "paused" in row[0].lower() or "\u23f8" in row[0], (
        f"the row does not show the pause: {row[0]}")


def test_the_status_line_does_not_promise_the_change_will_apply(monkeypatch):
    """'3 resource(s) will change' was false. Zero would have."""
    out = _comment(monkeypatch, paused=True)
    status = [ln for ln in out.splitlines() if ln.startswith("**Status:**")]
    assert status, "no status line"
    assert "paused" in status[0].lower() or "not applied" in status[0].lower(), (
        f"the status line still promises the change lands: {status[0]}")


# -- the 100% case must not change ------------------------------------------

def test_an_unpaused_environment_renders_exactly_as_before(monkeypatch):
    """Every PR in the repo today is this case. New noise here would be a
    worse defect than the one being fixed."""
    _paused_yaml(monkeypatch, set())
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    with_arg = dp.format_comment("f" * 40, _results(), base_sha="b" * 40,
                                 artifact_url=URL, paused_apps=set())
    without = dp.format_comment("f" * 40, _results(), base_sha="b" * 40,
                                artifact_url=URL)
    assert with_arg == without, (
        "passing an empty paused set changed the output")
    assert "paused" not in with_arg.lower()


def test_a_paused_environment_with_no_changes_says_nothing(monkeypatch):
    """A paused environment that this PR does not touch is not news."""
    results = {a: dp.DiffResult("", [], 0, False, None, dp.OUT_NO_DIFF, None)
               for a in APPS}
    out = _comment(monkeypatch, paused=True, results=results)
    assert "paused" not in out.lower(), (
        "a paused environment with nothing to apply should not be flagged")
