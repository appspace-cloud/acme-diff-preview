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


def _public_cloud_teardown_phase_table() -> list:
    """Checklist for a cl-* folder removal. Not the pv-* Phase 1/2/3 table.

    COPS-2701: the private-cloud table tells reviewers to arm
    `appspace.decommission` and claims Phase 3 deletes managed resources.
    Neither is true on public cloud. This table is the manual procedure
    already documented in the config-repo READMEs and COPS-2700.
    """
    return [
        "| Step | State | What it does |",
        "|------|-------|--------------|",
        "| **1 — confirm no shared user content** | \u2b1c operator | "
        "bucket/DNS are keyed on `buckets.userContent.suffix`, not "
        "`appspace.suffix`; a surviving sibling can share them |",
        "| **2 — remove folder** | " + _PH_THIS_PR + " | "
        "deletes the Argo CD Applications only; workloads and KCC "
        "objects keep running unmanaged (`preserveResourcesOnDeletion`) |",
        "| **3 — delete the namespace** | \u2b1c operator | "
        "`kubectl delete namespace <env>` removes workloads and namespaced "
        "KCC CRs |",
        "| **4 — clean abandoned GCP objects** | \u2b1c operator | "
        "KCC `deletion-policy: abandon` leaves URL maps, forwarding "
        "rules, backend services, target HTTPS proxies, health checks, "
        "buckets, DNSRecordSets and any GCE VMs in the project |",
        "| **5 — verify** | \u2b1c operator | "
        "nothing named after the environment survives in the GCP project |",
    ]


_PH_THIS_PR = "\u2705 **this PR**"
_PH_PENDING = "\u2b1c pending"
_PH_NA = "\u2014 not applicable"
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
