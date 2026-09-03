"""COPS-2721: call out value-file edits that already exist in a higher layer.

When an operator pastes HPA metrics/behavior (or any other block) into
`customer.yaml` that `gcp/config.yaml` (or another ancestor) already sets to
the same values, helm deep-merge leaves the rendered manifests byte-identical.
ACME Diff Preview correctly reports "No manifest changes", and without a
second panel the operator reads that as "the tool missed my HPA change"
(acme-config-prod #4520).

This module is the pure half: given the changed keys of one value file and
the flattened ancestor chain above it, decide which additions are redundant
copies of a higher layer and render the callout. Deliberately NOT flagged:

  * keys the parent does not set (those can change the render)
  * keys removed from the file (owned by the input-changes panel)
  * version-only pins (routine bump noise)

The finding is informational (REVIEW), never a BLOCK. The service half
(fetching both sides, walking the value-file chain) lives in diff_preview
next to the sibling panels.
"""


def changed_keys(old_flat: dict, new_flat: dict) -> set:
    """Dotted keys added, removed, or whose value differs."""
    keys = set(old_flat) | set(new_flat)
    _absent = object()
    return {k for k in keys
            if old_flat.get(k, _absent) != new_flat.get(k, _absent)}


def merge_flats(ordered_flats) -> dict:
    """Last-wins flatten merge, matching helm `-f` / Argo valueFiles order."""
    out = {}
    for flat in ordered_flats:
        if flat:
            out.update(flat)
    return out


def source_of(key: str, chain) -> str:
    """Last ancestor path that sets `key`, or '' if none."""
    src = ""
    for path, flat in chain or []:
        if flat and key in flat:
            src = path
    return src


def assess(path: str, old_flat: dict, new_flat: dict, parent_chain) -> dict:
    """One value file's redundancy finding, or None.

    parent_chain: ordered list of (path, flat) for files that helm applies
    BEFORE `path`. Later entries win, same as live merge.
    """
    delta = changed_keys(old_flat or {}, new_flat or {})
    if not delta:
        return None
    parent_eff = merge_flats(flat for _p, flat in (parent_chain or []))
    redundant = []
    effective = []
    for key in sorted(delta):
        if key not in (new_flat or {}):
            continue  # removed — not a redundant copy
        # Skip pure version pins; those are the routine bump shape.
        if key.rsplit(".", 1)[-1] == "version":
            continue
        new_val = new_flat[key]
        if key in parent_eff and parent_eff[key] == new_val:
            redundant.append({
                "key": key,
                "source": source_of(key, parent_chain) or "(higher layer)",
            })
        else:
            effective.append(key)
    if not redundant:
        return None
    return {
        "path": path,
        "redundant": redundant,
        "effective": effective,
        "all_redundant": not effective,
    }


def render_lines(findings, header: str) -> list:
    """Markdown block for the PR comment. `header` is the sentinel constant
    the verdict matcher in comment_render owns (one-constant rule)."""
    if not findings:
        return []
    lines = [
        "### \U0001f4da Higher-layer values already cover this PR",
        "",
        "Some keys this PR writes into a value file are **already set to the "
        "same value** by an ancestor (`gcp/config.yaml`, a cohort "
        "`config.yaml`, …). Helm deep-merges those layers, so copying them "
        "into `customer.yaml` does **not** change rendered manifests — ACME "
        "Diff Preview reporting \"No manifest changes\" is expected for "
        "those keys, not a missed diff.",
        "",
    ]
    for f in sorted(findings, key=lambda x: x["path"]):
        lines.append(f"`{f['path']}`:")
        lines.append("")
        # Group by source file so a pasted HPA block reads as one bullet
        # per ancestor, not one per dotted key.
        by_src = {}
        for item in f["redundant"]:
            by_src.setdefault(item["source"], []).append(item["key"])
        for src, keys in sorted(by_src.items()):
            shown = keys[:8]
            more = len(keys) - len(shown)
            keytxt = ", ".join(f"`{k}`" for k in shown)
            if more > 0:
                keytxt += f" (+{more} more)"
            lines.append(
                f"- \u267b\ufe0f already provided by `{src}`: {keytxt}")
        if f.get("all_redundant"):
            lines.append(
                "- \u2139\ufe0f **every non-version key this file changed "
                "is redundant** with a higher layer — expect no resource "
                "diff from these copies alone")
        elif f.get("effective"):
            n = len(f["effective"])
            lines.append(
                f"- the other {n} changed key(s) are **not** covered "
                "upstream and can still affect the render")
        lines.append("")
    lines.append(
        f"*{header} Drop the redundant copies from the env file, or keep "
        "them only when you intentionally want a local pin that matches "
        "today's parent (and accept that Diff Preview stays quiet until "
        "the parent drifts).*")
    lines.append("")
    return lines


def noop_status_hint(has_redundancy: bool, has_input_changes: bool) -> str:
    """Bitbucket build-status / footer hint when renders are unchanged.

    Keeps SUCCESSFUL (nothing failed) but stops the status reading like a
    silent miss when the PR clearly edited YAML.
    """
    if has_redundancy:
        return ("No manifest changes — some config edits already match a "
                "higher layer (see PR comment)")
    if has_input_changes:
        return ("No manifest changes — config YAML changed but render is "
                "identical (see PR comment)")
    return "No manifest changes"
