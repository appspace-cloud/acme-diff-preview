"""COPS-2702: the ArgoCD API endpoint is configurable and separate from the
public host, and plaintext can never be aimed at the public internet.

Why these tests exist. The service runs inside the Argo CD hub cluster but
called the PUBLIC https://argocd.appspace.com for every API call: measured on
the live hub, one `argocd app list -o json` is 128 MB of JSON / 47 MB on the
wire, this process issues ~377/day, and that is 99.3% of ALL traffic reaching
that load balancer (real browsers are 0.7%). Pointing the API at the in-cluster
Service takes that off Cloud NAT and off the public ALB.

Two traps this pins down, both found by auditing rather than in production:

  * ARGOCD_SERVER fed BOTH the API calls and one human-facing link. Moving the
    API in-cluster without splitting it would post an unroutable
    https://argocd-server.argocd.svc:80 into a Bitbucket build status.
  * ARGOCD_PLAINTEXT with a public ARGOCD_SERVER would POST ARGOCD_PASS in
    CLEARTEXT to the public host on the very first startup login, so the guard
    has to fire at import — strictly before any login can run.

Note on style: the import-time guard is exercised in a SUBPROCESS on purpose.
Re-importing diff_preview inside the suite swaps sys.modules out from under
every other test module that already holds a reference to it (14 unrelated
failures when this file first did that), so in-process cases monkeypatch the
module attributes instead, which is what the rest of the suite does.
"""
import os
import subprocess
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import diff_preview as mod  # noqa: E402


CHART = os.path.abspath(os.path.join(os.path.dirname(__file__), "..",
                                     "charts", "acme-diff-preview"))


def _read_chart(relpath):
    """Read a chart file for the wiring assertions.

    Text assertions rather than `helm template`: nothing else in this suite
    shells out to helm for the service's OWN chart, and CI is not guaranteed a
    helm binary. What matters here is only that the template names the values,
    which a string check settles.
    """
    with open(os.path.join(CHART, relpath), encoding="utf-8") as fh:
        return fh.read()


def _read_source(name):
    """Read a module's source for the sentinel assertions below.

    A context manager rather than open(...).read() so the handle is closed
    deterministically even when an assertion in the caller fails.
    """
    with open(os.path.join(SRC, name), encoding="utf-8") as fh:
        return fh.read()


def _import_in_subprocess(**env):
    """Import diff_preview in a clean interpreter with a specific environment.

    Returns (returncode, combined output). Used for anything decided at import.
    """
    child = dict(os.environ)
    child.pop("ARGOCD_SERVER", None)
    child.pop("ARGOCD_PLAINTEXT", None)
    child.pop("ARGOCD_WEB_HOST", None)
    for k, v in env.items():
        child[k] = v
    for k, v in (("BB_USER", "test"), ("BB_TOKEN", "test"),
                 ("ARGOCD_PASS", "test"),
                 ("JFROG_WEBHOOK_SECRET", "testsecret")):
        child.setdefault(k, v)
    probe = (
        "import diff_preview as m; "
        "print(m.ARGOCD_SERVER, m.ARGOCD_WEB_HOST, m.ARGOCD_PLAINTEXT)"
    )
    r = subprocess.run([sys.executable, "-c", probe],
                       cwd=SRC, env=child, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


# ── defaults: an image-only deploy must behave exactly as before ─────────────
def test_defaults_keep_the_public_host_and_tls():
    rc, out = _import_in_subprocess()
    assert rc == 0, out
    assert out.split() [:3] == ["argocd.appspace.com", "argocd.appspace.com",
                                "False"], out


def test_default_transport_flags_are_unchanged():
    """No --plaintext and no --insecure at defaults: the wire behaviour of
    2.97.0 exactly."""
    assert mod.ARGOCD_PLAINTEXT is False
    assert mod._auth_flags() == ["--server", mod.ARGOCD_SERVER, "--grpc-web"]
    assert "--insecure" not in mod._auth_flags()


# ── the in-cluster configuration the Deployment will set ─────────────────────
def test_in_cluster_config_adds_plaintext_to_cli(monkeypatch):
    monkeypatch.setattr(mod, "ARGOCD_SERVER", "argocd-server.argocd.svc:80")
    monkeypatch.setattr(mod, "ARGOCD_PLAINTEXT", True)
    flags = mod._auth_flags()
    assert flags == ["--server", "argocd-server.argocd.svc:80", "--grpc-web",
                     "--plaintext"]
    # --insecure is a different thing and must never appear: it disables
    # certificate verification, which is not what a plaintext port needs.
    assert "--insecure" not in flags


def test_in_cluster_config_is_accepted_at_import():
    rc, out = _import_in_subprocess(
        ARGOCD_SERVER="argocd-server.argocd.svc:80", ARGOCD_PLAINTEXT="1")
    assert rc == 0, out
    assert "argocd-server.argocd.svc:80" in out
    assert "True" in out


# ── the credential-leak guard ────────────────────────────────────────────────
def test_plaintext_against_public_host_refuses_to_start():
    """Must fail at IMPORT: otherwise the startup login POSTs ARGOCD_PASS in
    cleartext to the public host on attempt 1, and the retry loop re-sends it."""
    rc, out = _import_in_subprocess(ARGOCD_SERVER="argocd.appspace.com",
                                    ARGOCD_PLAINTEXT="1")
    assert rc != 0, "import should have refused to start"
    assert "cleartext" in out
    assert "ARGOCD_PLAINTEXT" in out


def test_plaintext_against_an_arbitrary_public_name_also_refuses():
    rc, out = _import_in_subprocess(ARGOCD_SERVER="evil.example.com:80",
                                    ARGOCD_PLAINTEXT="1")
    assert rc != 0, out
    assert "cleartext" in out


def test_plaintext_flag_spellings():
    for val in ("1", "true", "TRUE", "yes", "on"):
        rc, out = _import_in_subprocess(
            ARGOCD_SERVER="argocd-server.argocd.svc:80", ARGOCD_PLAINTEXT=val)
        assert rc == 0 and "True" in out, (val, out)
    for val in ("", "0", "false", "no", "off"):
        rc, out = _import_in_subprocess(ARGOCD_PLAINTEXT=val)
        assert rc == 0 and "False" in out, (val, out)


# ── the session URL follows the transport ────────────────────────────────────
def test_session_url_is_the_single_scheme_seam():
    """The REST login is the only place a session URL is built, so every
    renewal path (startup retry, proactive TTL refresh, reactive re-login)
    inherits the scheme from there. One seam means nothing can half-migrate."""
    src = _read_source("diff_preview.py")
    assert 'scheme = "http" if ARGOCD_PLAINTEXT else "https"' in src
    assert 'url  = f"{scheme}://{ARGOCD_SERVER}/api/v1/session"' in src
    assert src.count("/api/v1/session") == 1


# ── the split is real, not cosmetic ──────────────────────────────────────────
def test_only_the_web_host_reaches_user_visible_urls():
    """Sentinel: ARGOCD_SERVER must never be interpolated into a URL a human or
    an external system receives. The audit found exactly one such site
    (post_build_status's fallback link); this keeps the count at zero."""
    src = _read_source("diff_preview.py")
    assert 'f"https://{ARGOCD_WEB_HOST}"' in src
    assert 'f"https://{ARGOCD_SERVER}"' not in src


def test_dev_hard_refresh_moved_in_step():
    """dev_hard_refresh.py reads the SAME ARGOCD_SERVER name and used to speak
    https only, so a future 'copy the Deployment env into the CronJob' would
    have made it TLS against a plaintext port. Its own docstring mandates that
    both copies of the login move together."""
    src = _read_source("dev_hard_refresh.py")
    assert "ARGOCD_PLAINTEXT" in src
    assert '"--plaintext"' in src
    assert 'scheme   = "http" if PLAINTEXT else "https"' in src
    assert "cleartext" in src


def test_chart_wires_the_endpoint_values_into_the_container():
    """The code being env-driven is useless if the chart cannot set the env.

    This is the gap the integration review caught: values.yaml had declared
    `argocd.server` since the chart was created and NOTHING consumed it — the
    deployment template emitted no ARGOCD_SERVER and the code hardcoded the
    public host. Exactly the declared-but-inert pattern audited in COPS-2698.
    Without this wiring the change ships unusable: there is no extraEnv or
    envFrom escape hatch in this chart, so an operator has no way to opt in.
    """
    dep = _read_chart("templates/deployment.yaml")
    for name, source in (("ARGOCD_SERVER", ".Values.argocd.server"),
                         ("ARGOCD_PLAINTEXT", ".Values.argocd.plaintext"),
                         ("ARGOCD_WEB_HOST", ".Values.argocd.webHost")):
        assert f"name: {name}" in dep, f"{name} is not emitted by the chart"
        assert source in dep, f"{name} is not driven by {source}"


def test_chart_defaults_keep_todays_behaviour():
    """Defaults must describe the public host over TLS.

    An image-and-chart upgrade with no values change has to be inert: same
    endpoint, no plaintext, and webHost empty so the code default applies.
    """
    values = _read_chart("values.yaml")
    assert "server: argocd.appspace.com" in values
    assert "plaintext: false" in values
    assert "webHost: ''" in values


def test_chart_documents_the_short_svc_form():
    """The hub runs a custom cluster domain, so the canonical
    argocd-server.argocd.svc.cluster.local returns NXDOMAIN there (verified
    live from both replicas). An operator copying the FQDN habit would get a
    service that cannot resolve its API, so the values file has to say it."""
    values = _read_chart("values.yaml")
    assert "argocd-server.argocd.svc:80" in values
    assert "NXDOMAIN" in values
