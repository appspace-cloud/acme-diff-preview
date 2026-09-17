"""COPS-2723: the bundled argocd CLI must track the fleet's Argo CD minor.

The fleet is moving to Argo CD 3.5.2 (argocd-agent v0.10.0 vendors argo-cd
v3.5.1, so principal and all 25 agents already run 3.5.x types). This service
calls the API server with `app list` / `app get` / `app get --hard-refresh`,
so its CLI should not be left a minor behind.

Verified live before the bump: a v3.5.2 CLI against the 3.4.8 hub returned a
byte-identical `app get -o json` and the same 1063 apps from `app list`, so the
bump is safe in both directions while the rollout is half done.

Deliberately NOT asserted here: the helm binary. `HELM_VERSION` is a separate
ARG and the argocd CLI takes no part in rendering, so this bump cannot change
`helm template` output and needs no MAIN_RENDER_CACHE_SALT bump.
"""
import re
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"


def _arg(name):
    m = re.search(rf"^ARG\s+{name}=(\S+)\s*$", DOCKERFILE.read_text(), re.M)
    assert m, f"{name} not found in Dockerfile"
    return m.group(1)


def _parts(v):
    return tuple(int(x) for x in v.lstrip("v").split("-")[0].split(".")[:3])


def test_argocd_cli_is_at_least_3_5():
    """A 3.4 CLI against the 3.5 fleet is the skew this ticket removes."""
    assert _parts(_arg("ARGOCD_VERSION")) >= (3, 5, 0), (
        f"ARGOCD_VERSION is {_arg('ARGOCD_VERSION')}; the fleet runs Argo CD 3.5.x"
    )


def test_helm_version_untouched_by_this_bump():
    """Guards the reasoning above: if helm moves, the render cache salt must
    be reconsidered (RELEASING.md), which this change deliberately does not do."""
    assert _parts(_arg("HELM_VERSION"))[0] == 3, (
        "HELM_VERSION left the 3.x line; re-read the render-cache salt section "
        "of RELEASING.md before shipping"
    )
