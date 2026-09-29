"""COPS-2766 block 5 S4: capacity cuts and fixed replicas released.

COPR-32597: an HPA floor of 1, a smaller floor on a connection service or a
CPU request under 30m reached prod with a routine verdict. acme-config-prod
#4608 cut bnym-b from 12 to 8 replicas, and #4523 moved signschannel on
pv-fedex-c from 10 fixed replicas to an HPA. On that sync the replicas field
went away and Kubernetes ran 1 replica until the HPA scaled it back.

Both are warnings (REVIEW, the build stays green): planned right-sizing
looks the same. The check runs on the full renders in _run_one_diff, and a
bug in it never turns a working diff into an error.
"""
import collections
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import comment_render as cr  # noqa: E402
import diff_preview as m  # noqa: E402
import logsink  # noqa: E402
import vm_analysis as vma  # noqa: E402

from test_cops2671_cov_dp_b import (  # noqa: E402,F401
    cold_render_cache, chart_dir, app_metadata, APP, NS, MAIN_SHA, PR_SHA)
from test_cops2714_pingscaler_calm import _ford, HPA_A, HPA_B  # noqa: E402
from test_coverage_orchestration import (  # noqa: E402,F401
    world, _mk_pr, BASE_SHA, IDENTITY, ANCILLARY)

DASHES = ("—", "–")


def _dep(name, replicas=None, cpu=None, image="v1", sidecar_cpu=None,
         init_cpu=None, kind="Deployment"):
    lines = ["apiVersion: apps/v1", f"kind: {kind}", "metadata:",
             f"  name: {name}", "  namespace: pv-x", "spec:"]
    if replicas is not None:
        lines.append(f"  replicas: {replicas}")
    lines += ["  template:", "    spec:"]
    if init_cpu is not None:
        lines += ["      initContainers:", "      - name: init", "        resources:",
                  "          requests:", f"            cpu: {init_cpu}"]
    lines += ["      containers:", f"      - name: {name}-container",
              f"        image: {image}"]
    if cpu is not None:
        lines += ["        resources:", "          requests:", f"            cpu: {cpu}"]
    if sidecar_cpu is not None:
        lines += ["      - name: monitoring", "        resources:", "          requests:",
                  f"            cpu: {sidecar_cpu}"]
    return "\n".join(lines) + "\n"


def _hpa(name, lo=None, hi=3, target=None, ref=True):
    lines = ["apiVersion: autoscaling/v2", "kind: HorizontalPodAutoscaler",
             "metadata:", f"  name: {name}", "  namespace: pv-x", "spec:"]
    if ref:
        lines += ["  scaleTargetRef:", '    apiVersion: "apps/v1"',
                  '    kind: "Deployment"', f"    name: {target or name}"]
    if lo is not None:
        lines.append(f"  minReplicas: {lo}")
    lines.append(f"  maxReplicas: {hi}")
    return "\n".join(lines) + "\n"


def _res(*docs):
    return m._parse_manifest_resources("---\n".join(docs))


def _cuts(main, pr):
    return vma._detect_capacity_floor_risk(_res(*main), _res(*pr))


# ── (a) _replica_floors ──────────────────────────────────────────────────

def test_an_hpa_overrides_the_deployment_by_its_scale_target():
    floors = vma._replica_floors(_res(_dep("web", 3), _hpa("web-hpa", 2, 5, target="web"),
                                      _dep("api", 4)))
    assert floors == {"web": (2, 5), "api": (4, None)}


def test_an_hpa_without_min_replicas_has_a_floor_of_1():
    assert vma._replica_floors(_res(_dep("web"), _hpa("web", hi=3))) == {"web": (1, 3)}


def test_an_hpa_without_scale_target_uses_its_own_name():
    assert vma._replica_floors(_res(_hpa("web", 2, 4, ref=False))) == {"web": (2, 4)}


def test_a_workload_with_no_floor_is_skipped():
    """No replicas field and no HPA (acme-ping-scaler owns it), or an HPA
    whose bounds are not numbers: there is no floor, so there is no entry,
    never a guessed 1."""
    bad = _hpa("api", 2, 4).replace("minReplicas: 2", "minReplicas: two")
    floors = vma._replica_floors(_res(_dep("web"), _dep("api", 3), bad,
                                      _dep("db", 2, kind="StatefulSet")))
    assert floors == {"db": (2, None)}
    assert vma._replica_floors(None) == {}
    assert vma._replica_floors({"flat-key": _dep("web", 3)}) == {}


# ── (b) _detect_capacity_floor_risk ──────────────────────────────────────

def test_an_hpa_floor_that_drops_to_1_is_a_cut():
    assert _cuts([_dep("svc"), _hpa("svc", 2, 3)], [_dep("svc"), _hpa("svc", 1, 3)]) == [
        ("svc", "HPA minReplicas 1 (max 3)")]


def test_an_hpa_pinned_to_1_is_not_a_cut():
    assert _cuts([_dep("svc"), _hpa("svc", 2, 3)], [_dep("svc"), _hpa("svc", 1, 1)]) == []


def test_an_hpa_that_already_had_a_floor_of_1_is_not_a_new_cut():
    assert _cuts([_dep("svc"), _hpa("svc", 1, 3)], [_dep("svc"), _hpa("svc", 1, 5)]) == []


def test_a_new_hpa_with_a_floor_of_1_is_a_cut():
    assert _cuts([_dep("svc", 3)], [_dep("svc"), _hpa("svc", hi=4)]) == [
        ("svc", "HPA minReplicas 1 (max 4)")]


def test_a_smaller_floor_on_a_connection_service_is_a_cut():
    assert _cuts([_dep("signschannel", 10)], [_dep("signschannel", 8)]) == [
        ("signschannel", "floor 10 → 8")]
    for svc in ("devicegateway", "signschannelgateway", "pushnotification"):
        assert _cuts([_dep(svc, 3)], [_dep(svc, 2)]) == [(svc, "floor 3 → 2")], svc


def test_a_cut_of_4_is_a_cut_on_any_workload():
    """#4608: bnym-b reservation 12 to 8."""
    assert _cuts([_dep("reservation", 12)], [_dep("reservation", 8)]) == [
        ("reservation", "floor 12 → 8")]


def test_a_cut_of_3_on_another_workload_is_not_a_cut():
    assert _cuts([_dep("reservation", 10)], [_dep("reservation", 7)]) == []


def test_a_cut_to_0_stays_the_zeroed_finding():
    assert _cuts([_dep("signschannel", 12)], [_dep("signschannel", 0)]) == []


def test_fixed_replicas_to_a_smaller_hpa_floor_is_a_cut():
    """#4523: signschannel from 10 replicas to an HPA with min 6."""
    assert _cuts([_dep("signschannel", 10)],
                 [_dep("signschannel"), _hpa("signschannel", 6, 12)]) == [
        ("signschannel", "floor 10 → 6")]


def test_a_floor_cut_and_an_hpa_of_1_are_one_item():
    assert _cuts([_dep("signschannel", 10)],
                 [_dep("signschannel"), _hpa("signschannel", 1, 3)]) == [
        ("signschannel", "HPA minReplicas 1 (max 3) and floor 10 → 1")]


def test_a_main_cpu_request_under_30m_is_a_cut():
    assert _cuts([_dep("svc", 2, "200m")], [_dep("svc", 2, "20m")]) == [
        ("svc", "CPU request 20m")]
    assert _cuts([_dep("svc", 2, "200m")], [_dep("svc", 2, "0.02")]) == [
        ("svc", "CPU request 20m")]
    assert _cuts([_dep("svc", 2, "200m")], [_dep("svc", 2, "'0.02'")]) == [
        ("svc", "CPU request 20m")]
    assert _cuts([_dep("svc", 2)], [_dep("svc", 2, "10m")]) == [
        ("svc", "CPU request 10m")]


def test_the_same_low_cpu_on_both_sides_is_not_a_cut():
    assert _cuts([_dep("svc", 2, "20m", image="v1")],
                 [_dep("svc", 2, "20m", image="v2")]) == []


def test_a_cpu_raise_under_30m_is_not_a_cut():
    """10m to 20m is more capacity, so it is not listed. A new workload with
    a low request has no base to compare, so it is."""
    assert _cuts([_dep("svc", 2, "10m")], [_dep("svc", 2, "20m")]) == []
    assert _cuts([_dep("svc", 2, "20m")], [_dep("svc", 2, "10m")]) == [
        ("svc", "CPU request 10m")]
    assert _cuts([], [_dep("svc", 2, "10m")]) == [("svc", "CPU request 10m")]


def test_only_the_main_container_counts():
    assert _cuts([_dep("svc", 2, "100m")], [_dep("svc", 2, "100m", init_cpu="5m")]) == []
    assert _cuts([_dep("svc", 2, "100m")], [_dep("svc", 2, "100m", sidecar_cpu="5m")]) == []


def test_the_first_container_is_the_main_one_when_no_name_matches():
    body = _dep("svc", 2, "100m").replace("svc-container", "app")
    low = _dep("svc", 2, "5m").replace("svc-container", "app")
    assert vma._detect_capacity_floor_risk(_res(body), _res(low)) == [
        ("svc", "CPU request 5m")]


def test_cpu_values_in_every_form():
    assert [vma._cpu_millicores(v) for v in ("20m", "0.02", "1", 1, 0.5, " 250m ")] == [
        20, 20, 1000, 1000, 500, 250]
    assert [vma._cpu_millicores(v) for v in (None, True, "abc", "1.5x", "nan")] == [
        None, None, None, None, None]


def test_a_broken_yaml_body_does_not_crash():
    broken = _dep("svc", 2, "20m") + "  bad: [unclosed\n"
    assert vma._main_container_cpu(broken, "svc") is None
    assert _cuts([_dep("svc", 2, "200m")], [broken]) == []
    assert vma._main_container_cpu(_dep("svc", 2), "svc") is None
    assert vma._main_container_cpu("kind: Deployment\n", "svc") is None


def test_unchanged_bodies_are_not_parsed(monkeypatch):
    calls = []
    monkeypatch.setattr(vma, "_yaml_doc", lambda text: calls.append(text) or {})
    same = [_dep("signschannel", 10, "20m"), _hpa("signschannel", 1, 3)]
    assert _cuts(same, same) == []
    # Only the HPA changes: the Deployment body is the same, so its CPU is not read.
    assert _cuts([same[0], _hpa("signschannel", 2, 3)], same) == [
        ("signschannel", "HPA minReplicas 1 (max 3) and floor 2 → 1")]
    assert calls == []


def test_a_ping_scaler_deployment_with_no_replicas_gives_no_cut():
    """acme-ping-scaler on: the chart renders no replicas field and no HPA,
    so the floor is unknown on that side and nothing is compared."""
    assert _cuts([_dep("signschannel", image="v1")], [_dep("signschannel", image="v2")]) == []
    assert _cuts([_dep("signschannel"), _hpa("signschannel", 6, 12)],
                 [_dep("signschannel", image="v2")]) == []
    assert _cuts([_dep("signschannel", image="v2")],
                 [_dep("signschannel"), _hpa("signschannel", 6, 12)]) == []


def test_deleted_and_empty_sides_give_nothing():
    assert _cuts([_dep("signschannel", 10)], []) == []
    assert vma._detect_capacity_floor_risk(None, None) == []


# ── (c) _detect_replicas_released ────────────────────────────────────────

def _released(main, pr):
    return vma._detect_replicas_released(_res(*main), _res(*pr))


def test_fixed_replicas_that_go_away_are_released():
    assert _released([_dep("svc", 10), _dep("api", 2)],
                     [_dep("svc"), _hpa("svc", 6, 12), _dep("api", 2)]) == [("svc", 10)]


def test_zero_one_or_no_replicas_before_is_not_released():
    """From 1, Kubernetes still runs 1 replica, so nothing drops."""
    assert _released([_dep("svc", 0)], [_dep("svc")]) == []
    assert _released([_dep("svc", 1)], [_dep("svc"), _hpa("svc", 2, 4)]) == []
    assert _released([_dep("svc", 2)], [_dep("svc"), _hpa("svc", 2, 4)]) == [("svc", 2)]
    assert _released([_dep("svc", image="v1")], [_dep("svc", image="v2")]) == []


def test_a_deleted_workload_is_not_released():
    assert _released([_dep("svc", 10)], []) == []
    assert _released([_hpa("svc", 2, 3)], [_hpa("svc", 1, 3)]) == []
    assert vma._detect_replicas_released(None, None) == []


# ── (d) _run_one_diff and argocd_diff ────────────────────────────────────

@pytest.fixture()
def helm_world(cold_render_cache, app_metadata, monkeypatch):
    """_run_one_diff with fake value reads and a fake helm; no shadow audit."""
    monkeypatch.setattr(m, "MAIN_RENDER_CACHE_SHADOW_RATE", 0.0)
    monkeypatch.setattr(m, "_fetch_value_files",
                        lambda files, sha: {f: f"appspace: {{}}\nside: {sha}\n" for f in files})
    renders = {}

    def helm(chart, release, namespace, vals):
        side = "pr" if any(PR_SHA in v for v in vals.values()) else "main"
        return renders[side], None

    monkeypatch.setattr(m, "_helm_template", helm)
    return renders


def _dep_ns(replicas, image="v1"):
    return _dep("signschannel", replicas, image=image).replace("namespace: pv-x",
                                                                 f"namespace: {NS}")


def test_run_one_diff_returns_the_capacity_facts(helm_world):
    helm_world.update(main=_dep_ns(10), pr=_dep_ns(6))
    out = m._run_one_diff(APP, PR_SHA, MAIN_SHA)
    assert out[1] is None and len(out) == 8, out
    assert out[6] == {"cuts": [("signschannel", "floor 10 → 6")], "released": []}
    helm_world.update(main=_dep_ns(10, "v1"), pr=_dep_ns(10, "v2"))
    out = m._run_one_diff(APP, PR_SHA, "othermain001")
    assert out[1] is None and out[6] is None, out


def test_argocd_diff_carries_the_capacity_facts(helm_world):
    helm_world.update(main=_dep_ns(10), pr=_dep_ns(None))
    r = m.argocd_diff(APP, PR_SHA, MAIN_SHA)
    assert r.outcome == m.OUT_DIFF
    assert r.capacity == {"cuts": [], "released": [("signschannel", 10)]}


def test_a_capacity_bug_never_fails_the_diff(helm_world, monkeypatch):
    """argocd_diff never raises: a broken detector gives no facts, a log
    line, and the same diff as today."""
    logged = []
    monkeypatch.setattr(logsink, "log", lambda msg, sev="INFO", **k: logged.append((sev, msg)))

    def boom(*a, **k):
        raise TypeError("unsupported operand type(s) for -: 'NoneType' and 'int'")

    monkeypatch.setattr(m, "_detect_capacity_floor_risk", boom)
    helm_world.update(main=_dep_ns(10), pr=_dep_ns(6))
    r = m.argocd_diff(APP, PR_SHA, MAIN_SHA)
    assert r.outcome == m.OUT_DIFF and r.capacity is None and r.n_res == 1
    assert any(sev == "WARNING" and "capacity check failed" in msg for sev, msg in logged), logged


SEC = ("===== /apps/Deployment pv-x/signschannel ======\n--- \n+++ \n"
       "@@ -1,2 +1,2 @@\n spec:\n-  replicas: 10\n+  replicas: 6\n")
CAP = {"cuts": [("signschannel", "floor 10 → 6")], "released": []}


def test_argocd_diff_reads_a_7_tuple_and_still_a_6_tuple(monkeypatch):
    monkeypatch.setattr(m, "_run_one_diff",
                        lambda *a, **k: (SEC, None, None, None, 0, None, CAP))
    r = m.argocd_diff("pv-x-ms", "aaaa1111", "bbbb2222")
    assert r.outcome == m.OUT_DIFF and r.capacity == CAP
    monkeypatch.setattr(m, "_run_one_diff",
                        lambda *a, **k: (SEC, None, None, None, 0, None))
    r = m.argocd_diff("pv-x-ms", "cccc3333", "dddd4444")
    assert r.outcome == m.OUT_DIFF and r.capacity is None


# ── (e) the merge summary ────────────────────────────────────────────────

def _r(capacity=None, deleted=None):
    return m.DiffResult("d", [], 1, True, "", m.OUT_DIFF, "", None, deleted,
                        capacity=capacity)


def _bullets(results):
    lines = cr._build_merge_summary(results, {}, None, None, None, None, False)
    return lines[2], [l[2:] for l in lines if l.startswith("- ")]


CUT_TAIL = (". Keep 2 replicas or more, keep the floor of the connection services, "
            "and a CPU request of 30m or more (COPR-32597).")
REL_TAIL = (". On sync the field goes away, and Kubernetes runs 1 replica until the "
            "HPA or acme-ping-scaler scales it back (acme-config-prod #4523).")


def test_both_findings_are_review_with_the_exact_text():
    verdict, b = _bullets({
        "pv-fedex-c-ms": _r({"cuts": [("signschannel", "floor 10 → 6")],
                             "released": [("signschannel", 10)]}),
        "pv-bnym-b-ms": _r({"cuts": [("reservation", "floor 12 → 8")],
                            "released": []})})
    assert verdict.startswith("⚠️ **Review before merging** (2 item(s))"), verdict
    assert b == [
        "\U0001f4c9 **Capacity cut** in `pv-bnym-b`, `pv-fedex-c`, check that it is "
        "planned: `reservation` floor 12 → 8 in `pv-bnym-b`, `signschannel` floor "
        "10 → 6 in `pv-fedex-c`" + CUT_TAIL,
        "\U0001f501 **Fixed replicas released** in `pv-fedex-c`, merge in a quiet "
        "window: `signschannel` (10)" + REL_TAIL]
    assert not any(d in line for line in b for d in DASHES)


def test_the_items_stop_at_4():
    cuts = [(f"svc{i}", "CPU request 20m") for i in range(6)]
    _verdict, b = _bullets({"pv-x-ms": _r({"cuts": cuts, "released": [("svc0", 5)]}),
                            "pv-y-ms": _r({"cuts": [], "released": [("api", 3)]})})
    assert b[0] == ("\U0001f4c9 **Capacity cut** in `pv-x`, check that it is planned: "
                    "`svc0` CPU request 20m, `svc1` CPU request 20m, `svc2` CPU "
                    "request 20m, `svc3` CPU request 20m (+2 more)" + CUT_TAIL)
    assert b[1].startswith("\U0001f501 **Fixed replicas released** in `pv-x`, `pv-y`, "
                           "merge in a quiet window: `svc0` (5) in `pv-x`, `api` (3) "
                           "in `pv-y`. On sync"), b


def test_an_env_with_two_apps_is_named_once():
    """-ms and -ss of one env are one env, so the items give no env name."""
    _verdict, b = _bullets({"pv-x-ms": _r({"cuts": [("api", "floor 12 → 8")],
                                           "released": []}),
                            "pv-x-ss": _r({"cuts": [("db", "floor 6 → 2")],
                                           "released": []})})
    assert b[0].startswith("\U0001f4c9 **Capacity cut** in `pv-x`, check that it is "
                           "planned: `api` floor 12 → 8, `db` floor 6 → 2. Keep"), b


def test_the_green_status_keeps_the_action_with_4_long_items():
    """The action comes first, so the status lead cut at 255 bytes still says
    what to do, with long aec names and 4 long items."""
    tail = "44 resource(s) will change - review comment"
    what = "HPA minReplicas 1 (max 3) and floor 10 → 1"
    for n, part, action in ((1, "cuts", "check that it is planned"),
                            (4, "cuts", "check that it is planned"),
                            (1, "released", "merge in a quiet window"),
                            (4, "released", "merge in a quiet window")):
        items = [(f"signschannelgateway{i}", what if part == "cuts" else 10)
                 for i in range(4 // n)]
        results = {f"pv-cust{i:02}--aec1-a-ms": _r(
            {"cuts": items if part == "cuts" else [],
             "released": items if part == "released" else []}) for i in range(n)}
        desc = cr.join_status_lead(cr.status_lead("\n".join(cr._build_merge_summary(
            results, {}, None, None, None, None, False))), tail)
        assert len(desc.encode()) <= 255 and desc.endswith(" | " + tail), desc
        assert action in desc, desc
        assert desc.count("pv-cust00--aec1-a") == (1 if n == 1 else 2), desc


def test_capacity_sorts_after_the_deletions():
    _verdict, b = _bullets({"pv-x-ms": _r({"cuts": [("svc", "CPU request 20m")],
                                           "released": [("svc", 2)]},
                                          deleted=["/v1/ConfigMap pv-x/c"])})
    assert [l[:1] for l in b] == ["\U0001f5d1", "\U0001f4c9", "\U0001f501"], b


def test_no_facts_and_old_results_give_no_finding():
    fields = [f for f in m.DiffResult._fields if f != "capacity"]
    Old = collections.namedtuple("Old", fields, defaults=[None] * (len(fields) - 7))
    old = Old("d", [], 1, True, "", m.OUT_DIFF, "")
    for results in ({"pv-x-ms": old}, {"pv-x-ms": _r()},
                    {"pv-x-ms": _r({"cuts": [], "released": []})}):
        _verdict, b = _bullets(results)
        assert not any(l[:1] in ("\U0001f4c9", "\U0001f501") for l in b), b


# ── (f) the ping-scaler panel ────────────────────────────────────────────

def test_the_ping_scaler_panel_says_what_it_restores(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda *a, **k: None)
    body = m.format_comment("c" * 12, _ford([HPA_A, HPA_B]))
    assert ("scales every Deployment in the namespace to **0** while the host is "
            "down. When the host answers, it sets the default replica count (2 on "
            "AEC) or the value in `acmePingScaler.customReplicas`. It never reads "
            "`definitions.<service>.replicas`.") in body
    assert "restores the configured replicas" not in body


# ── (g) process_pr ───────────────────────────────────────────────────────

PMAP = {IDENTITY: ["pv-orch-a-ms", "pv-orch-a-ss"],
        ANCILLARY: ["pv-orch-a-ms", "pv-orch-a-ss"]}


def test_a_capacity_plan_stays_green(world, monkeypatch):
    sinks, plan = world
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)
    plan["pv-orch-a-ms"] = m.DiffResult(
        "d", [("/apps/Deployment pv-orch-a/signschannel", SEC.split("\n", 1)[1])],
        1, True, "", m.OUT_DIFF, "changes",
        capacity={"cuts": [("signschannel", "floor 10 → 6")],
                  "released": [("signschannel", 10)]})
    m.process_pr(_mk_pr(), PMAP, base_sha=BASE_SHA)
    body, (state, desc) = sinks.upserts[-1], sinks.statuses[-1]
    assert state == "SUCCESSFUL", desc
    assert m._extract_status_token(body) == "clean"
    assert desc.startswith("⚠️ Capacity cut in pv-orch-a, check that it is planned: "
                           "signschannel floor 10 → 6. Keep 2 replicas"), desc
    assert "\U0001f501 **Fixed replicas released** in `pv-orch-a`, merge in" in body
