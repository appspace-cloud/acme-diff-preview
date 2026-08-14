"""Turning a render failure into something a reviewer can act on.

Sliced out of diff_preview.py unchanged (COPS-2658 phase 6).

Helm tells you a schema was violated or a value was missing in the least
actionable way it can: a wall of stderr, often several hundred lines, with
the one fact that matters buried in it. These functions answer the three
questions a reviewer actually has -- what failed, where, and what to add --
and cap the output so a failure never crowds out the rest of the comment.

`_render_reason` maps a raw helm error onto the outcome vocabulary, which is
why this module imports from `vocabulary` and nothing else in the repo.
"""
import re

from vocabulary import (
    REASON_INVALID_YAML,
    REASON_MISSING_REQUIRED,
    REASON_RENDER,
    REASON_SCHEMA_INVALID,
    REASON_TEMPLATE,
)


# COPS-2564: a flat 400-char cap used to be applied to helm stderr right here,
# before anything parsed it. That is fine for a one-line render error, but a
# schema failure is a LIST: acme-config-prod PR 3837 produced 53 violations and
# the comment showed four and a half of them, cut mid path
# ("definitions/a"), so the reader could not tell which services were broken.
# Keep the violation lines whole (they are short, one per line, and the whole
# point of the message) and bound everything else as before.
_HELM_ERROR_MAX = 400
_SCHEMA_ERROR_MAX_LINES = 80


def _cap_helm_error(err: str) -> str:
    """Bound a helm failure for storage, without cutting a violation list."""
    err = err or ""
    if "- at '" not in err:
        return err[:_HELM_ERROR_MAX]
    lines = err.splitlines()
    kept = lines[:_SCHEMA_ERROR_MAX_LINES]
    if len(lines) > _SCHEMA_ERROR_MAX_LINES:
        kept.append(f"- ... and {len(lines) - _SCHEMA_ERROR_MAX_LINES} more lines")
    return "\n".join(kept)


def _render_reason(render_err: str) -> str:
    """Classify a helm render error into a REASON_* code (FIX F, v2.4.9).

    A `helm template` failure caused by a value file that is not parseable
    YAML gets its own reason so the PR comment can tell the author to fix the
    YAML syntax, instead of the generic "helm template failed to render the
    chart with these values" which points them at chart values by mistake.
    Everything else stays REASON_RENDER.
    """
    e = (render_err or "").lower()
    # COPS-2554: match Helm's own message-independent SIGNATURE for a
    # template `required()` failure ("execution error at (<template>:<line>):
    # <message>") instead of guessing keywords from <message>, which chart
    # authors write however they like. Live PR 3823 broke on a chart's own
    # custom message ("Missing Image Tag on => platform") that contained none
    # of the previously-matched phrases and fell through to generic
    # REASON_RENDER: retried 5 times for a failure that can never resolve on
    # retry, then showed only "diff unavailable" with the real cause hidden.
    # Auditing this chart's own templates turned up more messages the old
    # keyword list silently missed the same way ("Cloud instance not found
    # for this deployment", "A valid appspace.prefix entry required!"). The
    # nil-pointer shape (accessing a field with no required() guard at all)
    # keeps its own separate check since it has a different Go-level phrase.
    if "execution error at (" in e or "nil pointer evaluating" in e:
        return REASON_MISSING_REQUIRED
    # COPS-2554: values.schema.json validation. Same class of bug -- this
    # chart ships a schema, and a violation is exactly as deterministic and
    # actionable as a missing required() value, but was not classified at
    # all before, so it too fell into the generic bucket.
    if "values don't meet the specifications of the schema" in e:
        return REASON_SCHEMA_INVALID
    if ("error converting yaml" in e or "did not find expected" in e
            or "could not find expected" in e or "mapping values are not allowed" in e
            or "yaml: line" in e or "found character that cannot start" in e
            or "yaml:" in e and "unmarshal" in e):
        return REASON_INVALID_YAML
    # COPS-2661: Go's text/template failing on the values it was given --
    # "template: <path>:<line>:<col>: executing ...". Same message-independent
    # signature philosophy as the required() match above, and checked AFTER
    # it so required/nil-pointer keep their dedicated clarity path (their
    # stderr carries this phrasing too on some helm versions). `helm
    # template` is a pure local computation, so this shape can never succeed
    # on retry: acme-config-prod #4244 (rabbit `instances` as a string where
    # the chart ranges over a list) was retried with backoff five times to
    # arrive at the same one-line generic hint.
    if "template:" in e and "executing" in e:
        return REASON_TEMPLATE
    return REASON_RENDER


def _explain_schema_error(err: str) -> list:
    """Break a Helm values.schema.json failure into one violation per line.

    COPS-2554: Helm reports every schema violation in one multi-line stderr
    block. Left as raw text (or worse, collapsed to the generic "diff
    unavailable" message) an operator has to find and parse it themselves.
    Each "- ..." line IS already the specific, actionable violation, so this
    only needs to extract and re-list them, same "one thing per line"
    principle as the required-value remedies.
    """
    lines = [l.strip() for l in (err or "").splitlines()]
    violations = [l[1:].strip() for l in lines if l.startswith("-")]
    if not violations:
        return [f"> {(err or 'no error output').splitlines()[0][:300]}"]
    # COPS-2564: cap by COUNT, never by characters. PR 3837 hit 53 violations
    # and a character cap cut the last one mid path, which reads like a
    # rendering bug and hides how many were left. Ten is enough to see the
    # pattern; the remainder is stated so nobody assumes the list is complete.
    out = [f"> {v}" for v in violations[:_SCHEMA_VIOLATIONS_SHOWN]]
    extra = len(violations) - _SCHEMA_VIOLATIONS_SHOWN
    if extra > 0:
        out.append(f"> *... and {extra} more violation(s) of the same kind*")
    return out


_SCHEMA_VIOLATIONS_SHOWN = 10
_NULL_VIOLATION_RE = re.compile(r"at '([^']+)': got null, want (\w+)")


def _schema_fix_hints(err: str) -> list:
    """Extra, cause-specific advice under a schema failure.

    Generic advice ("correct each value listed above") is useless for the one
    cause we keep hitting: a key whose entire body was removed or commented
    out is read by YAML as null, and the schema then rejects it. That is what
    broke acme-config-prod PR 3837 (53 services) and, in a different file,
    COPR-31637. The fix is an explicit empty map -- and under
    microservices.definitions, deleting the key instead is actively dangerous,
    because deployment/vpa/pdb/iamPolicyMember all range over that map, so a
    missing key deletes the microservice from the environment.
    """
    nulls = _NULL_VIOLATION_RE.findall(err or "")
    if not nulls:
        return []
    hints = [
        f"> **Why:** {len(nulls)} of these are `null`, which is what YAML "
        f"gives a key whose body was deleted or commented out.",
        "> **Fix:** write an explicit empty map to keep the entry with pure "
        "chart defaults, for example `myservice: {}`.",
    ]
    if any("/microservices/definitions/" in p for p, _ in nulls):
        hints.append(
            "> \u26a0\ufe0f Do **not** delete the key instead: the chart "
            "renders one microservice per entry under "
            "`microservices.definitions`, so removing it deletes that "
            "microservice from the environment.")
    return hints


_STDERR_QUOTE_LINES = 6


def _quote_helm_error(err: str) -> list:
    """The captured stderr, quoted for the comment.

    COPS-2661: `DiffResult.error` already held the actionable text for every
    render failure -- `_helm_template` captures and caps it -- and the
    comment path for the generic bucket threw it away, printing only the
    one-line hint. Whatever else the comment says, the words helm actually
    produced are the one thing the author can act on, so they are quoted
    verbatim (backticks swapped so a stray one cannot break the span).
    """
    lines = [l.rstrip() for l in (err or "").splitlines() if l.strip()]
    if not lines:
        return []
    out = [f"> `{l[:300].replace(chr(96), chr(39))}`"
           for l in lines[:_STDERR_QUOTE_LINES]]
    extra = len(lines) - _STDERR_QUOTE_LINES
    if extra > 0:
        out.append(f"> *... and {extra} more line(s) — full stderr in the "
                   f"pod logs*")
    return out


def _missing_value_remedies() -> list:
    """The remedies for a MISSING REQUIRED VALUE block, one per line.

    COPS-2548: these used to be a single long sentence that crammed two
    unrelated pieces of advice together ("define it in customer.yaml or a
    parent config.yaml" and "if the chart version changed..."), which the
    renderer showed as one wall of text. An operator had to untangle it to
    work out what to actually do. Separate lines, most likely cause first.
    """
    return [
        "> **Fix:** add the missing value to this environment's "
        "`customer.yaml`, or to the `config.yaml` of its cohort or ring if "
        "every environment at that level needs it.",
        "> If this PR moved the environment to a new folder, check that the "
        "new parent `config.yaml` carries what the old one did.",
        "> If this PR changed the chart version, the new chart may require "
        "values the old one did not.",
    ]
