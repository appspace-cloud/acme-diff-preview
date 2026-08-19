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


def render_lines(findings, header: str, env_threshold: int,
                 spoke_threshold: int) -> list:
    """Markdown block for the comment. `header` is the sentinel constant the
    verdict matcher in comment_render owns (one-constant rule)."""
    if not findings:
        return []
    lines = []
    for f in sorted(findings, key=lambda x: (-x["envs"], x["path"])):
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
