"""Contract test: leader.py against a REAL Kubernetes API (kind), not a mock.

Every other test in test_leader.py drives the elector against _FakeLeaseApi,
a hand-written simulation of the Lease API that WE wrote based on our own
reading of the docs. That proves the algorithm is internally consistent, but
it cannot catch a case where our understanding of the real API is subtly
wrong (a MicroTime format the real server actually rejects, a 409 body
shaped differently than we assumed, RBAC behaving differently than we
expect). This file closes that gap by running the exact same LeaderElector
against an ephemeral, real kind cluster, with a real ServiceAccount token,
real TLS verification (verify_tls=True, the in-cluster default — every
_FakeLeaseApi test uses verify_tls=False and so never exercises this path),
and the exact RBAC shape shipped in charts/acme-diff-preview/templates/
role.yaml, applied for real.

Opt-in and self-contained: skipped unless RUN_KIND_TESTS=1 is set, so the
normal `pytest tests/` run (local or the main CI job) is completely
unaffected — no docker/kind requirement, no added runtime. A dedicated CI
job sets the env var and provisions kind/kubectl before running this file
only. The cluster is created and destroyed by a module-scoped fixture.
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import leader  # noqa: E402

pytestmark = [
    pytest.mark.kind,
    # COPS-2595: every test in this file races a REAL lease against the REAL
    # API server clock (e.g. test_real_expiry_based_takeover sleeps out a 2s
    # lease to prove expiry-based takeover). The suite-wide sleep neutraliser
    # would make that sleep return instantly, the lease would never expire and
    # the takeover would never happen. Real clock required, so opt the whole
    # module out.
    pytest.mark.realtime,
    pytest.mark.skipif(
        os.environ.get("RUN_KIND_TESTS") != "1",
        reason="opt-in only: set RUN_KIND_TESTS=1 to run the real-cluster "
               "contract tests (creates/destroys an ephemeral kind cluster)"),
]

CLUSTER_NAME = "adp-leader-contract-test"
MICROTIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")


def _require(*tools):
    missing = [t for t in tools if shutil.which(t) is None]
    if missing:
        pytest.skip(f"missing required tool(s) for the kind contract test: "
                    f"{', '.join(missing)}")


def _kubectl(kctx, *args, input=None):
    return subprocess.run(
        ["kubectl", "--context", kctx, *args],
        input=input, capture_output=True, text=True, timeout=30, check=True)


def _kubectl_apply_idempotent(kctx, ns, *create_args):
    """`kubectl create ...` is not idempotent (fails on a second run); this
    file shares one namespace/ServiceAccount across every test in the
    module for speed, so each fixture invocation must not fail just because
    a previous test already created them. dry-run + apply is the standard
    create-or-update pattern for exactly this."""
    manifest = subprocess.run(
        ["kubectl", "--context", kctx, "create", *create_args,
         "-n", ns, "--dry-run=client", "-o", "yaml"],
        capture_output=True, text=True, timeout=30, check=True).stdout
    _kubectl(kctx, "apply", "-f", "-", input=manifest)


@pytest.fixture(scope="module")
def kind_cluster():
    """One real, ephemeral kind cluster shared by every test in this file."""
    _require("kind", "kubectl", "docker")
    subprocess.run(["kind", "delete", "cluster", "--name", CLUSTER_NAME],
                   capture_output=True, timeout=60)  # clean slate; ignore failures
    subprocess.run(["kind", "create", "cluster", "--name", CLUSTER_NAME,
                    "--wait", "90s"], capture_output=True, text=True,
                   timeout=180, check=True)
    try:
        yield f"kind-{CLUSTER_NAME}"
    finally:
        subprocess.run(["kind", "delete", "cluster", "--name", CLUSTER_NAME],
                       capture_output=True, timeout=60)


@pytest.fixture()
def rig(kind_cluster, tmp_path, request):
    """A fresh namespace + ServiceAccount + the SHIPPED RBAC shape + a real
    bearer token, laid out on disk exactly like the in-cluster ServiceAccount
    mount (token/ca.crt/namespace), so LeaderElector needs zero test-only
    code paths to talk to it. One lease name per test (test node id, sanitized)
    so tests never see each other's leases even though they share a cluster.
    """
    kctx = kind_cluster
    ns = "adp-test"
    sa = "adp-test-sa"
    lease_name = re.sub(r"[^a-z0-9-]", "-", request.node.name.lower())[:50]

    _kubectl_apply_idempotent(kctx, ns, "namespace", ns)
    _kubectl_apply_idempotent(kctx, ns, "serviceaccount", sa)
    # The EXACT RBAC shape shipped in charts/acme-diff-preview/templates/
    # role.yaml: get/update/patch scoped to one lease by resourceName (create
    # cannot be resourceName-scoped, hence the separate rule). If the chart's
    # RBAC ever drifts from what the app actually needs, this is what catches
    # it — a real 403 from a real authorizer, not our own assumption.
    role = {
        "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
        "metadata": {"name": "adp-test-role", "namespace": ns},
        "rules": [
            {"apiGroups": ["coordination.k8s.io"], "resources": ["leases"],
             "resourceNames": [lease_name], "verbs": ["get", "update", "patch"]},
            {"apiGroups": ["coordination.k8s.io"], "resources": ["leases"],
             "verbs": ["create"]},
        ],
    }
    _kubectl(kctx, "apply", "-f", "-", input=yaml.dump(role))
    _kubectl_apply_idempotent(kctx, ns, "rolebinding", "adp-test-rb",
                              "--role=adp-test-role",
                              f"--serviceaccount={ns}:{sa}")
    token = _kubectl(kctx, "create", "token", sa, "-n", ns,
                     "--duration=1h").stdout.strip()

    kubeconfig = yaml.safe_load(subprocess.run(
        ["kind", "get", "kubeconfig", "--name", CLUSTER_NAME],
        capture_output=True, text=True, timeout=30, check=True).stdout)
    cluster = kubeconfig["clusters"][0]["cluster"]

    sa_dir = tmp_path / "sa"
    sa_dir.mkdir()
    (sa_dir / "token").write_text(token)
    (sa_dir / "ca.crt").write_bytes(
        base64.b64decode(cluster["certificate-authority-data"]))
    (sa_dir / "namespace").write_text(ns)

    return {"kctx": kctx, "ns": ns, "sa": sa, "lease_name": lease_name,
            "sa_dir": str(sa_dir), "api_host": cluster["server"]}


def _elector(rig, identity, **kw):
    kw.setdefault("retry_period", 1)
    return leader.LeaderElector(
        rig["lease_name"], identity, namespace=rig["ns"],
        sa_dir=rig["sa_dir"], api_host=rig["api_host"],
        verify_tls=True,   # the in-cluster default; every _FakeLeaseApi test
                            # in test_leader.py uses verify_tls=False and so
                            # never actually exercises real TLS verification
        **kw)


def _get_lease_via_kubectl(rig):
    """Read the lease back through an INDEPENDENT path (kubectl, not our own
    client) — proves the real API server actually accepted and stored our
    write, not just that our own client believes it did."""
    out = _kubectl(rig["kctx"], "get", "lease", rig["lease_name"],
                  "-n", rig["ns"], "-o", "json").stdout
    return json.loads(out)


def test_real_acquire_persists_valid_microtime(rig):
    el = _elector(rig, "pod-a")
    el.tick()
    assert el.is_leader() is True

    lease = _get_lease_via_kubectl(rig)
    assert lease["spec"]["holderIdentity"] == "pod-a"
    assert lease["spec"]["leaseDurationSeconds"] == 15
    # The real proof: the API server's MicroTime parser accepted our
    # hand-built timestamp string and echoed it back unchanged. Our own
    # fake mock in test_leader.py would accept ANY string here, format
    # bugs included, since it never validates — only a real API server does.
    assert MICROTIME_RE.match(lease["spec"]["acquireTime"])
    assert MICROTIME_RE.match(lease["spec"]["renewTime"])


def test_real_renewal_bumps_renew_time_and_keeps_acquire_time(rig):
    el = _elector(rig, "pod-a")
    el.tick()
    before = _get_lease_via_kubectl(rig)["spec"]
    time.sleep(1.2)
    el.tick()
    after = _get_lease_via_kubectl(rig)["spec"]
    assert after["acquireTime"] == before["acquireTime"]
    assert after["renewTime"] != before["renewTime"]
    assert el.is_leader() is True


def test_real_second_replica_does_not_steal_a_fresh_lease(rig):
    a = _elector(rig, "pod-a")
    b = _elector(rig, "pod-b")
    a.tick()
    b.tick()
    assert a.is_leader() is True
    assert b.is_leader() is False
    assert _get_lease_via_kubectl(rig)["spec"]["holderIdentity"] == "pod-a"


def test_real_release_hands_over_immediately(rig):
    a = _elector(rig, "pod-a")
    b = _elector(rig, "pod-b")
    a.tick()
    b.tick()
    assert a.is_leader() is True

    a.release()   # real PUT against the real API, blanking holderIdentity
    assert _get_lease_via_kubectl(rig)["spec"]["holderIdentity"] == ""

    b.tick()       # takes over on the very next tick, no expiry wait needed
    assert b.is_leader() is True
    assert _get_lease_via_kubectl(rig)["spec"]["holderIdentity"] == "pod-b"


def test_real_expiry_based_takeover(rig):
    # Short lease_duration so this test doesn't have to sleep out the
    # production 15s default; still a REAL expiry against the real clock
    # and the real API server (only the timers are test-tuned).
    a = _elector(rig, "pod-a", lease_duration=2, renew_deadline=1)
    b = _elector(rig, "pod-b", lease_duration=2, renew_deadline=1)
    a.tick()
    b.tick()
    assert b.is_leader() is False
    time.sleep(3)   # pod-a stops renewing (simulates a crash); let it expire
    b.tick()
    assert b.is_leader() is True
    lease = _get_lease_via_kubectl(rig)["spec"]
    assert lease["holderIdentity"] == "pod-b"
    assert lease["leaseTransitions"] == 1


def test_real_403_without_rbac_is_swallowed_not_raised(kind_cluster, tmp_path):
    """A ServiceAccount with NO RoleBinding must get a real 403 from the
    real authorizer, and tick() must swallow it exactly like any other
    HTTP error — never raise, never crash the poll loop."""
    kctx = kind_cluster
    ns = "adp-test-norbac"
    sa = "adp-test-norbac-sa"
    _kubectl_apply_idempotent(kctx, ns, "namespace", ns)
    _kubectl_apply_idempotent(kctx, ns, "serviceaccount", sa)
    # Deliberately NO Role/RoleBinding.
    token = _kubectl(kctx, "create", "token", sa, "-n", ns,
                     "--duration=1h").stdout.strip()
    kubeconfig = yaml.safe_load(subprocess.run(
        ["kind", "get", "kubeconfig", "--name", CLUSTER_NAME],
        capture_output=True, text=True, timeout=30, check=True).stdout)
    cluster = kubeconfig["clusters"][0]["cluster"]
    sa_dir = tmp_path / "sa"
    sa_dir.mkdir()
    (sa_dir / "token").write_text(token)
    (sa_dir / "ca.crt").write_bytes(
        base64.b64decode(cluster["certificate-authority-data"]))
    (sa_dir / "namespace").write_text(ns)

    events = []
    el = leader.LeaderElector(
        "adp-forbidden-lease", "pod-a", namespace=ns, sa_dir=str(sa_dir),
        api_host=cluster["server"], verify_tls=True, retry_period=1,
        on_event=events.append)
    el.tick()   # must not raise
    assert el.is_leader() is False
    assert any("non-fatal" in e for e in events)
