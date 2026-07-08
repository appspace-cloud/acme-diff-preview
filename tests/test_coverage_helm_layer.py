"""Coverage campaign, pass H: the helm ORCHESTRATION layer.

Scope note (the honest line): what stays out of unit tests is helm's render
CORRECTNESS — asserting what manifests a given chart produces would test a
fake, not the service. What this file covers is OUR code around the helm
CLI: the pull-once locking and cache/TTL logic, error classification
(not-found vs transient), the atomic tmp-dir rename and the v2.5.14 orphan
cleanup, chart subdir discovery (pure filesystem), template argv plumbing,
and — most importantly — _run_one_diff's own YAML parse-and-compare
algorithm, fed by a fake `helm` binary exactly the same way this suite
already fakes `argocd` and acme-mcp fakes `kubectl`.
"""
import json
import os
import stat
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m  # noqa: E402


APP = "pv-helm-a-ms"
REG = "registry.example.com"
CHART = "appspace-ms"


def _mk_fake_helm(tmp_path, *, login_rc=0, pull_mode="ok", count_file=None,
                  fail_times=0, pull_sleep=0, template_sleep=0):
    """Write a fake `helm` binary.

    pull_mode: 'ok' (untars a chart dir), 'notfound' (permanent error),
               'transient' (fails `fail_times` times, then succeeds).
    pull_sleep / template_sleep: seconds each subcommand sleeps before
              acting — used to exercise the caller's timeout branches.
    template: emits a Deployment whose replicas value is read from a
              `replicas_marker:` line in the LAST -f values file, so the
              diff the service computes is driven by OUR canned values.
              A `MARKER_BOOM` line makes the render fail.
    """
    count = count_file or str(tmp_path / "calls.log")
    script = f'''#!/bin/bash
echo "$1" >> "{count}"
case "$1" in
  registry)
    exit {login_rc};;
  pull)
    sleep {pull_sleep}
    dest=""; prev=""
    for a in "$@"; do [ "$prev" = "-d" ] && dest="$a"; prev="$a"; done
    mode="{pull_mode}"
    if [ "$mode" = "notfound" ]; then
      echo "Error: chart not found: unexpected status code: 404" >&2; exit 1
    fi
    if [ "$mode" = "transient" ]; then
      n=$(grep -c '^pull$' "{count}")
      if [ "$n" -le {fail_times} ]; then
        echo "Error: connection reset by registry" >&2; exit 1
      fi
    fi
    mkdir -p "$dest/{CHART}"
    printf 'apiVersion: v2\\nname: {CHART}\\n' > "$dest/{CHART}/Chart.yaml"
    exit 0;;
  template)
    sleep {template_sleep}
    vf=""; prev=""
    for a in "$@"; do [ "$prev" = "-f" ] && vf="$a"; prev="$a"; done
    if grep -q "MARKER_BOOM" "$vf" 2>/dev/null; then
      echo "helm template failed: execution error at (templates/deploy.yaml)" >&2; exit 1
    fi
    R=$(grep "replicas_marker:" "$vf" 2>/dev/null | awk '{{print $2}}')
    [ -z "$R" ] && R=1
    printf 'apiVersion: apps/v1\\nkind: Deployment\\nmetadata:\\n  name: webx\\n  namespace: pv-helm-a\\nspec:\\n  replicas: %s\\n' "$R"
    exit 0;;
esac
exit 0
'''
    p = tmp_path / "helm"
    p.write_text(script)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p), count


def _calls(count_file, verb):
    try:
        return sum(1 for l in open(count_file) if l.strip() == verb)
    except OSError:
        return 0


@pytest.fixture()
def helm_world(tmp_path, monkeypatch):
    """Clean helm-layer module state pointed at a throwaway cache dir."""
    monkeypatch.setattr(m, "HELM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(m, "OCI_USER", "user")
    monkeypatch.setattr(m, "OCI_PASS", "secret")
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    with m._helm_cache_lock:
        m._helm_chart_cache.clear()
    m._helm_chart_pull_ts.clear()
    with m._helm_pull_locks_lock:
        m._helm_pull_locks.clear()
    with m._helm_login_lock:
        m._helm_logged_in.clear()
        m._helm_login_ts.clear()
    yield tmp_path
    with m._helm_cache_lock:
        m._helm_chart_cache.clear()
    m._helm_chart_pull_ts.clear()
    with m._helm_pull_locks_lock:
        m._helm_pull_locks.clear()
    with m._helm_login_lock:
        m._helm_logged_in.clear()
        m._helm_login_ts.clear()


# ── _find_chart_subdir (pure filesystem, no helm at all) ─────────────────

def test_find_chart_subdir_prefers_the_dir_with_chart_yaml(tmp_path):
    (tmp_path / "dependency").mkdir()
    real = tmp_path / CHART
    real.mkdir()
    (real / "Chart.yaml").write_text("apiVersion: v2\n")
    assert m._find_chart_subdir(str(tmp_path)) == str(real)


def test_find_chart_subdir_falls_back_to_first_subdir(tmp_path):
    (tmp_path / "only-dir").mkdir()
    assert m._find_chart_subdir(str(tmp_path)) == str(tmp_path / "only-dir")


def test_find_chart_subdir_no_subdirs_returns_input(tmp_path):
    (tmp_path / "loose-file.txt").write_text("x")
    assert m._find_chart_subdir(str(tmp_path)) == str(tmp_path)


def test_find_chart_subdir_oserror_returns_input():
    assert m._find_chart_subdir("/nonexistent/xyz") == "/nonexistent/xyz"


# ── _helm_login ──────────────────────────────────────────────────────────

def test_helm_login_success_is_cached_within_ttl(helm_world, monkeypatch):
    helm, count = _mk_fake_helm(helm_world)
    monkeypatch.setattr(m, "HELM_BIN", helm)
    assert m._helm_login(REG) is True
    assert m._helm_login(REG) is True
    assert _calls(count, "registry") == 1, "second login within TTL must be a cache hit"


def test_helm_login_failure_clears_state_and_retries_next_call(helm_world, monkeypatch):
    helm, count = _mk_fake_helm(helm_world, login_rc=1)
    monkeypatch.setattr(m, "HELM_BIN", helm)
    assert m._helm_login(REG) is False
    assert m._helm_login(REG) is False
    assert _calls(count, "registry") == 2, "a failed login must NOT be cached"


def test_helm_login_without_credentials_is_false_without_subprocess(helm_world, monkeypatch):
    helm, count = _mk_fake_helm(helm_world)
    monkeypatch.setattr(m, "HELM_BIN", helm)
    monkeypatch.setattr(m, "OCI_PASS", "")
    assert m._helm_login(REG) is False
    assert _calls(count, "registry") == 0


# ── _helm_template (argv plumbing + error propagation) ───────────────────

def test_helm_template_renders_with_values_files(helm_world, monkeypatch):
    helm, _ = _mk_fake_helm(helm_world)
    monkeypatch.setattr(m, "HELM_BIN", helm)
    yaml_out, err = m._helm_template("/tmp/chartpath", "pv-helm-a-ms", "pv-helm-a",
                                     {"base.yaml": "a: 1\n",
                                      "customer.yaml": "replicas_marker: 5\n"})
    assert err is None
    assert "kind: Deployment" in yaml_out and "replicas: 5" in yaml_out, \
        "the LAST -f file must win (helm -f override order)"


def test_helm_template_error_is_returned_not_raised(helm_world, monkeypatch):
    helm, _ = _mk_fake_helm(helm_world)
    monkeypatch.setattr(m, "HELM_BIN", helm)
    yaml_out, err = m._helm_template("/tmp/chartpath", "r", "ns",
                                     {"v.yaml": "MARKER_BOOM: true\n"})
    assert yaml_out is None and "execution error" in err


# ── _ensure_chart (cache, locking, classification, orphan cleanup) ───────

def test_ensure_chart_rejects_unsafe_version_and_name(helm_world, monkeypatch):
    helm, count = _mk_fake_helm(helm_world)
    monkeypatch.setattr(m, "HELM_BIN", helm)
    assert m._ensure_chart(REG, CHART, "1.0.0; rm -rf /") is None
    assert m._ensure_chart(REG, "../evil", "1.0.0") is None
    assert _calls(count, "pull") == 0, "unsafe input must never reach helm"


def test_ensure_chart_pulls_once_then_serves_from_memory(helm_world, monkeypatch):
    helm, count = _mk_fake_helm(helm_world)
    monkeypatch.setattr(m, "HELM_BIN", helm)
    path1 = m._ensure_chart(REG, CHART, "1.0.0")
    assert path1 and path1.endswith(CHART) and os.path.isfile(os.path.join(path1, "Chart.yaml"))
    path2 = m._ensure_chart(REG, CHART, "1.0.0")
    assert path2 == path1
    assert _calls(count, "pull") == 1, "second call must be a memory-cache hit"


def test_ensure_chart_reuses_existing_disk_dir_without_pulling(helm_world, monkeypatch):
    helm, count = _mk_fake_helm(helm_world)
    monkeypatch.setattr(m, "HELM_BIN", helm)
    disk = os.path.join(m.HELM_CACHE_DIR, REG, CHART, "2.0.0", CHART)
    os.makedirs(disk)
    open(os.path.join(disk, "Chart.yaml"), "w").write("apiVersion: v2\n")
    path = m._ensure_chart(REG, CHART, "2.0.0")
    assert path == disk
    assert _calls(count, "pull") == 0, "a warm disk cache must not re-pull"


def test_ensure_chart_not_found_raises_permanent_error(helm_world, monkeypatch):
    helm, _ = _mk_fake_helm(helm_world, pull_mode="notfound")
    monkeypatch.setattr(m, "HELM_BIN", helm)
    with pytest.raises(m.OciChartNotFound, match="not found"):
        m._ensure_chart(REG, CHART, "9.9.9")


def test_ensure_chart_transient_failure_returns_none_and_leaves_no_orphan_dir(helm_world, monkeypatch):
    # v2.5.14 regression: an exhausted-retry pull used to leak one mkdtemp()
    # directory directly under HELM_CACHE_DIR that no cleanup path ever
    # removed (prune only walks the registry/chart/version hierarchy).
    helm, count = _mk_fake_helm(helm_world, pull_mode="transient", fail_times=99)
    monkeypatch.setattr(m, "HELM_BIN", helm)
    assert m._ensure_chart(REG, CHART, "3.0.0") is None
    assert _calls(count, "pull") == 3, "transient errors retry exactly 3 times"
    strays = [d for d in os.listdir(m.HELM_CACHE_DIR) if d != REG]
    assert strays == [], f"orphan tmp dirs leaked under the cache root: {strays}"


def test_ensure_chart_transient_then_success_recovers(helm_world, monkeypatch):
    helm, count = _mk_fake_helm(helm_world, pull_mode="transient", fail_times=2)
    monkeypatch.setattr(m, "HELM_BIN", helm)
    path = m._ensure_chart(REG, CHART, "4.0.0")
    assert path and os.path.isfile(os.path.join(path, "Chart.yaml"))
    assert _calls(count, "pull") == 3


def test_ensure_chart_login_failure_short_circuits(helm_world, monkeypatch):
    helm, count = _mk_fake_helm(helm_world, login_rc=1)
    monkeypatch.setattr(m, "HELM_BIN", helm)
    assert m._ensure_chart(REG, CHART, "5.0.0") is None
    assert _calls(count, "pull") == 0, "no pull may be attempted without a login"


# ── _run_one_diff: OUR parse-and-compare algorithm, end to end ───────────

@pytest.fixture()
def diff_world(helm_world, monkeypatch):
    monkeypatch.setitem(m._app_chart_map, APP, CHART)
    monkeypatch.setitem(m._app_chart_revision_map, APP, "2603.0.1")
    monkeypatch.setitem(m._app_chart_registry_map, APP, REG)
    monkeypatch.setitem(m._app_value_files_map, APP,
                        ["$config/gcp/dev/x/pv-helm-a/customer.yaml"])
    monkeypatch.setitem(m._app_namespace_map, APP, "pv-helm-a")
    with m._main_render_lock:
        m._main_render_cache.clear()
    yield helm_world
    with m._main_render_lock:
        m._main_render_cache.clear()


def _values_by_sha(pr_marker, main_marker):
    def fake_fetch(value_files, sha):
        marker = pr_marker if sha == "prsha0000001" else main_marker
        return {vf: f"appspace: {{}}\n{marker}\n" for vf in value_files}
    return fake_fetch


def test_run_one_diff_detects_a_real_change(diff_world, monkeypatch):
    helm, _ = _mk_fake_helm(diff_world)
    monkeypatch.setattr(m, "HELM_BIN", helm)
    monkeypatch.setattr(m, "_fetch_value_files",
                        _values_by_sha("replicas_marker: 3", "replicas_marker: 2"))
    out = m._run_one_diff(APP, "prsha0000001", "mainsha00001")
    diff_text, reason, detail, version_change = out
    assert reason is None and detail is None
    assert "=====" in diff_text and "Deployment" in diff_text, \
        "the diff must carry resource section headers"
    assert "replicas: 2" in diff_text and "replicas: 3" in diff_text
    assert version_change is None


def test_run_one_diff_identical_renders_report_no_diff(diff_world, monkeypatch):
    helm, _ = _mk_fake_helm(diff_world)
    monkeypatch.setattr(m, "HELM_BIN", helm)
    monkeypatch.setattr(m, "_fetch_value_files",
                        _values_by_sha("replicas_marker: 2", "replicas_marker: 2"))
    diff_text, reason, detail, version_change = m._run_one_diff(
        APP, "prsha0000001", "mainsha00002")
    assert reason is None and diff_text == "", "identical renders must diff to empty"


def test_run_one_diff_render_failure_maps_to_reason(diff_world, monkeypatch):
    helm, _ = _mk_fake_helm(diff_world)
    monkeypatch.setattr(m, "HELM_BIN", helm)
    monkeypatch.setattr(m, "_fetch_value_files",
                        _values_by_sha("MARKER_BOOM: 1", "replicas_marker: 2"))
    out = m._run_one_diff(APP, "prsha0000001", "mainsha00003")
    assert out[0] is None and out[1] == m.REASON_RENDER


def test_run_one_diff_chart_not_found_is_permanent(diff_world, monkeypatch):
    helm, _ = _mk_fake_helm(diff_world, pull_mode="notfound")
    monkeypatch.setattr(m, "HELM_BIN", helm)
    monkeypatch.setattr(m, "_fetch_value_files",
                        _values_by_sha("replicas_marker: 2", "replicas_marker: 2"))
    out = m._run_one_diff(APP, "prsha0000001", "mainsha00004")
    assert out[0] is None and out[1] == m.REASON_OCI_NOT_FOUND


def test_run_one_diff_version_change_is_reported(diff_world, monkeypatch):
    helm, _ = _mk_fake_helm(diff_world)
    monkeypatch.setattr(m, "HELM_BIN", helm)
    monkeypatch.setattr(m, "_fetch_value_files",
                        _values_by_sha("replicas_marker: 2", "replicas_marker: 2"))
    out = m._run_one_diff(APP, "prsha0000001", "mainsha00005",
                          chart_revision="2604.0.0")
    diff_text, reason, detail, version_change = out
    assert version_change == ("2603.0.1", "2604.0.0")


# ── _render_main_side_resources (decommission listing + its cache) ───────

def test_render_main_side_resources_returns_parsed_dict_and_caches(diff_world, monkeypatch):
    helm, count = _mk_fake_helm(diff_world)
    monkeypatch.setattr(m, "HELM_BIN", helm)
    monkeypatch.setattr(m, "_fetch_value_files",
                        _values_by_sha("replicas_marker: 2", "replicas_marker: 2"))
    # Warm the chart first, as a normal diff would have: the render cache key
    # includes the chart's pull generation, so the FIRST-ever pull happening
    # inside the render call would legitimately change the key between calls
    # (that generation bump is exactly how a dev-tag republish invalidates
    # renders of the previous build).
    m._ensure_chart(REG, CHART, "2603.0.1")
    res = m._render_main_side_resources(APP, "mainsha00006")
    assert len(res) == 1 and any("Deployment" in k[0] for k in res)
    templates_before = _calls(count, "template")
    res2 = m._render_main_side_resources(APP, "mainsha00006")
    assert res2 == res
    assert _calls(count, "template") == templates_before, "second call must hit the cache"


def test_render_main_side_resources_missing_metadata_raises(diff_world, monkeypatch):
    with pytest.raises(RuntimeError, match="metadata"):
        m._render_main_side_resources("unknown-app-ms", "mainsha00007")


# ── _start_heartbeat ─────────────────────────────────────────────────────

def test_heartbeat_refreshes_liveness_and_honors_shutdown(monkeypatch):
    monkeypatch.setattr(m.time, "sleep", lambda s: threading.Event().wait(0.005))
    monkeypatch.setattr(m, "_loop_idle", True, raising=False)
    before = m._last_ok
    m._shutdown = False
    try:
        m._start_heartbeat()
        threading.Event().wait(0.1)
        assert m._last_ok > before, "an idle loop must still refresh liveness"
    finally:
        m._shutdown = True
        threading.Event().wait(0.05)  # let the daemon loop observe the flag and exit
        m._shutdown = False
