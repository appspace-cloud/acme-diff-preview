"""Reading facts back out of ArgoCD apps, PR comments and config.

Sliced out of diff_preview.py unchanged (COPS-2658 phase 6).

Five small parsers with one thing in common: each pulls a fact out of a
structure this service does not own, and each returns something falsy rather
than raising when the structure is not the shape it expected. A chart
reference, a git repo, a build-status token, the sha a previous comment was
written for, the configured repo list.

Failing soft is the right default here: a missing annotation on one app must
not take down a run that covers hundreds.
"""
import re


# ── Multi-repo support (COPS-2507) ──────────────────────────────────────────
# DIFF_REPOS: semicolon-separated repo entries, each "slug" or "slug:scopes"
# where scopes is a |-separated list of path prefixes the service should
# consider inside that repo (files outside every scope are invisible to
# affected-app matching AND new-env detection). An entry with no scopes
# means "whole repo" — every ArgoCD app in that repo is reachable, and any
# tree the repo has that ArgoCD does NOT manage (e.g. a legacy-pipeline
# path) is simply never matched by any app, so it stays silent on its own
# without needing an explicit scope exclusion. Production runs both
# acme-config-dev and acme-config-stage with no scope restriction (stage
# gained azure/ coverage in v2.6.3, when pv-stage-corporate-b was onboarded
# to ArgoCD as the first Azure spoke). Scopes remain available for a repo
# that genuinely wants to exclude an in-repo tree the service should never
# look at, regardless of whether ArgoCD apps exist there.
#   DIFF_REPOS="acme-config-dev;acme-config-stage"
#   DIFF_REPOS="acme-config-dev;acme-config-stage:gcp/|azure/"   (scoped form, still supported)
# Default preserves the exact single-repo behavior this service always had.
def _parse_diff_repos(raw: str) -> dict:
    repos: dict = {}
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        slug, _, scope_raw = entry.partition(":")
        slug = slug.strip()
        if not slug:
            continue
        scopes = [s.strip() for s in scope_raw.split("|") if s.strip()]
        repos[slug] = {"scopes": scopes}
    if not repos:
        repos["acme-config-dev"] = {"scopes": []}
    return repos


# Marker written into the footer of every comment we post. find_existing_comment
# also matches the legacy "argocd-diff-preview" marker so comments created by
# older pods are still updated in place (no duplicate comment) during rollout.
COMMENT_MARKER     = "acme-diff-preview"


def _extract_comment_sha(raw: str) -> str:
    """Pull the 8-char PR sha out of a previously-posted comment's header.

    BUG FIX: the header is written as "**Commit** `{sha}`" (bold markdown,
    space before the backtick). The regex used to read it back was
    r'Commit `([0-9a-f]{8})`' -- missing the "**" and the space -- so it
    NEVER matched any comment this bot ever posted, in any version since
    the header format was introduced. Every call returned "". That made
    the cross-pod sha-dedup check (`comment_sha == pr_sha[:8]`) permanently
    false, so a pod restart caused a full, unnecessary re-diff of every
    currently open PR even when the posted comment already covered the
    exact same commit. Confirmed empirically against real format_comment()
    output before fixing; regression test constructs a REAL comment via
    format_comment rather than a hand-typed string, so this class of
    generated-vs-parsed drift cannot silently reappear.
    """
    m = re.search(r'\*\*Commit\*\*\s*`([0-9a-f]{8})`', raw)
    return m.group(1) if m else ""


def _extract_status_token(raw: str) -> str:
    """Pull the machine-readable clean/permanent/transient token out of a
    previously-posted comment's footer.

    BUG FIX: the footer is written as "{COMMENT_MARKER} [{token}]" (an
    em-dash and a space precede the marker, never a literal '['). The
    regex used to read it back required a literal bracket before the marker --
    requiring a literal '[' immediately before the marker -- so it NEVER
    matched, in any version since the token was introduced (comment
    itself said "1.9.1+"). Every call silently fell back to matching
    human-readable substrings, which happened to reproduce the intended
    behavior for "clean" and error/transient cases but not for "permanent"
    errors (oci_not_found's status text also contains "Diff incomplete",
    the substring used to detect *transient* problems) -- so a permanent,
    unfixable error was retried forever instead of being left alone, and
    in the pod-crash recovery path (fix_stuck_inprogress) a stuck-INPROGRESS
    PR with a permanent error could be resolved to a false "SUCCESSFUL"
    Bitbucket status instead of "FAILED". Confirmed empirically against
    real format_comment() output across all 5 outcome scenarios before
    fixing (clean, clean-with-diff, permanent, transient, error).

    COPS-2668: `blocked` was missing from this list while process_pr emits it
    (the empty `microservices.definitions` guard, COPR-31637). An unrecognised
    token returns "" and falls through every branch of fix_stuck_inprogress to
    its final `else: SUCCESSFUL`, so a pod killed mid-flight resolved a
    correctly-blocked PR to a green merge gate, directly contradicting the
    blocking comment sitting next to it. Any token the writer emits must be
    readable here.
    """
    # COPS-2715: LAST match, not first. The token is written in the footer,
    # so the last occurrence is always the real one -- while everything
    # before it can be rendered manifest content, i.e. bytes a pull request
    # author controls. A first-match read lets a hunk containing this exact
    # sequence shadow the footer, and an unrecognised or wrong token falls
    # through fix_stuck_inprogress to SUCCESSFUL (the COPS-2668 failure).
    # The exposure predates inline tiny diffs: the no-page fallback and the
    # INLINE profile already put hunks in the body.
    ms = re.findall(re.escape(COMMENT_MARKER)
                    + r'\s+\[(clean|permanent|transient|blocked)\]', raw)
    return ms[-1] if ms else ""


def _extract_app_git_repo(app):
    """Return the config repo slug for an app's git source, or None.

    The git source is the one WITHOUT a chart (multi-source apps: source-1 is
    the git config repo providing value files via the $config alias). repoURL
    formats seen live: git@bitbucket.org:appspace-cloud/acme-config-dev and
    https://bitbucket.org/appspace-cloud/acme-config-dev(.git).
    """
    spec = app.get("spec", {})
    srcs = spec.get("sources") or ([spec["source"]] if spec.get("source") else [])
    for s in srcs:
        if s.get("chart"):
            continue
        repo_url = (s.get("repoURL") or "").strip().rstrip("/")
        if not repo_url:
            continue
        if repo_url.endswith(".git"):
            repo_url = repo_url[:-4]
        slug = repo_url.split("/")[-1]
        # git@host:workspace/slug has the slug after the last '/', same rule.
        return slug or None
    return None


def _extract_app_chart_info(app):
    """Return (chart_name, targetRevision, registry_host, value_files) for an app's OCI source.

    Apps are multi-source: source-1 is the git config repo (provides value files via $config
    alias), source-2 is the OCI Helm chart. There are two registries:
      helm-oci-dev.repo.appspace.com     — dev charts
      helm-oci-release.repo.appspace.com — released/stable charts (stage, prod)
    Both use the same credentials (OCI_USER / OCI_PASS env vars).

    Returns (None, None, None, []) when no OCI source is found.
    """
    spec = app.get("spec", {})
    srcs = spec.get("sources") or ([spec["source"]] if spec.get("source") else [])
    for s in srcs:
        chart = s.get("chart")
        if chart:
            repo_url = s.get("repoURL", "")
            # Strip scheme if present (repoURL may be bare hostname or oci:// URL)
            registry = repo_url.replace("oci://", "").split("/")[0]
            value_files = s.get("helm", {}).get("valueFiles", [])
            return chart, s.get("targetRevision"), registry, value_files
    return None, None, None, []
