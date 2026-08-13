"""Grouping: collapse things that are the same change into one line.

Sliced out of diff_preview.py unchanged (COPS-2658 phase 3). Four groupings
that share one idea -- say a repeated thing once -- plus the helm-error
explainer they key on:

  * `_rollup_by_service`, for one change applied to many microservices
  * `_group_changed_apps_by_fingerprint`, for byte-identical diffs
  * `_group_changed_apps_by_shape`, for same-shape diffs whose values differ
  * `_group_failures`, for many environments failing the same way

`_explain_required_error` lives here because `_failure_signature` keys on the
rendered explanation rather than raw helm stderr: two environments whose
stderr differs only in a temp path are the same problem, and keying on the
raw string would split the group and hand the reader back the wall of text
this exists to remove. It moved with its caller rather than being reached
across a module boundary.

Every grouping fails open toward verbosity. `_is_risky_result` is the guard
that keeps a deletion, a zeroed replica set or a downgrade out of any group
summary at all.
"""
import re

from comment_render import _is_version_downgrade
from vocabulary import OUT_INDETERMINATE, REASON_MISSING_REQUIRED


_HELM_EXEC_ERR_RE = re.compile(
    r"execution error at \(([^)]+?):(\d+):\d+\):\s*(.+)", re.DOTALL)
_HELM_TPL_ERR_RE = re.compile(
    r"template:\s*([^\s:]+?):(\d+):\d+:\s*executing[^<]*at\s*<([^>]+)>:\s*(.+)",
    re.DOTALL)


def _explain_required_error(err: str) -> list:
    """Markdown lines spelling out a REASON_MISSING_REQUIRED render failure.

    v2.6.2 (born from acme-config-dev PR #6848): when a chart's `required`
    guard trips - or a template nil-derefs a value block that is absent -
    the developer must see, in the PR comment itself: WHAT value is missing,
    WHERE in the chart it tripped, and WHERE to add it. Before this, the
    comment showed 200 raw chars of helm stderr, and reviewers were blocked
    guessing.

    Handles the two shapes helm emits:
    - `execution error at (chart/templates/x.yaml:25:15): <required msg>`
      (the chart author's own message, usually naming the values path)
    - `template: chart/templates/x.yaml:15:124: executing "..." at
      <$thing.image.tag>: nil pointer evaluating interface {}.tag`
      (no custom message - we name the dereferenced field instead)
    """
    err = err or ""
    m1 = _HELM_EXEC_ERR_RE.search(err)
    if m1:
        tpl, line, msg = m1.group(1), m1.group(2), m1.group(3).strip()
        # keep only the chart-relative template path (drop tmp dirs)
        tpl = tpl[tpl.find("templates/"):] if "templates/" in tpl else tpl
        return [
            f"> **{msg.splitlines()[0][:300]}**",
            f"> Chart template: `{tpl}:{line}`",
        ]
    m2 = _HELM_TPL_ERR_RE.search(err)
    if m2:
        tpl, line, expr, msg = (m2.group(1), m2.group(2),
                                m2.group(3).strip(), m2.group(4).strip())
        tpl = tpl[tpl.find("templates/"):] if "templates/" in tpl else tpl
        # COPS-2548 (live feedback on PR 3813): leading with the chart's own
        # loop variable told the operator nothing about their config. When the
        # chart is iterating microservice definitions, the cause is concrete
        # and worth naming: an entry exists under microservices.definitions
        # with no image mapping behind it. Everything else keeps the generic
        # wording, since we cannot know which values block a foreign chart
        # meant to read.
        field = expr.split(".", 1)[1] if "." in expr else expr
        if "microservice" in expr.lower():
            head = (f"> **A `microservices.definitions` entry has no `image` "
                    f"mapping**, so the chart could not read its `{field}`.")
        else:
            head = (f"> **The chart reads `{expr}` but that value block is "
                    f"missing or empty.**")
        return [
            head,
            f"> Chart template: `{tpl}:{line}` ({msg.splitlines()[0][:120]})",
        ]
    return [f"> {err.splitlines()[0][:300] if err else 'no error output'}"]


_MICROSERVICE_KEY_RE = re.compile(
    r"^appspace\.microservices\.definitions\.([^.]+)\.(.+)$")
INPUT_ROLLUP_MIN_SERVICES = 3  # collapse only when it actually saves space


def _service_and_rest(key: str):
    """Split 'appspace.microservices.definitions.<service>.<rest>' into
    (service, rest). Returns (None, key) for any key outside that shape,
    so the caller leaves it un-rolled-up."""
    m = _MICROSERVICE_KEY_RE.match(key)
    return (m.group(1), m.group(2)) if m else (None, key)


def _rollup_by_service(keys: list, sig_fn, render_group, render_single) -> list:
    """Group full dotted keys sharing the
    appspace.microservices.definitions.<service>.<rest> shape by (rest,
    sig_fn(key)), collapsing any group of INPUT_ROLLUP_MIN_SERVICES or
    more services into ONE line via render_group(rest, sig, services).
    Smaller groups and any key outside that shape render individually via
    render_single(key) -- the exact same per-key line as before this
    existed, so a typical small PR (one or two services touched) is
    byte-for-byte unchanged.

    v2.7.0, born from acme-config-prod PR #3837: removing the Spot
    compute-class override from 67 services rendered as 67 near-identical
    bullets, capped at 25 lines with "+110 more" and no way for a reviewer
    to tell it was one change applied 67 times.
    """
    buckets, order, singles = {}, [], []
    for k in keys:
        service, rest = _service_and_rest(k)
        if service is None:
            singles.append(k)
            continue
        bucket_key = (rest, sig_fn(k))
        if bucket_key not in buckets:
            buckets[bucket_key] = []
            order.append(bucket_key)
        buckets[bucket_key].append(service)

    lines = []
    for rest, sig in order:
        services = sorted(buckets[(rest, sig)])
        if len(services) >= INPUT_ROLLUP_MIN_SERVICES:
            lines.append(render_group(rest, sig, services))
        else:
            for s in services:
                lines.append(render_single(
                    f"appspace.microservices.definitions.{s}.{rest}"))
    for k in singles:
        lines.append(render_single(k))
    return lines


def _group_changed_apps_by_fingerprint(changed_apps: list) -> list:
    """Group (app, DiffResult) pairs whose full diff is byte-for-byte
    identical (same fingerprint) into one entry each, so format_comment can
    show a single full representative diff per group instead of one per
    app (COPS-2579 item 2 -- the fix for acme-config-prod PR #3837, where
    248 apps sharing the exact same 67-resource change showed as 6
    arbitrary, truncated, mutually-duplicate diffs).

    Apps with no fingerprint (legacy/coerced DiffResult, e.g. from
    _result()'s 3-tuple path, or hand-built in a test) always form their
    own singleton group -- they never merge with anything, since None is
    never equal to another None group's identity here.

    Returns [(representative_app, member_apps, representative_result), ...]
    sorted by representative_app name, with member_apps sorted too, so the
    grouping is fully deterministic regardless of the input order
    (changed_apps arrives in worker-completion order).
    """
    buckets, order = {}, []
    for app, r in changed_apps:
        key = r.fingerprint if r.fingerprint else ("__no_fingerprint__", app)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append((app, r))
    groups = []
    for key in order:
        members = sorted(buckets[key], key=lambda x: x[0])
        rep_app, rep_r = members[0]
        groups.append((rep_app, [a for a, _ in members], rep_r))
    groups.sort(key=lambda g: g[0])
    return groups




def _is_risky_result(r) -> bool:
    """Facts that mean an app must never be folded into a group summary.

    Named once and used everywhere (COPS-2629). The same expression was
    inline in format_comment's budget check; two copies of a safety
    predicate drift, and the copy that drifts is the one that stops
    protecting anything.
    """
    return bool(r.deleted_resources or r.replicas_zeroed
                or getattr(r, "template_artifacts", None)
                or getattr(r, "vm_changes", None)
                or (r.version_change
                    and _is_version_downgrade(*r.version_change)))


def _shape_signature(r) -> tuple:
    """What makes two changes the SAME SHAPE (COPS-2629).

    The exact set of changed resource headers, plus the real resource
    count. Deliberately NOT the count alone: nine resources and nine
    resources are one change only if they are the same nine, and grouping
    on the count would let a genuinely different change hide inside a line
    that claims to describe it.

    Values are excluded on purpose, which is what makes this useful. On
    acme-config-prod PR #4026, 22 `-glb` applications changed the same 9
    resources with per-customer names inside, so fingerprint grouping
    (COPS-2579) saw 22 distinct diffs and rendered 44 lines. The shape is
    identical even though no two diffs are; the values live on the page.
    Sections are (header, body) pairs, read the same way
    _format_app_diff_block reads them. A dict lookup here would have been a
    second, private idea of what a section is, and it raised on every real
    diff the moment the full suite ran.
    """
    hdrs = []
    for sec in (r.sections or []):
        try:
            hdrs.append(sec[0])
        except (TypeError, KeyError, IndexError):
            # Unknown section shape. Return None rather than a shared
            # placeholder: a placeholder would make every unreadable app
            # match every other one and group changes that were never
            # compared. Callers treat None as "never group".
            return None
    return (r.n_res, tuple(hdrs))


def _group_changed_apps_by_shape(changed_apps: list, skip=()) -> dict:
    """app -> (representative_app, members) for same-shape changes.

    Takes the same (app, DiffResult) pair list as
    _group_changed_apps_by_fingerprint, so the two grouping passes read
    from one structure rather than two views that can disagree.

    Risky apps are excluded outright rather than grouped and annotated: a
    deletion block exists so that someone reads it for that environment,
    and folding it into "and 21 others" is the outcome this whole service
    is built to prevent.

    Apps in `skip` are left alone. Those are already handled by the
    byte-identical grouping (COPS-2579), which names its own members; two
    mechanisms claiming the same app would render it twice or not at all.
    """
    buckets = {}
    for app, r in changed_apps:
        if app in skip or _is_risky_result(r):
            continue
        sig = _shape_signature(r)
        if sig is None:
            continue
        buckets.setdefault(sig, []).append(app)
    out = {}
    for members in buckets.values():
        # INPUT_ROLLUP_MIN_SERVICES, not 2. COPS-2605 already settled this
        # for the routine-bump rollup: below three, collapsing costs a
        # reader their per-app detail without saving them anything, because
        # a two-app comment was never the problem. Reusing that constant
        # rather than inventing a second threshold keeps the two rollups
        # from disagreeing about when a comment is "big".
        if len(members) < INPUT_ROLLUP_MIN_SERVICES:
            continue
        members.sort()
        for app in members:
            out[app] = (members[0], members)
    return out


def _failure_signature(r) -> tuple:
    """What makes two failures THE SAME problem (COPS-2629).

    Measured on acme-config-prod PR #4026: 22 environments failed with a
    byte-identical MISSING REQUIRED VALUE block, and the comment printed
    the same three lines of remediation advice 22 times. That is one
    problem with one fix, reported as if it were 22.

    The signature is the rendered explanation, not the raw stderr. Two
    environments whose helm output differs only in a temp path or trailing
    whitespace produce the same explanation and are the same problem; if
    the raw string were the key, that noise would split the group and hand
    the operator back the wall of text this exists to remove.

    Keyed on reason as well, so a missing value and a schema violation
    never merge even in the unlikely event their prose collides.
    """
    return (r.reason, tuple(_explain_required_error(r.error))
            if r.reason == REASON_MISSING_REQUIRED
            else (r.error or "").strip()[:400])


def _group_failures(results, reasons) -> dict:
    """app -> (representative_app, members) for grouped failure reasons.

    Mirrors diff_group_for_app (COPS-2579) for the surface it left out.
    Apps not in a multi-member group are absent, so callers render them
    exactly as before: one app is not a group, and the single-app wording
    is what every existing golden asserts.
    """
    buckets = {}
    for app, r in results.items():
        if r.outcome != OUT_INDETERMINATE or r.reason not in reasons:
            continue
        buckets.setdefault(_failure_signature(r), []).append(app)
    out = {}
    for members in buckets.values():
        if len(members) < 2:
            continue
        members.sort()
        for app in members:
            out[app] = (members[0], members)
    return out
