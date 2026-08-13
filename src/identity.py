"""Environment identity: is this the same environment under a new name.

Sliced out of diff_preview.py unchanged (COPS-2658 phase 5).

A rename and a deletion look identical in a diff -- resources disappear from
one path and appear at another -- and calling a rename a deletion is the
single most alarming thing this service could get wrong. `_is_rename_of` and
`_same_env_identity` exist to tell them apart, and they are deliberately
conservative: anything ambiguous stays a deletion, because an over-reported
deletion costs a reviewer a second look while an under-reported one costs an
environment.

`_check_customer_name` and `_extract_appspace_identity` supply the identity
facts those comparisons read.

`_section_kind` comes from manifest.py, which is where the header format it
decodes is built (`_diff_resources`). Phase 4's closure had parked it in
vm_analysis because the workload detectors happened to read it first.
"""
import re

from comment_render import _section_name
from manifest import _section_kind


_appspace_key_re      = re.compile(r"^\s*appspace:\s*(#.*)?$")


_customer_name_key_re = re.compile(r"^\s*customerName:\s*([^\s#]+)")
_suffix_key_re        = re.compile(r"^\s*suffix:\s*([^\s#]+)")


def _extract_appspace_identity(content: str) -> tuple:
    """Return (customer_name, suffix) as declared directly under the
    top-level `appspace:` mapping in a customer.yaml/config.yaml file.

    v2.5.15 (Finding 7). Mirrors _extract_chart_version_checked's
    direct-child-of-appspace tracking (last-key-wins on a duplicate key),
    applied to customerName and suffix instead of version.

    customerName is the true identity of an environment (drives the
    namespace and related wiring). suffix is the variant (a/b/c...); it can
    be declared locally in this file OR inherited from a parent config.yaml
    higher in the tier. instanceName is NOT read here -- it names virtual
    machines only and is not an environment identity signal.

    Either element of the returned tuple is None when not declared in THIS
    file. A None suffix does not mean "no suffix", only "not declared here"
    -- a caller that needs the chain-resolved effective value must fetch and
    check ancestor config.yaml files separately; this function only reads
    what one specific file states.
    """
    in_appspace     = False
    appspace_indent = -1
    child_indent    = None
    customer_name = None
    suffix        = None
    for line in (content or "").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if in_appspace and indent <= appspace_indent:
            in_appspace  = False
            child_indent = None
        if _appspace_key_re.match(line):
            in_appspace     = True
            appspace_indent = indent
            child_indent    = None
            continue
        if in_appspace:
            if child_indent is None and indent > appspace_indent:
                child_indent = indent
            if indent == child_indent:
                cm = _customer_name_key_re.match(line)
                if cm:
                    customer_name = cm.group(1).strip("'\"")
                sm = _suffix_key_re.match(line)
                if sm:
                    suffix = sm.group(1).strip("'\"")
    return customer_name, suffix


# COPS-2546: read-at-sha caching for every fetch that is not a helm value file.
# The v2.13.2/v2.13.3 additions (identity checks, cohort guard, identity-move
# augmenter) called _bb_fetch_status directly, so every poll cycle re-fetched
# files that cannot change (content at a git sha is immutable), multiplied by
# every open PR and every retry. Combined with the retry-until-determinate loop
# this exhausted the Bitbucket API budget live on 2026-07-29: 1449s iterations
# and acme-config-dev PR 6938 stuck INPROGRESS for hours.
#
# This deliberately REUSES _vf_cache instead of adding a second dict:
#   - _vf_cache is already bounded by _bound_vf_cache() once per iteration, so a
#     long-lived pod cannot grow it without limit. A private cache here would
#     have shipped an unbounded leak in a pod that runs for weeks.
#   - the paths overlap heavily (the new-env ancestor chain reads the very same
#     config.yaml files _fetch_value_files reads), so sharing turns those into
#     cross-hits rather than duplicate calls.
#   - one singleflight map means concurrent duplicates dedupe across both paths.
#
# Storage contract is _fetch_value_files': content for BB_OK, None for
# BB_NOT_FOUND, and nothing at all for BB_ERROR, because a transient failure
# must never be cached as a fact. Status is therefore derivable on read.
# ── COPS-2562: cheap environment-name validation ────────────────────────────
#
# COPS-2552 resolved prefix/customerName/suffix/esSuffix through each app's
# ENTIRE value-file chain at BOTH shas to rebuild the exact GCP service
# account id. Correct, but on a mass version bump (PR 3831: 212 apps, 14
# changed files) that was ~65s of a 121.5s iteration and the single largest
# consumer of Bitbucket API calls, on a token shared with the Azure DevOps
# pipelines (COPS-2543).
#
# The expensive part existed only to learn two values that are constants in
# practice. Verified across all three config repos, 2026-07-30:
#   appspace.prefix                     13 decls, {pv, cl},  always 2 chars
#   appspace.suffix                    307 decls, {a, b, c}, always 1 char
#   appspace.externalSecretsTool.suffix  0 decls, chart default "es", 2 chars
# so len(GSA id) == len(customerName) + 8 and GCP's 30-char limit means
# customerName <= 22. The cap below is 20, leaving two characters of margin
# for a future longer prefix/suffix/esSuffix or another derived resource
# without having to model each resource type again. Longest real name today
# is 19 ("westinghousenuclear"), across 322 environments, so nothing needs
# grandfathering.
CUSTOMER_NAME_MAX = 20

# Deliberately NOT the strict GCP id regex. pv-3ds-c is a real live prod
# environment: "3ds" starts with a digit and fails ^[a-z]..., but the full
# id "pv-3ds-c-es" is valid because the prefix supplies the leading letter.
# Validating customerName alone with the strict pattern would block it.
_CUSTOMER_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def _check_customer_name(name):
    """Validate appspace.customerName. Returns (status, detail).

    "ok" / "invalid" / "unresolved" -- the three-way distinction FIX A
    (v2.4.9) established, so "not declared here" can never be mistaken for
    "rejected".
    """
    if not name:
        return "unresolved", None
    name = str(name)
    if len(name) > CUSTOMER_NAME_MAX:
        return "invalid", (
            f"`appspace.customerName` is {len(name)} characters "
            f"(`{name}`), the maximum is {CUSTOMER_NAME_MAX}. The derived GCP "
            f"service account id is `<prefix>-<customerName>-<suffix>-es`, and "
            f"GCP rejects service account IDs longer than 30 characters, which "
            f"leaves {CUSTOMER_NAME_MAX} for the name plus margin. This is a "
            f"hard Google limit, not an Appspace one: the environment would "
            f"deploy and then silently fail, with ArgoCD reporting Synced "
            f"while every pod sits in CreateContainerConfigError. Shorten the "
            f"name and push again.")
    if not _CUSTOMER_NAME_RE.match(name):
        return "invalid", (
            f"`appspace.customerName` (`{name}`) must contain only lowercase "
            f"letters, digits and hyphens, and must not start or end with a "
            f"hyphen. It becomes part of a GCP service account id, which GCP "
            f"validates strictly.")
    return "ok", None


def _is_rename_of(old_header: str, new_header: str) -> bool:
    """True when two headers plausibly name the SAME resource renamed.

    Deliberately narrow. A false positive here SUPPRESSES a real deletion
    warning, which is strictly worse than the noise this fixes, so this is
    two explainable rules rather than a similarity score. Both additionally
    require the same kind.

    Rule A - hash rename: identical except the final `-<token>` segment.
      pv-x-acme-secret-generator-cb71f3d8 -> pv-x-acme-secret-generator-3abbd629
      (the Job name carries a content hash, so every version bump renames it)

    Rule B - one token inserted or removed anywhere in the hyphen-token list.
      ...-mediatransform-access -> ...-mediatransform-gsa-access
      (mediatransform moved from workload identity to a dedicated GSA)
    """
    if _section_kind(old_header) != _section_kind(new_header):
        return False
    a, b = _section_name(old_header), _section_name(new_header)
    if not a or not b or a == b:
        return False

    ta, tb = a.split("-"), b.split("-")

    # Rule A: same length, differ only in the final token.
    if len(ta) == len(tb) and len(ta) > 1 and ta[:-1] == tb[:-1]:
        return True

    # Rule B: exactly one token inserted or removed.
    if abs(len(ta) - len(tb)) == 1:
        longer, shorter = (ta, tb) if len(ta) > len(tb) else (tb, ta)
        for i in range(len(longer)):
            if longer[:i] + longer[i + 1:] == shorter:
                return True
    return False


def _split_renames_from_deletions(deleted: list, created: list):
    """Split a deletion list into (real_deletions, renames).

    Each creation can absorb at most one deletion, so two deletions racing
    for one creation leave the loser reported as a genuine deletion. Order
    of the surviving deletions is preserved.
    """
    unused = list(created or [])
    real, renames = [], []
    for d in (deleted or []):
        match = next((c for c in unused if _is_rename_of(d, c)), None)
        if match is None:
            real.append(d)
        else:
            unused.remove(match)
            renames.append((d, match))
    return real, renames


def _same_env_identity(old_identity: tuple, new_identity: tuple) -> bool:
    """True when two (customer_name, suffix) pairs look like the SAME
    environment (v2.5.15, Finding 7).

    customer_name is the primary key: it drives the namespace and is the
    real identity signal (confirmed on real prod renames: 'seagal'->'segal'
    is a typo fix to a DIFFERENT identity even with the same suffix, and
    'bnym--aec1'->'bny--aec1' likewise). A mismatch there is decisive
    regardless of suffix.

    suffix is compared only when BOTH sides declare one. An undeclared
    suffix on either side is UNKNOWN, not "no suffix" -- treating it as a
    mismatch would make an ordinary same-identity rename (suffix inherited
    from a parent config.yaml, not stated in the leaf file) look like a
    decommission. Missing/unparseable data on both customer_name and suffix
    degrades to trusting the rename, the same conservative-default posture
    already used elsewhere in this module (_is_version_downgrade returns
    False rather than block on noise it cannot interpret).
    """
    old_name, old_suffix = old_identity
    new_name, new_suffix = new_identity
    if old_name and new_name and old_name != new_name:
        return False
    if old_suffix and new_suffix and old_suffix != new_suffix:
        return False
    return True
