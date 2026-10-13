#!/usr/bin/env python3
"""Write src/kcc_immutable_fields.json from the Config Connector CRDs (COPS-2833).

The immutable guard reads the fields Config Connector refuses to change from
this file, so nobody picks them by hand. A field counts when its schema says
so in one of the two ways KCC uses:

- the description starts with "Immutable." (also "Optional. Immutable."):
  the generated Terraform-based kinds;
- an `x-kubernetes-validations` rule that is exactly `self == oldSelf`: the
  direct kinds, checked by the API server itself.

Run it again when the KCC version changes on the clusters:

    kubectl --context=gcp-prod-pv-na2-a get crd -o json > /tmp/crds.json
    python3 tools/kcc_immutable_fields.py /tmp/crds.json src/kcc_immutable_fields.json
"""
import json
import re
import sys
from collections import Counter

IMMUTABLE_DESC = re.compile(r"^\s*(?:(?:Optional|Required)\.\s*)*Immutable\.", re.I)
VERSION_KEY = "cnrm.cloud.google.com/version"


def _walk(schema, path, out):
    """Record every immutable path under `schema`. `[]` is a list item and
    `*` a map value, so the guard can match any index or key."""
    if not isinstance(schema, dict):
        return
    for rule in schema.get("x-kubernetes-validations") or []:
        if re.sub(r"\s+", " ", rule.get("rule", "")).strip() == "self == oldSelf":
            out[path] = "cel"
    if path not in out and IMMUTABLE_DESC.match(schema.get("description") or ""):
        out[path] = "desc"
    for name, sub in (schema.get("properties") or {}).items():
        _walk(sub, f"{path}.{name}", out)
    if isinstance(schema.get("items"), dict):
        _walk(schema["items"], f"{path}[]", out)
    if isinstance(schema.get("additionalProperties"), dict):
        _walk(schema["additionalProperties"], f"{path}.*", out)


def build(crd_list):
    kinds, versions = {}, Counter()
    for crd in crd_list["items"]:
        spec = crd["spec"]
        if not spec["group"].endswith(".cnrm.cloud.google.com"):
            continue
        meta = crd.get("metadata") or {}
        version = ((meta.get("annotations") or {}).get(VERSION_KEY)
                   or (meta.get("labels") or {}).get(VERSION_KEY))
        if version:
            versions[version] += 1
        served = next((v for v in spec["versions"] if v.get("storage")), spec["versions"][0])
        props = ((served.get("schema") or {}).get("openAPIV3Schema") or {}).get("properties") or {}
        found = {}
        _walk(props.get("spec") or {}, "spec", found)
        if found:
            kinds[f"{spec['group']}/{spec['names']['kind']}"] = dict(sorted(found.items()))
    return {"kcc_version": versions.most_common(1)[0][0] if versions else "",
            "kinds": dict(sorted(kinds.items()))}


def dump(data) -> str:
    """One line per kind, so a KCC upgrade reads as a short diff."""
    lines = ["{", f'  "kcc_version": {json.dumps(data["kcc_version"])},', '  "kinds": {']
    items = list(data["kinds"].items())
    for i, (kind, paths) in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        lines.append(f"    {json.dumps(kind)}: {json.dumps(paths, separators=(',', ':'))}{comma}")
    return "\n".join(lines + ["  }", "}", ""])


def main(argv):
    if len(argv) != 3:
        print(__doc__)
        return 2
    with open(argv[1], encoding="utf-8") as fh:
        data = build(json.load(fh))
    with open(argv[2], "w", encoding="utf-8") as fh:
        fh.write(dump(data))
    print(f"{len(data['kinds'])} kinds, {sum(len(p) for p in data['kinds'].values())} paths, "
          f"KCC {data['kcc_version']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
