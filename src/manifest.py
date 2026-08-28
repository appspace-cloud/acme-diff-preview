"""Rendered-manifest parsing and resource-level diffing.

Sliced out of diff_preview.py unchanged (COPS-2658 phase 5).

What a helm render actually produced, read as resources rather than as text.
`_split_yaml_docs` and `_summarize_rendered_manifest` turn a rendered
manifest into a resource map; `_diff_resources` and the create/delete
detectors say what changed between two of those maps.

`_detect_template_artifacts` and `_is_checksum_only_section` are the two
noise filters that keep the rest honest: a template artifact is a rendering
detail rather than a change a reviewer chose, and a section whose only
changed lines are checksum annotations is cascade noise.
"""
import re

import logsink  # structured logging seam (same-dir module)

from redact import (
    _redact_k8s_env_pairs,
    _redact_secret_section,
    _redact_sensitive,
    _unquote,
)


def _is_checksum_only_section(body: str) -> bool:
    """True when every changed line is a checksum/tracking annotation only.

    These sections appear in Deployments as cascading side-effects of ConfigMap
    changes. They carry no operator-useful information. Extended to cover helm
    template output which includes argocd.argoproj.io/tracking-id and similar
    annotations that always drift between renders.
    """
    _ANNOTATION_NOISE = (
        "checksum/",
        "argocd.argoproj.io/tracking-id",
        "kubectl.kubernetes.io/last-applied-configuration",
        "deployment.kubernetes.io/revision",
        "meta.helm.sh/release-",
        # COPS-2668: `helm.sh/resource-policy` is deliberately NOT here. It is
        # not cosmetic like its neighbours: `keep` is what carries a resource
        # through an Argo cascade, so `keep` -> absent is the difference
        # between a Namespace surviving a decommission and being deleted with
        # everything inside it. This filter runs upstream of every safety
        # detector, so listing it here did not merely hide the line from the
        # comment -- it hid the change from the code that decides whether the
        # PR is dangerous.
        "helm.sh/chart",
    )
    changed = []
    for l in body.splitlines():
        # Skip difflib unified-diff structural lines (---, +++, @@ hunk headers);
        # they start with -/+ but are not content changes.
        if l.startswith("---") or l.startswith("+++") or l.startswith("@@"):
            continue
        if l.startswith("< ") or l.startswith("> ") or l.startswith("-") or l.startswith("+"):
            stripped = l.lstrip("+-< >").strip()
            if stripped:
                changed.append(stripped)
    return bool(changed) and all(
        any(noise in l for noise in _ANNOTATION_NOISE) for l in changed
    )


def _summarize_rendered_manifest(rendered: str) -> tuple:
    """Summarize a rendered multi-document manifest for the PR comment.

    v2.5.6 (Finding B): a successfully rendered NEW environment used to be
    posted as up to 30,000 chars of raw "+" pseudo-diff — a wall of text
    with no review value (everything is new, there is nothing to compare).
    What a reviewer needs instead: how many resources, of which kinds, and
    which applications. This helper extracts exactly that.

    Line-based parsing on purpose: PyYAML is not in the container (H9 was
    deferred for that same reason) and helm's own output is stable enough
    for top-level `kind:` and `metadata: -> name:` extraction.

    Returns (total_resources, kind_counts: dict, workload_names: sorted list).
    Workloads are Deployment/StatefulSet/DaemonSet/CronJob/Job — the names a
    reviewer recognizes as "applications".
    """
    WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "CronJob", "Job"}
    total = 0
    kind_counts = {}
    workloads = set()
    for doc in rendered.split("\n---"):
        kind = None
        name = None
        in_metadata = False
        for line in doc.splitlines():
            if line.startswith("kind:") and kind is None:
                kind = line.split(":", 1)[1].strip()
            elif line.startswith("metadata:"):
                in_metadata = True
            elif in_metadata and name is None and line.startswith("  name:"):
                name = line.split(":", 1)[1].strip().strip("'\"")
            elif in_metadata and line and not line.startswith(" "):
                in_metadata = False
        if not kind:
            continue
        total += 1
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        if kind in WORKLOAD_KINDS and name:
            workloads.add(name)
    return total, kind_counts, sorted(workloads)


def _redact_rendered_manifest(rendered: str) -> str:
    """Redact a full rendered multi-document manifest before it can reach a
    PR comment or the full-diff artifact (v2.25.0).

    Mirrors _redact_for_display per document instead of per diff section: a
    v1 `kind: Secret` document is whole-masked; every other document gets
    the key-name redaction plus the two-line k8s env-var pass. Kinds merely
    containing "Secret" (ExternalSecret, SealedSecret) hold references, not
    values, and are NOT whole-masked. Structural scheduling fields (`key`,
    `topologyKey`) stay exempt from redaction via _redact_sensitive itself.
    """
    out = []
    for doc in rendered.split("\n---"):
        kind = None
        for line in doc.splitlines():
            if line.startswith("kind:"):
                kind = line.split(":", 1)[1].strip()
                break
        if kind == "Secret":
            # _redact_secret_section is built for diff section BODIES whose
            # header carries the resource identity — on a whole document it
            # would mask `kind:` and `metadata.name:` too, leaving an
            # anonymous blob. Keep the identity part (apiVersion / kind /
            # metadata) readable, but still run it through the standard
            # key-name redaction (chart-authored annotations could hold
            # values), and whole-mask everything from the first top-level
            # `data:` / `stringData:` on with the proven Secret masker
            # (covers block scalars, multi-line PEM blobs, etc.).
            doc_lines = doc.split("\n")
            split_at = next((i for i, l in enumerate(doc_lines)
                             if l.startswith(("data:", "stringData:"))),
                            None)
            if split_at is None:
                out.append(_redact_k8s_env_pairs(_redact_sensitive(doc)))
            else:
                head = "\n".join(doc_lines[:split_at])
                tail = "\n".join(doc_lines[split_at:])
                out.append(_redact_k8s_env_pairs(_redact_sensitive(head))
                           + "\n" + _redact_secret_section(tail))
        else:
            out.append(_redact_k8s_env_pairs(_redact_sensitive(doc)))
    return "\n---".join(out)


def _split_yaml_docs(yaml_text):
    """Yield top-level YAML documents, expanding a `kind: List` wrapper.

    Helm/kubectl output can wrap resources in `kind: List` with an `items:`
    array. Before v2.5.0 that whole document parsed to zero resources (silent
    loss). We detect a List doc and re-emit each item as its own document at
    top-level indentation so the normal line scan can pick it up.
    """
    for doc in re.split(r'\n---\s*\n|^---\s*\n', yaml_text, flags=re.MULTILINE):
        if not doc.strip():
            continue
        # Is this a List wrapper? (kind: List with an items: sequence)
        if re.search(r'^kind:\s*List\s*$', doc, re.MULTILINE) and \
           re.search(r'^items:\s*$', doc, re.MULTILINE):
            # Split items on the `- ` sequence markers at column 0 and dedent.
            body = doc.split("items:", 1)[1]
            # Each item starts with "- " at the item indent; capture blocks.
            items = re.split(r'\n(?=- )', body.strip())
            for it in items:
                it = it.strip()
                if it.startswith("- "):
                    it = it[2:]
                # dedent: drop the common leading whitespace helm added to items
                lines = it.splitlines()
                dedented = []
                for i, ln in enumerate(lines):
                    if i == 0:
                        dedented.append(ln)
                    elif ln.startswith("  "):
                        dedented.append(ln[2:])
                    else:
                        dedented.append(ln)
                block = "\n".join(dedented).strip()
                if block:
                    yield block
        else:
            yield doc


def _detect_deleted_resources(sections: list) -> list:
    """Headers of sections that DELETE a resource entirely.

    A true deletion is the manifest diffed against empty (_diff_resources
    with an absent PR side): every content line is a minus and there are NO
    context lines. Bodies come from difflib.unified_diff with its default 3
    context lines, so any partial change where at least one line survives
    always carries context lines (they start with a space). "Minus lines and
    no plus lines" alone is NOT enough -- that is also the signature of a
    change that only removes lines from a manifest that still exists, and
    it made PR 3829 report 110 deletions for a removed `replicas:` line and
    PR 6956 report 2480 for a removed tolerations block (COPS-2563)."""
    deleted = []
    for header, body in sections:
        minus = plus = context = 0
        for line in body.splitlines():
            if line.startswith("+++") or line.startswith("---"):
                continue
            if line.startswith("+"):
                plus += 1
            elif line.startswith("-"):
                minus += 1
            elif line.startswith(" "):
                context += 1
        if minus and not plus and not context:
            deleted.append(header)
    return deleted


def _detect_created_resources(sections: list) -> list:
    """Headers of sections that CREATE a resource entirely.

    Exact mirror of _detect_deleted_resources: the manifest diffed against
    an absent main side, so every content line is a plus and there are NO
    context lines. Needed to tell a rename from a deletion (COPS-2594).
    """
    created = []
    for header, body in sections:
        minus = plus = context = 0
        for line in body.splitlines():
            if line.startswith("+++") or line.startswith("---"):
                continue
            if line.startswith("+"):
                plus += 1
            elif line.startswith("-"):
                minus += 1
            elif line.startswith(" "):
                context += 1
        if plus and not minus and not context:
            created.append(header)
    return created


# COPS-2714: the one deletion wave that is a feature working as designed.
# Enabling acme-ping-scaler makes the micro-services chart stop rendering
# every HPA in the namespace (hpa.yaml: "Skip all HPA rendering when
# acmePingScaler is enabled to prevent replica conflicts") -- both would own
# replica counts. So a PR that turns the scaler on shows dozens of HPA
# deletions, and the DO-NOT-MERGE deletion block made an intentional
# activation look like an incident (acme-config-prod #4444: 23 HPAs).
# The pairing is deliberately narrow, exactly like the rename split above,
# because a false match would SUPPRESS a real deletion warning: the calm
# path requires the acme-ping-scaler Deployment to be CREATED in this same
# diff, and only ever reclassifies HorizontalPodAutoscaler headers.
_PINGSCALER_DEPLOY_PREFIX = "/apps/Deployment "
_PINGSCALER_NAME = "acme-ping-scaler"
_HPA_HDR_PREFIX = "/autoscaling/HorizontalPodAutoscaler "


def _detect_pingscaler_created(created: list) -> bool:
    """True when this diff CREATES the acme-ping-scaler Deployment.

    Field report (the first 2.106.0 render of acme-config-prod #4444): the
    two halves of the chart's contract live in DIFFERENT apps of the same
    environment -- the ping-scaler Deployment is rendered by
    supporting-services ({env}-ss) while the HPAs it displaces belong to
    micro-services ({env}-ms). A same-diff pairing therefore never fires in
    production. So this detects only the creation half, per app; the
    render layer pairs it with HPA deletions across the SAME ENVIRONMENT
    (comment_render._pingscaler_reclass). Name matched exactly: a
    lookalike must not unlock the calm path.
    """
    return any(
        h.startswith(_PINGSCALER_DEPLOY_PREFIX)
        and h.split(" ", 1)[1].split("/")[-1] == _PINGSCALER_NAME
        for h in created)


def _hpa_headers(deleted) -> list:
    """The HorizontalPodAutoscaler subset of a deleted-headers list.

    One definition shared by the summary and the panel, so the two can
    never disagree about which deletions the handover explains."""
    return [h for h in (deleted or []) if h.startswith(_HPA_HDR_PREFIX)]


# Go's own output for a nil or missing template/printf argument. Matched
# tightly on purpose: a bare "%!" or the word "value" appears in legitimate
# ConfigMap data (log format strings, embedded templates), and a block that
# fires on real config is a block people learn to override. These three
# shapes are only ever produced by a value the chart read and did not get.
_TEMPLATE_ARTIFACT_RE = re.compile(
    r"%![a-zA-Z]?\((?:<nil>|MISSING)\)|<no value>")


def _detect_template_artifacts(sections: list) -> list:
    """Sections whose APPLIED side renders an unresolved template value.

    COPS-2632 shape: with `appspace.hostingID` absent the chart rendered
    `hosting-id: hst-%!s(<nil>)`, helm exited 0, and the comment reported a
    routine change. The chart's own guard could not catch it - it reads
    `{{- if .Values.appspace.hostingID }}`, so an absent value skips the
    validation rather than failing it. A chart author who uses `required`
    lands in REASON_MISSING_REQUIRED already; one who uses an `if` guard
    produced silence until this existed.

    Only `+` lines count. An artifact on the `-` side, or replaced by a real
    value, means this PR is FIXING one, and blocking that would be exactly
    backwards. Context lines are ignored for the same reason: an artifact
    already present on both sides is not this PR's doing.
    """
    hit = []
    for header, body in sections:
        for line in body.splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue
            if _TEMPLATE_ARTIFACT_RE.search(line):
                hit.append(header)
                break
    return hit


# KCC Compute* kinds that rejected `hosting-id: hst-%!s(<nil>)` live
# (COPS-2632). Global BLOCK on every artifact was shipped in 2.47.0 and
# rolled back to REVIEW in 2.48.0: the chart is the authority on what is
# required. COPS-2677 re-blocks only this class — the API/KCC reject path
# that actually broke linux-services — and leaves ConfigMap/Deployment
# artifacts as REVIEW.
_KCC_BLOCKING_ARTIFACT_KINDS = (
    "ComputeInstance", "ComputeDisk", "ComputeAddress",
    "ComputeFirewall", "ComputeForwardingRule",
    "ComputeNetwork", "ComputeSubnetwork", "ComputeRoute",
)


def _is_kcc_blocking_artifact(header: str) -> bool:
    """True when a template-artifact header is a KCC Compute* resource."""
    if "cnrm.cloud.google.com" not in header:
        return False
    return any(k in header for k in _KCC_BLOCKING_ARTIFACT_KINDS)


def _diff_resources(main_res: dict, pr_res: dict) -> str:
    """Diff two pre-parsed resource dicts (from _parse_manifest_resources).

    Returns a diff string in the ArgoCD `===== /Kind ns/name =====` format.
    Returns empty string if there are no differences.
    """
    import difflib
    all_keys = sorted(set(main_res) | set(pr_res),
                      key=lambda k: (k[0], k[1], k[2]))
    parts = []
    for key in all_keys:
        type_key, ns, name = key
        a_text = main_res.get(key, "")
        b_text = pr_res.get(key, "")
        if a_text == b_text:
            continue
        a_lines = a_text.splitlines(keepends=True)
        b_lines = b_text.splitlines(keepends=True)
        delta = list(difflib.unified_diff(a_lines, b_lines, lineterm="\n"))
        if not delta:   # pragma: no cover - differing text always diffs non-empty
            continue
        hdr = f"/{type_key} {ns}/{name}" if ns else f"/{type_key} {name}"
        parts.append(f"===== {hdr} ======\n" + "".join(delta))
    return "\n".join(parts)


_DECOM_WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "CronJob", "Job")


def _summarize_resources_dict(resources: dict) -> tuple:
    """(total, kind_counts, workload_names) from a _parse_manifest_resources dict."""
    kind_counts = {}
    workloads = set()
    for (type_key, _ns, name) in resources:
        kind_counts[type_key] = kind_counts.get(type_key, 0) + 1
        if type_key.split("/")[-1] in _DECOM_WORKLOAD_KINDS:
            workloads.add(name)
    return len(resources), kind_counts, sorted(workloads)


def _is_header_only_block(block) -> bool:
    """True when an emitted app block carries nothing but its own header.

    COPS-2651. Such a block repeats exactly the two facts its Changeset
    overview row already carries -- the app name and the resource count --
    while the row additionally carries the deep link the header lacks.

    Judged on what was actually emitted rather than on why it was kept,
    because the reasons multiply (risky, fingerprint-grouped, shape-grouped)
    and each one assumes a body will follow. The lines are the only thing
    that knows whether one did.

    The group preamble ("Identical diff across N environments") counts as
    content: it names environments no single row does.
    """
    body = [ln for ln in block if ln.strip()]
    if len(body) != 1:
        return False
    only = body[0].lstrip()
    return only.startswith("\u26a0\ufe0f **`") and "resource(s) changed" in only


def _flatten_yaml(node, prefix=""):
    """Flatten nested mappings to {dotted.path: scalar/list} (PyYAML output)."""
    out = {}
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                out.update(_flatten_yaml(v, p))
            else:
                out[p] = v
    return out


def _section_kind(header: str) -> str:
    """'/external-secrets.io/ExternalSecret card-deployment-key' -> 'ExternalSecret'.

    Headers are built as "/{type_key} {ns}/{name}" or "/{type_key} {name}"
    (see _diff_resources). The kind therefore lives on the LEFT of the first
    space, and must be read from there.

    COPS-2594: the previous implementation split on the last slash first, so
    for any namespaced resource it returned the resource NAME instead of the
    kind -- "/v1/Secret my-ns/db-credentials" gave "db-credentials". That
    silently made _is_sensitive_kind False for every namespaced Secret,
    ExternalSecret, RoleBinding and so on, so a deleted namespaced Secret
    was listed without the sensitive-kind flag and never got a reserved
    display slot in _prioritise_risk_sections. Exactly the class of miss the
    deleted-resources block exists to prevent (PR 6773)."""
    try:
        left = header.split(" ", 1)[0]
        return left.rsplit("/", 1)[-1]
    except Exception:
        return ""


def _strip_trailing_comment(value: str) -> str:
    """Strip a trailing ` # comment` from an unquoted YAML scalar (v2.5.3).

    A quoted value ('...' or "...") is left untouched -- a '#' inside quotes
    is literal data, not a comment, and k8s resource names can't legally
    contain one anyway (DNS-1123), so this only ever affects the defensive
    unquoted-scalar case.
    """
    if value[:1] in ("'", '"'):
        return value
    return re.sub(r'\s+#.*$', '', value).strip()


def _parse_manifest_resources(yaml_text):
    """Split a multi-document YAML string into a dict keyed by (group/Kind, ns/name).

    Each value is the normalized document text (stripped, consistent trailing newline).
    Documents without kind/metadata are skipped.
    """
    resources = {}
    for doc in _split_yaml_docs(yaml_text):
        doc = doc.strip()
        if not doc:   # pragma: no cover - _split_yaml_docs never yields blank
            continue
        kind = ns = name = api = ""
        in_meta = False
        meta_child_indent = None  # indent of metadata's direct children,
                                   # determined dynamically instead of the
                                   # hardcoded "exactly 2 spaces" this used to
                                   # assume. Real `helm template` output is
                                   # always 2-space, so this never triggered
                                   # in production, but hardcoding it was
                                   # fragile (v2.5.3 defensive hardening).
                                   # Still required (not "any indent") to
                                   # avoid matching a deeper nested `name:`,
                                   # e.g. metadata.ownerReferences[].name.
        for line in doc.splitlines():
            if line.startswith("apiVersion:"):
                api = line.split(":", 1)[1].strip()
            elif line.startswith("kind:"):
                kind = line.split(":", 1)[1].strip()
            elif line.startswith("metadata:"):
                in_meta = True
                meta_child_indent = None
            elif in_meta:
                stripped = line.lstrip()
                if not stripped:
                    continue
                indent = len(line) - len(stripped)
                if indent == 0:
                    in_meta = False
                    continue
                if meta_child_indent is None:
                    meta_child_indent = indent
                if indent == meta_child_indent:
                    if stripped.startswith("namespace:"):
                        ns = _strip_trailing_comment(
                            stripped.split(":", 1)[1].strip())
                    elif stripped.startswith("name:"):
                        name = _strip_trailing_comment(
                            stripped.split(":", 1)[1].strip())
        if kind and not name:
            # Fallback for flow-style metadata (e.g. `metadata: {name: x}`),
            # valid YAML that the block-style line scan above cannot see.
            # Without this the whole resource was skipped on BOTH sides and
            # a real change reported as no-diff (bughunt F5a).
            m = re.search(r"^metadata:\s*\{(.*)\}\s*$", doc, re.MULTILINE)
            if m:
                flow = m.group(1)
                def _flow_val(field):
                    fm = re.search(
                        r"\b" + field + r":\s*(\"([^\"]*)\"|'([^']*)'|([^,}\s]+))",
                        flow)
                    return (fm.group(2) or fm.group(3) or fm.group(4)) if fm else ""
                name = name or _flow_val("name")
                ns   = ns or _flow_val("namespace")
        if not (kind and name):
            if kind or name or "apiVersion:" in doc:
                # A K8s-looking document we could not identify: say so instead
                # of dropping it silently (diagnosability for future parser gaps).
                logsink.debug(f"manifest parser: skipping unidentifiable document "
                              f"(kind={kind!r} name={name!r}): {doc[:120]!r}")
            continue
        # Use ArgoCD-style key: /Kind ns/name (group prefix for non-core).
        # Strip matching surrounding quotes from name/namespace so a change
        # that only re-quotes the name (name: x vs name: "x") is seen as the
        # SAME resource, not a phantom add+delete (v2.5.0 H1).
        name = _unquote(name)
        ns   = _unquote(ns)
        grp = api.split("/")[0] if "/" in api else ""
        type_key = f"{grp}/{kind}" if grp and grp not in ("v1", "") else kind
        key = (type_key, ns or "", name)
        if key in resources and resources[key] != doc + "\n":
            # Same (kind, ns, name) emitted twice with different content
            # (umbrella charts merging subchart output). Keep both diffable
            # instead of silently overwriting the first (bughunt F5b).
            n2 = 2
            while (key[0], key[1], f"{name}#{n2}") in resources:
                n2 += 1
            logsink.log(f"manifest parser: duplicate resource {key} in render "
                        f"\u2014 keeping both as '#{n2}' variant", "WARNING")
            key = (key[0], key[1], f"{name}#{n2}")
        resources[key] = doc + "\n"
    return resources
