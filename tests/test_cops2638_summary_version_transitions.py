"""COPS-2638: the Merge summary states every version transition with its
environments, on both surfaces.

The most common PR shape — a version bump — was invisible in the verdict
block. acme-config-prod #4036 summarised a fleet bump as "2 app(s)
change, nothing risk-flagged"; #4037 never said it bumps 2603.0.13 →
2603.1.14. The existing "environments jumping" line only fires for PURE
bumps (rollup_by_sig), so any PR mixing a bump with other changes — the
#4037 shape — lost the line entirely.

The general fact is DiffResult.version_change = (main_rev, pr_rev): the
chart targetRevision ArgoCD currently has versus the one the PR pins.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as dp  # noqa: E402

URL = "https://argocd.appspace.com/diff/acme-config-prod/4037/abc123"


def _changed(name="x", n=3, vc=None):
    hdrs = ["/apps/Deployment d-%s-%d" % (name, i) for i in range(n)]
    secs = [(h, "  image: acme/%s:%d" % (name, i)) for i, h in
            enumerate(hdrs)]
    r = dp.DiffResult("\n".join("--- %s" % h for h in hdrs), secs,
                      n, True, None, dp.OUT_DIFF, None)
    return r._replace(version_change=vc)


def _summary(out):
    return out.split("## \u2139\ufe0f Merge summary")[1].split("---")[0]


def _comment(results, **kw):
    return dp.format_comment("a" * 40, results, base_sha="b" * 40,
                             artifact_url=URL, **kw)


def test_mixed_bump_states_transition_and_envs(monkeypatch):
    """The #4037 shape: a bump plus unrelated changes. The old rollup
    line never fired here; the new line must."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    results = {
        "pv-myschroders-a-ms": _changed("ms", n=5,
                                        vc=("2603.0.13", "2603.1.14")),
        "pv-myschroders-a-ss": _changed("ss", n=4,
                                        vc=("2603.0.13", "2603.1.14")),
        "pv-other-a-glb": _changed("glb", n=2),
    }
    head = _summary(_comment(results))
    assert "`2603.0.13` \u2192 `2603.1.14`" in head
    assert "pv-myschroders-a" in head
    assert "environment(s) bump" in head


def test_envs_are_deduped_across_app_suffixes(monkeypatch):
    """ms+ss+glb of one environment is ONE environment bumping, listed
    once — operators count customers, not ArgoCD apps."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    results = {"pv-acme-a-%s" % s: _changed(s, vc=("1.0.0", "1.1.0"))
               for s in ("ms", "ss", "glb")}
    head = _summary(_comment(results))
    # A pure fleet bump may surface as the rollup's "jumping" line
    # instead of the general "bump" line — the point is ONE line, one
    # environment, never two wordings for the same transition.
    assert head.count("`1.0.0` \u2192 `1.1.0`") == 1
    assert "**1 environment(s)" in head


def test_two_transitions_get_two_lines(monkeypatch):
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    results = {
        "pv-a-ms": _changed("a", vc=("1.0.0", "1.1.0")),
        "pv-b-ms": _changed("b", vc=("2.0.0", "2.2.0")),
    }
    head = _summary(_comment(results))
    assert "`1.0.0` \u2192 `1.1.0`" in head
    assert "`2.0.0` \u2192 `2.2.0`" in head


def test_no_version_change_no_line(monkeypatch):
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    head = _summary(_comment({"pv-a-ms": _changed("a")}))
    assert "environment(s) bump" not in head


def test_downgrade_is_not_called_a_bump_and_names_the_versions(monkeypatch):
    """A downgrade keeps its REVIEW finding — now with the version pair
    it lacked — and must never also appear as a routine 'bump'."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    results = {"pv-a-ms": _changed("a", vc=("2603.1.14", "2603.0.13"))}
    head = _summary(_comment(results))
    assert "environment(s) bump" not in head
    assert "downgrade" in head.lower()
    assert "`2603.1.14` \u2192 `2603.0.13`" in head


def test_the_page_surface_carries_the_line_too(monkeypatch):
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    results = {"pv-a-ms": _changed("a", vc=("1.0.0", "1.1.0"))}
    out = _comment(results,
                   profile=dp.RenderProfile("page", is_complete_record=True,
                                            inline_diffs=True))
    assert "`1.0.0` \u2192 `1.1.0`" in _summary(out)
