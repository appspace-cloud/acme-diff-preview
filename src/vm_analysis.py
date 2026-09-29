"""VM and KCC infrastructure analysis: what a linux-services change does.

Sliced out of diff_preview.py unchanged (COPS-2658 phase 4).

These are the detectors, not the renderers. They read diff sections and
return structured facts -- which VM fields changed, whether a disk is
shrinking, whether a workload was zeroed, whether a role is moving from the
legacy prefix to KCC -- and the comment layer decides how to say it.

The detectors are deliberately conservative in the same direction as
everything else in this service: an unrecognised shape is reported as a fact
worth a reviewer's attention rather than quietly normalised away. A VM disk
that might be shrinking is a disk that gets flagged.
"""
import re

import yaml

from comment_render import (
    _NEW_ENV_CHECK_PREFIX,
    _VM_PANEL_DANGER_HDR,
    _VM_PANEL_ROUTINE_HDR,
    _section_name,
)
from manifest import _IP_KINDS, _section_kind  # decoder lives with the format it decodes


# Hibernation / zeroPods counting (COPS-2683): charts scale Deployments and
# StatefulSets. ReplicaSet is controller-owned and not chart-emitted alone.
# DaemonSet / Job / CronJob are not hibernation targets for this detector
# (decommission inventory still uses the broader set in manifest.py).
_WORKLOAD_KINDS = ("Deployment", "StatefulSet")


def _replicas_end_state(body: str):
    """(ends_at_zero, ends_positive) for one workload section body.

    Reads only the `+` side, because that is the state being applied. The
    previous version required a paired `- replicas: N` and therefore missed
    the most consequential case there is: the chart does not render
    `replicas` at all until `appspace.zeroPods` sets it, so switching an
    environment off produces a bare `+ replicas: 0` with no minus line.
    Live proof, acme-config-dev PR #7063: 110 workloads went to zero and the
    merge summary said "Routine - nothing dangerous detected".
    """
    ends_zero = ends_pos = False
    for line in body.splitlines():
        if not line.startswith("+"):
            continue
        ls = line.lstrip("+ ").strip()
        if not ls.startswith("replicas:"):
            continue
        try:
            value = int(ls.split(":", 1)[1].strip())
        except ValueError:
            continue
        if value == 0:
            ends_zero = True
        else:
            ends_pos = True
    return ends_zero, ends_pos


def _detect_replicas_zeroed(sections: list) -> list:
    """Workload sections whose applied state is exactly 0 replicas.

    Zeroing can be legitimate hibernation (`zeroPods`), so this is a fact to
    report rather than a reason to block. It is computed on the FULL pre-cap
    section list for the usual reason: a safety fact may never depend on what
    survived a display cap.
    """
    zeroed = []
    for header, body in sections:
        if _section_kind(header) not in _WORKLOAD_KINDS:
            continue
        ends_zero, ends_pos = _replicas_end_state(body)
        if ends_zero and not ends_pos:
            zeroed.append(header)
    return zeroed


def _res_kind_name(key):
    """(Kind, name) of a _parse_manifest_resources key, name None if absent."""
    if not isinstance(key, tuple):
        return str(key).rsplit("/", 1)[-1], None
    return key[0].rsplit("/", 1)[-1], (key[2] if len(key) > 2 else None)


_REPLICAS_FIELD_RE = re.compile(r"^\s{0,4}replicas:")


def _detect_replicas_released(main_resources, pr_resources) -> list:
    """[(workload, n)]: fixed replicas whose field goes away (COPS-2766).

    An HPA or acme-ping-scaler takes over, so the chart stops rendering
    `replicas`. On sync the field goes away and Kubernetes runs 1 replica
    until the new owner scales it back (acme-config-prod #4523, 10 to 1).
    Only a workload on both sides, with n > 1 before: from 1, nothing drops.
    """
    out = []
    pr_resources = pr_resources or {}
    for key, body in (main_resources or {}).items():
        kind, name = _res_kind_name(key)
        head = pr_resources.get(key)
        if kind not in _WORKLOAD_KINDS or not name or head is None:
            continue
        n = _manifest_replicas(body)
        if n is not None and n > 1 and \
                not any(_REPLICAS_FIELD_RE.match(l) for l in head.splitlines()):
            out.append((name, n))
    return sorted(out)


def _count_hpas_remaining(pr_resources) -> int:
    """How many HorizontalPodAutoscaler resources the PR-side render still
    wants. Used with full-env shutdown: flipping `zeroPods` zeroes Deployments
    but historically left HPAs unchanged, so they never appear in the unified
    diff and a REVIEW-only "shutting down" finding looked complete (COPS-2677).
    """
    if not pr_resources:
        return 0
    n = 0
    for key in pr_resources:
        type_key = key[0] if isinstance(key, tuple) else str(key)
        if "HorizontalPodAutoscaler" in type_key:
            n += 1
    return n


_HPA_TARGET_NAME_RE = re.compile(
    r"^\s*name:\s*[\"']?([^\s\"'#]+)[\"']?\s*(?:#.*)?$", re.M)


def _hpa_scale_target_name(body: str):
    """scaleTargetRef.name from an HPA manifest body, or None."""
    if not body:
        return None
    # Prefer the block under scaleTargetRef so a top-level metadata.name
    # cannot poison the reading.
    idx = body.find("scaleTargetRef:")
    if idx < 0:
        return None
    block = body[idx:idx + 400]
    m = _HPA_TARGET_NAME_RE.search(block)
    return m.group(1) if m else None


def _zeroed_workload_names(pr_resources) -> set:
    """Deployment/StatefulSet names whose PR-side replicas end at 0."""
    names = set()
    if not pr_resources:
        return names
    for key, body in pr_resources.items():
        type_key = key[0] if isinstance(key, tuple) else str(key)
        kind = type_key.rsplit("/", 1)[-1]
        if kind not in _WORKLOAD_KINDS:
            continue
        if _manifest_replicas(body) != 0:
            continue
        name = key[2] if isinstance(key, tuple) and len(key) > 2 else None
        if name:
            names.add(name)
    return names


def _count_hpas_targeting_zeroed(pr_resources) -> int:
    """HPAs whose scaleTargetRef names a zeroed Deployment/StatefulSet.

    COPS-2683: partial scale-to-zero must not shout every leftover HPA in
    the fleet (COPS-2680), but must still warn when the zeroed workloads
    keep an HPA that can fight them back up.
    """
    targets = _zeroed_workload_names(pr_resources)
    if not targets or not pr_resources:
        return 0
    n = 0
    for key, body in pr_resources.items():
        type_key = key[0] if isinstance(key, tuple) else str(key)
        if "HorizontalPodAutoscaler" not in type_key:
            continue
        if _hpa_scale_target_name(body) in targets:
            n += 1
    return n


_MANIFEST_REPLICAS_RE = re.compile(r"^(\s*)replicas:\s*(\d+)\s*(?:#.*)?$")


def _manifest_replicas(body: str):
    """spec.replicas from a full manifest body, or None if unset.

    HPA-managed Deployments often omit the field; those are not "at zero".
    Only shallow `replicas:` lines count (indent <= 4) so a nested key inside
    template metadata cannot poison the reading.
    """
    if not body:
        return None
    found = None
    for line in body.splitlines():
        m = _MANIFEST_REPLICAS_RE.match(line)
        if not m:
            continue
        if len(m.group(1)) > 4:
            continue
        found = int(m.group(2))
    return found


def _count_workload_replicas(pr_resources):
    """(total_workloads, zeroed) from the full PR-side resource map.

    Same reason as `_count_hpas_remaining`: unchanged Deployments never appear
    in the unified diff. Counting only diff sections made scaling two
    services to 0 look like a whole-environment shutdown (COPS-2680 /
    acme-config-prod #4321).
    """
    if not pr_resources:
        return 0, 0
    total = zeroed = 0
    for key, body in pr_resources.items():
        type_key = key[0] if isinstance(key, tuple) else str(key)
        kind = type_key.rsplit("/", 1)[-1]
        if kind not in _WORKLOAD_KINDS:
            continue
        total += 1
        if _manifest_replicas(body) == 0:
            zeroed += 1
    return total, zeroed


def _detect_workload_shutdown(sections: list, pr_resources=None,
                               hpas_remaining=None,
                               replica_stats=None):
    """{"zeroed", "workloads", "hpas_remaining", "hpas_targeting_zeroed"}
    over workloads, or None.

    The ratio is what separates "one service was scaled down" from "this
    environment is being switched off", and the two deserve different
    wording in the merge summary. Counted here, pre-cap, alongside the other
    safety facts.

    Prefer the full PR-side render for `workloads` / `zeroed` when available
    (`pr_resources` or precomputed `replica_stats=(total, zeroed)` from
    `_run_one_diff`). Diff sections alone only see what changed (COPS-2680).
    `hpas_remaining` is the same: unchanged HPAs are invisible in unified
    diffs (COPS-2677).

    COPS-2683: `replica_stats=(0, 0)` (empty parse) must not disable the
    sections fallback when unified-diff hunks still show workloads going
    to zero. `hpas_targeting_zeroed` counts only HPAs aimed at zeroed
    workloads so partial scale can warn without the fleet HPA shout.
    """
    total = zeroed = 0
    if replica_stats is not None:
        total, zeroed = replica_stats
    elif pr_resources:
        total, zeroed = _count_workload_replicas(pr_resources)
    if not total:
        # Fall back to unified-diff sections (COPS-2683): replica_stats=(0,0)
        # or an empty parse must not hide Deployments going to zero in hunks.
        total = zeroed = 0
        for header, body in sections or []:
            if _section_kind(header) not in _WORKLOAD_KINDS:
                continue
            total += 1
            ends_zero, ends_pos = _replicas_end_state(body)
            if ends_zero and not ends_pos:
                zeroed += 1
        if not total:
            return None
    if hpas_remaining is None:
        hpas_remaining = _count_hpas_remaining(pr_resources)
    targeting = _count_hpas_targeting_zeroed(pr_resources) if pr_resources else 0
    return {"zeroed": zeroed, "workloads": total,
            "hpas_remaining": int(hpas_remaining or 0),
            "hpas_targeting_zeroed": int(targeting or 0)}


# ── Capacity floors (COPS-2766, COPR-32597) ──────────────────────────
# Warnings only: a planned right-sizing looks the same as a mistake.
# The connection services hold long-lived device connections, so any
# smaller floor there is flagged, not only a cut of 4.
_CONN_SERVICES = frozenset({"devicegateway", "signschannel",
                            "signschannelgateway", "pushnotification"})
_HPA_BOUND_RE = re.compile(r"^(\s*)(minReplicas|maxReplicas):\s*(.*?)\s*(?:#.*)?$")
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _yaml_doc(text):
    return yaml.load(text, Loader=_YAML_LOADER)


def _hpa_bounds(body):
    """(minReplicas, maxReplicas) of an HPA body. min defaults to 1 like
    Kubernetes. A value that is not a number gives None."""
    lo, hi = 1, None
    for line in body.splitlines():
        mt = _HPA_BOUND_RE.match(line)
        if not mt or len(mt.group(1)) > 4:
            continue
        v = mt.group(3).strip("\"'")
        v = int(v) if v.isdigit() else None
        if mt.group(2) == "minReplicas":
            lo = v
        else:
            hi = v
    return lo, hi


def _replica_floors(resources) -> dict:
    """{workload: (floor, max)}. A Deployment or StatefulSet gives
    (spec.replicas, None). An HPA overrides its scaleTargetRef (else its own
    name) with (minReplicas, maxReplicas). No replicas field and no HPA
    (acme-ping-scaler), or HPA bounds that are not numbers: no entry."""
    floors, hpas = {}, {}
    for key, body in (resources or {}).items():
        kind, name = _res_kind_name(key)
        if not name:
            continue
        if kind in _WORKLOAD_KINDS:
            n = _manifest_replicas(body)
            if n is not None:
                floors[name] = (n, None)
        elif kind == "HorizontalPodAutoscaler":
            hpas[_hpa_scale_target_name(body) or name] = _hpa_bounds(body)
    for target, (lo, hi) in hpas.items():
        if lo is None or hi is None:
            floors.pop(target, None)
        else:
            floors[target] = (lo, hi)
    return floors


def _cpu_millicores(v):
    """'20m' -> 20, '0.02' -> 20, '1' or 1 -> 1000; anything else None."""
    if v is None or isinstance(v, bool):
        return None
    try:
        s = str(v).strip()
        if s.endswith("m"):
            return int(s[:-1])
        return round(float(s) * 1000)
    except (ValueError, OverflowError):
        return None


def _main_container_cpu(body, name):
    """CPU request of the main container in millicores, or None. The main
    container is the one named like the workload (the chart names it
    `<service>-container`), else the first. initContainers never count."""
    try:
        containers = _yaml_doc(body)["spec"]["template"]["spec"]["containers"]
        main = next((c for c in containers
                     if c.get("name") in (name, f"{name}-container")), containers[0])
        return _cpu_millicores(main["resources"]["requests"]["cpu"])
    except Exception:
        return None


def _detect_capacity_floor_risk(main_resources, pr_resources) -> list:
    """[(workload, what)] for the workloads whose render changes and whose
    capacity drops (COPR-32597):

    - an HPA floor under 2 with room to scale (max > 1), unless the base
      already had one;
    - a smaller floor on a connection service (#4523: 10 replicas to an HPA
      with min 6 on signschannel);
    - a cut of 4 or more on any workload (#4608, 12 to 8). A cut to 0 stays
      the zeroed-replicas finding;
    - a main container CPU request under 30m that is new or lower in this
      PR. A raise, 10m to 20m, is no cut.

    The floor compares fixed replicas with an HPA min. A workload with no
    floor on a side (acme-ping-scaler) is not compared.
    """
    main_resources, pr_resources = main_resources or {}, pr_resources or {}
    changed, bodies = set(), set()
    for key in set(main_resources) | set(pr_resources):
        old, new = main_resources.get(key), pr_resources.get(key)
        kind, name = _res_kind_name(key)
        if old == new or not name:
            continue
        if kind in _WORKLOAD_KINDS:
            changed.add(name)
            bodies.add(key)
        elif kind == "HorizontalPodAutoscaler":
            changed.update(_hpa_scale_target_name(b) or name for b in (old, new) if b)
    if not changed:
        return []
    before, after = _replica_floors(main_resources), _replica_floors(pr_resources)
    cpu = {}
    for key in bodies:
        if key not in pr_resources:
            continue
        new_cpu = _main_container_cpu(pr_resources[key], key[2])
        if new_cpu is None or new_cpu >= 30:
            continue
        old_cpu = (_main_container_cpu(main_resources[key], key[2])
                   if key in main_resources else None)
        if old_cpu is not None and new_cpu >= old_cpu:
            continue    # the same or a raise: no cut
        cpu[key[2]] = new_cpu
    out = []
    for w in sorted(changed):
        o, n = before.get(w), after.get(w)
        parts = []
        if n and n[1] is not None and n[0] < 2 and n[1] > 1 \
                and not (o and o[1] is not None and o[0] < 2):
            parts.append(f"HPA minReplicas {n[0]} (max {n[1]})")
        if o and n and 0 < n[0] < o[0] and (w in _CONN_SERVICES or o[0] - n[0] >= 4):
            parts.append(f"floor {o[0]} \u2192 {n[0]}")
        if w in cpu:
            parts.append(f"CPU request {cpu[w]}m")
        if parts:
            out.append((w, " and ".join(parts)))
    return out


# ── VM-domain (KCC linux-services) risk detection ────────────────────
# The slowest thing on this platform to recover from is a botched virtual
# machine change: unlike a Kubernetes rollout there is no quick rollback
# for a wrong machine type, a shrunk disk, or a deletion-policy flip that
# lets the next cascade actually destroy the VM. The reviewers who read
# these comments daily asked for VM changes to be unmistakable. Detection
# is deterministic and runs on the FULL pre-cap section list (the PR-6773
# lesson: display caps must never hide a safety fact), exactly like the
# deleted-resources detection above.

_VM_KINDS = ("ComputeInstance", "ComputeDisk", "ComputeAddress",
             "ComputeDiskResourcePolicyAttachment")
_VM_DELETION_POLICY_KEY = "cnrm.cloud.google.com/deletion-policy"
# Fields worth reporting per kind, taken from the templates in
# acme-components helm-charts/supporting-services/templates/
# kcc-linux-services/. `type` only counts as a disk type when the value is
# disk-shaped (pd-*/hyperdisk-*), which keeps unrelated `type:` keys (e.g.
# a network accessConfigs type) out of the report.
_VM_TRACKED_FIELDS = {
    "ComputeInstance": ("machineType", "zone", "desiredStatus",
                        "deletionProtection", "size", "type",
                        # deviceName lives inside attachedDisk[], but the
                        # body is parsed line by line, so the nesting does
                        # not matter here. Tracked because a mismatch makes
                        # KCC rename the attachment by detaching and
                        # reattaching a live disk (COPS-2592).
                        "deviceName",
                        _VM_DELETION_POLICY_KEY),
    "ComputeDisk": ("size", "type", "location", _VM_DELETION_POLICY_KEY),
    "ComputeAddress": ("address", _VM_DELETION_POLICY_KEY),
    "ComputeDiskResourcePolicyAttachment": ("resourceID", "zone"),
}
_VM_DISK_TYPE_RE = re.compile(r"^(pd-|hyperdisk-)")


def _vm_unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


# COPS-2766: every shrink producer writes this, and the `shrink` merge gate
# looks for it. A shrink is never valid, so no trailer lifts that gate.
_VM_SHRINK_REASON = "GCP cannot shrink a disk in place"


def _vm_deleted_body_policy(body: str) -> str:
    """Return 'abandon', 'delete', or '' from minus lines of a deleted CR."""
    saw_abandon = saw_delete = False
    for line in (body or "").splitlines():
        if not line.startswith("-"):
            continue
        if "deletion-policy" not in line:
            continue
        low = line.lower()
        if "abandon" in low:
            saw_abandon = True
        if "deletion-policy: delete" in low or 'deletion-policy: "delete"' in low \
                or "deletion-policy: 'delete'" in low:
            saw_delete = True
    if saw_delete and not saw_abandon:
        return "delete"
    if saw_abandon:
        return "abandon"
    return ""


def _released_addresses(sections, deleted) -> list:
    """COPS-2766: the deleted ComputeAddress and DNSRecordSet headers that GCP
    releases, all but an explicit `deletion-policy: abandon`."""
    gone = set(deleted or ())
    return [h for h, body in sections if h in gone and _section_kind(h) in _IP_KINDS
            and _vm_deleted_body_policy(body) != "abandon"]


def _detect_vm_changes(sections: list) -> list:
    """Structured facts for every VM-domain (KCC linux-services) section.

    Returns a list of dicts:
      {header, kind, name, fields: [(field, old, new)], created, deleted,
       dangerous: [reason, ...], notes: [note, ...]}

    The severity rules come straight from the rendering templates and their
    runbook comments in acme-components:
      - deletion-policy moving to `delete`, or deletionProtection turning
        false, means the next cascade/prune can actually destroy the
        resource in GCP (both are driven by allowDeletion) — dangerous.
      - a machineType change requires parking the VM first (desiredStatus:
        TERMINATED, wait for KCC, then back to RUNNING). A machineType
        change with no TERMINATED transition or TERMINATED state anywhere
        in the section is exactly the mistake the template comment warns
        about — dangerous.
      - zone and disk `type` are immutable in GCP: changing them means
        destroy-and-recreate — dangerous.
      - a disk size DECREASE is impossible in place (GCP only grows disks),
        so it implies recreation and data loss — dangerous. Growth is the
        routine case.
      - a whole VM-domain resource disappearing from the render under
        `deletion-policy: abandon` (chart default when allowDeletion is
        unset) is unmanage: GCP is kept (orphaned). The same disappearance
        with `deletion-policy: delete`, or with no policy line (KCC's own
        default is delete), is dangerous. A snapshot-policy attachment
        disappearing is a schedule note, not a VM destroy.
    Everything else in the domain (status transitions, brand-new resources,
    an address re-pin) is reported as a routine/notable line — the panel
    only shouts when shouting is deserved, or nobody trusts it.
    """
    facts = []
    for header, body in sections or []:
        kind = _section_kind(header)
        if kind not in _VM_KINDS:
            continue
        tracked = _VM_TRACKED_FIELDS[kind]
        minus_vals, plus_vals = {}, {}
        untracked_keys = set()
        minus_n = plus_n = context_n = 0
        context_terminated = False
        for line in body.splitlines():
            if line.startswith("+++") or line.startswith("---"):
                continue
            sign = line[:1]
            if sign == " ":
                context_n += 1
                if "desiredStatus:" in line and "TERMINATED" in line:
                    context_terminated = True
                continue
            if sign not in ("+", "-"):
                continue
            if sign == "+":
                plus_n += 1
            else:
                minus_n += 1
            content = line[1:].strip()
            if ":" not in content:
                continue
            key, _, val = content.partition(":")
            key = key.strip()
            if key not in tracked:
                # COPS-2618: a closed tracked-field list means anything
                # outside it was dropped in silence, so a section that
                # genuinely changed could still leave the panel with
                # nothing to say and the caller would render
                # "VM infrastructure - no changes". That is what happened
                # on acme-config-prod #3923, where three objects each
                # gained five taxonomy labels and the panel denied it.
                # Remembering the untracked keys keeps the panel honest by
                # construction, for fields nobody has thought of yet as
                # much as for labels.
                if key and not key.startswith("-") and " " not in key:
                    untracked_keys.add(key)
                continue
            val = _vm_unquote(val)
            if key == "type" and not _VM_DISK_TYPE_RE.match(val):
                continue
            (plus_vals if sign == "+" else minus_vals).setdefault(
                key, []).append(val)
        deleted = bool(minus_n and not plus_n and not context_n)
        created = bool(plus_n and not minus_n and not context_n)
        fields, dangerous, notes = [], [], []
        if not deleted and not created:
            for key in tracked:
                olds, news = minus_vals.get(key, []), plus_vals.get(key, [])
                if not olds and not news:
                    continue
                old = olds[0] if olds else ""
                new = news[0] if news else ""
                if old == new:
                    continue
                fields.append((key, old, new))
        byk = {k: (o, n) for (k, o, n) in fields}
        orphaned = False
        if deleted:
            if kind == "ComputeDiskResourcePolicyAttachment":
                # Attachments do not carry deletion-policy. Pruning the CR may
                # detach the schedule from the disk, but it does not destroy
                # the VM, disk, IP, or existing snapshots (COPS-2682).
                notes.append(
                    "snapshot-policy attachment leaves KCC — the disk may "
                    "stop getting new scheduled snaps; existing snapshots "
                    "and the disk itself stay")
            else:
                policy = _vm_deleted_body_policy(body)
                if policy == "delete":
                    dangerous.append(
                        "%s removed from the render with "
                        "`deletion-policy: delete` — Argo prune can destroy "
                        "this resource in GCP" % kind)
                elif policy == "abandon":
                    # COPS-2682 / acme-config-prod #4326: disabling KCC for a
                    # TERMINATED svc VM must read as unmanage, not destroy.
                    # COPS-2766: only when the CR says so. kcc-linux-services
                    # always writes the policy, and with no line KCC deletes.
                    orphaned = True
                    notes.append(
                        "%s leaves Argo under `deletion-policy: abandon` — "
                        "GCP resource is kept (orphaned), not deleted" % kind)
                else:
                    dangerous.append(
                        "%s removed from the render entirely (an enabled flag "
                        "turned off, or the environment dropped the domain)"
                        % kind)
        elif created:
            notes.append("new %s — appears in this environment for the "
                         "first time" % kind)
        else:
            pol = byk.get(_VM_DELETION_POLICY_KEY)
            if pol and pol[1] == "delete":
                dangerous.append("deletion-policy moves to `delete` — the "
                                 "next cascade can destroy this resource "
                                 "in GCP")
            prot = byk.get("deletionProtection")
            if prot and prot[1].lower() == "false":
                dangerous.append("deletionProtection turns OFF — GCP-side "
                                 "delete protection is removed")
            if "machineType" in byk:
                ds_new = byk.get("desiredStatus", ("", ""))[1]
                if ds_new != "TERMINATED" and not context_terminated:
                    dangerous.append("machineType changes while the VM is "
                                     "not parked TERMINATED — the runbook "
                                     "requires stopping the VM first")
            if "deviceName" in byk:
                o, n = byk["deviceName"]
                if o and n:
                    dangerous.append(
                        "attachedDisk `deviceName` changes `%s` → `%s` — KCC "
                        "renames an attachment by DETACHING and reattaching "
                        "the disk; on a RUNNING VM that happens under a "
                        "mounted filesystem" % (o, n))
                else:
                    # Pin added or removed. The chart omits deviceName when
                    # adopting, so the attachment name simply stops being
                    # managed; nothing is detached. Visible, not alarming —
                    # flagging it would block every adoption PR, which is
                    # what COPS-2608 just stopped doing.
                    notes.append(
                        "attachedDisk `deviceName` %s (`%s`) — the attachment "
                        "name stops or starts being managed; no disk "
                        "operation follows from this alone"
                        % ("removed" if o else "added", o or n))
            if "zone" in byk:
                dangerous.append("zone is immutable — changing it means "
                                 "destroy-and-recreate")
            if "type" in byk:
                dangerous.append("disk type is immutable — changing it "
                                 "means destroy-and-recreate")
            if "size" in byk:
                o, n = byk["size"]
                try:
                    if int(float(n)) < int(float(o)):
                        dangerous.append(
                            f"disk size DECREASES — {_VM_SHRINK_REASON}; "
                            "this implies recreation and data loss")
                except ValueError:
                    # A size that is not plainly numeric (a templated value,
                    # or one carrying a unit suffix) cannot be compared, so
                    # the shrink check is skipped on purpose. The field is
                    # still reported above as an ordinary changed field.
                    #
                    # COPS-2668: this used to `continue`, which targets the
                    # OUTER `for header, body in sections` loop -- there is no
                    # loop in between -- so one unparseable size discarded the
                    # whole section: the untracked-keys note and the
                    # facts.append below both went with it, and the panel fell
                    # silent about a disk it could see changing. `pass` skips
                    # only the comparison, which is what the comment above
                    # always claimed.
                    pass
        # COPS-2618: a change the tracked list cannot describe is still a
        # change. Naming the keys keeps the line actionable -- a reviewer can
        # tell a taxonomy-label rollout from something worth opening the diff
        # for -- and guarantees the caller never renders "no changes" over a
        # section that visibly moved.
        if untracked_keys and not deleted and not created:
            shown = sorted(untracked_keys)
            notes.append(
                "other field(s) changed, not individually tracked by this "
                "panel: %s%s" % (", ".join("`%s`" % k for k in shown[:8]),
                                 "" if len(shown) <= 8
                                 else " and %d more" % (len(shown) - 8)))
        facts.append({"header": header, "kind": kind,
                      "name": _section_name(header), "fields": fields,
                      "created": created, "deleted": deleted,
                      "orphaned": orphaned,
                      "dangerous": dangerous, "notes": notes})
    return facts


def _vm_deletion_armed_flat(flat: dict) -> bool:
    """Phase 1 state: the real VM, disk and IP are only deleted by the
    cascade when allowDeletion is armed. Same keys _decommission_fully_phased
    reads, applied to an already flattened dict.

    COPS-2683: chart digs role-level `allowDeletion` too, not only
    `defaults.allowDeletion`. Treating defaults alone left role-armed
    environments looking unarmed (and the reverse strip path incomplete).
    """
    if not flat:
        return False
    val = flat.get("appspace.infra.deployLinuxServicesK8s.defaults.allowDeletion")
    if str(val).strip().lower() == "true":
        return True
    for role in _VM_ROLE_NAMES:
        if role == "defaults":
            continue
        k = f"appspace.infra.deployLinuxServicesK8s.{role}.allowDeletion"
        if str(flat.get(k, "")).strip().lower() == "true":
            return True
    return False


_VM_FLAT_PREFIX = "appspace.infra.deployLinuxServicesK8s."

# COPS-2710: the arming flags themselves, which cannot be "the VM config the
# arming acts through". Removing `allowDeletion` is a disarm: helm keeps
# rendering every CR, they simply go back to `deletion-policy: abandon`,
# which is the safe direction. Counting it as a strip made a rollback on an
# armed environment render as "VM CONFIG STRIPPED WHILE ARMING DECOMMISSION"
# and advise the operator to do the opposite of what they were doing.
_VM_ARMING_LEAF_KEYS = ("allowDeletion", "confirmProdDeletion")


def _is_vm_arming_key(flat_key: str) -> bool:
    return flat_key.rsplit(".", 1)[-1] in _VM_ARMING_LEAF_KEYS


def _vm_config_stripped(old_flat: dict, new_flat: dict) -> list:
    """Keys under deployLinuxServicesK8s this diff removes or switches off.

    COPS-2660: `allowDeletion` only takes effect through resources helm still
    renders. Strip the role blocks or flip an `enabled` to false in the same
    PR that arms deletion, and the chart stops emitting the VM CRs, ArgoCD
    prunes them, and the live objects go out under their current
    `deletion-policy: abandon` -- the real VM, disk and IP are orphaned in
    the cloud, not deleted. acme-config-prod PR #4247 shipped exactly that
    shape while its comment read "Phase 1 done".

    Removal and `true -> false` are the same event to the chart (both end
    the render), so both are reported. Returned keys are the evidence the
    warning shows the reviewer; empty list means the VM config survived the
    diff intact.
    """
    removed = [k for k in old_flat
               if k.startswith(_VM_FLAT_PREFIX) and k not in new_flat
               and not _is_vm_arming_key(k)]
    disabled = [k for k in old_flat
                if k.startswith(_VM_FLAT_PREFIX) and k.endswith(".enabled")
                and str(old_flat.get(k)).strip().lower() == "true"
                and str(new_flat.get(k, "")).strip().lower() == "false"]
    return sorted(set(removed) | set(disabled))


# Disk-size keys differ between the legacy and KCC value schemas.
_VM_DISK_SIZE_KEYS = ("dataDiskSizeGb", "bootDiskSizeGb",
                      "dataDiskSize", "bootDiskSize", "diskSize")


_VM_ROLE_NAMES = ("defaults", "svc", "mongo", "rabbit")

_LEGACY_PREFIX = "appspace.infra.deployLinuxServices."
_KCC_PREFIX = "appspace.infra.deployLinuxServicesK8s."


def _norm_machine_type(v) -> str:
    """Compare machine types the way an operator reads them: trimmed,
    unquoted, case-insensitive. `n2d-highmem-2` and ` "N2D-Highmem-2" `
    are the same machine, and a PR that only re-quotes a value is not a
    resize."""
    return str(v or "").strip().strip("'\"").strip().lower()


def _kcc_enabled_roles(flat: dict) -> list:
    """Roles explicitly enabled under the KCC key. `defaults` is config, not
    a role, so it never appears here."""
    return sorted({
        k[len(_KCC_PREFIX):].split(".", 1)[0]
        for k, v in flat.items()
        if k.startswith(_KCC_PREFIX)
        and k.endswith(".enabled")
        and str(v).strip().lower() == "true"
        and k[len(_KCC_PREFIX):].split(".", 1)[0] in _VM_ROLE_NAMES
        and k[len(_KCC_PREFIX):].split(".", 1)[0] != "defaults"
    })


def _kcc_role_value(flat: dict, role: str, leaf: str):
    """A role's value for a leaf, falling back to `defaults` the way the
    chart does."""
    v = flat.get(f"{_KCC_PREFIX}{role}.{leaf}")
    return flat.get(f"{_KCC_PREFIX}defaults.{leaf}") if v is None else v


def _detect_kcc_adoption(old_flat: dict, new_flat: dict) -> dict:
    """Classify a values-level change as a Terraform -> KCC ownership
    transfer, in which the existing GCP VM is adopted by name and nothing
    is created or resized (COPS-2608).

    Returns a dict when the file is an ownership move, otherwise None:

      {"kind": "adoption", "roles": [...]}  the VM changes owner and is
          adopted by name; gets the card;
      {"kind": "cleanup"}                    KCC was already live at base
          and this PR only drops the dead legacy block; routine, no card.

    Both suppress the machineType danger, because in neither case is a
    machine being resized. Only the first renders a card.

    Deliberately conservative: anything it cannot prove is an ownership
    move is left to the existing danger rules, because a false "this is
    safe" is far worse than a false alarm.

    Not an ownership move, on purpose:
      - `createNewBootDisk` true on an enabled role: that builds a new VM;
      - machine types that genuinely differ: a resize wearing adoption's
        coat;
      - legacy keys removed with no KCC role enabled at all: that is a VM
        being switched off, which is exactly what the danger rules exist
        for.

    When the legacy `machineType` is absent the comparison is treated as
    satisfied: the old Terraform module defaulted that value, so a
    customer.yaml that relied on the default has no old value, and nothing
    that does not exist can be changing. The rendered level stays the
    authority in that case.
    """
    legacy_removed = any(
        k.startswith(_LEGACY_PREFIX) and k in old_flat and k not in new_flat
        for k in set(old_flat) - set(new_flat))
    if not legacy_removed:
        return None

    roles = _kcc_enabled_roles(new_flat)
    if not roles:
        return None

    # KCC already live at base with the same roles: this PR only drops the
    # dead legacy block. Nothing is adopted, nothing is resized.
    if _kcc_enabled_roles(old_flat) == roles:
        return {"kind": "cleanup"}

    legacy_mt = None
    for k, v in old_flat.items():
        if k.startswith(_LEGACY_PREFIX) and k.rsplit(".", 1)[-1] == "machineType":
            legacy_mt = v
            break

    facts = []
    for role in roles:
        if str(_kcc_role_value(new_flat, role, "createNewBootDisk")
               ).strip().lower() != "false":
            return None  # greenfield, or unspecified: not provably adoption
        mt = _kcc_role_value(new_flat, role, "machineType")
        if (legacy_mt is not None
                and _norm_machine_type(mt) != _norm_machine_type(legacy_mt)):
            return None  # the values really differ: it is a resize
        facts.append({
            "role": role,
            "instance": _kcc_role_value(new_flat, role, "instanceName"),
            "machineType": mt,
            "dataDiskSizeGb": _kcc_role_value(new_flat, role, "dataDiskSizeGb"),
            "manageMetadata": _kcc_role_value(new_flat, role, "manageMetadata"),
        })
    return {"kind": "adoption", "roles": facts}


def _kcc_move_disk_shrink(old_flat: dict, new_flat: dict, roles: list) -> str:
    """Danger reason when a disk shrinks *across* the Terraform -> KCC key
    move, or "" when it does not.

    The ordinary shrink rule compares old and new of the same key, so it is
    blind here: the old size lives on `deployLinuxServices.dataDiskSizeGb`
    and the new one on `deployLinuxServicesK8s.<role>.dataDiskSizeGb`. Two
    different keys are never compared, so a 256 -> 128 shrink slipped
    through the whole panel. Classifying the move is what makes the
    comparison possible, so the check belongs here (COPS-2608).
    """
    for leaf in _VM_DISK_SIZE_KEYS:
        old_v = old_flat.get(_LEGACY_PREFIX + leaf)
        if old_v is None:
            continue
        for role in roles:
            new_v = _kcc_role_value(new_flat, role, leaf)
            if new_v is None:
                continue
            try:
                if float(str(new_v)) < float(str(old_v)):
                    return (f"`{leaf}` DECREASES across the Terraform \u2192 KCC "
                            f"move for role `{role}` (`{old_v}` \u2192 `{new_v}`) "
                            f"\u2014 {_VM_SHRINK_REASON}")
            except (TypeError, ValueError):
                continue
    return ""


# COPS-2766 (C21): these machine families take only Hyperdisk, so GCP rejects
# the VM on a pd- disk and KCC never creates it (acme-config-prod #4482, #4331).
_VM_DISK_FAMILY_RE = re.compile(r"^(n4|n4a|n4d|c4|c4a|c4d)-")
_VM_DISK_FAMILY_REASON = "this machine family takes only Hyperdisk, and GCP rejects a pd- disk"
# The supporting-services chart defaults (values.yaml, defaults.gcp).
_KCC_CHART_MACHINE_TYPES = {"svc": "n2d-highmem-2", "rabbit": "n2d-highmem-2",
                            "mongo": "n2d-highmem-4"}
_KCC_CHART_DISK_TYPE = "pd-ssd"
# The leaves the rule reads. A change to no other leaf can add an error.
_VM_DISK_FAMILY_LEAVES = ("enabled", "machineType", "svcMachineType", "rabbitMachineType",
                          "mongoMachineType", "dataDiskType", "bootDiskType",
                          "createNewBootDisk", "instanceName", "instances")


def _kcc_true(v) -> bool:
    return str(v).strip().lower() == "true"


def _kcc_rendered_roles(flat: dict) -> list:
    """The roles the chart renders a VM for: KCC on and the role enabled."""
    return _kcc_enabled_roles(flat) if _kcc_true(flat.get(_KCC_PREFIX + "enabled")) else []


def _kcc_machine_type(flat: dict, role: str) -> str:
    """Like the chart helper: the role, the legacy deployLinuxServices key,
    defaults.gcp.<role>MachineType, then the chart default."""
    legacy = _LEGACY_PREFIX + ("" if role == "svc" else role + ".") + "machineType"
    return _norm_machine_type(flat.get(f"{_KCC_PREFIX}{role}.machineType") or flat.get(legacy)
                              or flat.get(f"{_KCC_PREFIX}defaults.gcp.{role}MachineType")
                              or _KCC_CHART_MACHINE_TYPES[role])


def _kcc_instance_names(flat: dict, role: str) -> set:
    """The VM names of a role: svc.instanceName or its chart default, else
    the instances list."""
    if role == "svc":
        return {str(flat.get(_KCC_PREFIX + "svc.instanceName") or "%s-%s-svc-%s" % tuple(
            flat.get("appspace." + k) for k in ("prefix", "customerName", "suffix")))}
    return {str(i.get("name") if isinstance(i, dict) else i)
            for i in flat.get(f"{_KCC_PREFIX}{role}.instances") or ()}


def _kcc_disk_type(flat: dict, role: str, disk: str) -> str:
    """The 'data' or 'boot' disk type: the role, defaults.gcp, then pd-ssd."""
    return _norm_machine_type(flat.get(f"{_KCC_PREFIX}{role}.{disk}DiskType")
                              or flat.get(f"{_KCC_PREFIX}defaults.gcp.{disk}DiskType")
                              or _KCC_CHART_DISK_TYPE)


def _vm_disk_family_errors(flat) -> list:
    """[(role, machineType, 'data' or 'boot', disk type)]: each rendered role on
    an n4 or c4 family with a pd- disk. The boot disk counts only when KCC
    creates it (createNewBootDisk true); else it is adopted as it is."""
    out = []
    for role in _kcc_rendered_roles(flat or {}):
        mt = _kcc_machine_type(flat, role)
        if not _VM_DISK_FAMILY_RE.match(mt):
            continue
        new_boot = _kcc_true(_kcc_role_value(flat, role, "createNewBootDisk"))
        for disk in ("data", "boot") if new_boot else ("data",):
            t = _kcc_disk_type(flat, role, disk)
            if t.startswith("pd-"):
                out.append((role, mt, disk, t))
    return out


def _vm_disk_family_text(errors) -> str:
    """The error and its fix, for the errors of one role on a new VM."""
    return (f"`{errors[0][1]}` with " + " and ".join(f"{d} disk `{t}`" for _r, _m, d, t in errors)
            + f": {_VM_DISK_FAMILY_REASON}. Set `dataDiskType` (and `bootDiskType` when "
            "`createNewBootDisk` is true) to `hyperdisk-balanced`.")


def _vm_disk_family_changes(old_flat, new_flat) -> list:
    """[(role, text)]: the n4 or c4 disk errors a change adds to a live env.

    A VM that runs at base keeps its disks: GCP cannot change a disk type in
    place. So moving it into the family is the error, unless its data disk
    and a boot disk KCC created are Hyperdisk at base. A new VM (a new role
    or a new name) is checked on its values. An error that is already on
    main does not count."""
    if old_flat is None or new_flat is None:
        return []
    ran = _kcc_rendered_roles(old_flat)
    before = {(r, d) for r, _m, d, _t in _vm_disk_family_errors(old_flat)}
    errors = _vm_disk_family_errors(new_flat)
    out = []
    for role in _kcc_rendered_roles(new_flat):
        old_mt, new_mt = _kcc_machine_type(old_flat, role), _kcc_machine_type(new_flat, role)
        if (role in ran and _kcc_instance_names(old_flat, role) & _kcc_instance_names(new_flat, role)
                and _VM_DISK_FAMILY_RE.match(new_mt) and not _VM_DISK_FAMILY_RE.match(old_mt)
                and not (_kcc_true(_kcc_role_value(old_flat, role, "createNewBootDisk"))
                         and all(_kcc_disk_type(old_flat, role, d).startswith("hyperdisk-")
                                 for d in ("data", "boot")))):
            out.append((role, f"`machineType` `{old_mt}` \u2192 `{new_mt}`: "
                              f"{_VM_DISK_FAMILY_REASON}. The running VM keeps its disks, "
                              "because a disk type cannot change in place. Keep the current "
                              "family, or plan a disk migration to Hyperdisk (snapshot and "
                              "restore)."))
            continue
        mine = [e for e in errors if e[0] == role and (role, e[2]) not in before]
        if mine:
            out.append((role, _vm_disk_family_text(mine)))
    return out


def _new_env_prereq_findings(flat: dict, env: str) -> tuple:
    """COPS-2766 (C21): (errors, lines) for a new GCP env from its value chain.

    The errors are the n4 or c4 disk errors, one line per role. The checks
    are warnings: a new VM that adopts a boot disk, and no VM at all. There
    is no deployWindows check: that Terraform path is going away."""
    errors = _vm_disk_family_errors(flat)
    roles = _kcc_rendered_roles(flat)
    lines = [f"- \u26d4 `{env}` \u00b7 **linux VM (KCC) \u00b7 {role}**: "
             + _vm_disk_family_text([e for e in errors if e[0] == role])
             for role in roles if any(e[0] == role for e in errors)]
    adopt = [r for r in roles if not _kcc_true(_kcc_role_value(flat, r, "createNewBootDisk"))]
    if adopt:
        lines.append(f"{_NEW_ENV_CHECK_PREFIX}`{env}` adopts a boot disk for "
                     + ", ".join(f"`{r}`" for r in adopt) + " that a new VM does not have yet "
                     "(`createNewBootDisk` is not true). Set `createNewBootDisk: true`, unless "
                     "the VM or its boot disk already exists (a move, or a disk restored by "
                     "hand).")
    if not roles:
        lines.append(f"{_NEW_ENV_CHECK_PREFIX}`{env}` renders no Linux VM (`svc`, `mongo` or "
                     "`rabbit`) from `deployLinuxServicesK8s`, so KCC creates no VM for it. "
                     "Enable the roles it needs, unless it runs with no VM on purpose.")
    return errors, lines


def _kcc_adoption_card(env_name: str, info: dict) -> list:
    """One card per adopted environment, replacing the nine
    "appears for the first time" bullets. Those are true of the Argo CD
    objects and misleading about GCP, where the VM already exists and is
    adopted by resourceID."""
    lines = [
        f"**\U0001f5a5\ufe0f VM INFRASTRUCTURE \u2014 ADOPTION \u2014 `{env_name}`**",
        "",
        "Terraform `deployLinuxServices` \u2192 KCC `deployLinuxServicesK8s`. "
        "The existing GCP VM is adopted by name (`createNewBootDisk: false`). "
        "**No VM is created or resized by this PR.**",
        "",
    ]
    for f in info["roles"]:
        inst = f["instance"] or f"(chart default for role `{f['role']}`)"
        lines.append(f"- **{f['role']}** \u2014 instance `{inst}`, machineType "
                     f"`{f['machineType']}` (unchanged)"
                     + (f", data disk {f['dataDiskSizeGb']}Gi"
                        if f["dataDiskSizeGb"] else "")
                     + f", manageMetadata={f['manageMetadata']}")
    lines += [
        "",
        "The Compute objects are new **in Argo CD**; the GCP resources "
        "already exist and are adopted by `resourceID`. After merge: "
        "`lastStartTimestamp` unchanged, all `Compute*` UpToDate, then the "
        "`terraform state rm` follow-up.",
        # COPS-2623: state the absence, not just the presences. deviceName is
        # the single most dangerous field in this migration -- COPS-2592
        # shipped an incident where rendering it detached a live disk -- and
        # an adoption reviewer is specifically looking for reassurance that
        # it is not being set. Silence reads the same as "nobody checked".
        "",
        "`attachedDisk.deviceName` is **not rendered**, so KCC leaves the "
        "live attachment name alone.",
        "",
    ]
    return lines

# Panel headers are constants because the merge summary recognises its own
# panels by them. Danger uses "##", routine and clean use "###", so the
# summary can tell severity apart without re-deriving any facts.
_VM_PANEL_CLEAN_HDR = "### \U0001f5a5\ufe0f VM infrastructure \u2014 no changes"


# Rendered-manifest bullets that differ only by resource name. Six snapshot
# policy attachments (daily/hourly/weekly x boot/data) are one fact -- the
# existing schedule comes under KCC management -- and reading them as six
# findings is how a reviewer learns to skim the panel (COPS-2623).
_VM_REPEAT_RE = re.compile(
    r"^- (?P<scope>`[^`]+`) \u00b7 `(?P<kind>\w+) [^`]+`: (?P<note>.+)$")
_VM_REPEAT_MIN = 3


def _collapse_repeated_vm_lines(routine):
    """Collapse same-scope, same-kind, same-note bullets into one count.

    Applies to every environment, adopted or not: where the individual
    resource names carry no information beyond the kind, printing them is
    noise. The names stay on the full-diff page, which is the complete
    record.

    Defensive like every other parser here: anything that does not match
    the shape passes through untouched and in order.
    """
    out, groups = [], {}
    for env, line in routine:
        mt = _VM_REPEAT_RE.match(line)
        if not mt:
            out.append((env, line))
            continue
        key = (env, mt.group("scope"), mt.group("kind"), mt.group("note"))
        if key not in groups:
            groups[key] = [len(out), 0]
            out.append((env, line))
        groups[key][1] += 1
    for (env, scope, kind, note), (idx, count) in groups.items():
        if count >= _VM_REPEAT_MIN:
            out[idx] = (env, f"- {scope} \u00b7 {count} \u00d7 `{kind}`: {note}")
    return out


def _vm_panel_lines(adoption_cards, adopted_envs, routine, dangerous):
    """Assemble the VM panel from its parts.

    Extracted from _summarize_vm_changes so the suppression decision has
    one testable place; that function needs a Bitbucket fetch to reach it.

    `routine` is a list of (environment, line). It carries the environment
    because of COPS-2623: an environment classified as a KCC adoption gets
    a card that, by its own docstring, REPLACES those lines -- they restate
    what the card says in prose and describe the ArgoCD objects as new when
    nothing in GCP is. Measured at 59-60% of a single-environment adoption
    comment. Every OTHER environment in the same PR keeps all of its lines,
    which is why a flat list of strings was not enough.

    `dangerous` is never filtered, for any environment. Suppressing
    evidence is the point; suppressing a verdict would be a different and
    far worse change, so the two lists stay separate all the way here.
    """
    routine = [(e, l) for e, l in routine if e not in adopted_envs]
    routine = _collapse_repeated_vm_lines(routine)
    routine_lines = [l for _, l in routine]
    if not dangerous and not routine_lines and not adoption_cards:
        # Always render the section, even empty. Operators asked for a
        # fixed place to look: "did this PR touch VMs at all?" must be
        # answerable without reading the rest. Audit of the last 40
        # acme-config-prod PRs found "GCP unify guard: Windows/LinuxVM
        # off" switching VMs off with no VM wording anywhere in the
        # comment -- silence is indistinguishable from "not checked".
        return [_VM_PANEL_CLEAN_HDR, "",
                "No changes to VM infrastructure (KCC linux-services) in "
                "this PR.", ""]
    if dangerous:
        _warn = ("**This PR touches virtual machine infrastructure (KCC linux-services). A botched VM change is slow and painful to recover from \u2014 verify every line below before merging.**")
        lines = [
            _VM_PANEL_DANGER_HDR,
            "",
            _warn,
            "",
        ] + list(dangerous)
        # Even when something else in the PR is dangerous, an adoption that
        # was classified still gets its card: the reviewer needs to know the
        # VM is being adopted rather than created while judging the real
        # danger above it.
        if adoption_cards:
            lines += [""] + list(adoption_cards)
        if routine_lines:
            lines += ["", "Routine VM changes in the same PR:", ""] + routine_lines
        return lines + [""]
    return ([_VM_PANEL_ROUTINE_HDR, ""]
            + (list(adoption_cards) + [""] if adoption_cards else [])
            + routine_lines + [""])
