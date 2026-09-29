"""Comment rendering helpers (COPS-2658 phase 2).

The pieces that turn analysed results into the Markdown a reviewer reads:
the merge summary and its severity verdicts, section and environment name
formatting, repeated-section grouping, and the full-diff page link.
Extracted verbatim from diff_preview.py; no logic changed in the move.

A leaf on the hub, though it may use the other leaves. It imports nothing
from the service and must stay that way.
"""
import re
from collections import Counter

import diff_ui
from manifest import _is_kcc_blocking_artifact, _hpa_headers, _section_kind
from vocabulary import (
    OUT_DIFF,
    OUT_ERROR,
    OUT_INDETERMINATE,
    PERMANENT_REASONS,
)


def _section_name(header: str) -> str:
    """'/batch/Job acme-secret-generator/pv-x-job-cb71f3d8' -> 'pv-x-job-cb71f3d8'.

    The name is the last path component to the RIGHT of the first space, so
    a namespace prefix is stripped."""
    try:
        right = header.split(" ", 1)[1]
    except Exception:
        return ""
    return right.rsplit("/", 1)[-1].strip().strip('"')


def _parse_version_tuple(version: str):
    """Leading dotted-numeric part of a chart version as an int tuple.

    '2602.4.9-dev' -> (2602, 4, 9). Returns None when the version does not
    start with a number (unparseable — comparisons are skipped)."""
    if not version:
        return None
    mnum = re.match(r"^(\d+(?:\.\d+)*)", version.strip())
    if not mnum:
        return None
    return tuple(int(x) for x in mnum.group(1).split("."))


_RELEASE_TAG_RE = re.compile(r"\d+(?:\.\d+)*(?:-rev(\d+))?")


def _rev_number(v):
    """N of a release tag '2603.1.38-revN', 0 for the bare tag, None for
    any other tag (-dev and feature tags have unrelated revs)."""
    m = _RELEASE_TAG_RE.fullmatch((v or "").strip())
    return int(m.group(1) or 0) if m else None


def _is_version_downgrade(current: str, new: str) -> bool:
    """True when `new` is a strictly LOWER chart version than `current`.

    v2.5.8: downgrades are legal but dangerous (schema regressions, data
    migrations that do not run backwards), so the PR comment must shout.
    Unparseable versions return False — never block on noise.

    COPS-2766: a -revN is newer than the bare tag, and rev2 is newer than
    rev1, only when both are release tags."""
    cur_t = _parse_version_tuple(current)
    new_t = _parse_version_tuple(new)
    if cur_t is None or new_t is None:
        return False
    # Pad to equal length so 2602.4 vs 2602.4.1 compares sanely.
    length = max(len(cur_t), len(new_t))
    cur_t += (0,) * (length - len(cur_t))
    new_t += (0,) * (length - len(new_t))
    cur_r, new_r = _rev_number(current), _rev_number(new)
    if cur_r is None or new_r is None:
        cur_r = new_r = 0
    return (new_t, new_r) < (cur_t, cur_r)


def parse_diff_sections(diff_text):
    """Parse ArgoCD diff output into [(header, body)] list.

    Returns empty list if no '=====' separators found in the output.
    """
    sections, hdr, lines = [], None, []
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("====="):
            if hdr and lines:
                sections.append((hdr, "".join(lines)))
            hdr   = line.strip().strip("=").strip()
            lines = []
        elif hdr is not None:
            lines.append(line)
    if hdr and lines:
        sections.append((hdr, "".join(lines)))
    return sections


_APP_COMPONENT_SUFFIX = re.compile(r"-(ss|ms|glb)$")

def _envs_from_apps(apps) -> list:
    """Derive environment names deterministically from ArgoCD app names.

    Apps follow \'<env>-<component>\' (e.g. pv-qa88-a-ss -> pv-qa88-a).
    Unknown suffixes fall back to the app name itself, so a new component
    type degrades to a slightly verbose but always-true environment list.
    The AI model is never asked for environment names - before v2.4.2 it
    copied literal example values straight from the prompt template.
    """
    return sorted({_APP_COMPONENT_SUFFIX.sub("", a.split("/")[-1]) for a in apps})


_REPEAT_GROUP_MIN = 3


def _changed_lines_signature(body: str) -> str:
    """Only the added/removed lines. Context is deliberately ignored: two
    resources take "the same change" even when the surrounding manifest
    differs, which is exactly the KCC-annotation case."""
    return "\n".join(l for l in body.splitlines()
                      if l[:1] in "+-" and not l.startswith(("---", "+++")))


def _group_repeated_sections(sections: list, risk_headers=None):
    """(representatives, duplicates_by_header).

    Order of first occurrence is preserved, so the reordering that puts
    risk sections and needles first still decides what a reviewer reads
    first. Risk sections are never grouped: a deletion always gets its
    own hunk, however many identical siblings it has.
    """
    risky = set(risk_headers or ())
    groups, order = {}, []
    for hdr, body in sections:
        if hdr in risky:
            order.append((hdr, body, None))
            continue
        sig = _changed_lines_signature(body)
        if sig in groups:
            groups[sig].append(hdr)
            continue
        groups[sig] = []
        order.append((hdr, body, sig))
    reps, dups = [], {}
    for hdr, body, sig in order:
        reps.append((hdr, body))
        if sig is None:
            continue
        others = groups[sig]
        if len(others) + 1 >= _REPEAT_GROUP_MIN:
            dups[hdr] = others
        else:
            reps.extend((h, body) for h in others)
    return reps, dups


def _name_list(headers, room: int = 240):
    """As many resource names as fit in a readable line, then "...".

    Naming them matters: a reviewer scanning for one specific resource
    needs to see whether it is in the group. Naming ALL of them defeats
    the point of grouping in the first place.
    """
    names = []
    for h in headers:
        piece = f"`{h}`"
        if room - len(piece) - 2 < 0:
            break
        names.append(piece)
        room -= len(piece) + 2
    if not names:
        return ""
    return ", ".join(names) + (", ..." if len(names) < len(headers) else "")


def _full_hunks_link(artifact_url: str, app: str = "") -> str:
    """One phrase for "the complete diff lives over there".

    Every place the comment folds content away has to point somewhere,
    and a reviewer must never have to guess whether the missing hunks
    are lost or just elsewhere.

    app (COPS-2622): deep-link straight to that application's section on
    the page instead of to the top of it. Before this, every per-app
    pointer carried the identical bare URL -- 8 copies on a 6-app comment,
    ~42 on a fleet bump -- which on a comment phase E had just shrunk to a
    decision summary was most of what remained. The anchor shape comes
    from diff_ui.app_anchor and is NOT rebuilt here: two copies of that
    logic would drift and every deep link would 404 in silence.
    """
    if artifact_url:
        if app:
            return (f"[Full hunks for `{app}`]"
                    f"({artifact_url}#{diff_ui.app_anchor(app)})")
        return f"[Full hunks in the full diff view]({artifact_url})"
    return ("Full hunks are in the diff-preview full-diff view, linked "
            "from the build status.")


def _fmt_service_list(services: list, shown: int = 8) -> str:
    head = ", ".join(services[:shown])
    more = f" (+{len(services) - shown} more)" if len(services) > shown else ""
    return f"{head}{more}"


def _panel_names(*segments, shown=3) -> str:
    """COPS-2766: '`a`, `b`, `c` (+N more)' from the name lists of our own
    panel lines, like '`pv-a` (moved from `x`), `pv-b` (+8 more)'."""
    names = [n for seg in segments for n in re.findall(r"(?:^|, )(`[^`]+`)", seg)]
    rest = len(names) - shown + sum(
        int(x) for seg in segments for x in re.findall(r"\(\+(\d+) more\)$", seg))
    return ", ".join(names[:shown]) + (f" (+{rest} more)" if rest > 0 else "")


def _routine_bump_label(sig) -> str:
    """One human line naming the transition a rollup group shares."""
    old_rev, new_rev, items = sig
    if old_rev or new_rev:
        label = f"chart `{old_rev}` \u2192 `{new_rev}`"
        extra = len(items)
    elif items:
        key, olds, news = items[0]
        label = f"`{key}`: `{olds}` \u2192 `{news}`"
        extra = len(items) - 1
    else:
        return "version-only change"
    if extra > 0:
        label += f" (+{extra} more field(s))"
    return label


_VM_PANEL_DANGER_HDR = ("## \U0001f5a5\ufe0f VM INFRASTRUCTURE "
                        "CHANGES")
_VM_PANEL_ROUTINE_HDR = "### \U0001f5a5\ufe0f VM INFRASTRUCTURE CHANGES (routine)"

# COPS-2660: the arming PR itself removed the VM config that allowDeletion
# acts through. Recognised here by the summary, rendered by the hub's
# appspace-state panel -- one constant so the two can never drift apart.
_DECOM_VM_STRIP_HDR = ("## \U0001f5a5\u26d4 VM CONFIG STRIPPED WHILE "
                       "ARMING DECOMMISSION")

# COPS-2668: same contract for the data purge, and for the same reason. The
# summary used to detect it by searching the panel for "PURGE", which matched
# the denial as readily as the warning: both branches name
# `appspace.decommissionPurgeData`. This sentence is emitted ONLY when the
# purge is genuinely armed, so matching it cannot confuse the two.
_DECOM_PURGE_HDR = "**DATA WILL BE PERMANENTLY DESTROYED.**"
# COPS-2697: strictly worse than the purge header above it — the purge header
# says this environment's own data goes, this one says a SURVIVING
# environment's data goes with it. Written verbatim by the producer in
# diff_preview and matched here, per the one-constant rule this module's
# docstring prescribes (same wiring as COPS-2660 / COPS-2668).
_DECOM_SHARED_UC_HDR = "**SHARED USER CONTENT - DO NOT MERGE WITHOUT CHECKING.**"
# COPS-2693 Plan B: written by the blast-radius panel (blast_radius.render_lines
# via diff_preview), matched here for the REVIEW verdict line. Same one-constant
# wiring as the headers above.
_BLAST_RADIUS_HDR = "**Blast radius.**"
# COPS-2766: the noCore panel (COPS-2758), written in diff_preview and
# matched here. The counts come from its header line.
_NOCORE_FLIP_HDR = "**noCore changes.**"
_NOCORE_COUNTS_RE = re.compile(
    r"noCore changes in (\d+) environment\(s\): on in (\d+), off in (\d+)")
_NOCORE_NAMES_RE = re.compile(re.escape(_NOCORE_FLIP_HDR) + r" `[^`]+` goes from "
                              r"`\w+` to `\w+` in \d+ environment\(s\): (.+?)\. ")
_NOCORE_UNKNOWN = "noCore check unavailable"
_NOCORE_UNKNOWN_RE = re.compile(re.escape(_NOCORE_UNKNOWN) + r" for (.+?): one of ")
# COPS-2766: legacyBackends false to true, the same wiring.
_LEGACY_BACKENDS_HDR = "**Legacy backends come back.**"
_LEGACY_BACKENDS_RE = re.compile(
    re.escape(_LEGACY_BACKENDS_HDR) + r".*? in \d+ environment\(s\): (.+?)\. ")
# COPS-2766: a prod cl-*-ms/-ss app serves every tenant of its constellation.
_TENANT_WIDE_HDR = "**Reaches every public-cloud tenant.**"
# COPS-2721: written by values_redundancy.render_lines via diff_preview,
# matched here for the REVIEW verdict line. Same one-constant wiring as
# blast radius: a quiet render caused by copying parent values into
# customer.yaml must not read as "the tool missed the change".
_VALUES_REDUNDANCY_HDR = "**Higher-layer values.**"
# COPS-2766: its merge-summary line. status_lead skips it: keys that change
# no manifest are no reason for a warning sign on a green status.
_HIGHER_LAYER_FINDING = ("\U0001f4da **Higher-layer values already cover "
                         "part of this PR**")
# COPS-2766: written by the inert-edit panel in diff_preview, matched here.
_INERT_EDIT_HDR = "**Edits with no effect.**"
# Written by the identity-change guard in diff_preview for a confirmed rename of
# a live environment, matched here for the REVIEW verdict line.
_IDENTITY_MIGRATION_HDR = "**Planned rename.**"
# COPS-2766: the auto-sync panel headers, written in diff_preview and matched
# here. The summary used `"PAUSED" in txt.upper()`, which also matched the
# resume panel ("drifted while paused") and the "remains paused" reminder, so
# every resume was announced as a pause.
_AUTOSYNC_PAUSED_HDR = "Auto-sync PAUSED for"
_AUTOSYNC_RESUMED_HDR = "Auto-sync RESUMED for"

# COPS-2668: and a third state. The summary used to know only purge-vs-not,
# so an environment with NO cascade armed — where the Applications go and
# every workload keeps running, orphaned — was announced as "resources are
# deleted", the exact opposite of the panel directly beneath it. Found by
# reading the rendered orphan comment while preparing its golden.
_DECOM_ORPHAN_HDR = ("**The ArgoCD Application is removed, but its resources "
                     "are NOT deleted")
# COPS-2701: public-cloud (cl-*) folder removal. Same orphan outcome as the
# header above, but the private-cloud Phase 2/3 / COPS-2539 wording must
# never appear — appspace.decommission is a no-op there (COPS-2700).
_DECOM_PUBLIC_CLOUD_HDR = ("**Public cloud (`cl-*`) teardown is MANUAL.**")
# COPS-2701: someone set appspace.decommission[:PurgeData] on a cl-* env.
# Private-cloud wording would call that "ARMED"; here it arms nothing.
_DECOM_PUBLIC_CLOUD_NOOP_HDR = (
    "**Public cloud: `appspace.decommission` does NOT arm cascade delete.**")
# COPS-2708: the reason, not just the mechanism. Every public-cloud panel
# said a finalizer is never templated, which reads like an omission somebody
# ought to fix. It is a safety property: a `cl-*` namespace is shared, so
# there is no delete that is safe to automate for one customer in it. Said
# once, here, and used by all three panels so they cannot drift.
_DECOM_PUBLIC_CLOUD_WHY = (
    "A `cl-*` namespace is **shared**: one constellation serves many "
    "customers from the same microservices, NEGs and load balancers, so no "
    "delete is safe to automate for one of them. That is why the "
    "cascade gate was deliberately never ported to the public-cloud "
    "ApplicationSets (COPS-2700), and why teardown here is operator-driven "
    "from start to finish.")
# COPS-2707: a teardown flag spelled in a way the platform does not read.
# Same contract as the constants above -- the panel writes it, the summary
# matches it, so the two can never drift apart.
_DECOM_FLAG_TYPO_HDR = (
    "**A teardown flag here is misspelled or in the wrong place, so it "
    "arms nothing (or not what you expect).**")
# COPS-2766: the cascade is armed in config and ArgoCD has not applied its
# finalizer. The panel writes it, the evaluator matches it for the gate.
_DECOM_CASCADE_NOT_LIVE_HDR = ("**The cascade is armed in config but NOT live "
                               "in the cluster.**")
# COPS-2766: a paused env still cascades on delete, but what main changed
# during the pause may not be live. A warning, not a gate.
_DECOM_PAUSED_HDR = "Auto-sync is paused on main"
# COPS-2766: a warning about a new env. The summary counts these lines.
_NEW_ENV_CHECK_PREFIX = "- ⚠️ **Check:** "


def _pingscaler_reclass(results) -> dict:
    """{app: set(deleted HPA headers)} explained by a ping-scaler being
    CREATED in the same environment by this same PR.

    Cross-app on purpose: the Deployment lands in {env}-ss while the HPAs
    leave {env}-ms (the first 2.106.0 render of acme-config-prod #4444
    proved a same-app pairing never fires). Still narrow: exact Deployment
    name (detected at diff time), HPA kinds only, same environment -- an
    activation in pv-a never explains a deletion in pv-b.
    """
    activated = {e for a, r in results.items()
                 if getattr(r, "pingscaler_created", None)
                 for e in _envs_from_apps([a])}
    if not activated:
        return {}
    out = {}
    for a, r in results.items():
        if not getattr(r, "deleted_resources", None):
            continue
        if not set(_envs_from_apps([a])) & activated:
            continue
        hpas = _hpa_headers(r.deleted_resources)
        if hpas:
            out[a] = set(hpas)
    return out


# COPS-2714: where the ping-scaler handover is documented for operators.
# Page-id URL on purpose (Confluence "3. Environments: charts and
# configuration", section "Replica control"): it survives page renames.
PINGSCALER_DOCS_URL = ("https://appspace.atlassian.net/wiki/spaces/cops/"
                       "pages/1181089800")

MERGE_SUMMARY_HDR = "## \u2139\ufe0f Merge summary"
_SEV_ROUTINE, _SEV_REVIEW, _SEV_BLOCK = 0, 1, 2
_VERDICTS = {
    _SEV_BLOCK: "\u26d4 **DO NOT MERGE** without checking the item(s) below",
    _SEV_REVIEW: "\u26a0\ufe0f **Review before merging**",
    _SEV_ROUTINE: "\u2705 **Routine** \u2014 nothing dangerous detected",
}


# COPS-2766: kinds whose deletion loses data or a public address. The
# deletion bullet leads with them, all of them, so a bucket cannot hide among
# 60 IAM bindings under "68 resource(s) deleted".
_DATA_KINDS = frozenset({
    "StorageBucket", "BigQueryDataset", "BigQueryTable", "ComputeDisk",
    "ComputeSnapshot", "SQLInstance", "SQLDatabase", "RedisInstance",
    "SecretManagerSecret", "ComputeAddress", "DNSRecordSet", "DNSManagedZone",
    "PersistentVolumeClaim", "PersistentVolume", "StorageAccount",
})


# COPS-2766: inside REVIEW a cause leads its effect, so the first line (and
# the green status) names the downgrade or the shutdown, not the deletions
# or HPAs they cause (#4549, #4567). Keyed by the finding's emoji. Others
# (could not be diffed, new env) come after these, and the higher-layer
# note always last: it changes no manifest.
_REVIEW_RANK = {
    "\u23f8": 0, "\u25b6": 0,      # paused, auto-sync paused / resumed
    "\u2b07": 1,                   # chart downgrade
    "\U0001f50c": 1,               # noCore changes, before the bs-pcs deletions
    "\U0001f6d1": 2,               # environment shutting down
    "\U0001f512": 2,               # decommission or data purge armed
    "\U0001f4a5": 3,               # wide-reach config change
    "\U0001f517": 4,               # NEG and BackendService, before the deletions
    "\U0001f500": 4,               # planned rename
    "\U0001f5d1": 5,               # resources deleted, env decommission
    "\U0001f9ca": 6,               # replicas scaled to zero
    "\U0001f4c9": 6,               # capacity cut
    "\U0001f39a": 7,               # ping-scaler activated
    "\U0001f501": 7,               # fixed replicas released
    "\U0001f5a5": 8,               # KCC resources unmanaged
    "\U0001f9ec": 9,               # unresolved chart value
    "\U0001f4da": 99,              # higher-layer values
    "\U0001f4a4": 98,              # an edit changes nothing rendered
    "\u2611": 0,                   # merge gate confirmed in a commit
}


def _is_kcc_header(header) -> bool:
    """'/compute.cnrm.cloud.google.com/v1beta1/X ns/name' -> True: the API
    group of the header ends in .cnrm.cloud.google.com."""
    group = header.split(" ", 1)[0].lstrip("/").split("/", 1)[0]
    return group.endswith(".cnrm.cloud.google.com")


def _deletion_finding(headers, n_envs, envs) -> str:
    """The deletion bullet, kinds with counts. Data kinds come first in the
    text, so the ~30 characters Bitbucket shows of the status say it.
    Otherwise GCP (KCC) kinds rank before the others, then by count: the
    top two, or all three when there are three. A BackendService (#4684)
    must not hide in "+1 kind(s)" behind Secrets and ConfigMaps."""
    n = Counter(_section_kind(h) for h in headers)
    gcp = {_section_kind(h) for h in headers if _is_kcc_header(h)}
    kinds = sorted(n, key=lambda k: (k not in gcp, -n[k], k))
    count = f"{len(headers)} resource(s)"
    data = [f"{n[k]} {k}" for k in kinds if k in _DATA_KINDS]
    if data:
        return (f"\U0001f5d1\ufe0f **Data or IP deleted: {', '.join(data)}** "
                f"({count} in {n_envs} environment(s)): {envs}")
    shown = kinds if len(kinds) <= 3 else kinds[:2]
    more = f", +{len(kinds) - 2} kind(s)" if len(kinds) > 3 else ""
    return (f"\U0001f5d1\ufe0f **{count} deleted** in {n_envs} environment(s) "
            f"({', '.join(f'{n[k]} {k}' for k in shown)}{more}): {envs}")


def _fmt_env_list(apps, shown=8) -> str:
    """Environment names, the way operators say them (no -ms/-ss/-glb)."""
    envs = sorted(set(_envs_from_apps(sorted(apps))))
    return _fmt_service_list(envs, shown=shown)


# COPS-2766: the icons follow the build colour (acme-config-dev #7372 was
# green under a DO NOT MERGE). A green build shows no red mark in our lines.
_RED_MARKS_RE = re.compile("\u26d4\ufe0f?|\U0001f6a8|\u274c")


def build_marks(lines, green):
    """On a green build, the lines with every \u26d4, \U0001f6a8 and \u274c
    as \u26a0\ufe0f. Otherwise the lines as they are."""
    if not (green and lines):
        return lines
    return [_RED_MARKS_RE.sub("\u26a0\ufe0f", l) for l in lines]


def _downgrade_mix_note(results) -> str:
    """COPS-2766: a PR that moves 3+ environments, some down and some up,
    is how a stale branch looks (#3315 reverted a bump merged 1 h before).
    '' otherwise."""
    vcs = {a: r.version_change for a, r in results.items() if r.version_change}
    down = set(_envs_from_apps(a for a, vc in vcs.items() if _is_version_downgrade(*vc)))
    up = set(_envs_from_apps(a for a, vc in vcs.items()
                             if _is_version_downgrade(vc[1], vc[0]))) - down
    if not (down and up and len(down | up) >= 3):
        return ""
    return (f"{len(up)} other environment(s) in this PR move up: check that "
            f"the branch is not out of date.")


# COPS-2766: merge gates. A gate fails the build until a Confirm-* line in a
# commit message of the PR lifts it. A kind with no trailer cannot be lifted:
# its fix says what to do. A transient kind is checked again by itself.
# kind: (summary text, trailer, token, fix). A gate is {kind, env, arg, lifted},
# and `why` when its text needs a reason.
GATES = {
    "orphan": ("Teardown with no cascade, the workloads keep running",
               "Confirm-Teardown", "blocked", ""),
    "public": ("Public-cloud teardown, nothing is deleted by itself",
               "Confirm-Teardown", "blocked", ""),
    "shared_uc": ("The purge deletes user content a surviving environment uses",
                  "Confirm-Teardown", "blocked", ""),
    "hold": ("Decommission or zeroPods set less than 7 days ago",
             "Confirm-Decommission", "blocked", ""),
    # Fail closed: a history no retry can read is never "hold met".
    "hold_unread": ("Could not confirm zeroPods or decommission has been on main "
                    "for 7 days (history unreadable)", "Confirm-Decommission",
                    "blocked", ""),
    "hold_retry": ("The git history for the 7-day hold is not readable yet", None,
                   "transient", "Re-checked automatically"),
    "ip": ("A static IP or DNS record is released", "Confirm-IP-Release", "blocked", ""),
    "cl_rename": ("A live public-cloud environment is renamed or moved",
                  "Confirm-Rename", "blocked", ""),
    "shrink": ("Disk shrink", None, "blocked",
               "GCP cannot shrink a disk in place, so keep the old size or grow it"),
    "not_live": ("The cascade finalizer is not live in ArgoCD yet", None, "transient",
                 "Re-checked automatically after ArgoCD syncs"),
    "clone_wake": ("An AEC clone starts running with a copy of production data",
                   "Confirm-Clone-Sanitized", "blocked", ""),
    "dup_identity": ("A new environment uses the name of another environment", None,
                     "blocked", "Use git mv, or choose another customerName or suffix"),
    "ashn_copy": ("A clone reuses the ashn of another environment", None, "blocked",
                  "Give the clone its own ashn"),
    "vm_disk": ("An n4 or c4 machine with a pd- disk, GCP rejects it", None, "blocked",
                "Use hyperdisk-balanced on a new VM, or keep the machine family"),
    "appset_miss": ("No ApplicationSet reads this folder, nothing deploys", None,
                    "blocked", "Check the cloud, tier and spoke folders"),
    "legacy_helm": ("The legacy Helm writer is switched back on", "Confirm-LegacyHelm",
                    "blocked", ""),
    "nocore_lost": ("A move turns noCore off", None, "blocked",
                    "Set appspace.infra.noCore in the moved customer.yaml"),
}


def gate_text(g) -> str:
    """The summary text of gate g, with its reason when it has one."""
    return GATES[g["kind"]][0] + (f" ({g['why']})" if g.get("why") else "")


def gate_trailer(g) -> str:
    """'<Trailer>: <arg>', the line that lifts gate g, or '' when none can."""
    trailer = GATES[g["kind"]][1]
    return f"{trailer}: {g.get('arg') or g['env']}" if trailer else ""


def open_gates(gates) -> list:
    """The gates no commit lifted, the blocking ones first."""
    return sorted((g for g in gates or () if not g.get("lifted")),
                  key=lambda g: GATES[g["kind"]][2] != "blocked")


def gate_token(gates) -> str:
    """'blocked', 'transient' or '': the footer token the open gates ask for."""
    todo = open_gates(gates)
    return GATES[todo[0]["kind"]][2] if todo else ""


def _gate_mark(g) -> str:
    """\u23f3 for a gate that is checked again by itself, else \u26d4."""
    return "\u23f3" if GATES[g["kind"]][2] == "transient" else "\u26d4"


def _gate_way_out(g, trailer_fmt) -> str:
    """The trailer that lifts g in trailer_fmt, or else the fix of its kind."""
    tr = gate_trailer(g)
    return trailer_fmt.format(tr) if tr else ". " + GATES[g["kind"]][3]


def gate_status_description(gates, limit=255) -> str:
    """The FAILED build status. It names the line to add or the fix, so a
    reviewer who reads only the checks list can act. A transient gate waits.
    It fits `limit` UTF-8 bytes and ends in '(see PR comment)', which
    fix_stuck_inprogress reads back. Over the limit the reason is cut, then a
    trailer comes alone (it names the env), and last the rest is cut. The
    panel has all of it in full."""
    todo = open_gates(gates)
    g = todo[0]
    head = "Waiting - " if GATES[g["kind"]][2] == "transient" else "Blocked - "
    end = (f" (+{len(todo) - 1} more)" if len(todo) > 1 else "") + " (see PR comment)"
    tails = [(f" in {g['env']}" if g["env"] else "")
             + _gate_way_out(g, ". To merge anyway, add '{}' to a commit message")]
    if gate_trailer(g):
        tails.append(_gate_way_out(g, ". To merge anyway, add '{}'"))
    for tail in tails:
        why = g.get("why") or ""
        why = _cut_utf8(why, limit + _utf8_len(why) - _utf8_len(f"{head}{gate_text(g)}{tail}{end}"))
        desc = f"{head}{gate_text(dict(g, why=why))}{tail}"
        if _utf8_len(desc + end) <= limit:
            return desc + end
    return _cut_utf8(desc, limit - _utf8_len(end)) + end


def gate_footer(gates) -> str:
    """The gate part of the comment's Status line: the FAILED status with its
    mark, so fix_stuck_inprogress can post it again. '' with no open gate."""
    todo = open_gates(gates)
    return f" | {_gate_mark(todo[0])} {gate_status_description(todo)}" if todo else ""


def _build_merge_summary(results, rollup_by_sig, vm_change_lines,
                         decommission_lines, appspace_state_lines,
                         new_env_lines, new_env_structural,
                         paused_changing=None, paused_envs=None,
                         block_headline=None, gates=None, green=False) -> list:
    """The verdict block that opens every comment.

    Reads the same deterministic facts the panels below use, so the
    summary can never disagree with the detail. Text panels built
    elsewhere are recognised by their own header constants rather than
    re-derived, for the same reason.

    block_headline (COPS-2676): optional short string naming the permanent
    render failure (e.g. "Missing Image Tag on => platform"). When set, the
    cannot-render bullet leads with it so operators see *why* without
    scrolling past deletions and bump noise.

    gates (COPS-2766): the merge gates, first. An open one is why the build
    is red and names the line that lifts it. A lifted one stays as a review
    item, so the override is visible.

    green (COPS-2766): the build is green. The verdict is then at most a
    review and no line has a red mark. The findings are the same.
    """
    findings = []          # (severity, line)
    sev = _SEV_ROUTINE

    for g in gates or ():
        text = gate_text(g)
        if g.get("lifted"):
            findings.append((_SEV_REVIEW, f"\u2611\ufe0f **Confirmed in a commit:** "
                                          f"`{gate_trailer(g)}` ({text})"))
        else:
            findings.append((_SEV_BLOCK, f"{_gate_mark(g)} **{text}**"
                             + (f" in `{g['env']}`" if g["env"] else "")
                             + _gate_way_out(g, " - to merge anyway, add `{}` to a "
                                                "commit message")))

    # COPS-2655. The pause finding below this one only fires when the PR
    # touches an identity file. This one fires whenever a CHANGED app sits
    # in a frozen environment, which is the case the pv-qa88-a probe
    # exposed: a cicd-versions.yaml bump rendered "Routine -- nothing
    # dangerous detected" for a change that would not be applied at all.
    #
    # _SEV_REVIEW, not _SEV_BLOCK: nothing dangerous is happening. The
    # danger is the reviewer believing something happened when it did not,
    # so the verdict must stop saying "Routine" and name the environments.
    if paused_changing:
        _envs = paused_envs or []
        findings.append((_SEV_REVIEW,
                         f"\u23f8\ufe0f **{len(_envs)} environment(s) are "
                         f"PAUSED** (`appspace.autosync: false`) \u2014 their "
                         f"changes below will NOT be applied on merge: "
                         f"{_fmt_env_list(_envs)}"))

    if decommission_lines:
        txt = "\n".join(decommission_lines)
        # COPS-2668: this was `"PURGE" in txt.upper()`, and it matched every
        # decommission there is. Both purge branches name
        # `appspace.decommissionPurgeData` — including the one whose entire
        # job is to say the purge is NOT armed — and uppercased that string
        # contains "PURGE". So the verdict announced "buckets/datasets are
        # destroyed" directly above a panel reading "Data is not purged".
        #
        # The module docstring already prescribes the remedy ("panels built
        # elsewhere are recognised by their own header constants"), which is
        # also how COPS-2660 wired the VM-strip finding. One constant, written
        # by the producer, matched here.
        purge = _DECOM_PURGE_HDR in txt
        orphan = _DECOM_ORPHAN_HDR in txt
        # COPS-2701: checked with orphan. Public-cloud teardowns also write
        # the orphan header (Applications gone, workloads stay), but the
        # verdict must not read as a private-cloud "decommission" that can
        # be armed with appspace.decommission.
        public_cloud = _DECOM_PUBLIC_CLOUD_HDR in txt
        # COPS-2697: checked before purge. Both can be true at once, and when
        # they are, the fact that matters is not "this environment's data is
        # destroyed" (expected, that is what a purge is) but "a DIFFERENT,
        # surviving environment loses its bucket and DNS record". AE-15284 was
        # a Sev1 of that shape; the ordinary purge wording would have read as
        # routine.
        shared_uc = _DECOM_SHARED_UC_HDR in txt
        # COPS-2766: a teardown that is a merge gate is BLOCK. A correctly
        # armed cascade, with or without purge, is a review: the build is
        # green for it.
        _sev = _SEV_BLOCK
        if shared_uc:
            _what = ("the user content bucket and DNS record are SHARED with a "
                     "surviving environment, which loses them too")
            _label = "Environment decommission"
        elif public_cloud:
            _what = ("public-cloud manual teardown: Applications are removed "
                     "but workloads stay until namespace/GCP cleanup "
                     "(no decommission gate; COPS-2700)")
            _label = "Public-cloud teardown"
        elif purge:
            _what = ("data purge is ARMED: buckets/datasets are destroyed, "
                     "not abandoned")
            _label = "Environment decommission"
            _sev = _SEV_REVIEW
        elif orphan:
            # No cascade: the Applications go, every workload keeps running.
            # Still a BLOCK \u2014 leaving a fleet of unmanaged workloads behind is
            # not a safer outcome, just a different one \u2014 but saying they are
            # "deleted" told the reviewer the opposite of what happens.
            _what = ("no cascade armed: the Applications are removed but "
                     "their workloads keep running, orphaned and unmanaged")
            _label = "Environment decommission"
        else:
            _what = "resources are deleted; data is abandoned, not purged"
            _label = "Environment decommission"
            _sev = _SEV_REVIEW
        findings.append((_sev,
                         "\U0001f5d1\ufe0f **" + _label + "** \u2014 "
                         + _what))
        # COPS-2707: the orphan finding above says the cascade is not armed.
        # When the reason is a misspelled flag, that is the actionable half
        # of the verdict -- the operator believes the flag is set, and the
        # panel is the only place that can tell them otherwise. Its own
        # finding, because it survives whichever branch chose `_what`.
        if _DECOM_PAUSED_HDR in txt:
            findings.append((_SEV_REVIEW,
                             f"\u23f8\ufe0f **{_DECOM_PAUSED_HDR}** for an environment "
                             "being removed: changes made on main during the pause "
                             "may not be live yet"))
        if _DECOM_FLAG_TYPO_HDR in txt:
            findings.append((_SEV_BLOCK,
                             "\U0001f6a8 **Teardown flag misspelled** "
                             "\u2014 the cascade flag on this environment "
                             "is not a key the platform reads, which is why "
                             "Phase 2 is pending (COPS-2707)"))
    if vm_change_lines:
        hdr = vm_change_lines[0]
        if hdr == _VM_PANEL_DANGER_HDR:
            # COPS-2635: when every dangerous bullet is a provision group,
            # the headline says what is actually happening in the
            # operator's words — "N environment(s) provision a NEW linux
            # VM" — instead of the generic danger flag. Any other danger
            # in the section (a resize, an armed deletion) keeps the
            # generic wording, because then "see the VM section" must not
            # sound like it is only about new machines.
            _dang = [l for l in vm_change_lines
                     if l.startswith("- \U0001f6a8")]
            _prov = [re.match(
                r"- \U0001f6a8 \*\*(\d+) environments? provisions? a new",
                l) for l in _dang]
            if _dang and all(_prov):
                _n = sum(int(m.group(1)) for m in _prov)
                findings.append((_SEV_BLOCK,
                                 f"\U0001f5a5\ufe0f **{_n} environment(s) "
                                 f"provision a NEW linux VM** \u2014 see "
                                 f"the VM section"))
            else:
                findings.append((_SEV_BLOCK,
                                 "\U0001f5a5\ufe0f **VM infrastructure change "
                                 "flagged dangerous** \u2014 see the VM section"))
        elif hdr == _VM_PANEL_ROUTINE_HDR:
            findings.append((_SEV_ROUTINE,
                             "\U0001f5a5\ufe0f VM infrastructure changed "
                             "(routine)"))

    deleted_apps = sorted(a for a, r in results.items() if r.deleted_resources)
    if deleted_apps:
        # COPS-2682: KCC CRs leaving the render under deletion-policy
        # abandon (or snapshot attachments that only drop the schedule
        # binding) are not GCP destroys. Pull them out of the
        # "resource(s) deleted" count so unmanage PRs stop looking like
        # destroy changes (acme-config-prod #4326).
        orphan_hdrs = set()
        for r in results.values():
            for f in (getattr(r, "vm_changes", None) or []):
                if f.get("orphaned") or (
                        f.get("deleted") and not f.get("dangerous")
                        and f.get("notes")):
                    orphan_hdrs.add(f.get("header"))
        # COPS-2714: HPAs removed because this same PR enables the
        # ping-scaler are the chart's documented contract, not a destroy.
        # They get their own REVIEW line below instead of the deletion count.
        ps_hdrs = _pingscaler_reclass(results)
        hard_hdrs = []
        hard_apps = []
        orphan_n = 0
        for a in deleted_apps:
            hard = [h for h in (results[a].deleted_resources or [])
                    if h not in orphan_hdrs and h not in ps_hdrs.get(a, ())]
            if hard:
                hard_hdrs += hard
                hard_apps.append(a)
            orphan_n += sum(
                1 for h in (results[a].deleted_resources or [])
                if h in orphan_hdrs)
        if hard_hdrs:
            # COPS-2683: count environments to match `_fmt_env_list` (same
            # class of app-vs-env lie as COPS-2675 on the render-blocked
            # headline). Orphan/abandon wording above is unchanged.
            #
            # COPS-2766: REVIEW, not BLOCK. The build never went red for a
            # deletion, and a stop sign on every planned cleanup taught
            # approvers to skip it. The kinds say what goes instead.
            findings.append((_SEV_REVIEW, _deletion_finding(
                hard_hdrs, len(set(_envs_from_apps(hard_apps))),
                _fmt_env_list(hard_apps))))
        if orphan_n:
            findings.append((_SEV_REVIEW,
                             f"\U0001f5a5\ufe0f **{orphan_n} KCC resource(s) "
                             f"unmanaged** (abandon / schedule attachment "
                             f"\u2014 GCP kept)"))
        ps_apps = sorted(a for a in deleted_apps if ps_hdrs.get(a))
        if ps_apps:
            n_hpa = sum(len(ps_hdrs[a]) for a in ps_apps)
            findings.append((_SEV_REVIEW,
                             f"\U0001f39a\ufe0f **acme-ping-scaler activated** "
                             f"in {_fmt_env_list(ps_apps)} \u2014 it takes "
                             f"over replica control; {n_hpa} HPA(s) removed "
                             f"by design "
                             f"([how it works]({PINGSCALER_DOCS_URL}))"))
    # COPS-2766: GKE deletes a NEG only when no BackendService uses it
    # (acme-config-prod #3888). Paired per env: a cl-* block's -glb app
    # (cl-qa-11-a-app1) belongs to the constellation that has the NEGs.
    neg = _envs_from_apps([a for a, r in results.items()
                           if getattr(r, "neg_removed", None)])
    bs = _envs_from_apps([a for a, r in results.items()
                          if any(_section_kind(h) == "ComputeBackendService"
                                 for h in r.deleted_resources or ())])
    both = [n for n in neg if any(b == n or (n.startswith("cl-") and b.startswith(n + "-"))
                                  for b in bs)]
    if both:
        # The action first: the green status lead is cut at 255 bytes.
        findings.append((_SEV_REVIEW,
                         "\U0001f517 **NEG and BackendService removed together** in "
                         + _fmt_service_list([f"`{n}`" for n in both], 3)
                         + ": after the sync, check `kubectl get svcneg -n "
                         "<namespace>`, and if one is stuck, delete the "
                         "BackendService, never the finalizer. GKE deletes a NEG "
                         "only when no BackendService uses it (acme-config-prod "
                         "#3888)."))
    renamed_apps = sorted(a for a, r in results.items()
                          if getattr(r, "renamed_resources", None))
    if renamed_apps:
        findings.append((_SEV_ROUTINE,
                         "\u267b\ufe0f resources renamed/recreated (not a "
                         f"deletion): {_fmt_env_list(renamed_apps)}"))

    downgraded = sorted(a for a, r in results.items()
                        if r.version_change
                        and _is_version_downgrade(*r.version_change))
    if downgraded:
        # COPS-2638: name the version pair, not just the fact. "Chart
        # version downgrade in pv-x" left the reviewer opening the app
        # block to learn FROM and TO what -- the same gap the bump line
        # closes for the routine direction.
        _dg = ", ".join(f"`{o}` \u2192 `{n}`" for o, n in
                        sorted({results[a].version_change
                                for a in downgraded}))
        _mix = _downgrade_mix_note(results)
        findings.append((_SEV_REVIEW,
                         f"\u2b07\ufe0f **Chart version downgrade** {_dg} in "
                         f"{_fmt_env_list(downgraded)}"
                         + (f". {_mix}" if _mix else "")))
    # COPS-2766: a new -dev chart in a PR that also takes release charts
    # (#4382). A -dev tag is mutable. A lone -dev pin is normal testing.
    dev_new = sorted(a for a, r in results.items() if r.version_change
                     and str(r.version_change[1]).endswith("-dev")
                     and not str(r.version_change[0]).endswith("-dev"))
    if dev_new and any(r.version_change and not str(r.version_change[1]).endswith("-dev")
                       for r in results.values()):
        _vers = ", ".join(f"`{v}`" for v in sorted({results[a].version_change[1]
                                                     for a in dev_new}))
        findings.append((_SEV_REVIEW,
                         f"\U0001f9ea **-dev chart next to release charts** {_vers} "
                         f"in {_fmt_env_list(dev_new)}. A -dev tag can be pushed "
                         f"again at any time: check that it belongs in this PR."))
    # COPS-2766: an image tag that goes down while the chart stays or goes up
    # (#4679 took device 1.117.4 -> 1.116.10 on pv-gsk--aec1-c: "Routine").
    img_apps = sorted(a for a, r in results.items()
                      if getattr(r, "image_downgrades", None))
    if img_apps:
        _pairs = sorted({(_section_name(h), o, n) for a in img_apps
                         for h, _repo, o, n in results[a].image_downgrades})
        _more = f" (+{len(_pairs) - 3} more)" if len(_pairs) > 3 else ""
        findings.append((_SEV_REVIEW,
                         f"\u2b07\ufe0f **Image downgrade** in "
                         f"{_fmt_env_list(img_apps)}: "
                         + ", ".join(f"`{s}` `{o}` \u2192 `{n}`"
                                     for s, o, n in _pairs[:3])
                         + f"{_more}. Check that an old pin or an old branch "
                         f"is not moving it back."))
    # COPS-2766: a chart bump ships a newer default, and a pin that was not
    # older before now holds the service back (COPR-32582).
    pin_apps = sorted(a for a, r in results.items()
                      if isinstance(getattr(r, "pins_behind", None), list)
                      and r.pins_behind)
    if pin_apps:
        _pins = sorted({tuple(p) for a in pin_apps for p in results[a].pins_behind})
        _more = f" (+{len(_pins) - 3} more)" if len(_pins) > 3 else ""
        findings.append((_SEV_REVIEW,
                         f"\U0001f4cc **Image pin left behind** in "
                         f"{_fmt_env_list(pin_apps)}: "
                         + "; ".join(f"`{s}` pinned `{p}`, the new chart "
                                     f"ships `{n}`" for s, p, n in _pins[:3])
                         + f"{_more}. The pin now holds the service back: "
                         f"bump it or remove it."))
    pin_skipped = sorted(a for a, r in results.items()
                         if getattr(r, "pins_behind", None) == "skipped")
    if pin_skipped:
        findings.append((_SEV_ROUTINE,
                         f"\u2139\ufe0f Pin check skipped in "
                         f"{_fmt_env_list(pin_skipped)}: the chart versions.yaml "
                         f"or a value file could not be read."))
    # COPS-2632 / COPS-2677: a rendered `%!s(<nil>)` or `<no value>` is a
    # value the chart read and this environment does not set. Live proof:
    # pv-stage1-a shipped `hosting-id: hst-%!s(<nil>)` and KCC rejected every
    # Compute* resource afterwards, while this summary called the PR routine.
    #
    # Severity is scoped on purpose (2.47 global BLOCK → 2.48 REVIEW → 2.88
    # COPS-2677 scoped BLOCK). The chart is still the authority for ordinary
    # fields (`required` → REASON_MISSING_REQUIRED). ConfigMap/Deployment
    # artifacts stay REVIEW. KCC Compute* artifacts BLOCK and fail the build:
    # that is the class KCC actually rejected in production.
    artifact_apps = sorted(a for a, r in results.items()
                           if getattr(r, "template_artifacts", None))
    if artifact_apps:
        block_apps, review_apps = [], []
        for a in artifact_apps:
            arts = results[a].template_artifacts or []
            if any(_is_kcc_blocking_artifact(h) for h in arts):
                block_apps.append(a)
            else:
                review_apps.append(a)
        if block_apps:
            n_res = sum(
                sum(1 for h in (results[a].template_artifacts or [])
                    if _is_kcc_blocking_artifact(h))
                for a in block_apps)
            findings.append((_SEV_BLOCK,
                             "\U0001f9ec **Unresolved KCC value** \u2014 "
                             f"{n_res} Compute* resource(s) render "
                             f"`%!s(<nil>)` or `<no value>` in "
                             f"{_fmt_env_list(block_apps)}. KCC rejects these "
                             "labels/fields in the cluster (COPS-2632). Set "
                             "the missing value (usually `appspace.hostingID`) "
                             "before merging."))
        if review_apps:
            n_res = sum(len(results[a].template_artifacts)
                        for a in review_apps)
            findings.append((_SEV_REVIEW,
                             "\U0001f9ec **Unresolved chart value** \u2014 "
                             f"{n_res} resource(s) render `%!s(<nil>)` or "
                             f"`<no value>` in {_fmt_env_list(review_apps)}. "
                             "The chart read a value this environment does not "
                             "set. Check it is intended: the chart does not "
                             "mark it `required`, so nothing failed the "
                             "render."))

    # An environment going fully dark and a single service being scaled down
    # are different events. Both used to render as "Replicas scaled to zero",
    # and on acme-config-dev PR #7063 the whole-environment case did not
    # render at all (see _replicas_end_state). A reviewer needs the shutdown
    # stated as a shutdown, in the summary, not inferred from a resource count.
    #
    # COPS-2677: leftover HPAs under zeroPods are REVIEW, not BLOCK. After
    # COPS-2548 AppSet stopped ignoring Deployment /spec/replicas, chart
    # `replicas: 0` reaches the cluster and HPA usually idles at zero
    # (ScalingDisabled). Blocking every hibernation PR that still has HPA
    # enabled would false-stop merges that work today. The chart gate that
    # skips hpa.yaml under zeroPods is the cleanup; here we only shout.
    #
    # COPS-2683: judge shutdown per environment across sibling apps (-ms/-ss)
    # before the headline, and for partial scale only count HPAs whose
    # scaleTargetRef names a zeroed workload (not the whole fleet).
    zeroed_apps = sorted(a for a, r in results.items() if r.replicas_zeroed)
    shutdown_apps = [a for a in zeroed_apps
                     if _is_env_shutdown(results[a])]
    partial_apps = [a for a in zeroed_apps if a not in set(shutdown_apps)]
    # Demote env-level shutdown when a sibling app of the same environment
    # still has running workloads (ms all-zero + ss partial must not read
    # as "Environment shutting down" for that env name).
    _shutdown_envs = set(_envs_from_apps(shutdown_apps))
    _partial_envs = set(_envs_from_apps(partial_apps))
    for a, r in results.items():
        env = _envs_from_apps([a])[0]
        if env not in _shutdown_envs:
            continue
        stats = getattr(r, "shutdown_stats", None) or {}
        if stats.get("workloads") and not _is_env_shutdown(r):
            _partial_envs.add(env)
    _demote = _shutdown_envs & _partial_envs
    if _demote:
        shutdown_apps = [a for a in shutdown_apps
                         if _envs_from_apps([a])[0] not in _demote]
        for a in zeroed_apps:
            if (_envs_from_apps([a])[0] in _demote
                    and a not in partial_apps):
                partial_apps.append(a)
    hpa_note_apps = [
        a for a in shutdown_apps
        if (getattr(results[a], "shutdown_stats", None) or {}).get(
            "hpas_remaining", 0) > 0
    ]
    clean_shutdown_apps = [a for a in shutdown_apps
                           if a not in set(hpa_note_apps)]
    if hpa_note_apps:
        n_hpa = sum(
            (results[a].shutdown_stats or {}).get("hpas_remaining", 0)
            for a in hpa_note_apps)
        findings.append((_SEV_REVIEW,
                         "\U0001f6d1 **Environment shutting down** \u2014 "
                         f"every workload scaled to 0 in "
                         f"{_fmt_env_list(hpa_note_apps)}, and "
                         f"{n_hpa} HorizontalPodAutoscaler(s) remain in "
                         "desired. Hibernation still applies `replicas: 0` "
                         "(COPS-2548); leftover HPAs can fight a later "
                         "scale-up. Prefer a chart that skips HPA under "
                         "`appspace.zeroPods` (COPS-2677)."))
    if clean_shutdown_apps:
        n_workloads = sum(results[a].shutdown_stats["workloads"]
                          for a in clean_shutdown_apps)
        findings.append((_SEV_REVIEW,
                         "\U0001f6d1 **Environment shutting down** \u2014 "
                         f"every workload ({n_workloads}) scaled to 0 in "
                         f"{_fmt_env_list(clean_shutdown_apps)}. "
                         "`appspace.zeroPods` hibernates the environment: "
                         "nothing will be running after this merges."))
    partial_hpa_apps = [
        a for a in partial_apps
        if (getattr(results[a], "shutdown_stats", None) or {}).get(
            "hpas_targeting_zeroed", 0) > 0
    ]
    clean_partial_apps = [a for a in partial_apps
                          if a not in set(partial_hpa_apps)]
    if partial_hpa_apps:
        n_hpa = sum(
            (results[a].shutdown_stats or {}).get("hpas_targeting_zeroed", 0)
            for a in partial_hpa_apps)
        findings.append((_SEV_REVIEW,
                         "\U0001f9ca **Replicas scaled to zero** in "
                         f"{_fmt_env_list(partial_hpa_apps)}, and "
                         f"{n_hpa} HorizontalPodAutoscaler(s) still target "
                         "those workloads. Prefer removing or disabling the "
                         "matching HPA with the scale-down (COPS-2683)."))
    if clean_partial_apps:
        findings.append((_SEV_REVIEW,
                         "\U0001f9ca **Replicas scaled to zero** in "
                         f"{_fmt_env_list(clean_partial_apps)}"))

    # COPS-2766 (COPR-32597): capacity facts from the full renders. Warnings
    # only, because a planned right-sizing looks the same. The action first:
    # the green status lead is cut at 255 bytes. An item names its env only
    # when the finding has more than one.
    cap = {a: r.capacity for a, r in sorted(results.items())
           if getattr(r, "capacity", None)}
    for part, text, action, tail in (
            ("cuts", "\U0001f4c9 **Capacity cut**", "check that it is planned",
             "Keep 2 replicas or more, keep the floor of the connection "
             "services, and a CPU request of 30m or more (COPR-32597)."),
            ("released", "\U0001f501 **Fixed replicas released**",
             "merge in a quiet window",
             "On sync the field goes away, and Kubernetes runs 1 replica until "
             "the HPA or acme-ping-scaler scales it back (acme-config-prod "
             "#4523).")):
        apps = [a for a in cap if cap[a].get(part)]
        envs = sorted(set(_envs_from_apps(apps)))
        items = [f"`{w}` " + (f"({what})" if part == "released" else what)
                 + (f" in `{_envs_from_apps([a])[0]}`" if len(envs) > 1 else "")
                 for a in apps for w, what in cap[a][part]]
        if items:
            findings.append((_SEV_REVIEW,
                             f"{text} in {_fmt_service_list([f'`{e}`' for e in envs], 3)}"
                             f", {action}: {_fmt_service_list(items, 4)}. {tail}"))

    if appspace_state_lines:
        txt = "\n".join(appspace_state_lines)
        # COPS-2660: its own finding ON TOP of the arming one below, because
        # they answer different questions. "Decommission ARMED" says what the
        # PR intends; this says the same PR broke the mechanism that intent
        # relies on. acme-config-prod #4247 shipped the shape: allowDeletion
        # added while the role blocks were stripped, so helm stops rendering
        # the VM CRs and ArgoCD prunes them still carrying
        # `deletion-policy: abandon` -- the cloud VM is orphaned, not deleted.
        if _DECOM_VM_STRIP_HDR in txt:
            findings.append((_SEV_BLOCK,
                             "\U0001f5a5⛔ **Decommission arming is BROKEN** "
                             "— this PR strips the Linux VM config in "
                             "the same change that arms deletion, so the "
                             "live VM, disk and IP would be pruned under "
                             "`abandon` and ORPHANED in the cloud, not "
                             "deleted. Keep the VM block and only add "
                             "`allowDeletion`."))
        # COPS-2707: its own axis, so a plain `if`. A misspelled flag is not
        # an alternative to the states below, it is the reason none of them
        # fired: the key never reached Helm, so the render is identical and
        # every other panel is quiet. acme-config-prod #4376 merged
        # `decomission: true` under a green "Routine" verdict, and the
        # operator went on to open the folder-removal PR believing Phase 2
        # was done.
        if _DECOM_FLAG_TYPO_HDR in txt:
            findings.append((_SEV_BLOCK,
                             "\U0001f6a8 **Teardown flag misspelled or misplaced** "
                             "\u2014 a `decommission` / `allowDeletion` / "
                             "`confirmProdDeletion` key in this PR is not one "
                             "the platform reads at that depth, so it arms "
                             "nothing (COPS-2707)"))
        # Arming destruction is the highest-severity thing a config-only PR
        # can do, and it is invisible in the manifest diff: the footer still
        # reads "No manifest changes". Live proof, acme-config-dev PR #7024:
        # the body shouted DECOMMISSION ARMED while this summary said
        # "Routine - nothing dangerous detected". A verdict that contradicts
        # the panel below it is worse than no verdict at all.
        # COPS-2701: checked before PURGE/DECOMMISSION ARMED. On cl-* the
        # panel writes _DECOM_PUBLIC_CLOUD_NOOP_HDR instead of those banners;
        # matching the private-cloud strings here would promise a cascade
        # that the ApplicationSets never template (COPS-2700).
        if _DECOM_PUBLIC_CLOUD_NOOP_HDR in txt:
            findings.append((_SEV_BLOCK,
                             "\U0001f6a8 **Public-cloud decommission flag "
                             "is a NO-OP** \u2014 `cl-*` ApplicationSets "
                             "never cascade-delete; workloads stay until "
                             "manual namespace/GCP cleanup (COPS-2700)"))
        elif "PURGE ARMED" in txt:
            # COPS-2766: REVIEW, the build is green for an arming PR.
            findings.append((_SEV_REVIEW,
                             "\U0001f512 **Data purge ARMED** \u2014 the "
                             "cascade will permanently destroy the BigQuery "
                             "dataset and the user content bucket"))
        elif "DECOMMISSION ARMED" in txt and _DECOM_VM_STRIP_HDR not in txt:
            # COPS-2660 follow-up: when the arming is broken, the BROKEN
            # finding above already states the arming and its consequence.
            # Read live on PR #7113, the summary told one event four ways;
            # the generic line adds nothing next to the specific one, so it
            # stands down and the story is told once.
            findings.append((_SEV_REVIEW,
                             "\U0001f512 **Decommission ARMED** \u2014 this "
                             "environment becomes eligible for cascade "
                             "deletion when its folder is removed"))
        elif "DISARMED" in txt.upper():
            findings.append((_SEV_ROUTINE,
                             "\U0001f513 decommission disarmed (safe "
                             "direction)"))
        if _AUTOSYNC_PAUSED_HDR in txt:
            findings.append((_SEV_REVIEW,
                             "\u23f8\ufe0f **ArgoCD auto-sync paused** for an "
                             "environment \u2014 changes stop being applied"))
        elif _AUTOSYNC_RESUMED_HDR in txt:
            findings.append((_SEV_REVIEW,
                             "\u25b6\ufe0f **ArgoCD auto-sync resumed** \u2014 "
                             "pending drift will be applied"))
        # COPS-2693 Plan B: a non-version change to a shared config.yaml that
        # reaches many environments at once. REVIEW, never BLOCK: legitimate
        # fleet-wide changes exist, but their reach must be impossible to miss
        # in the verdict, because with automated+prune+selfHeal it lands on
        # everything simultaneously ~5 minutes after merge.
        if _BLAST_RADIUS_HDR in txt:
            m = re.search(r"(\d+) environments across (\d+) spoke", txt)
            _reach = (f" \u2014 reaches {m.group(1)} environments across "
                      f"{m.group(2)} spoke(s)" if m else "")
            findings.append((_SEV_REVIEW,
                             "\U0001f4a5 **Wide-reach config change**"
                             + _reach +
                             "; changes to shared config bypass cohort "
                             "staging (see the blast-radius note)"))
        # COPS-2766: a noCore flip is REVIEW. The one error, a move that turns
        # it off, is the nocore_lost gate above.
        nc = _NOCORE_COUNTS_RE.search(txt)
        if nc:
            # The names first: the Builds panel shows only the start.
            names = (_panel_names(*_NOCORE_NAMES_RE.findall(txt))
                     or f"{nc.group(1)} environment(s)")
            findings.append((_SEV_REVIEW,
                             f"\U0001f50c **noCore changes** in {names}: on in "
                             f"{nc.group(2)}, off in {nc.group(3)} (read the noCore "
                             f"note, COPS-2758)"))
        if _NOCORE_UNKNOWN in txt:
            # Named envs when a value file is bad YAML, the PR when it crashed.
            unk = _NOCORE_UNKNOWN_RE.search(txt)
            findings.append((_SEV_REVIEW,
                             f"\U0001f50c **{_NOCORE_UNKNOWN}** for "
                             f"{_panel_names(unk.group(1)) if unk else 'this PR'}: "
                             f"check `appspace.infra.noCore` by hand before you merge"))
        lb = _LEGACY_BACKENDS_RE.search(txt)
        if lb:
            findings.append((_SEV_REVIEW,
                             f"\U0001f517 **Legacy backends come back** in "
                             f"{_panel_names(lb.group(1))}: sync `-ms` and `-glb`, "
                             f"then check `kubectl get svcneg`"))
        # COPS-2766: routine, the reach is a fact and not a risk. The names
        # come from our own line only.
        _tw = [l for l in txt.splitlines()
               if l.startswith("\U0001f310 " + _TENANT_WIDE_HDR)]
        if _tw:
            findings.append((_SEV_ROUTINE,
                             "\U0001f310 **Reaches every public-cloud tenant** of "
                             + ", ".join(f"`{c}`" for c in re.findall(r"`([^`]+)`", _tw[0]))))
        if _IDENTITY_MIGRATION_HDR in txt:
            findings.append((_SEV_REVIEW,
                             "\U0001f500 **Planned rename of a live environment** "
                             "\u2014 the old namespace keeps running until you clean "
                             "it up by hand (see the rename note)"))
        # COPS-2721: customer.yaml (or another leaf) re-states values a
        # parent config.yaml already sets identically. Manifests stay
        # byte-identical; without this line the verdict says Routine /
        # "No manifest changes" and operators conclude Diff Preview missed
        # the edit (acme-config-prod #4520).
        if _VALUES_REDUNDANCY_HDR in txt:
            findings.append((_SEV_REVIEW,
                             _HIGHER_LAYER_FINDING + " \u2014 some keys match an "
                             "ancestor config.yaml, so they do not change "
                             "rendered manifests (see the higher-layer note)"))
        # COPS-2766: keys a live env changed while all its apps render the same.
        m = re.search(re.escape(_INERT_EDIT_HDR) + r" (\d+) keys? changed.*?\n- `([^`]+)`: "
                      r"`([^`]+)`", txt, re.S)
        if m:
            more = int(m[1]) - 1
            findings.append((_SEV_REVIEW,
                             f"\U0001f4a4 **An edit changes nothing rendered**: `{m[3]}` "
                             f"in `{m[2]}`" + (f" (+{more} more)" if more else "")))
    # COPS-2766: the checks of a new env are one REVIEW finding, led by the first.
    checks = [l for l in new_env_lines or () if l.startswith(_NEW_ENV_CHECK_PREFIX)]
    if checks:
        first = checks[0][len(_NEW_ENV_CHECK_PREFIX):].split(". ", 1)[0].rstrip(".")
        findings.append((_SEV_REVIEW, f"\U0001f195 **New environment: {len(checks)} "
                                      f"check(s) to review** - {first}"))
    if new_env_lines and (new_env_structural or not checks):
        # A structural problem makes the build red, so it is a stop sign too.
        findings.append((_SEV_BLOCK if new_env_structural else _SEV_ROUTINE,
                         "\U0001f195 **New environment** in this PR"
                         + (" \u2014 its configuration did not validate"
                            if new_env_structural else "")))

    # The 50% case: fleets jumping from one version to another. Named the
    # way operations talks about it -- environments and versions.
    for _sig in sorted(rollup_by_sig or {}):
        apps = [a for _rep, _mem, _r in rollup_by_sig[_sig] for a in _mem]
        findings.append((_SEV_ROUTINE,
                         f"\u2b06\ufe0f **{len(set(_envs_from_apps(apps)))} "
                         f"environment(s) jumping** "
                         f"{_routine_bump_label(_sig)}: "
                         f"{_fmt_env_list(apps)}"))

    # COPS-2638: the line above only fires for PURE bumps (the rollup only
    # forms when an app's entire diff is the transition). A bump mixed
    # with any other change -- acme-config-prod #4037, where 31 resources
    # moved for other reasons -- lost the line entirely, and the single
    # most common PR shape became invisible in the verdict. version_change
    # is the general fact: the chart targetRevision ArgoCD currently has
    # versus the one the PR pins, set whenever they differ. Transitions a
    # fleet-jump line already names are skipped, as are downgrades (their
    # REVIEW finding below names them); this line is ROUTINE because a
    # bump is these PRs' normal business.
    _named = {(s[0], s[1]) for s in (rollup_by_sig or {}) if s[0] or s[1]}
    _bumps = {}
    for a, r in results.items():
        if (r.outcome == OUT_DIFF and r.version_change
                and r.version_change not in _named
                and not _is_version_downgrade(*r.version_change)):
            _bumps.setdefault(r.version_change, []).append(a)
    for (_old, _new), apps in sorted(_bumps.items()):
        findings.append((_SEV_ROUTINE,
                         f"\u2b06\ufe0f **{len(set(_envs_from_apps(apps)))} "
                         f"environment(s) bump** `{_old}` \u2192 `{_new}`: "
                         f"{_fmt_env_list(sorted(apps))}"))

    changed = [a for a, r in results.items() if r.outcome == OUT_DIFF]
    errored = [a for a, r in results.items() if r.outcome == OUT_ERROR]
    unknown = [a for a, r in results.items() if r.outcome == OUT_INDETERMINATE]
    # COPS-2629 point 4: split by whether the failure is PERMANENT.
    #
    # Escalating every undiffable app to BLOCK would be wrong. One
    # transient timeout among 200 apps is not a reason to stop a
    # maintenance window, and a verdict that cries wolf is one people learn
    # to scroll past -- the same failure this umbrella keeps guarding
    # against, arriving from the other direction.
    #
    # PERMANENT_REASONS is already defined as "the deployer would fail the
    # same way", which is exactly the condition that makes merging unsafe:
    # helm could not render it here and it will not render in the cluster
    # either. Reusing that set rather than inventing a second opinion means
    # the verdict and the retry logic can never disagree about what is
    # broken.
    blocked = [a for a in unknown
               if results[a].reason in PERMANENT_REASONS]
    soft = [a for a in unknown if a not in set(blocked)]
    if blocked:
        # COPS-2675: `blocked` holds APPS (dict keys like `pv-x-ms`), and an
        # environment can fail on more than one of its apps at once -- this
        # exact "Missing Image Tag" class hits every -ms app of a cohort
        # together. len(blocked) then over-counts relative to the names
        # _fmt_env_list actually shows (deduped to environments, the same
        # population used two call sites above for the bump/rollup lines),
        # so the headline could read "3 environment(s)" over a list of 2
        # names with no "+more" to account for the gap. Count the same
        # deduped population the list displays.
        _blocked_envs = set(_envs_from_apps(blocked))
        # COPS-2676: name the error in the verdict bullet. Without this the
        # summary only listed environments and the actionable "Missing Image
        # Tag" / template path lived ~40% down the comment under deletion and
        # bump noise (acme-config-prod #4310).
        _why = (f" \u2014 **{block_headline}**"
                if block_headline else
                " \u2014 helm failed here and the deployer will fail the "
                "same way")
        findings.append((_SEV_BLOCK,
                         f"\u26d4 **{len(_blocked_envs)} environment(s) cannot "
                         f"render**{_why}: "
                         f"{_fmt_env_list(blocked)}"))
    if errored or soft:
        findings.append((_SEV_REVIEW,
                         f"\u2754 **{len(errored) + len(soft)} app(s) "
                         f"could not be diffed** \u2014 the comment below "
                         f"cannot prove they are safe"))
    if not findings:
        findings.append((
            _SEV_ROUTINE,
            (f"\u2705 {len(changed)} app(s) change, nothing risk-flagged"
             if changed else
             "\u2705 No manifest changes and no risky configuration change")))

    sev = max(s for s, _ in findings)
    if green:
        sev = min(sev, _SEV_REVIEW)
    n_check = sum(1 for s, _ in findings if s >= _SEV_REVIEW)
    verdict = _VERDICTS[sev]
    if sev >= _SEV_REVIEW:
        verdict += f" ({n_check} item(s))"
    order = {_SEV_BLOCK: 0, _SEV_REVIEW: 1, _SEV_ROUTINE: 2}
    findings.sort(key=lambda f: (order[f[0]], _REVIEW_RANK.get(f[1][:1], 10)
                                 if f[0] == _SEV_REVIEW else 0))
    return build_marks([MERGE_SUMMARY_HDR, "", verdict, ""] +
                       [f"- {line}" for _s, line in findings] + [""], green)


# COPS-2766: a green build status leads with the top finding of the merge
# summary. The Builds panel on the PR page shows the status: #4684 was
# approved seconds after a green "129 resource(s) will change".
# The build is green, so the marker never says DO NOT MERGE, and it is
# never red (the icon rule). The stop sign is for comments before 2.122.0.
_STATUS_MARKS = {"\u26d4": "\u26a0\ufe0f", "\u26a0": "\u26a0\ufe0f"}


def status_lead(comment_md) -> str:
    """'<marker> <first finding as plain text>', or '' when the verdict is
    routine or there is no summary.

    Reads only our own summary, before the first '---' of the comment, so
    author content further down cannot fake it. Pure: process_pr and
    fix_stuck_inprogress call it on the same comment and get the same lead."""
    lines = (comment_md or "").split("\n---\n", 1)[0].splitlines()
    if MERGE_SUMMARY_HDR not in lines:
        return ""
    rest = [l for l in lines[lines.index(MERGE_SUMMARY_HDR) + 1:] if l.strip()]
    mark = _STATUS_MARKS.get(rest[0][:1]) if rest else None
    bullet = next((l[2:] for l in rest[1:] if l.startswith("- ")), "")
    if bullet.startswith(_HIGHER_LAYER_FINDING):
        return ""       # it sorts last, so nothing else needs a review
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", bullet)
    text = text.replace("**", "").replace("`", "").strip()
    emoji, _, after = text.partition(" ")
    if not re.search(r"[A-Za-z0-9]", emoji):
        text = after.strip()
    text = re.sub(r"(?i)do\s+not\s+merge", "review", build_marks([text], True)[0])
    return f"{mark} {text}" if mark and text else ""


def _utf8_len(s) -> int:
    return len(s.encode("utf-8", "surrogatepass"))


def _cut_utf8(s, room) -> str:
    """s in `room` UTF-8 bytes: whole, or cut and ending in '...', or ''."""
    if _utf8_len(s) <= room:
        return s
    s = s.encode("utf-8", "surrogatepass")[:max(room - 3, 0)]     # 3 for the "..."
    s = s.decode("utf-8", "ignore").rstrip()
    return s and s + "..."


def join_status_lead(lead, description, limit=255) -> str:
    """'<lead> | <description>' in `limit` UTF-8 bytes. We do not know the
    unit Bitbucket counts, and bytes are the largest one, so this fits any.
    Only the lead is cut, ending in '...'. The description is never cut:
    when it leaves no room for a lead, it comes back unchanged."""
    if not lead:
        return description
    tail = f" | {description}"
    room = limit - _utf8_len(tail)
    if _utf8_len(lead) <= room:
        return lead + tail
    cut, n = "", 3                       # 3 for the "..."
    for c in lead:
        n += _utf8_len(c)
        if n > room:
            break
        cut += c
    return f"{cut.rstrip()}...{tail}" if cut.strip() else description


def commit_authors_line(names) -> str:
    """COPS-2766: the line under the verdict that names the other people who
    wrote commits in the PR. None means the commits could not be read.

    Not a '- ' bullet, so status_lead never reads it. Git author names are
    PR-controlled markdown: each one is one line with no backticks and no
    \u26d4 \U0001f6a8 \u274c (a green build shows none of them outside a
    fence), cut to 40 characters, inside backticks."""
    if names is None:
        return ("\U0001f465 Could not read who wrote the commits of this PR. "
                "Check the commit authors before you approve.")
    if not names:
        return ""
    drop = dict.fromkeys(map(ord, "`\u26d4\U0001f6a8\u274c"))
    shown = ", ".join("`%s`" % " ".join(n.translate(drop).split())[:40]
                      for n in names[:5])
    more = f" and {len(names) - 5} more" if len(names) > 5 else ""
    return (f"\U0001f465 **Other people wrote commits in this PR:** {shown}{more}. "
            "Their approval is not independent, so ask another person to approve.")


_SHUTDOWN_MIN_WORKLOADS = 2


def _is_env_shutdown(r) -> bool:
    """True when every workload in this app ends at zero replicas.

    The floor of two workloads is deliberate: a one-workload app dropping to
    zero is a scale-down, and calling that an environment shutdown on every
    small app is how a warning trains people to skip it (the same reasoning
    as COPS-2605's three-group rollup floor).
    """
    stats = getattr(r, "shutdown_stats", None) or {}
    total = stats.get("workloads") or 0
    return (total >= _SHUTDOWN_MIN_WORKLOADS
            and stats.get("zeroed") == total)
