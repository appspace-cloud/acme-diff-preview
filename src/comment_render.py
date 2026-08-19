"""Comment rendering helpers (COPS-2658 phase 2).

The pieces that turn analysed results into the Markdown a reviewer reads:
the merge summary and its severity verdicts, section and environment name
formatting, repeated-section grouping, and the full-diff page link.
Extracted verbatim from diff_preview.py; no logic changed in the move.

A leaf on the hub, though it may use the other leaves. It imports nothing
from the service and must stay that way.
"""
import re

import diff_ui
from manifest import _is_kcc_blocking_artifact
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


def _is_version_downgrade(current: str, new: str) -> bool:
    """True when `new` is a strictly LOWER chart version than `current`.

    v2.5.8: downgrades are legal but dangerous (schema regressions, data
    migrations that do not run backwards), so the PR comment must shout.
    Unparseable versions return False — never block on noise."""
    cur_t = _parse_version_tuple(current)
    new_t = _parse_version_tuple(new)
    if cur_t is None or new_t is None:
        return False
    # Pad to equal length so 2602.4 vs 2602.4.1 compares sanely.
    length = max(len(cur_t), len(new_t))
    cur_t += (0,) * (length - len(cur_t))
    new_t += (0,) * (length - len(new_t))
    return new_t < cur_t


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

# COPS-2668: and a third state. The summary used to know only purge-vs-not,
# so an environment with NO cascade armed — where the Applications go and
# every workload keeps running, orphaned — was announced as "resources are
# deleted", the exact opposite of the panel directly beneath it. Found by
# reading the rendered orphan comment while preparing its golden.
_DECOM_ORPHAN_HDR = ("**The ArgoCD Application is removed, but its resources "
                     "are NOT deleted")


_SEV_ROUTINE, _SEV_REVIEW, _SEV_BLOCK = 0, 1, 2
_VERDICTS = {
    _SEV_BLOCK: "\u26d4 **DO NOT MERGE** without checking the item(s) below",
    _SEV_REVIEW: "\u26a0\ufe0f **Review before merging**",
    _SEV_ROUTINE: "\u2705 **Routine** \u2014 nothing dangerous detected",
}


def _fmt_env_list(apps, shown=8) -> str:
    """Environment names, the way operators say them (no -ms/-ss/-glb)."""
    envs = sorted(set(_envs_from_apps(sorted(apps))))
    return _fmt_service_list(envs, shown=shown)


def _build_merge_summary(results, rollup_by_sig, vm_change_lines,
                         decommission_lines, appspace_state_lines,
                         new_env_lines, new_env_structural,
                         paused_changing=None, paused_envs=None,
                         block_headline=None) -> list:
    """The verdict block that opens every comment.

    Reads the same deterministic facts the panels below use, so the
    summary can never disagree with the detail. Text panels built
    elsewhere are recognised by their own header constants rather than
    re-derived, for the same reason.

    block_headline (COPS-2676): optional short string naming the permanent
    render failure (e.g. "Missing Image Tag on => platform"). When set, the
    cannot-render bullet leads with it so operators see *why* without
    scrolling past deletions and bump noise.
    """
    findings = []          # (severity, line)
    sev = _SEV_ROUTINE

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
        # COPS-2697: checked before purge. Both can be true at once, and when
        # they are, the fact that matters is not "this environment's data is
        # destroyed" (expected, that is what a purge is) but "a DIFFERENT,
        # surviving environment loses its bucket and DNS record". AE-15284 was
        # a Sev1 of that shape; the ordinary purge wording would have read as
        # routine.
        shared_uc = _DECOM_SHARED_UC_HDR in txt
        if shared_uc:
            _what = ("the user content bucket and DNS record are SHARED with a "
                     "surviving environment, which loses them too")
        elif purge:
            _what = ("data purge is ARMED: buckets/datasets are destroyed, "
                     "not abandoned")
        elif orphan:
            # No cascade: the Applications go, every workload keeps running.
            # Still a BLOCK \u2014 leaving a fleet of unmanaged workloads behind is
            # not a safer outcome, just a different one \u2014 but saying they are
            # "deleted" told the reviewer the opposite of what happens.
            _what = ("no cascade armed: the Applications are removed but "
                     "their workloads keep running, orphaned and unmanaged")
        else:
            _what = "resources are deleted; data is abandoned, not purged"
        findings.append((_SEV_BLOCK,
                         "\U0001f5d1\ufe0f **Environment decommission** \u2014 "
                         + _what))
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
        # binding) are not GCP destroys. Pull them out of the BLOCK
        # "resource(s) deleted" count so unmanage PRs stop looking like
        # DO NOT MERGE destroy changes (acme-config-prod #4326).
        orphan_hdrs = set()
        for r in results.values():
            for f in (getattr(r, "vm_changes", None) or []):
                if f.get("orphaned") or (
                        f.get("deleted") and not f.get("dangerous")
                        and f.get("notes")):
                    orphan_hdrs.add(f.get("header"))
        hard_n = 0
        hard_apps = []
        orphan_n = 0
        for a in deleted_apps:
            hard = [h for h in (results[a].deleted_resources or [])
                    if h not in orphan_hdrs]
            if hard:
                hard_n += len(hard)
                hard_apps.append(a)
            orphan_n += sum(
                1 for h in (results[a].deleted_resources or [])
                if h in orphan_hdrs)
        if hard_n:
            # COPS-2683: count environments to match `_fmt_env_list` (same
            # class of app-vs-env lie as COPS-2675 on the render-blocked
            # headline). Orphan/abandon wording above is unchanged.
            n_envs = len(set(_envs_from_apps(hard_apps)))
            findings.append((_SEV_BLOCK,
                             f"\u274c **{hard_n} resource(s) deleted** in "
                             f"{n_envs} environment(s): "
                             f"{_fmt_env_list(hard_apps)}"))
        if orphan_n:
            findings.append((_SEV_REVIEW,
                             f"\U0001f5a5\ufe0f **{orphan_n} KCC resource(s) "
                             f"unmanaged** (abandon / schedule attachment "
                             f"\u2014 GCP kept)"))
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
        findings.append((_SEV_REVIEW,
                         f"\u2b07\ufe0f **Chart version downgrade** {_dg} in "
                         f"{_fmt_env_list(downgraded)}"))
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
        # Arming destruction is the highest-severity thing a config-only PR
        # can do, and it is invisible in the manifest diff: the footer still
        # reads "No manifest changes". Live proof, acme-config-dev PR #7024:
        # the body shouted DECOMMISSION ARMED while this summary said
        # "Routine - nothing dangerous detected". A verdict that contradicts
        # the panel below it is worse than no verdict at all.
        if "PURGE ARMED" in txt:
            findings.append((_SEV_BLOCK,
                             "\U0001f6a8 **Data purge ARMED** \u2014 the "
                             "cascade will permanently destroy the BigQuery "
                             "dataset and the user content bucket"))
        elif "DECOMMISSION ARMED" in txt and _DECOM_VM_STRIP_HDR not in txt:
            # COPS-2660 follow-up: when the arming is broken, the BROKEN
            # finding above already states the arming and its consequence.
            # Read live on PR #7113, the summary told one event four ways;
            # the generic line adds nothing next to the specific one, so it
            # stands down and the story is told once.
            findings.append((_SEV_BLOCK,
                             "\U0001f512 **Decommission ARMED** \u2014 this "
                             "environment becomes eligible for cascade "
                             "deletion when its folder is removed"))
        elif "DISARMED" in txt.upper():
            findings.append((_SEV_ROUTINE,
                             "\U0001f513 decommission disarmed (safe "
                             "direction)"))
        if "PAUSED" in txt.upper():
            findings.append((_SEV_REVIEW,
                             "\u23f8\ufe0f **ArgoCD auto-sync paused** for an "
                             "environment \u2014 changes stop being applied"))
        elif "RESUMED" in txt.upper():
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
    if new_env_lines:
        findings.append((_SEV_REVIEW if new_env_structural else _SEV_ROUTINE,
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
    n_check = sum(1 for s, _ in findings if s >= _SEV_REVIEW)
    verdict = _VERDICTS[sev]
    if sev >= _SEV_REVIEW:
        verdict += f" ({n_check} item(s))"
    order = {_SEV_BLOCK: 0, _SEV_REVIEW: 1, _SEV_ROUTINE: 2}
    findings.sort(key=lambda f: order[f[0]])
    return ["## \u2139\ufe0f Merge summary", "", verdict, ""] + \
           [f"- {line}" for _s, line in findings] + [""]


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
