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

from comment_render import (
    _VM_PANEL_DANGER_HDR,
    _VM_PANEL_ROUTINE_HDR,
    _section_name,
)
from manifest import _section_kind  # decoder lives with the format it decodes


_WORKLOAD_KINDS = ("Deployment", "StatefulSet", "ReplicaSet")


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


def _detect_workload_shutdown(sections: list):
    """{"zeroed": n, "workloads": total} over the workload sections, or None.

    The ratio is what separates "one service was scaled down" from "this
    environment is being switched off", and the two deserve different
    wording in the merge summary. Counted here, pre-cap, alongside the other
    safety facts.
    """
    total = zeroed = 0
    for header, body in sections:
        if _section_kind(header) not in _WORKLOAD_KINDS:
            continue
        total += 1
        ends_zero, ends_pos = _replicas_end_state(body)
        if ends_zero and not ends_pos:
            zeroed += 1
    if not total:
        return None
    return {"zeroed": zeroed, "workloads": total}


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
      - a whole VM-domain resource disappearing from the render (an
        `enabled` flag turning off, or the environment dropping the domain)
        is dangerous; a snapshot-policy attachment disappearing silently
        ends that disk's backup schedule.
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
        if deleted:
            if kind == "ComputeDiskResourcePolicyAttachment":
                dangerous.append("snapshot-policy attachment removed — this "
                                 "disk loses its backup schedule")
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
                        dangerous.append("disk size DECREASES — GCP cannot "
                                         "shrink a disk in place; this "
                                         "implies recreation and data loss")
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
                      "dangerous": dangerous, "notes": notes})
    return facts


def _vm_deletion_armed_flat(flat: dict) -> bool:
    """Phase 1 state: the real VM, disk and IP are only deleted by the
    cascade when allowDeletion is armed. Same key _decommission_fully_phased
    reads, applied to an already flattened dict."""
    val = flat.get("appspace.infra.deployLinuxServicesK8s.defaults.allowDeletion")
    return str(val).strip().lower() == "true"


_VM_FLAT_PREFIX = "appspace.infra.deployLinuxServicesK8s."


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
               if k.startswith(_VM_FLAT_PREFIX) and k not in new_flat]
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
                            f"\u2014 GCP cannot shrink a disk in place")
            except (TypeError, ValueError):
                continue
    return ""


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
