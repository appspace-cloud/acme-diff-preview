"""COPS-2693 Plan B: blast-radius assessment for shared-config changes.

The cadence cohorts stage version bumps procedurally (sandbox -> weekly ->
monthly), so a bad version reaches a small ring first. What bypasses that
staging is an edit to a SHARED `config.yaml` (cohort, spoke, tree or region
level): with `automated + prune + selfHeal` fleet-wide, whatever it renders
lands on every environment under it simultaneously, roughly five minutes
after merge. Reviewers can only infer that reach from the size of the diff.

This module is the pure half: given the changed keys of one shared file and
the environments it reaches, decide whether the change deserves a REVIEW
callout and render it. Deliberately NOT flagged:

  * version-only changes - the routine bump flow, however many environments
    they touch. Flagging those would train reviewers to ignore the finding.
  * anything below the thresholds - a single-spoke cohort tweak is the
    normal unit of work, not an event.
  * added/removed files - new-environment and decommission territory, each
    already owned by a dedicated panel.

The finding is informational (REVIEW), never a BLOCK: legitimate fleet-wide
changes exist, and the merge stays a human decision. The service half
(fetching both sides, mapping files to apps) lives in diff_preview next to
the sibling panels.
"""


def changed_keys(old_flat: dict, new_flat: dict) -> set:
    """Dotted keys added, removed, or whose value differs."""
    keys = set(old_flat) | set(new_flat)
    _absent = object()
    return {k for k in keys
            if old_flat.get(k, _absent) != new_flat.get(k, _absent)}


def is_version_only(keys) -> bool:
    """True when every changed key is a version pin (last segment 'version').

    That is the shape of the routine cohort bump - `appspace.version` in a
    cohort or spoke `config.yaml` - and it must never fire the finding, no
    matter how many environments inherit it. An empty set is version-only
    by convention (nothing to warn about).
    """
    return all(k.rsplit(".", 1)[-1] == "version" for k in keys)


def spoke_of(identity_file: str) -> str:
    """Cluster segment of an env path, for both fleet shapes.

    `gcp/prod/private-cloud/na2-a/monthly/pv-x-a/customer.yaml` -> `na2-a`
    `gcp/prod/public-cloud/na1-a/cl-prod-b/app3/customer.yaml`  -> `na1-a`
    Unknown shapes group under '?' rather than inflating the spoke count.
    """
    parts = identity_file.split("/")
    for i, p in enumerate(parts):
        if p in ("private-cloud", "public-cloud") and i + 1 < len(parts):
            return parts[i + 1]
    return "?"


def assess(path: str, keys: set, env_files, env_threshold: int,
           spoke_threshold: int):
    """One shared file's finding, or None.

    env_files: identity files (customer.yaml paths) of the environments the
    changed file reaches, already deduplicated by the caller.
    """
    if not keys or is_version_only(keys):
        return None
    envs = len(set(env_files))
    spokes = len({spoke_of(f) for f in env_files})
    if envs < env_threshold and spokes < spoke_threshold:
        return None
    return {"path": path, "envs": envs, "spokes": spokes,
            "keys": sorted(keys)}


_ABSENT = object()
_PATHS_SHOWN = 4


def _shown(value, hidden) -> str:
    """A value as the note shows it: like YAML, `unset` when missing."""
    if value is _ABSENT:
        return "`unset`"
    if hidden:
        return "***"
    if isinstance(value, bool):
        txt = "true" if value else "false"
    elif value is None:
        txt = "null"
    else:
        txt = (value if isinstance(value, str) else repr(value)).replace("`", "'")
    return f"`{txt[:48]}{'...' if len(txt) > 48 else ''}`"


def sibling_findings(files, env_threshold: int, spoke_threshold: int,
                     hide=None) -> list:
    """COPS-2766: the same change summed over sibling shared files.

    acme-config-prod #4565 set noCore in 11 weekly cohorts. Each one reached
    about 5 environments, under the thresholds, and all 57 got it at once.
    files: [(path, old_flat, new_flat, env_files)] of the shared files that
    got no finding of their own. A change is (key, old, new), and a missing
    key is not the same as false. Version keys are exempt, like in assess.
    A change in 2 or more files that reaches a threshold is kept, and the
    changes over the same files are one finding. hide(key) is true for a key
    whose values must not be shown.
    """
    groups = {}
    for path, old_flat, new_flat, env_files in files:
        for k in changed_keys(old_flat, new_flat):
            if is_version_only({k}):
                continue
            old, new = old_flat.get(k, _ABSENT), new_flat.get(k, _ABSENT)
            sig = (k, None if old is _ABSENT else repr(old),
                   None if new is _ABSENT else repr(new))
            hidden = bool(hide and hide(k))
            g = groups.setdefault(sig, {
                "paths": set(), "envs": set(),
                "change": f"`{k}` {_shown(old, hidden)} → {_shown(new, hidden)}"})
            g["paths"].add(path)
            g["envs"].update(env_files)
    out = {}
    for (k, _o, _n), g in sorted(groups.items(), key=lambda x: x[0][0]):
        spokes = len({spoke_of(f) for f in g["envs"]})
        if len(g["paths"]) < 2 or (len(g["envs"]) < env_threshold
                                   and spokes < spoke_threshold):
            continue
        f = out.setdefault(frozenset(g["paths"]), {
            "paths": sorted(g["paths"]), "envs": len(g["envs"]),
            "spokes": spokes, "keys": [], "changes": []})
        f["keys"].append(k)
        f["changes"].append(g["change"])
    return list(out.values())


def _sibling_line(f, header: str) -> str:
    shown = f["changes"][:6]
    more = len(f["changes"]) - len(shown)
    paths = f["paths"][:_PATHS_SHOWN]
    more_paths = len(f["paths"]) - len(paths)
    return ("⚠️ " + header + f" The same change in {len(f['paths'])} sibling "
            f"`config.yaml` files reaches **{f['envs']} environments across "
            f"{f['spokes']} spoke(s)**: " + ", ".join(shown)
            + (f" (+{more} more)" if more > 0 else "")
            + ". Each file alone is under the thresholds, but on merge they "
            "all land at the same time: " + ", ".join(f"`{p}`" for p in paths)
            + (f" (+{more_paths} more files)" if more_paths > 0 else "") + ".")


def render_lines(findings, header: str, env_threshold: int,
                 spoke_threshold: int) -> list:
    """Markdown block for the comment. `header` is the sentinel constant the
    verdict matcher in comment_render owns (one-constant rule)."""
    if not findings:
        return []
    lines = []
    for f in sorted(findings, key=lambda x: (-x["envs"],
                                             x.get("path") or x["paths"][0])):
        if "paths" in f:
            lines += [_sibling_line(f, header), ""]
            continue
        shown = f["keys"][:6]
        more = len(f["keys"]) - len(shown)
        keytxt = ", ".join("`%s`" % k for k in shown) + (
            f" (+{more} more)" if more > 0 else "")
        lines.append(
            "⚠️ " + header + f" `{f['path']}` changes non-version "
            f"values reaching **{f['envs']} environments across "
            f"{f['spokes']} spoke(s)**: {keytxt}. Changes to this shared "
            "file bypass cohort staging — they land on everything under "
            "it simultaneously on merge (auto-sync, prune and selfHeal are "
            "armed fleet-wide).")
        lines.append("")
    lines.append(
        f"*Thresholds: ≥{env_threshold} environments or "
        f"≥{spoke_threshold} spokes, version-only changes exempt "
        "(DIFF_BLAST_ENVS / DIFF_BLAST_SPOKES).*")
    lines.append("")
    return lines
