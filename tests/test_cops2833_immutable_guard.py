"""COPS-2833: a PR that changes a field that cannot change on a live resource.

acme-config-dev PR #7492 set `dataDiskType: hyperdisk-balanced` on pv-dev-07-a,
whose ComputeDisk pv-dev-07-svc-a-data already exists as pd-ssd. Config
Connector rejected it with `deny-immutable-field-updates`, the sync stayed
failed, and the preview had only posted a warning on a green build.

The guard compares the main render with the PR render of one app. Only a
resource on both sides counts, so a new env, a new resource and a renamed one
are out of scope. `block` is a rejection we are sure of: it opens the
`immutable` gate, lifted by `Confirm-Recreate: <env>`. `review` is a change
that may be rejected, or whose outcome depends on live state: a warning only.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import comment_render as cr  # noqa: E402
import diff_preview as m  # noqa: E402
import immutable_guard as ig  # noqa: E402

from test_coverage_orchestration import (  # noqa: E402,F401
    world, _mk_pr, PATH_MAP, BASE_SHA)

DISK_KEY = ("compute.cnrm.cloud.google.com/ComputeDisk", "pv-dev-07-a", "pv-dev-07-svc-a-data")


def _disk(disk_type="pd-ssd", size=128, extra=""):
    return f"""apiVersion: compute.cnrm.cloud.google.com/v1beta1
kind: ComputeDisk
metadata:
  name: pv-dev-07-svc-a-data
  namespace: pv-dev-07-a
  annotations:
    cnrm.cloud.google.com/deletion-policy: abandon
    cnrm.cloud.google.com/project-id: appspace-engineering
  labels:
    hosting-id: hst-00000000
spec:
  location: us-east1-b
  size: {size}
  type: {disk_type}
{extra}"""


def _one(main_doc, pr_doc, key=DISK_KEY):
    return ig.find_changes({key: main_doc}, {key: pr_doc})


def _blocks(changes):
    return [c for c in changes if c["level"] == ig.BLOCK]


def _reviews(changes):
    return [c for c in changes if c["level"] == ig.REVIEW]


# ── PR #7492 replay ───────────────────────────────────────────────────────

def test_pr_7492_disk_type_blocks_with_the_resource_field_and_values():
    changes = _one(_disk("pd-ssd"), _disk("hyperdisk-balanced"))
    assert len(changes) == 1, changes
    c = changes[0]
    assert c["level"] == ig.BLOCK
    assert (c["kind"], c["name"], c["path"]) == ("ComputeDisk", "pv-dev-07-svc-a-data", "spec.type")
    assert (c["old"], c["new"]) == ("pd-ssd", "hyperdisk-balanced")


def test_the_rules_come_from_the_kcc_crds():
    rules = ig.kcc_rules()
    assert ig.kcc_version() == "1.157.0"
    assert "spec.type" in rules["compute.cnrm.cloud.google.com/ComputeDisk"]
    assert "spec.bootDisk" in rules["compute.cnrm.cloud.google.com/ComputeInstance"]
    assert {"spec.role", "spec.member", "spec.resourceRef"} <= set(
        rules["iam.cnrm.cloud.google.com/IAMPolicyMember"])
    assert "spec.replication" in rules["secretmanager.cnrm.cloud.google.com/SecretManagerSecret"]
    assert len(rules) > 200, "every KCC kind, not a hand-picked list"


# ── every case the old render guard caught is still caught ────────────────

def _instance(image="projects/ubuntu-os-cloud/global/images/ubuntu-2204-jammy-v20260623", mt="n2d-standard-2"):
    return f"""apiVersion: compute.cnrm.cloud.google.com/v1beta1
kind: ComputeInstance
metadata:
  name: pv-x-svc-a
  namespace: pv-x-a
spec:
  zone: us-east1-b
  machineType: {mt}
  bootDisk:
    initializeParams:
      size: 20
      sourceImageRef:
        external: {image}
"""


INSTANCE_KEY = ("compute.cnrm.cloud.google.com/ComputeInstance", "pv-x-a", "pv-x-svc-a")


def test_compute_instance_boot_disk_change_blocks_and_names_the_leaf():
    new_image = "projects/ubuntu-os-cloud/global/images/ubuntu-2404-noble-v20260701"
    [c] = _blocks(_one(_instance(), _instance(image=new_image), INSTANCE_KEY))
    assert c["path"] == "spec.bootDisk.initializeParams.sourceImageRef.external"
    assert c["new"] == new_image


def test_machine_type_is_not_immutable_in_kcc():
    assert _one(_instance(), _instance(mt="n2d-standard-4"), INSTANCE_KEY) == []


@pytest.mark.parametrize("kind,group", [("BigQueryDataset", "bigquery"), ("StorageBucket", "storage")])
def test_data_location_change_blocks(kind, group):
    def doc(loc):
        return (f"apiVersion: {group}.cnrm.cloud.google.com/v1beta1\nkind: {kind}\n"
                f"metadata:\n  name: d\n  namespace: n\nspec:\n  location: {loc}\n")
    key = (f"{group}.cnrm.cloud.google.com/{kind}", "n", "d")
    [c] = _blocks(_one(doc("us-central1"), doc("europe-west1"), key))
    assert (c["path"], c["old"], c["new"]) == ("spec.location", "us-central1", "europe-west1")


def test_a_location_that_only_changes_case_is_the_same():
    doc = ("apiVersion: bigquery.cnrm.cloud.google.com/v1beta1\nkind: BigQueryDataset\n"
           "metadata:\n  name: d\n  namespace: n\nspec:\n  location: {}\n")
    key = ("bigquery.cnrm.cloud.google.com/BigQueryDataset", "n", "d")
    assert _one(doc.format("US"), doc.format("us"), key) == []


def _member(role="roles/storage.objectViewer", member="serviceAccount:a@p.iam.gserviceaccount.com",
            ref="external: projects/p/serviceAccounts/x@p.iam.gserviceaccount.com"):
    return f"""apiVersion: iam.cnrm.cloud.google.com/v1beta1
kind: IAMPolicyMember
metadata:
  name: m
  namespace: n
spec:
  member: {member}
  role: {role}
  resourceRef:
    kind: IAMServiceAccount
    {ref}
"""


MEMBER_KEY = ("iam.cnrm.cloud.google.com/IAMPolicyMember", "n", "m")


@pytest.mark.parametrize("pr_doc,path", [
    (_member(role="roles/storage.objectAdmin"), "spec.role"),
    (_member(member="serviceAccount:b@p.iam.gserviceaccount.com"), "spec.member"),
])
def test_iam_policy_member_change_blocks(pr_doc, path):
    [c] = _blocks(_one(_member(), pr_doc, MEMBER_KEY))
    assert c["path"] == path


def test_a_cel_immutable_reference_blocks_even_when_only_its_form_changes():
    """`self == oldSelf` is checked by the API server on the whole value."""
    [c] = _blocks(_one(_member(), _member(ref="name: x"), MEMBER_KEY))
    assert c["path"].startswith("spec.resourceRef")


def test_data_project_change_is_a_review():
    def doc(project):
        return ("apiVersion: storage.cnrm.cloud.google.com/v1beta1\nkind: StorageBucket\n"
                "metadata:\n  name: b\n  namespace: n\n  annotations:\n"
                f"    cnrm.cloud.google.com/project-id: {project}\n"
                "spec:\n  location: us\n")
    key = ("storage.cnrm.cloud.google.com/StorageBucket", "n", "b")
    changes = _one(doc("appspace-cloud"), doc("appspace-cloud-private-st"), key)
    assert _blocks(changes) == []
    [c] = _reviews(changes)
    assert c["path"] == "metadata.annotations.cnrm.cloud.google.com/project-id"


# ── no false positive ─────────────────────────────────────────────────────

def test_a_new_env_is_out_of_scope():
    assert ig.find_changes({}, {DISK_KEY: _disk("hyperdisk-balanced")}) == []


def test_a_new_resource_is_out_of_scope():
    other = ("compute.cnrm.cloud.google.com/ComputeDisk", "pv-dev-07-a", "pv-dev-07-svc-a-boot")
    assert ig.find_changes({DISK_KEY: _disk()}, {DISK_KEY: _disk(), other: _disk("x")}) == []


def test_a_renamed_resource_is_out_of_scope():
    renamed = ("compute.cnrm.cloud.google.com/ComputeDisk", "pv-dev-07-a", "pv-dev-07-svc-a-data2")
    assert ig.find_changes({DISK_KEY: _disk()}, {renamed: _disk("hyperdisk-balanced")}) == []


def test_a_no_op_render_is_clean():
    assert _one(_disk(), _disk()) == []


def test_a_key_reorder_is_clean():
    reordered = _disk().replace("  location: us-east1-b\n  size: 128\n  type: pd-ssd\n",
                                "  type: pd-ssd\n  size: 128\n  location: us-east1-b\n")
    assert reordered != _disk()
    assert _one(_disk(), reordered) == []


def test_a_list_reorder_of_an_immutable_list_is_clean():
    a = _disk(extra="  licenses:\n  - lic-a\n  - lic-b\n")
    b = _disk(extra="  licenses:\n  - lic-b\n  - lic-a\n")
    assert _one(a, b) == []


@pytest.mark.parametrize("pr_doc", [
    _disk(size=256),                                   # a disk grows in place
    _disk().replace("hosting-id: hst-00000000", "hosting-id: hst-00000001"),
])
def test_a_mutable_field_is_clean(pr_doc):
    assert _one(_disk(), pr_doc) == []


def test_a_kind_with_no_rule_is_clean():
    key = ("compute.cnrm.cloud.google.com/ComputeBackendService", "n", "be")
    doc = ("apiVersion: compute.cnrm.cloud.google.com/v1beta1\nkind: ComputeBackendService\n"
           "metadata:\n  name: be\n  namespace: n\nspec:\n  location: global\n  timeoutSec: {}\n")
    assert _one(doc.format(30), doc.format(60), key) == []


def test_an_unparsable_side_is_skipped():
    assert _one("kind: [", _disk("hyperdisk-balanced")) == []


# ── not sure, so a review and never a block ───────────────────────────────

def test_an_immutable_field_set_for_the_first_time_is_a_review():
    changes = _one(_disk(), _disk(extra="  physicalBlockSizeBytes: 4096\n"))
    assert _blocks(changes) == []
    [c] = _reviews(changes)
    assert (c["path"], c["old"], c["new"]) == ("spec.physicalBlockSizeBytes", "", "4096")


def test_an_immutable_field_removed_is_a_review():
    changes = _one(_disk(extra="  description: data disk\n"), _disk())
    assert _blocks(changes) == []
    assert [c["path"] for c in _reviews(changes)] == ["spec.description"]


def test_argo_replace_turns_a_block_into_a_review():
    replace = "    argocd.argoproj.io/sync-options: Replace=true\n"
    a = _disk().replace("  annotations:\n", "  annotations:\n" + replace)
    b = _disk("hyperdisk-balanced").replace("  annotations:\n", "  annotations:\n" + replace)
    changes = _one(a, b)
    assert _blocks(changes) == []
    assert [c["path"] for c in _reviews(changes)] == ["spec.type"]


def test_a_resource_id_unset_that_matches_the_name_is_the_same():
    assert _one(_disk(), _disk(extra="  resourceID: pv-dev-07-svc-a-data\n")) == []


def test_a_resource_id_change_blocks():
    a = _disk(extra="  resourceID: old-name\n")
    b = _disk(extra="  resourceID: new-name\n")
    [c] = _blocks(_one(a, b))
    assert c["path"] == "spec.resourceID"


# ── Kubernetes ────────────────────────────────────────────────────────────

def _deploy(app="web"):
    return f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
  namespace: n
spec:
  replicas: 2
  selector:
    matchLabels:
      app.kubernetes.io/name: {app}
  template:
    metadata:
      labels:
        app.kubernetes.io/name: {app}
    spec:
      containers: [{{name: c, image: "img:1"}}]
"""


def test_deployment_selector_change_blocks():
    key = ("apps/Deployment", "n", "web")
    [c] = _blocks(_one(_deploy(), _deploy(app="web2"), key))
    assert c["path"] == "spec.selector.matchLabels.app.kubernetes.io/name"
    assert c["family"] == "kubernetes"


def test_deployment_template_change_is_clean():
    key = ("apps/Deployment", "n", "web")
    assert _one(_deploy(), _deploy().replace("img:1", "img:2"), key) == []


def _job(image="img:1", annotations="", ttl=""):
    return f"""apiVersion: batch/v1
kind: Job
metadata:
  name: migrate
  namespace: n
{annotations}spec:
{ttl}  template:
    spec:
      restartPolicy: Never
      containers: [{{name: c, image: "{image}"}}]
"""


JOB_KEY = ("batch/Job", "n", "migrate")


def test_job_template_change_blocks():
    [c] = _blocks(_one(_job(), _job("img:2"), JOB_KEY))
    assert c["path"].startswith("spec.template")


def test_a_hook_job_is_recreated_so_it_is_clean():
    hook = "  annotations:\n    helm.sh/hook: post-install\n"
    assert _one(_job(annotations=hook), _job("img:2", annotations=hook), JOB_KEY) == []


def test_a_job_with_a_ttl_may_be_gone_so_it_is_a_review():
    ttl = "  ttlSecondsAfterFinished: 600\n"
    changes = _one(_job(ttl=ttl), _job("img:2", ttl=ttl), JOB_KEY)
    assert _blocks(changes) == [] and len(_reviews(changes)) == 1


def test_statefulset_volume_claim_template_change_blocks():
    def sts(size):
        return ("apiVersion: apps/v1\nkind: StatefulSet\nmetadata:\n  name: s\n  namespace: n\n"
                "spec:\n  serviceName: s\n  selector: {matchLabels: {a: b}}\n"
                "  volumeClaimTemplates:\n  - metadata: {name: data}\n"
                f"    spec: {{resources: {{requests: {{storage: {size}}}}}}}\n")
    [c] = _blocks(_one(sts("10Gi"), sts("20Gi"), ("apps/StatefulSet", "n", "s")))
    assert c["path"].startswith("spec.volumeClaimTemplates")


def test_service_cluster_ip_change_blocks():
    def svc(ip):
        return (f"apiVersion: v1\nkind: Service\nmetadata:\n  name: s\n  namespace: n\n"
                f"spec:\n  clusterIP: {ip}\n  ports: [{{port: 80}}]\n")
    [c] = _blocks(_one(svc("None"), svc("10.0.0.5"), ("Service", "n", "s")))
    assert (c["path"], c["old"], c["new"]) == ("spec.clusterIP", "None", "10.0.0.5")


def _pvc(sc="standard-rwo", size="10Gi"):
    return ("apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata:\n  name: p\n  namespace: n\n"
            f"spec:\n  storageClassName: {sc}\n  accessModes: [ReadWriteOnce]\n"
            f"  resources:\n    requests:\n      storage: {size}\n")


PVC_KEY = ("PersistentVolumeClaim", "n", "p")


def test_pvc_storage_class_change_blocks():
    [c] = _blocks(_one(_pvc(), _pvc(sc="premium-rwo"), PVC_KEY))
    assert c["path"] == "spec.storageClassName"


def test_pvc_shrink_blocks_and_growth_is_a_review():
    [c] = _blocks(_one(_pvc(size="20Gi"), _pvc(size="10Gi"), PVC_KEY))
    assert c["path"] == "spec.resources.requests.storage"
    grow = _one(_pvc(size="10Gi"), _pvc(size="20Gi"), PVC_KEY)
    assert _blocks(grow) == [] and len(_reviews(grow)) == 1


def test_immutable_configmap_data_change_blocks_and_a_normal_one_is_clean():
    def cm(value, immutable=True):
        flag = "immutable: true\n" if immutable else ""
        return (f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n  namespace: n\n"
                f"{flag}data:\n  k: {value}\n")
    key = ("ConfigMap", "n", "c")
    [c] = _blocks(_one(cm("a"), cm("b"), key))
    assert c["path"] == "data.k"
    assert _one(cm("a", False), cm("b", False), key) == []


def test_secret_type_change_blocks():
    def sec(t):
        return f"apiVersion: v1\nkind: Secret\nmetadata:\n  name: s\n  namespace: n\ntype: {t}\n"
    [c] = _blocks(_one(sec("Opaque"), sec("kubernetes.io/tls"), ("Secret", "n", "s")))
    assert c["path"] == "type"


def test_role_binding_role_ref_change_blocks():
    def rb(role):
        return ("apiVersion: rbac.authorization.k8s.io/v1\nkind: RoleBinding\n"
                "metadata:\n  name: r\n  namespace: n\n"
                f"roleRef: {{apiGroup: rbac.authorization.k8s.io, kind: Role, name: {role}}}\n"
                "subjects: [{kind: ServiceAccount, name: sa}]\n")
    [c] = _blocks(_one(rb("reader"), rb("writer"), ("rbac.authorization.k8s.io/RoleBinding", "n", "r")))
    assert c["path"] == "roleRef.name"


# ── Azure Service Operator ────────────────────────────────────────────────

def test_aso_location_and_owner_changes_block():
    def rg(location, owner="sub"):
        return ("apiVersion: resources.azure.com/v1api20200601\nkind: ResourceGroup\n"
                f"metadata:\n  name: pv-x-svc-a\n  namespace: pv-x-a\nspec:\n  location: {location}\n"
                f"  owner:\n    armId: /subscriptions/{owner}\n")
    key = ("resources.azure.com/ResourceGroup", "pv-x-a", "pv-x-svc-a")
    [c] = _blocks(_one(rg("centralus"), rg("eastus"), key))
    assert (c["path"], c["family"]) == ("spec.location", "aso")
    [c] = _blocks(_one(rg("centralus"), rg("centralus", owner="other"), key))
    assert c["path"].startswith("spec.owner")
    assert _one(rg("centralus"), rg("CentralUS"), key) == []


# ── a crash never costs a comment ─────────────────────────────────────────

def test_a_bug_in_the_guard_gives_no_change(monkeypatch):
    monkeypatch.setattr(ig, "_resource_changes", lambda *a, **k: 1 / 0)
    assert ig.find_changes({DISK_KEY: _disk()}, {DISK_KEY: _disk("x")}) == []


# ── the gate, the override and the panel ──────────────────────────────────

CHANGE = {"level": ig.BLOCK, "family": "kcc", "kind": "ComputeDisk", "name": "pv-dev-07-svc-a-data",
          "ns": "pv-dev-07-a", "path": "spec.type", "old": "pd-ssd", "new": "hyperdisk-balanced",
          "why": ""}
REVIEW = dict(CHANGE, level=ig.REVIEW, path="spec.physicalBlockSizeBytes", old="", new="4096",
              why=ig.WHY_SET)


def _results(*changes):
    return {"pv-dev-07-a-ss": m.DiffResult("x", [], 1, True, None, m.OUT_DIFF, "changes",
                                           immutable_changes=list(changes))}


def test_a_block_change_opens_one_gate_per_env():
    [g] = m._immutable_gates(_results(CHANGE, dict(CHANGE, path="spec.description")))
    assert (g["kind"], g["env"]) == ("immutable", "pv-dev-07-a")
    assert "ComputeDisk pv-dev-07-svc-a-data spec.type" in g["why"]
    assert cr.gate_trailer(g) == "Confirm-Recreate: pv-dev-07-a"
    assert "Confirm-Recreate: pv-dev-07-a" in cr.gate_status_description([g])


def test_a_review_change_opens_no_gate():
    assert m._immutable_gates(_results(REVIEW)) == []


def test_the_panel_names_the_resource_field_values_and_the_fix():
    gates = m._immutable_gates(_results(CHANGE))
    text = "\n".join(m._immutable_lines(gates, _results(CHANGE)))
    assert "`pv-dev-07-a`" in text and "**ComputeDisk** `pv-dev-07-svc-a-data`" in text
    assert "`spec.type`: `pd-ssd` \u2192 `hyperdisk-balanced`" in text
    assert "deletion-policy: abandon" in text and "Confirm-Recreate: pv-dev-07-a" in text
    assert "destroy-and-recreate" not in text and "nothing is recreated" in text


def test_a_lifted_gate_keeps_the_lines_as_a_confirmed_warning():
    gates = m._immutable_gates(_results(CHANGE))
    gates[0]["lifted"] = True
    text = "\n".join(m._immutable_lines(gates, _results(CHANGE)))
    assert "\u26d4" not in text and "\u2611\ufe0f" in text
    assert "`spec.type`: `pd-ssd` \u2192 `hyperdisk-balanced`" in text


def test_review_lines_say_why_they_are_not_blocking():
    text = "\n".join(m._immutable_lines([], _results(REVIEW)))
    assert "\u26a0\ufe0f" in text and ig.WHY_SET in text and "\u26d4" not in text


def test_the_old_disk_type_and_zone_wording_is_gone():
    import vm_analysis
    src = open(vm_analysis.__file__, encoding="utf-8").read() + open(m.__file__, encoding="utf-8").read()
    assert "destroy-and-recreate" not in src


# ── end to end: the build goes red, and the override turns it green ───────

def _replay_plan(plan):
    plan["pv-orch-a-ss"] = m.DiffResult(
        "--- main\n+++ pr", [("compute.cnrm.cloud.google.com/ComputeDisk pv-orch-a/pv-orch-svc-a-data",
                              "-  type: pd-ssd\n+  type: hyperdisk-balanced")],
        1, True, "", m.OUT_DIFF, "changes",
        immutable_changes=[dict(CHANGE, ns="pv-orch-a", name="pv-orch-svc-a-data")])


def test_pr_7492_replay_fails_the_build(world, monkeypatch):
    sinks, plan = world
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)
    monkeypatch.setattr(m, "_pr_commit_messages", lambda *a, **k: ["Use hyperdisk"])
    _replay_plan(plan)
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    state, desc = sinks.statuses[-1]
    assert state == "FAILED", sinks.statuses
    assert "Confirm-Recreate: pv-orch-a" in desc
    body = sinks.upserts[-1]
    assert "**ComputeDisk** `pv-orch-svc-a-data`" in body
    assert "`spec.type`: `pd-ssd` \u2192 `hyperdisk-balanced`" in body


def test_confirm_recreate_turns_the_failure_into_a_warning(world, monkeypatch):
    sinks, plan = world
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)
    monkeypatch.setattr(m, "_pr_commit_messages",
                        lambda *a, **k: ["Move the data disk\n\nConfirm-Recreate: pv-orch-a"])
    _replay_plan(plan)
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    assert sinks.statuses[-1][0] == "SUCCESSFUL", sinks.statuses
    assert "\u2611\ufe0f" in sinks.upserts[-1]


def test_argocd_diff_carries_the_changes(monkeypatch):
    text = ("===== compute.cnrm.cloud.google.com/ComputeDisk pv-dev-07-a/pv-dev-07-svc-a-data =====\n"
            "-  type: pd-ssd\n+  type: hyperdisk-balanced\n")
    monkeypatch.setattr(m, "_run_one_diff", lambda *a, **k: (
        text, None, "", None, 0, None, None, None, None, None, [CHANGE]))
    r = m.argocd_diff("pv-dev-07-a-ss", "aaaa1111", "bbbb2222")
    assert r.outcome == m.OUT_DIFF and r.immutable_changes == [CHANGE]
