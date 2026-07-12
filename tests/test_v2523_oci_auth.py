"""v2.5.23 — OCI pull must see the credentials written by helm registry login.

Production incident (first exercised 2026-07-12, latent since v2.5.19 R1):
_helm_login() writes credentials to the DEFAULT helm registry config, but
R1 gave each pull an isolated HELM_REGISTRY_CONFIG pointing at a fresh,
empty file — so every pull ran unauthenticated and the private registry
answered 403. Masked locally by ambient docker/helm credentials; surfaced
on the first real PRs after the 2.5.15 -> 2.5.2x production jump.

The blob-store race R1 guards against (helm #8059) lives in the CACHE
homes (mutable blob/index state). The registry config is written only at
login and read-only during pulls, so sharing it is safe — and required.
"""
import os
import sys

os.environ.setdefault("BB_USER", "test-user")
os.environ.setdefault("BB_TOKEN", "test-token")
os.environ.setdefault("ARGOCD_PASS", "test-pass")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m


def test_pull_env_shares_registry_config_with_login(monkeypatch, tmp_path):
    """The env passed to `helm pull` must NOT override HELM_REGISTRY_CONFIG:
    login wrote the credentials to the default location, and an isolated
    empty registry config means an unauthenticated pull (the 403 incident).
    The four cache/config/data isolations MUST stay (helm #8059)."""
    captured = {}

    def fake_run(cmd, **kw):
        class R: returncode = 0; stdout = ""; stderr = ""
        if cmd[:2] == [m.HELM_BIN, "pull"]:
            captured["env"] = kw.get("env")
            # Pretend the chart landed where _ensure_chart expects it.
            for d in os.listdir(kw_tmp[0]):
                pass
        return R()

    # _ensure_chart needs login to "succeed" without a subprocess.
    monkeypatch.setattr(m, "_helm_login", lambda registry: True)
    kw_tmp = [str(tmp_path)]
    monkeypatch.setattr(m.subprocess, "run", fake_run)
    monkeypatch.setattr(m, "HELM_CACHE_DIR", str(tmp_path))

    try:
        m._ensure_chart("helm-oci-dev.repo.appspace.com", "appspace-ms",
                        "0.0.0-test")
    except Exception:
        pass  # extraction fails with the stub; we only need the env capture

    env = captured.get("env")
    assert env is not None, "helm pull was not invoked with a custom env"
    base = os.environ.get("HELM_REGISTRY_CONFIG")
    assert env.get("HELM_REGISTRY_CONFIG") == base, (
        "helm pull must inherit the registry config that _helm_login wrote "
        f"(got {env.get('HELM_REGISTRY_CONFIG')!r}) — an isolated empty "
        "config means unauthenticated pulls (403 incident)")
    # v2.5.24: HELM_CONFIG_HOME is inherited too (default registry-config
    # path derives from it); only the three cache homes stay isolated.
    for isolated in ("HELM_REPOSITORY_CACHE", "HELM_CACHE_HOME",
                     "HELM_DATA_HOME"):
        assert env.get(isolated) and env[isolated] != os.environ.get(isolated), (
            f"{isolated} must stay isolated per pull (helm #8059 race)")


def test_pull_env_does_not_isolate_config_home(monkeypatch, tmp_path):
    """v2.5.24: HELM_CONFIG_HOME must be inherited too. helm derives the
    DEFAULT registry-config path from it, so an isolated config home orphans
    the credentials login wrote — proven in the production pod: config-home
    isolation -> 403 on every pull, cache-only isolation -> success."""
    captured = {}

    def fake_run(cmd, **kw):
        class R: returncode = 0; stdout = ""; stderr = ""
        if cmd[:2] == [m.HELM_BIN, "pull"]:
            captured["env"] = kw.get("env")
        return R()

    monkeypatch.setattr(m, "_helm_login", lambda registry: True)
    monkeypatch.setattr(m.subprocess, "run", fake_run)
    monkeypatch.setattr(m, "HELM_CACHE_DIR", str(tmp_path))
    try:
        m._ensure_chart("helm-oci-dev.repo.appspace.com", "appspace-ms",
                        "0.0.0-test2")
    except Exception:
        pass
    env = captured.get("env")
    assert env is not None
    assert env.get("HELM_CONFIG_HOME") == os.environ.get("HELM_CONFIG_HOME"), (
        "HELM_CONFIG_HOME must be inherited — isolating it moves the default "
        "registry-config path and produces unauthenticated pulls (403)")
    for isolated in ("HELM_REPOSITORY_CACHE", "HELM_CACHE_HOME", "HELM_DATA_HOME"):
        assert env.get(isolated) and env[isolated] != os.environ.get(isolated)
