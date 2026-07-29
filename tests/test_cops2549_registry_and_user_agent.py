"""Chart registry follows the version suffix, and outbound calls identify themselves.

COPS-2549, part 1. _run_one_diff took the OCI registry from the LIVE app spec
but the version from the PR, so a PR pointing an environment at a `-dev` chart
asked the release registry for a tag that only exists in the dev one. Live on
acme-config-prod PRs 3808 and 3809: red, blocking previews for charts that were
published correctly.

ArgoCD does not have this problem because every ApplicationSet derives the
repoURL from the version itself:

    {{if hasSuffix "-dev" .appspace.version}}helm-oci-dev{{else}}helm-oci-release{{end}}

Any environment in any of the three config repos may point at either kind of
package, so the rule is purely the suffix. It is applied to BOTH sides
independently: the main side is not safe either, because the live app can lag
behind main (not yet synced), and then the main-side pull fails the same way.

Part 2: outbound requests went out as Python-urllib/3.x, indistinguishable from
any other script in the logs.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m

DEV = "helm-oci-dev.repo.appspace.com"
REL = "helm-oci-release.repo.appspace.com"


# ── part 1: registry follows the version, per side ──────────────────────────

def test_dev_suffix_selects_the_dev_registry():
    assert m._registry_for_version("2602.2.13-rev1-cops-2548-dev") == DEV


def test_plain_version_selects_the_release_registry():
    assert m._registry_for_version("2603.0.12-rev1") == REL


def test_release_to_dev_transition_uses_a_different_registry_per_side():
    """The live app sits on release (the prod default) and the PR moves it to a
    dev chart. Each side must be pulled from its own registry."""
    main_rev, pr_rev = "2602.2.13-rev1", "2602.2.13-rev1-cops-2548-dev"
    assert m._registry_for_version(main_rev) == REL
    assert m._registry_for_version(pr_rev) == DEV


def test_dev_to_release_transition_is_handled_too():
    """The reverse: an environment being promoted off a dev chart."""
    main_rev, pr_rev = "2602.2.13-rev1-cops-2548-dev", "2602.2.13-rev1"
    assert m._registry_for_version(main_rev) == DEV
    assert m._registry_for_version(pr_rev) == REL


def test_registry_is_not_taken_from_the_live_app_anymore():
    """Regression guard: the pull must not reuse one registry for both sides."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "src",
                            "diff_preview.py")).read()
    i = src.index("pr_fut   = _pull_ex.submit(_ensure_chart,")
    window = src[i:i + 260]
    assert "_ensure_chart, registry, chart_name, pr_rev" not in window, (
        "the PR side still pulls from the live app's registry")
    assert "pr_registry" in window and "main_registry" in window, window


def test_empty_version_falls_back_to_release():
    """Defensive: never build an empty registry reference."""
    assert m._registry_for_version("") == REL
    assert m._registry_for_version(None) == REL


# ── part 2: User-Agent ──────────────────────────────────────────────────────

def test_user_agent_names_the_service_and_its_version():
    ua = m._user_agent()
    assert ua.startswith("AppspaceAcmeDiffPreview/")
    assert ua.split("/", 1)[1], "the version part must not be empty"


def test_http_helper_sets_the_user_agent(monkeypatch):
    seen = {}
    class FakeResp:
        status = 200
        def read(self): return b"{}"
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_urlopen(req, *a, **k):
        seen["ua"] = req.get_header("User-agent")
        return FakeResp()
    monkeypatch.setattr(m.urllib.request, "urlopen", fake_urlopen)
    m.http("GET", "https://example.com/x")
    assert seen["ua"] == m._user_agent()


def test_explicit_user_agent_is_not_overridden(monkeypatch):
    seen = {}
    class FakeResp:
        status = 200
        def read(self): return b"{}"
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_urlopen(req, *a, **k):
        seen["ua"] = req.get_header("User-agent")
        return FakeResp()
    monkeypatch.setattr(m.urllib.request, "urlopen", fake_urlopen)
    m.http("GET", "https://example.com/x", headers={"User-Agent": "custom/1"})
    assert seen["ua"] == "custom/1"
