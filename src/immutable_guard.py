"""COPS-2833: fields that cannot change on a resource that already exists.

The guard compares the main render with the PR render of one app. A resource
counts only when both renders have it under the same key (group/Kind,
namespace, name), so a new environment, a new resource and a renamed one are
out of scope: other guards cover them.

Two levels:

- `block`: the API server, Config Connector or Azure Service Operator rejects
  the change for sure. The sync keeps failing and nothing is recreated. Each
  one opens the `immutable` merge gate of its environment.
- `review`: it may be rejected, or the answer depends on live state the
  render does not show (a field set or removed for the first time, a
  reference written another way, an object Argo CD replaces). A warning only.

Config Connector rules come from its CRDs (src/kcc_immutable_fields.json,
written by tools/kcc_immutable_fields.py), plus the fields every KCC kind keeps
for its life: location, zone, region and resourceID. Kubernetes and Azure
Service Operator have no such marks in their schemas, so their rules are the
short lists below: only rejections we are sure of.
"""
import json
import os
import re

import yaml

import logsink

BLOCK, REVIEW = "block", "review"

WHY_SET = ("set for the first time: fine only if it matches what the live "
           "resource already has")
WHY_REMOVED = "removed from the render: may be rejected"
WHY_REF_FORM = ("the reference is written another way: fine only if it still "
                "names the same object")
WHY_PROJECT = ("the project changes: the resource cannot move to another "
               "project, check this is the project where it already is")
WHY_REPLACE = ("Argo CD replaces this object (sync option Replace or Force), so "
               "the change deletes and recreates it")
WHY_TTL = ("the Job may already be gone after its TTL; if it still exists, the "
           "sync fails")
WHY_GROW = ("the volume grows: only when its StorageClass allows expansion")
WHY_UNREADABLE = "the size cannot be compared"

_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_RULES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "kcc_immutable_fields.json")
_KCC_GROUP = ".cnrm.cloud.google.com"
_ASO_GROUP = ".azure.com"

# Every Config Connector kind: the place and the GCP name never change after
# creation, whatever the CRD says. resourceID defaults to metadata.name.
_KCC_IDENTITY = ("spec.location", "spec.zone", "spec.region", "spec.resourceID")
# A project or folder change may be a move (rejected) or the same project
# written another way. We cannot tell, so these are review only.
_KCC_CONTAINER_REFS = ("spec.projectRef", "spec.folderRef", "spec.organizationRef")
_KCC_CONTAINER_ANNOTATIONS = ("cnrm.cloud.google.com/project-id",
                              "cnrm.cloud.google.com/folder-id",
                              "cnrm.cloud.google.com/organization-id")

# Kubernetes API validation (pkg/apis/*/validation in kubernetes/kubernetes):
# an update that changes these fields is rejected on every cluster.
_K8S_RULES = {
    "apps/Deployment": ("spec.selector",),
    "apps/ReplicaSet": ("spec.selector",),
    "apps/DaemonSet": ("spec.selector",),
    "apps/StatefulSet": ("spec.selector", "spec.serviceName",
                         "spec.volumeClaimTemplates", "spec.podManagementPolicy"),
    "batch/Job": ("spec.selector", "spec.template", "spec.completionMode"),
    "Service": ("spec.clusterIP", "spec.clusterIPs"),
    "PersistentVolumeClaim": ("spec.accessModes", "spec.storageClassName",
                              "spec.volumeMode", "spec.selector", "spec.dataSource",
                              "spec.dataSourceRef", "spec.volumeName",
                              "spec.resources.requests.storage"),
    "Secret": ("type",),
    "rbac.authorization.k8s.io/RoleBinding": ("roleRef",),
    "rbac.authorization.k8s.io/ClusterRoleBinding": ("roleRef",),
    "storage.k8s.io/StorageClass": ("provisioner", "parameters", "reclaimPolicy",
                                    "volumeBindingMode"),
}
# A ConfigMap or Secret with `immutable: true` keeps its data for life.
_K8S_IMMUTABLE_DATA = ("data", "binaryData", "stringData", "immutable")

# Azure Service Operator: its webhooks refuse a new owner or Azure name, and
# Azure never moves a resource to another location. A disk keeps how it was
# created.
_ASO_RULES = ("spec.owner", "spec.azureName", "spec.location")
_ASO_KIND_RULES = {"compute.azure.com/Disk": ("spec.creationData",)}

_HOOK_ANNOTATIONS = ("helm.sh/hook", "argocd.argoproj.io/hook")
_SYNC_OPTIONS = "argocd.argoproj.io/sync-options"
_PLACE_LEAVES = ("location", "zone", "region")
_NAME_LEAVES = ("resourceID", "azureName")

_rules_cache = {}


def _load():
    if "data" not in _rules_cache:
        with open(_RULES_FILE, encoding="utf-8") as fh:
            _rules_cache["data"] = json.load(fh)
    return _rules_cache["data"]


def kcc_rules() -> dict:
    """{"group/Kind": {path: "desc" | "cel"}} from the KCC CRDs."""
    return _load()["kinds"]


def kcc_version() -> str:
    return _load()["kcc_version"]


def find_changes(main_res, pr_res) -> list:
    """Every immutable-field change between two renders of one app.

    `main_res` and `pr_res` map (type_key, namespace, name) to the document
    text, as manifest._parse_manifest_resources returns them. A bug here is
    logged and gives []: a missing warning is better than a crashed diff."""
    try:
        changes = []
        for key in sorted(pr_res):
            old_txt = main_res.get(key)
            if old_txt is None or old_txt == pr_res[key]:
                continue
            changes += _resource_changes(key, old_txt, pr_res[key])
        return changes
    except Exception as e:
        logsink.log(f"[immutable] guard skipped: {e}", "WARNING")
        return []


def _doc(text):
    try:
        doc = yaml.load(text, Loader=_LOADER)
    except yaml.YAMLError:
        return None
    return doc if isinstance(doc, dict) else None


def _parts(path: str) -> tuple:
    return tuple(path.split("."))


def _rules_for(type_key: str, old: dict):
    """[(parts, source, family)] for one kind, ancestors before children."""
    group = type_key.rsplit("/", 1)[0] if "/" in type_key else ""
    if group.endswith(_KCC_GROUP):
        rules = [(_parts(p), s, "kcc") for p, s in kcc_rules().get(type_key, {}).items()]
        rules += [(_parts(p), "id", "kcc") for p in _KCC_IDENTITY]
        rules += [(_parts(p), "project", "kcc") for p in _KCC_CONTAINER_REFS]
        rules += [(("metadata", "annotations", a), "project", "kcc")
                  for a in _KCC_CONTAINER_ANNOTATIONS]
    elif group.endswith(_ASO_GROUP):
        paths = _ASO_RULES + _ASO_KIND_RULES.get(type_key, ())
        rules = [(_parts(p), "aso", "aso") for p in paths]
    else:
        rules = [(_parts(p), "k8s", "kubernetes") for p in _K8S_RULES.get(type_key, ())]
        if type_key in ("ConfigMap", "Secret") and old.get("immutable") is True:
            rules += [((p,), "k8s", "kubernetes") for p in _K8S_IMMUTABLE_DATA]
    # An immutable parent covers its children: keep the top-most rule only.
    kept = []
    for parts, source, family in sorted(rules, key=lambda r: len(r[0])):
        if not any(parts[:len(k[0])] == k[0] for k in kept):
            kept.append((parts, source, family))
    return kept


def _values(doc, parts):
    """The values at a path, `[]` expanding a list and `*` a map. A missing
    step, or a null, gives nothing: null in YAML means unset."""
    nodes = [doc]
    for part in parts:
        nxt = []
        many = part.endswith("[]")
        key = part[:-2] if many else part
        for node in nodes:
            if key == "*":
                if isinstance(node, dict):
                    nxt.extend(node.values())
                continue
            if not isinstance(node, dict) or key not in node:
                continue
            value = node[key]
            if many:
                if isinstance(value, list):
                    nxt.extend(value)
            else:
                nxt.append(value)
        nodes = nxt
    return [v for v in nodes if v is not None]


def _canon(v):
    """One spelling per value: a number, a bool and their string are equal,
    and a null inside a map is the same as no key."""
    if isinstance(v, dict):
        return {str(k): _canon(x) for k, x in v.items() if x is not None}
    if isinstance(v, list):
        return [_canon(x) for x in v]
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _key(v) -> str:
    return json.dumps(_canon(v), sort_keys=True)


def _same(old_vals, new_vals) -> bool:
    """Equal values, in any order: a list that only moves is not a change."""
    if [_key(v) for v in old_vals] == [_key(v) for v in new_vals]:
        return True
    if len(old_vals) == len(new_vals) == 1 and all(isinstance(v, list) for v in (*old_vals, *new_vals)):
        return sorted(_key(v) for v in old_vals[0]) == sorted(_key(v) for v in new_vals[0])
    return sorted(_key(v) for v in old_vals) == sorted(_key(v) for v in new_vals)


def _flat(v, prefix) -> dict:
    out = {}
    if isinstance(v, dict):
        for k, x in v.items():
            out.update(_flat(x, f"{prefix}.{k}"))
    elif isinstance(v, list):
        for i, x in enumerate(v):
            out.update(_flat(x, f"{prefix}[{i}]"))
    elif v is not None:
        out[prefix] = v
    return out


def _show(v) -> str:
    if v is None or v == "":
        return ""
    if isinstance(v, (dict, list)):
        s = json.dumps(_canon(v), sort_keys=True, separators=(",", ":"))
    else:
        s = _canon(v)
    return s if len(s) <= 80 else s[:79] + "\u2026"


def _leaf(path, old_vals, new_vals):
    """(path, old, new) of the first leaf that differs, so the line names
    `spec.bootDisk.initializeParams.sourceImageRef.external` and not just
    `spec.bootDisk`. Values at a path with `[]` are shown as a whole."""
    if "[]" in path or len(old_vals) > 1 or len(new_vals) > 1:
        return (path, ", ".join(_show(v) for v in old_vals),
                ", ".join(_show(v) for v in new_vals))
    old = old_vals[0] if old_vals else None
    new = new_vals[0] if new_vals else None
    if isinstance(old, (dict, list)) or isinstance(new, (dict, list)):
        a, b = _flat(old, path), _flat(new, path)
        for k in sorted(set(a) | set(b)):
            if _key(a.get(k)) != _key(b.get(k)):
                return k, _show(a.get(k)), _show(b.get(k))
    return path, _show(old), _show(new)


def _ref_target(v) -> str:
    """The last segment a reference names, whatever form it is written in."""
    if isinstance(v, dict):
        v = v.get("external") or v.get("name") or ""
    return str(v).rstrip("/").rsplit("/", 1)[-1].lower()


def _project(v) -> str:
    if isinstance(v, dict):
        v = v.get("external") or v.get("name") or ""
    v = str(v).strip()
    return v[len("projects/"):] if v.startswith("projects/") else v


def _quantity(v):
    """Bytes of a Kubernetes quantity like 10Gi or 500M, or None."""
    m = re.fullmatch(r"\s*([0-9.]+)\s*([KMGTPE]i?)?\s*", str(v))
    if not m:
        return None
    units = {"": 1, "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "P": 10**15,
             "E": 10**18, "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40,
             "Pi": 2**50, "Ei": 2**60}
    try:
        return float(m.group(1)) * units[m.group(2) or ""]
    except ValueError:
        return None


def _resource_changes(key, old_txt, new_txt) -> list:
    type_key, ns, name = key
    old, new = _doc(old_txt), _doc(new_txt)
    if old is None or new is None:
        return []
    annotations = (old.get("metadata") or {}).get("annotations") or {}
    if not isinstance(annotations, dict) or any(a in annotations for a in _HOOK_ANNOTATIONS):
        return []   # a hook is deleted and created again on every sync
    options = str(annotations.get(_SYNC_OPTIONS, ""))
    replaced = "Replace=true" in options or "Force=true" in options
    ttl = type_key == "batch/Job" and (old.get("spec") or {}).get("ttlSecondsAfterFinished") is not None
    kind = type_key.rsplit("/", 1)[-1]
    changes = []
    for parts, source, family in _rules_for(type_key, old):
        path = ".".join(parts)
        old_vals, new_vals = _values(old, parts), _values(new, parts)
        leaf = parts[-1]
        if leaf in _NAME_LEAVES:
            # Unset means the GCP or Azure name is metadata.name.
            meta_name = (old.get("metadata") or {}).get("name", name)
            old_vals = old_vals or [meta_name]
            new_vals = new_vals or [(new.get("metadata") or {}).get("name", name)]
        if not old_vals and not new_vals:
            continue
        if leaf in _PLACE_LEAVES:
            def norm(vals):
                return [re.sub(r"\s+", "", str(v)).lower() for v in vals]
            if norm(old_vals) == norm(new_vals):
                continue
        if _same(old_vals, new_vals):
            continue
        level, why = BLOCK, ""
        if not old_vals:
            level, why = REVIEW, WHY_SET
        elif not new_vals:
            level, why = REVIEW, WHY_REMOVED
        elif source == "project":
            if [_project(v) for v in old_vals] == [_project(v) for v in new_vals]:
                continue
            level, why = REVIEW, WHY_PROJECT
        elif (source == "desc" and "Ref" in path
              and [_ref_target(v) for v in old_vals] == [_ref_target(v) for v in new_vals]):
            level, why = REVIEW, WHY_REF_FORM
        elif path == "spec.resources.requests.storage":
            a, b = _quantity(old_vals[0]), _quantity(new_vals[0])
            if a is None or b is None:
                level, why = REVIEW, WHY_UNREADABLE
            elif b > a:
                level, why = REVIEW, WHY_GROW
        if level == BLOCK and replaced:
            level, why = REVIEW, WHY_REPLACE
        elif level == BLOCK and ttl and path == "spec.template":
            level, why = REVIEW, WHY_TTL
        where, before, after = _leaf(path, old_vals, new_vals)
        changes.append({"level": level, "family": family, "kind": kind, "name": name,
                        "ns": ns, "path": where, "old": before, "new": after, "why": why})
    return changes
