"""Decommission and new-environment analysis.

Sliced out of diff_preview.py unchanged (COPS-2658 phase 4).

Two ends of an environment's life, together because they answer the same
shape of question: what is this PR doing to an environment as a whole,
rather than to one resource inside it.

`_decommission_phase_table` and `_cascade_retention_reason` cover the
teardown -- which phase a decommission is in, and why a resource survives a
cascade delete. `_new_env_status` covers the other end. `_strip_trailing_comment` (now in manifest.py)
comes along as the only in-repo helper they depend on.

`_apps_to_skip_for_decommission` and `_moves_missing_cohort_lines` were left
in the hub on purpose. Both are monkeypatch targets, and phase 2's pre-flight
rule refuses to move a member the suite patches. Their only reader is
`process_pr`, which never moves, so the seam would provably hold -- but
relaxing the rule is worth its own decision rather than 40 quiet lines.
"""

import re

from manifest import _strip_trailing_comment


def _new_env_status(render_error: str):
    """Classify a new-environment render failure into a Bitbucket status.

    Returns (bitbucket_state, is_expected):
      ("SUCCESSFUL", True)  — the failure is EXPECTED for a first-time env
                              (helm needs credentials/constellation files that
                              only exist after the first deploy). Stays green.
      ("FAILED", False)     — anything else. Default is FAILED (v2.5.4,
                              Finding 5) — see below for why.

    Before v2.4.9 every new-env render failure produced the same green
    "will be created on merge" status, so a genuinely broken new env (e.g. a
    missing/typo'd appspace.version) merged with a green check and then simply
    failed to deploy with no earlier warning (FIX E).

    v2.5.4 (Finding 5): FIX E only ever built a DENY-list of known-bad
    patterns (invalid YAML, missing version, chart not found, ...) and
    defaulted everything NOT on that list to green "expected". That default
    was backwards: "chart pull failed" (a generic exception — network,
    disk, or an actual bug) and "registry login may have failed" matched
    none of the deny-list patterns and went green; worse, a genuine
    render_failed error unrelated to missing credentials (the same
    error class fixed for existing envs in Finding 1, e.g. a type-mismatched
    value making a template execution fail) also went green, silently
    telling a reviewer "this will be created cleanly on merge" for a
    brand-new environment that would actually fail to deploy.
    The fix inverts the polarity to an ALLOW-list: only the one specific,
    well-understood shape already documented above and in the original FIX E
    comment — Helm's `required` template function failing because
    constellation/secret files don't exist until first deploy, which always
    surfaces as "Missing required value" — is treated as expected. Every
    other error, recognized or not, defaults to FAILED.
    """
    err = (render_error or "").lower()
    # The one well-understood, deliberately-designed "expected" shape: Helm's
    # `required` function failing on a value that only exists after the first
    # real deploy (constellation files, post-deploy secrets). This is the
    # ONLY case that stays green for a new environment.
    if "missing required value" in err:
        return "SUCCESSFUL", True
    # Everything else — invalid YAML, missing appspace.version, chart/OCI
    # not found, a generic chart-pull exception, a registry-login failure,
    # or any other render_failed error — is FAILED by default now.
    return "FAILED", False


_CASCADE_KEEP_CRD_REASON = "CRD (shared with other environments)"
_CASCADE_KEEP_POLICY_REASON = "helm.sh/resource-policy: keep"
_CASCADE_KEEP_DELETE_FALSE_REASON = "sync-options: Delete=false"


def _cascade_retention_reason(type_key: str, doc_text) -> str:
    """Why ArgoCD's cascade delete SKIPS this resource, or "" if it deletes it.

    Mirrors shouldBeDeleted (controller/appcontroller.go), which is the only
    authority on what a cascade actually removes:

        !kube.IsCRD(obj) && !isSelfReferencedApp(app, kube.GetObjectRef(obj)) &&
        (deleteOption == nil || *deleteOption != synccommon.SyncValueFalse) &&
        !resourceutil.HasAnnotationOption(obj, helm.ResourcePolicyAnnotation,
                                          helm.ResourcePolicyKeep)

    The self-reference clause is about the Application object itself, which
    never appears in a rendered manifest, so only the other three apply here.

    doc_text is the rendered document. Anything that is not a string (older
    callers pass placeholder values) is treated as carrying no annotations,
    which errs towards counting a resource as deleted: overstating the
    destruction is the safe direction for a warning.
    """
    if type_key.split("/")[-1] == "CustomResourceDefinition":
        return _CASCADE_KEEP_CRD_REASON
    if not isinstance(doc_text, str):
        return ""
    for line in doc_text.splitlines():
        stripped = line.strip()
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        value = _strip_trailing_comment(value.strip()).strip("\"'")
        if key.strip() == "helm.sh/resource-policy" and value == "keep":
            return _CASCADE_KEEP_POLICY_REASON
        if key.strip() == "argocd.argoproj.io/sync-options" and "Delete=false" in value:
            return _CASCADE_KEEP_DELETE_FALSE_REASON
    return ""


def _split_resources_by_cascade_fate(resources: dict) -> tuple:
    """(deleted_subdict, {(type_key, reason): count}) for a cascade delete."""
    deleted, retained = {}, {}
    for key, doc in resources.items():
        reason = _cascade_retention_reason(key[0], doc)
        if reason:
            rk = (key[0], reason)
            retained[rk] = retained.get(rk, 0) + 1
        else:
            deleted[key] = doc
    return deleted, retained


def _decommission_armed_flat(flat: dict) -> bool:
    """Same parsing _decommission_cascades uses, applied to an already
    flattened dict instead of re-fetching from Bitbucket."""
    return str(flat.get("appspace.decommission", "")).lower() == "true"


# ── teardown flags the platform never reads (COPS-2707) ───────────────────
#
# `appspace.decommission`, `appspace.decommissionPurgeData`, and the VM tree's
# `allowDeletion` / `confirmProdDeletion` are the keys whose only job is to
# authorise destruction. Misspell one, put one at a depth the chart never
# reads, or get its casing wrong, and neither the chart nor the ApplicationSet
# templatePatch reads it: the environment renders byte-identically, every
# panel in this service stays quiet, and the verdict is "Routine -- nothing
# dangerous detected".
#
# acme-config-prod #4376 merged `appspace.decomission: true` -- one `m` --
# with exactly that green comment. The folder-removal PR that followed was
# correctly blocked for having no cascade armed, but nothing on either PR
# could tell the operator WHY the flag they had just merged did not count.
_TEARDOWN_FLAG_MAX_EDITS = 2

_APPSPACE_PREFIX = "appspace."
_CASCADE_FLAG_LEAVES = ("decommission", "decommissionPurgeData")
# VM arming keys hang off a role (allowDeletion) or only off defaults
# (confirmProdDeletion on Azure stage/prod). Parent varies by segment.
_VM_ARMING_PREFIX = "appspace.infra.deployLinuxServicesK8s."
_VM_ARMING_LEAVES = ("allowDeletion", "confirmProdDeletion")
_VM_ROLE_NAMES = ("defaults", "svc", "mongo", "rabbit")


def _flag_edit_distance(a: str, b: str) -> int:
    """Levenshtein distance between two flag names, two rows at a time.

    The inputs are 10 to 25 characters and get compared a handful of times
    per PR, so the plain form is both fast enough and easier to audit than
    pulling in a dependency for it.
    """
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _squash_flag(name: str) -> str:
    """Lowercase, letters and digits only -- what two keys look like to a
    reader who is not counting characters."""
    return "".join(c for c in (name or "") if c.isalnum()).lower()


def _reads_as_flag(leaf: str, canonical: str) -> bool:
    """True when a reviewer reads `leaf` as `canonical` but Helm will not.

    Two independent signals, because operators make two different kinds of
    mistake. Casing and separators (`decommissionpurgedata`,
    `decommission_purge_data`) are exact matches to the eye and invisible to
    a YAML key lookup. A dropped or doubled letter (`decomission`,
    `decommisson`) is what actually gets typed. An exact match is not a typo
    and returns False, so no caller has to special-case the correct spelling.
    """
    if leaf == canonical:
        return False
    if _squash_flag(leaf) == _squash_flag(canonical):
        return True
    return (_flag_edit_distance(leaf.lower(), canonical.lower())
            <= _TEARDOWN_FLAG_MAX_EDITS)


def _canonical_vm_arming_path(leaf: str) -> str:
    """Where the runbook and chart read VM arming flags (defaults block)."""
    return f"{_VM_ARMING_PREFIX}defaults.{leaf}"


def _vm_arming_key_is_chart_readable(key: str) -> bool:
    """True when supporting-services reads this exact flat key for arming."""
    if not key.startswith(_VM_ARMING_PREFIX):
        return False
    rest = key[len(_VM_ARMING_PREFIX):]
    if rest.count(".") != 1:
        return False
    role, leaf = rest.split(".")
    if role not in _VM_ROLE_NAMES or leaf not in _VM_ARMING_LEAVES:
        return False
    if leaf == "confirmProdDeletion" and role != "defaults":
        return False
    return True


def _canonical_teardown_flag(key: str):
    """The teardown flag a flat key is a near-miss of, as
    (canonical_leaf, parent_prefix), or (None, None).

    Depth is part of the match. `appspace.decomission` is the typo;
    `appspace.something.decomission` is a key inside another map that happens
    to share a name, and guessing at it would put noise into a panel whose
    entire value is that it only speaks when something is wrong.
    """
    if key.startswith(_VM_ARMING_PREFIX):
        rest = key[len(_VM_ARMING_PREFIX):]
        if rest.count(".") == 1:
            role, leaf = rest.split(".")
            for canonical_leaf in _VM_ARMING_LEAVES:
                if not _reads_as_flag(leaf, canonical_leaf):
                    continue
                if canonical_leaf == "confirmProdDeletion":
                    return canonical_leaf, f"{_VM_ARMING_PREFIX}defaults."
                return canonical_leaf, f"{_VM_ARMING_PREFIX}{role}."
        return None, None
    if not key.startswith(_APPSPACE_PREFIX):
        return None, None
    leaf = key[len(_APPSPACE_PREFIX):]
    if "." in leaf:
        return None, None
    best, best_distance = None, None
    for canonical in _CASCADE_FLAG_LEAVES:
        if not _reads_as_flag(leaf, canonical):
            continue
        distance = _flag_edit_distance(leaf.lower(), canonical.lower())
        if best_distance is None or distance < best_distance:
            best, best_distance = canonical, distance
    if best is None:
        return None, None
    return best, _APPSPACE_PREFIX


def _flag_is_true(flat: dict, key: str) -> bool:
    return str((flat or {}).get(key, "")).strip().lower() == "true"


def _misplaced_vm_arming_flags(flat: dict, previous: dict = None) -> list:
    """VM arming keys spelled correctly but at a depth the chart never reads.

    Role-level `allowDeletion` (e.g. under `svc`) is valid and omitted here.
    `confirmProdDeletion` is only read from `defaults` on Azure stage/prod.
    """
    found = []
    for key in (flat or {}):
        if not _flag_is_true(flat, key):
            continue
        leaf = key.rsplit(".", 1)[-1]
        if leaf not in _VM_ARMING_LEAVES:
            continue
        if _vm_arming_key_is_chart_readable(key):
            continue
        canonical = _canonical_vm_arming_path(leaf)
        if _flag_is_true(flat, canonical):
            continue
        if previous is not None and _flag_is_true(previous, key):
            continue
        found.append({"found": key, "canonical": canonical})
    return found


def _teardown_flag_typos(flat: dict, previous: dict = None) -> list:
    """Keys that read as a teardown flag, are set to `true`, and do nothing.

    Returns `[{"found": flat key, "canonical": flat key that works}]`,
    sorted, so a caller can render a stable two-column table.

    Only `true` is reported: that is the value which creates a false belief
    of arming, and skipping the rest keeps a leftover `decomission: false`
    out of the comment. A typo standing next to a correctly spelled flag
    that IS armed is skipped too -- the cascade is armed either way, so
    nothing the operator intended failed to happen.

    With `previous` given, the answer is limited to flags this diff turned
    on. That is what the PR-review panels want: every other branch in
    `_summarize_appspace_state_changes` is a transition, and firing on a
    pre-existing key would block unrelated PRs that merely touch the file.
    Pass `previous=None` to ask the state question instead -- is this
    environment carrying an inert flag right now -- which is what the
    folder-removal panel needs to explain why the cascade is not armed.
    """
    found = []
    for key in (flat or {}):
        if not _flag_is_true(flat, key):
            continue
        canonical_leaf, parent = _canonical_teardown_flag(key)
        if canonical_leaf is None:
            continue
        canonical = parent + canonical_leaf
        if _flag_is_true(flat, canonical):
            continue
        if previous is not None and _flag_is_true(previous, key):
            continue
        found.append({"found": key, "canonical": canonical})
    for item in _misplaced_vm_arming_flags(flat, previous=previous):
        if any(d["found"] == item["found"] for d in found):
            continue
        found.append(item)
    return sorted(found, key=lambda d: d["found"])


def _teardown_flag_typo_table(typos: list,
                              found_label: str = "You wrote") -> list:
    """Two columns and nothing else: what is written, and what works.

    A reviewer comparing `decomission` with `decommission` in running prose
    has to count letters. Side by side in a table they do not.
    """
    rows = [f"| {found_label} | The key the platform reads |", "|---|---|"]
    for typo in typos:
        rows.append(f"| `{typo['found']}` | `{typo['canonical']}` |")
    return rows


# Shared by the panel writer and the two readers below, so none of them can
# drift into looking for a heading the others stopped emitting.
_FLAG_TYPO_PANEL_HDR_PREFIX = "## \u26d4 STOP \u2014 teardown flag misspelled in "


def _teardown_flag_typo_panels(lines) -> list:
    """Only the STOP panels out of a rendered appspace-state block.

    A misspelled teardown flag is a broken PR rather than a change to
    review, so the comment surface renders this and stops. Pulling the panel
    back out of the assembled block, rather than having the producer hand it
    over separately, keeps one code path building it: two producers for the
    same panel is how the summary and the body drift apart (COPS-2668).

    A PR can touch several environments, so every STOP panel is collected.
    """
    out, keeping = [], False
    for line in lines or []:
        if line.startswith(_FLAG_TYPO_PANEL_HDR_PREFIX):
            keeping = True
        elif keeping and (line.startswith("## ") or line.startswith("### ")):
            keeping = False
        if keeping:
            out.append(line)
    return out


_TYPO_ROW_RE = re.compile(r"^\| `([^`]+)` \| `([^`]+)` \|$")


def _teardown_flag_typo_pairs(lines) -> list:
    """Read back the (wrong, right) pairs `_teardown_flag_typo_table` wrote.

    Reader and writer sit together on purpose. The Bitbucket build-status
    description has to name the key and its fix -- it is the whole message
    for anyone reading the checks list instead of the comment -- and by the
    time the status is posted the rendered panel is the only place that
    pairing still exists. Adjacency is what stops the format drifting away
    from the parse.
    """
    pairs = []
    for line in lines or []:
        match = _TYPO_ROW_RE.match(line.strip())
        if match:
            pairs.append((match.group(1), match.group(2)))
    return pairs


def _is_public_cloud_env(identity_file: str, env_name: str = "") -> bool:
    """True for public-cloud / cl-* environments (COPS-2700 / COPS-2701).

    The private-cloud decommission gate (COPS-2539) was deliberately never
    ported to the cl-* ApplicationSets. On those units
    `preserveResourcesOnDeletion: true` is set and no cascade finalizer is
    ever templated, so `appspace.decommission: true` is a silent no-op.
    Detect by path (`/public-cloud/`) or by the `cl-` environment prefix.
    """
    path = (identity_file or "").replace("\\", "/")
    if "/public-cloud/" in f"/{path.strip('/')}/":
        return True
    return (env_name or "").startswith("cl-")


def _public_cloud_env_name(identity_file: str, fallback: str = "") -> str:
    """The constellation a public-cloud identity file belongs to.

    Everywhere on private cloud the environment is the basename of the
    identity file's parent directory (`.../pv-qa-15-a/customer.yaml`).
    Public cloud nests a block under the constellation
    (`.../cl-dev11-a/constellation/customer.yaml`), so that same rule yields
    `constellation`, `api`, `user-content` or `app7` -- none of which name
    anything an operator can act on, with thirteen constellations in the
    fleet (COPS-2708).

    The `cl-` path segment is the answer and is unambiguous: it is the only
    segment that can carry that prefix. Falls back to whatever the caller
    already had when there is no such segment, so a layout this does not
    recognise degrades to today's behaviour rather than to an empty name.
    """
    for segment in (identity_file or "").replace("\\", "/").split("/"):
        if segment.startswith("cl-"):
            return segment
    return fallback


# The block that owns a constellation's shared workloads. Every other block
# under a `cl-*` directory (`api`, `cloud`, `user-content`, `app1`..`app16`)
# is a load-balancer instance in front of those same workloads.
_PUBLIC_CLOUD_SHARED_BLOCK = "constellation"


def _public_cloud_teardown_phase_table(block: str = None) -> list:
    """Checklist for a cl-* folder removal. Not the pv-* Phase 1/2/3 table.

    COPS-2701: the private-cloud table tells reviewers to arm
    `appspace.decommission` and claims Phase 3 deletes managed resources.
    Neither is true on public cloud. This table is the manual procedure
    already documented in the config-repo READMEs and COPS-2700.

    COPS-2708: step 3 depends on WHICH block is going. A constellation's
    namespace holds the `-ms` and `-ss` workloads that every customer in it
    is served from, so `kubectl delete namespace` is the right instruction
    when the `constellation` block goes and a catastrophic one when a single
    load-balancer block does -- removing `app1` must not take `app2` and
    every other customer down with it. `block=None` keeps the original
    generic wording for callers that do not know.
    """
    if block and block != _PUBLIC_CLOUD_SHARED_BLOCK:
        namespace_step = (
            "| **3 — leave the namespace alone** | \u26d4 do NOT delete | "
            f"only the `{block}` load balancer is going; the namespace holds "
            "the shared workloads every other customer in this constellation "
            "is served from |")
        verify_step = (
            "| **5 — verify** | \u2b1c operator | "
            f"nothing named after `{block}` survives in the GCP project, and "
            "the sibling blocks still serve |")
    else:
        namespace_step = (
            "| **3 — delete the namespace** | \u2b1c operator | "
            "`kubectl delete namespace <env>` removes the workloads and "
            "namespaced KCC CRs — this is every customer in the "
            "constellation, so confirm the whole constellation is going |")
        verify_step = (
            "| **5 — verify** | \u2b1c operator | "
            "nothing named after the environment survives in the GCP project |")
    return [
        "| Step | State | What it does |",
        "|------|-------|--------------|",
        "| **1 — confirm no shared user content** | \u2b1c operator | "
        "bucket/DNS are keyed on `buckets.userContent.suffix`, not "
        "`appspace.suffix`; a surviving sibling can share them |",
        "| **2 — remove folder** | " + _PH_THIS_PR + " | "
        "deletes the Argo CD Applications only; workloads and KCC "
        "objects keep running unmanaged (`preserveResourcesOnDeletion`) |",
        namespace_step,
        "| **4 — clean abandoned GCP objects** | \u2b1c operator | "
        "KCC `deletion-policy: abandon` leaves URL maps, forwarding "
        "rules, backend services, target HTTPS proxies, health checks, "
        "buckets, DNSRecordSets and any GCE VMs in the project |",
        verify_step,
    ]


_PH_THIS_PR = "\u2705 **this PR**"
_PH_PENDING = "\u2b1c pending"
_PH_NA = "\u2014 not applicable"
# COPS-2710: a phase being taken back. Rendering the row as plain pending
# again would be true and useless -- it hides that this diff is what undid
# it, which is the one thing the reviewer of a rollback needs to see.
_PH_UNDONE = "\u21a9\ufe0f **undone by this PR**"
# COPS-2660: the arming flag is present but the same PR removed the VM config
# it acts through, so the phase can never complete as written.
_PH_BROKEN = "\u26d4 **broken by this PR**"
# COPS-2669: _PH_DONE lived alone in diff_preview.py while its four
# siblings lived here, so the module that RENDERS the phase table did not
# own its own vocabulary and a caller had to import the states from two
# places. The hub re-exports it, so nothing outside changes.
_PH_DONE = "\u2705 **done**"


def _decommission_phase_table(vm_state, cascade_state, removal_state,
                              declares_vms: bool, purge: bool = False,
                              purge_this_pr: bool = False) -> list:
    """The one phase table, rendered identically by all three decommission
    panels. Each *_state is one of the _PH_* constants, or None to fall back
    to pending.

    Callers pass the states they already have in scope; nothing is fetched
    here. Phase 1 is always rendered so the model stays complete, but reads
    "not applicable" when the environment declares no VMs -- delete.md says
    to skip the step, not to hide that the step exists, and a table that
    silently started at Phase 2 would just make the reader wonder what they
    were missing.
    """
    # COPS-2669: purge_this_pr exists because arming the purge is not one of
    # the three phases -- it is a qualifier on Phase 2, which is where this
    # note already renders. The purge panel used to claim
    # cascade_state=_PH_THIS_PR, telling the reviewer this PR armed a cascade
    # an earlier PR had armed; reporting the phase honestly then left no row
    # marked "this PR" at all, in a table whose job is to locate the reader.
    # Marking the qualifier on its own row costs neither.
    if purge and purge_this_pr:
        purge_note = (" \u2014 \u2b05 **this PR arms `decommissionPurgeData`**, "
                      "so the cascade will **permanently destroy** the BigQuery "
                      "dataset and the user content bucket (soft-delete off on "
                      "content; backup bucket always abandoned), not just "
                      "abandon them")
    elif purge:
        purge_note = (" \u2014 with `decommissionPurgeData` armed the cascade "
                      "will **permanently destroy** the BigQuery dataset and "
                      "the user content bucket (soft-delete off on content; "
                      "backup bucket always abandoned), not just abandon them")
    else:
        purge_note = (" \u2014 `decommissionPurgeData` is not armed, so the "
                      "BigQuery dataset and the content bucket are abandoned "
                      "and stay recoverable (backup bucket is always abandoned)")
    # The pointer to the inventory only makes sense while Phase 3 is still
    # ahead of the reviewer; on the removal PR itself they are already
    # looking at that inventory.
    removal_note = ("deletes the Applications and every resource they manage, "
                    "Config Connector cloud resources included \u2014 the "
                    "destructive step, and this PR is it"
                    if removal_state == _PH_THIS_PR else
                    "a later PR deletes the Applications and every resource "
                    "they manage, Config Connector cloud resources included; "
                    "that PR gets its own full inventory panel")
    rows = ["| Phase | State | What it does |",
            "|-------|-------|--------------|"]
    if declares_vms:
        vm_note = ("`deployLinuxServicesK8s.defaults.allowDeletion` lets the "
                   "cascade delete the real VM, its data disk and its reserved "
                   "IP; without it they survive under the abandon policy")
        rows.append(
            f"| **Phase 1 \u2014 arm VM deletion** | {vm_state or _PH_PENDING} | "
            f"{vm_note} |")
    else:
        rows.append(
            f"| **Phase 1 \u2014 arm VM deletion** | {_PH_NA} | this environment "
            "declares no `deployLinuxServicesK8s` VMs, so there is nothing to "
            "arm |")
    rows.append(
        f"| **Phase 2 \u2014 arm cascade** | {cascade_state or _PH_PENDING} | "
        "`appspace.decommission` makes the Applications eligible for the "
        f"cascade-delete finalizer{purge_note} |")
    rows.append(
        f"| **Phase 3 \u2014 remove folder** | {removal_state or _PH_PENDING} | "
        f"{removal_note} |")
    return rows
