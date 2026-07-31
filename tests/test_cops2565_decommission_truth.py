"""Environment decommission must not promise a cleanup that never happens (COPS-2565 step 2).

What the comment says today
---------------------------
When a PR deletes an environment's identity file, the comment shows:

    # ENVIRONMENT DECOMMISSION
    `pv-foo-a` is being deleted by this PR ...
    - Resources that will be removed: 302 total - 45 Deployment, 45 Service, ...

What actually happens
---------------------
Every ApplicationSet sets `syncPolicy.preserveResourcesOnDeletion: true` (44
occurrences across the units). When the git generator stops yielding an
environment, the ApplicationSet controller deletes the *Application* but does
NOT cascade-delete the resources it created. The workloads keep running.

COPS-2539 built the opt-in gate for this and its own comment in
argocd-apps-pv-qa states the consequence plainly: without the finalizer,
removing a customer.yaml "silently abandons every resource the charts
created". Opting in with `appspace.decommission: true` templates ArgoCD's
cascade finalizer onto that environment's Applications, which (verified
2026-07-27) does survive preserveResourcesOnDeletion.

Measured on 2026-07-31: the gate exists only in the `argocd-apps-pv-qa` pilot
unit, and **zero** environments across acme-config-dev, -stage and -prod have
opted in. So for every environment today, the comment states the exact
opposite of what will happen, on the single most destructive and least
reversible operation the tool reports, and the reviewer is left with orphaned
Deployments, Services, PVCs and load balancers that nobody knows about.

The fix reads `appspace.decommission` from the environment's own customer.yaml
at the base sha. That file is already fetched, so this needs no ApplicationSet
read, no ownerReferences mapping and no new RBAC.
"""
import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m

IDENT = "gcp/prod/private-cloud/eu1-b/weekly/pv-foo-c/customer.yaml"
OPTED_IN = "---\nappspace:\n  customerName: foo\n  decommission: true\n"
NOT_OPTED_IN = "---\nappspace:\n  customerName: foo\n  version: 1.0.0\n"


def _candidate():
    return {"env_name": "pv-foo-c", "identity_file": IDENT,
            "apps": ["pv-foo-c-ms"], "env_dir": os.path.dirname(IDENT)}


def _fetch(main_content):
    """Identity file gone at the PR sha, present at main with given content."""
    def fake(path, sha, repo=None):
        if sha == "prsha":
            return (None, m.BB_NOT_FOUND)
        return (main_content, m.BB_OK)
    return fake


def test_orphaning_is_stated_when_the_env_did_not_opt_in(monkeypatch):
    """The default and, as of today, the only real case. The reviewer must be
    told the resources SURVIVE and need manual cleanup."""
    monkeypatch.setattr(m, "_bb_fetch_status", _fetch(NOT_OPTED_IN))
    monkeypatch.setattr(m, "_render_main_side_resources",
                        lambda app, sha: {("apps/Deployment", "ns", "web"): {},
                                          ("v1/Service", "ns", "web"): {}})
    lines, envs = m._evaluate_env_decommissions([_candidate()], "prsha", "mainsha")
    body = "\n".join(lines)
    assert envs == ["pv-foo-c"]
    assert "will be removed" not in body, "states a cleanup that will not happen"
    low = body.lower()
    assert "orphan" in low or "keep running" in low or "left running" in low, body
    assert "decommission: true" in body, "must name the flag that changes this"


def test_deletion_is_stated_when_the_env_opted_in(monkeypatch):
    """With the COPS-2539 gate the resources really are cascade-deleted, so the
    original destructive wording is correct and must survive."""
    monkeypatch.setattr(m, "_bb_fetch_status", _fetch(OPTED_IN))
    monkeypatch.setattr(m, "_render_main_side_resources",
                        lambda app, sha: {("apps/Deployment", "ns", "web"): {}})
    body = "\n".join(m._evaluate_env_decommissions([_candidate()], "prsha", "mainsha")[0])
    assert "will be removed" in body
    assert "orphan" not in body.lower()


def test_unreadable_identity_file_does_not_claim_deletion(monkeypatch):
    """If the base-side file cannot be read we do not know which side we are
    on. Never guess towards the reassuring answer: orphaning is the default
    everywhere today, so an unknown must not read as a clean cleanup."""
    def fake(path, sha, repo=None):
        if sha == "prsha":
            return (None, m.BB_NOT_FOUND)
        return (None, m.BB_ERROR)
    monkeypatch.setattr(m, "_bb_fetch_status", fake)
    monkeypatch.setattr(m, "_render_main_side_resources", lambda app, sha: {})
    body = "\n".join(m._evaluate_env_decommissions([_candidate()], "prsha", "mainsha")[0])
    assert "will be removed" not in body


def test_the_decommission_warning_itself_still_fires(monkeypatch):
    """Whatever the wording, the block must still shout. This is a destructive
    change and the headline is the point."""
    monkeypatch.setattr(m, "_bb_fetch_status", _fetch(NOT_OPTED_IN))
    monkeypatch.setattr(m, "_render_main_side_resources", lambda app, sha: {})
    body = "\n".join(m._evaluate_env_decommissions([_candidate()], "prsha", "mainsha")[0])
    assert "ENVIRONMENT DECOMMISSION" in body
    assert "pv-foo-c" in body


def test_a_false_positive_still_warns_about_nothing(monkeypatch):
    """Unchanged contract: if the identity file is NOT gone at the PR sha,
    say nothing at all."""
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda path, sha, repo=None: (NOT_OPTED_IN, m.BB_OK))
    lines, envs = m._evaluate_env_decommissions([_candidate()], "prsha", "mainsha")
    assert (lines, envs) == ([], [])


# ── step 4, reduced: kubeVersion ────────────────────────────────────────────

def test_kube_version_matches_the_real_clusters():
    """Verified 2026-07-31: every GKE cluster in appspace-cloud and
    appspace-devops runs 1.35.x. Rendering against 1.30.0 was five minors
    behind. All clusters share one version, so a per-cluster lookup would buy
    nothing over a single correct constant."""
    major, minor = m.KUBE_VERSION.split(".")[:2]
    assert (int(major), int(minor)) >= (1, 35), m.KUBE_VERSION


def test_no_chart_relies_on_helm_capabilities_yet():
    """The reason a single constant is enough. Helm's .Capabilities are famously
    unreliable when rendering outside the target cluster, so the day a chart
    starts branching on them, the constant stops being sufficient and this
    guard is the reminder to revisit it (see COPS-2565)."""
    charts = os.path.expanduser("~/gitprojects/acme-components/helm-charts")
    if not os.path.isdir(charts):
        import pytest
        pytest.skip("acme-components not checked out")
    hits = []
    for root, _dirs, files in os.walk(charts):
        for fn in files:
            if not fn.endswith((".yaml", ".tpl")):
                continue
            p = os.path.join(root, fn)
            try:
                body = open(p, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            if ".Capabilities" in body or "semverCompare" in body:
                hits.append(os.path.relpath(p, charts))
    assert not hits, (
        "a chart now branches on Helm Capabilities/KubeVersion, so a single "
        f"hardcoded KUBE_VERSION is no longer safe: {hits}")
