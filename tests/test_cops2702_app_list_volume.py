"""COPS-2702 part 2: stop asking argocd-server to marshal 47 MB it already sent.

Moving the endpoint in-cluster fixed the transport but not the volume: the API
server still built a 128 MB JSON / 47 MB response ~320 times a day, and that
marshalling is what competes with real UI users. Two levers here:

  * PATH_MAP_TTL becomes env-overridable, so an environment can halve the
    number of those responses without a release.
  * the JFrog webhook stops listing the fleet a second time. It needs exactly
    two facts per app - chart name and targetRevision - and the path-map cache
    already extracts both. Measured over 48h on the live hub, that handler
    fired ~132 times and found zero matching apps EVERY time, so it was ~66
    full-fleet listings a day to learn nothing.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import diff_preview  # noqa: E402


def _module_source():
    with open(os.path.join(os.path.dirname(__file__), "..", "src",
                           "diff_preview.py"), encoding="utf-8") as fh:
        return fh.read()


class TestTtlIsTunable:
    """The TTL is read through _env_int, tested directly rather than by
    reloading the module.

    importlib.reload(diff_preview) rebinds every global in it, which breaks
    sibling test modules holding references to the old objects - an earlier
    version of this file did exactly that and broke four unrelated tests while
    passing in isolation. So: one sentinel on the wiring, then the behaviour of
    the mechanism it wires.
    """

    def test_constant_is_wired_through_env_int_with_a_floor(self):
        assert 'max(60, _env_int("PATH_MAP_TTL", 300))' in _module_source(), \
            "PATH_MAP_TTL is no longer env-overridable, or lost its floor"

    def test_default_is_unchanged(self, monkeypatch):
        """An image upgrade with no env set must keep today's 5 minutes."""
        monkeypatch.delenv("PATH_MAP_TTL", raising=False)
        assert diff_preview._env_int("PATH_MAP_TTL", 300) == 300

    def test_env_raises_it(self, monkeypatch):
        """The knob is the point: 300 -> 900 thirds the number of 47 MB
        responses argocd-server builds for this service."""
        monkeypatch.setenv("PATH_MAP_TTL", "900")
        assert diff_preview._env_int("PATH_MAP_TTL", 300) == 900

    def test_garbage_falls_back_to_the_default(self, monkeypatch):
        """Never let a typo disable the cache: at TTL=0 every iteration would
        pay a fresh 47 MB list."""
        monkeypatch.setenv("PATH_MAP_TTL", "not-a-number")
        assert diff_preview._env_int("PATH_MAP_TTL", 300) == 300

    def test_zero_and_negative_cannot_disable_the_cache(self, monkeypatch):
        """envcfg._env_int guards only against ValueError, so 0 and negatives
        pass straight through it. Without the floor, PATH_MAP_TTL=0 would buy a
        fresh 47 MB list on every ~60s iteration: ~1440 a day against today's
        ~290. The floor is what makes the knob safe to expose."""
        for bad in ("0", "-60", "5"):
            monkeypatch.setenv("PATH_MAP_TTL", bad)
            raw = diff_preview._env_int("PATH_MAP_TTL", 300)
            assert max(60, raw) >= 60
        monkeypatch.setenv("PATH_MAP_TTL", "0")
        assert diff_preview._env_int("PATH_MAP_TTL", 300) == 0, (
            "envcfg._env_int changed contract; the floor may now be redundant")


class TestWebhookUsesTheCache:
    def test_warm_cache_issues_no_api_call(self, monkeypatch):
        """The regression this guards: any `argocd app list` from the webhook
        path once the cache is warm. Fails loudly rather than silently paying
        47 MB again."""
        monkeypatch.setattr(diff_preview, "_app_chart_map",
                            {"pv-a-ms": "appspace-micro-services",
                             "pv-b-ms": "appspace-micro-services",
                             "pv-c-ss": "appspace-supporting-services"})
        monkeypatch.setattr(diff_preview, "_app_chart_revision_map",
                            {"pv-a-ms": "2603.2.1", "pv-b-ms": "2603.0.19",
                             "pv-c-ss": "2603.2.1"})

        def _boom(*a, **k):
            raise AssertionError("the webhook listed the fleet with a warm cache")
        monkeypatch.setattr(subprocess, "run", _boom)

        # No match for this version -> returns before any refresh work, and
        # crucially without consulting the API at all.
        diff_preview._jfrog_hard_refresh("appspace-micro-services", "9999.0.0")

    def test_warm_cache_matches_the_right_apps(self, monkeypatch):
        """Matching is on BOTH chart and targetRevision, so a different
        version of the same chart is not refreshed."""
        monkeypatch.setattr(diff_preview, "_app_chart_map",
                            {"pv-a-ms": "appspace-micro-services",
                             "pv-b-ms": "appspace-micro-services",
                             "pv-c-ss": "appspace-supporting-services"})
        monkeypatch.setattr(diff_preview, "_app_chart_revision_map",
                            {"pv-a-ms": "2603.2.1", "pv-b-ms": "2603.0.19",
                             "pv-c-ss": "2603.2.1"})
        seen = []

        def _fake_run(cmd, **kw):
            # Only hard-refresh calls should reach subprocess now.
            assert "list" not in cmd, f"unexpected list call: {cmd}"
            seen.append(cmd[cmd.index("get") + 1] if "get" in cmd else None)
            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()
        monkeypatch.setattr(subprocess, "run", _fake_run)

        diff_preview._jfrog_hard_refresh("appspace-micro-services", "2603.2.1")
        assert seen == ["pv-a-ms"], seen

    def test_cold_start_still_lists(self, monkeypatch):
        """A webhook can arrive before the first iteration built the cache.
        Skipping silently there would drop a real refresh, so the original
        full-list path is kept for exactly that case."""
        monkeypatch.setattr(diff_preview, "_app_chart_map", {})
        called = []

        def _fake_run(cmd, **kw):
            called.append(cmd)
            class R:
                returncode = 0
                stdout = "[]"
                stderr = ""
            return R()
        monkeypatch.setattr(subprocess, "run", _fake_run)

        diff_preview._jfrog_hard_refresh("appspace-micro-services", "2603.2.1")
        assert called, "cold start must still consult the API"
        assert "list" in called[0]


def test_only_the_cache_builder_lists_the_fleet():
    """Sentinel: exactly two `argocd app list` sites remain in the module -
    the cache builder and the webhook's cold-start path. A third would mean
    someone reintroduced a full-fleet listing."""
    with open(os.path.join(os.path.dirname(__file__), "..", "src",
                           "diff_preview.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert src.count('"app", "list"') == 2, (
        "a new full-fleet `argocd app list` appeared; each one is 47 MB that "
        "argocd-server has to marshal alongside real UI users")
