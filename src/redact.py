"""Display-time redaction for diff text (COPS-2658 phase 1).

Everything that decides whether a value must be hidden before it reaches a
pull request comment or the full-diff page lives here. Extracted verbatim
from diff_preview.py: no logic was changed in the move.

The module is a leaf on purpose. It imports nothing from the service and
must keep importing nothing from it, so the dependency arrow only ever
points this way.
"""
import re


def _unquote(s: str) -> str:
    """Strip one layer of matching surrounding quotes from a scalar (v2.5.0 H1)."""
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


_SENSITIVE_KEYS = re.compile(
    r'(?i)(password|passwd|pwd|pass|secret|token|key|api[-_]?key|private[-_]?key'
    r'|auth|credential|bearer|jwt|access[-_]?token|refresh[-_]?token'
    r'|connection[-_]?string|dsn|mongodb[-_]?uri|postgres[-_]?url'
    r'|encryption[-_]?key|signing[-_]?key)',
)
# COPS-2579: only bare "key" is exempted below, by exact structural field
# name -- NOT bare "pass" or "auth". An earlier draft of this fix also
# dropped those two, on the (untested) theory that they might over-match
# things like "compassRegion" or "authorEmail". The existing test suite
# caught the real cost of that: test_redact_pass_abbreviation pins that a
# field literally named "redisPass" (no "password" substring at all) must
# still be redacted, and the same abbreviation shape plausibly exists for
# auth fields (serviceAuth, basicAuth) with no compound alternative that
# would catch them. Removing bare key/pass/auth without evidence each one
# is actually a live false positive would trade a proven leak-prevention
# behavior for a hypothetical cosmetic one -- not a trade this module
# makes. Only "key" had a concrete, audited false positive (tolerations
# `key:`, `topologyKey:` on acme-config-prod PR #3837): that one is fixed
# precisely, by name, below.
# Bare "key" is kept (it is the only way to catch an arbitrary custom field
# like sshKey/gpgKey that no explicit compound names), which means it still
# over-matches two Kubernetes API field names that are also bare "key":
# a toleration's `key:` (the taint key it tolerates) and a node/pod affinity
# matchExpressions item's `key:` (the label key being matched), plus the
# compound `topologyKey:` in topologySpreadConstraints. All three are fixed,
# structural PodSpec field names -- never used to hold a secret value --
# unlike `kind: Secret` data keys, which are arbitrary and still get
# whole-masked regardless of name by _redact_secret_section. Exempt them by
# exact name instead of narrowing the regex further, so the fix is precise
# and does not quietly reopen a real "*Key" secret field.
_SCHEDULING_FIELD_EXEMPT = frozenset({"key", "topologykey"})


def _is_scheduling_field(key_part: str) -> bool:
    """True if key_part (the 'name:' text captured before the value, e.g.
    'topologyKey: ' or '- key: ') names a structural Kubernetes scheduling
    field that must never be treated as sensitive, regardless of substring
    overlap with _SENSITIVE_KEYS. See _SCHEDULING_FIELD_EXEMPT above."""
    name = key_part.strip().lstrip("-").strip().rstrip(":=").strip().strip('"\'')
    return name.lower() in _SCHEDULING_FIELD_EXEMPT

# v2.5.21 (F1): hard cap on the helm-error text fed to _redact_error_detail's
# regex, applied BEFORE matching to kill the quadratic backtracking. Far above
# the caller's own [:400] so error diagnostics keep full context.
_REDACT_DETAIL_MAX_CHARS = 4000

def _is_block_scalar_opener(val: str) -> bool:
    """True if a YAML value is a block-scalar indicator (`|`, `>`), even with
    a chomping/indentation indicator or a trailing comment.

    The membership test this replaces (val in ("|", "|-", ...)) missed valid
    openers with a trailing comment, e.g. `tls.crt: |- # PEM cert`: YAML
    allows a comment after the indicator, so the value string was
    "|- # PEM cert", not "|-". The opener line got masked, but in_block was
    never entered, so the continuation lines (the real secret bytes) leaked
    verbatim -- the same leak class as FIX D / v2.5.0 H3 / v2.5.14, confirmed
    live. Grammar: [|>] then up to two of {digit 1-9, + , -} in any order,
    optional whitespace, optional # comment."""
    return bool(re.match(r'^[|>](?:[1-9]|[+-]){0,2}\s*(?:#.*)?$', val.strip()))


def _redact_secret_section(text: str) -> str:
    """Display-time redaction for `kind: Secret` diff sections.

    Inside a Secret, the key NAME is not a reliable sensitivity signal
    (ca.crt, connection-string, arbitrary app keys), so every `key: value`
    line is masked, keeping keys and diff markers so the reader still sees
    WHICH entries changed. Runs at display time only - the diff engine
    compares the real values, so changes are still detected.

    v2.5.14: a Secret data value rendered as a YAML block scalar (`key: |`,
    `|-`, `>`, ...) -- a common shape for multi-line secrets such as TLS
    certs, PEM keys, or a multi-line .env blob -- only had its OPENER line
    masked. The continuation lines (the actual secret bytes) matched neither
    `key: value` nor anything else this function checked, so they fell
    through to the `else` branch and were emitted verbatim. Confirmed live:
    a `tls.crt: |-` value with base64 content on the following indented
    lines leaked that content in full into the Bitbucket PR comment. Reuses
    the same in-block/dedent tracking already proven correct in
    _redact_k8s_env_pairs / _mask_block_line.
    """
    out = []
    in_block = False
    block_indent = 0
    for line in text.splitlines():
        if in_block:
            marker_len = 1 if line[:1] in "+- " else 0
            rest = line[marker_len:]
            content_indent = len(rest) - len(rest.lstrip())
            if line.strip() == "":
                out.append(line)
                continue
            if content_indent > block_indent:
                out.append(_mask_block_line(line))
                continue
            in_block = False  # dedented out of the block -> normal handling

        m = re.match(r'^([+\- ]*)([\w.\-/]+\s*[:=]\s*)(.+)$', line)
        if m and m.group(3).strip() not in ("{}", "[]", "Opaque"):
            val = m.group(3).strip()
            out.append(f"{m.group(1)}{m.group(2)}[REDACTED]")
            if _is_block_scalar_opener(val):
                # Block scalar opener (incl. a trailing comment): the value
                # itself is on the following indented lines, not on this line.
                # Enter block mode so those continuation lines get masked too
                # instead of leaking.
                marker_len = 1 if line[:1] in "+- " else 0
                rest = line[marker_len:]
                block_indent = len(rest) - len(rest.lstrip())
                in_block = True
        else:
            out.append(line)
    return "\n".join(out)


def _redact_k8s_env_pairs(text: str) -> str:
    """Redact the two-line Kubernetes env-var form.

    Rendered Deployment manifests express env vars as:
        - name: appspace_someSecretKey
          value: <the actual secret>
    The single-line redactors test the YAML key of each line, but here the
    secret sits on a line whose own key is literally `value` — which never
    matches _SENSITIVE_KEYS. So before v2.4.9 every such secret leaked in
    full into the PR comment regardless of the (sensitive) name above it
    (FIX D, the highest-severity finding of the July 2026 campaign).

    This pass handles three shapes of the k8s env-var pattern:
      1. two-line inline:  - name: X\n  value: <secret>
      2. two-line block:   - name: X\n  value: |\n    <secret lines...>
      3. flow mapping:     - {name: X, value: <secret>}
    In every case, if the NAME string matches _SENSITIVE_KEYS the value is
    masked. Diff markers, the name line, and the `value:` key are preserved.
    Block scalars (H3) and flow mappings (H4) were added in v2.5.0 after the
    v2.4.9 FIX D only covered the two-line inline shape.
    """
    lines = text.splitlines()
    # Capture the name token from a `- name: X` / `name: X` line.
    name_re  = re.compile(r'^[+\- ]*\s*-?\s*name\s*:\s*(.+?)\s*$')
    # Inline value, or a block-scalar opener (value: | / |- / > / >- ...).
    value_re = re.compile(r'^([+\- ]*)(\s*)value\s*:\s*(.*)$')
    # Flow mapping: - {name: X, value: Y}
    flow_re  = re.compile(r'^([+\- ]*\s*-?\s*\{)(.*)(\})\s*$')
    last_name_sensitive = False
    in_block = False          # inside a block-scalar value we are masking
    block_indent = 0          # column of the `value:` key that opened the block
    out = []
    for line in lines:
        # Continuation lines of a sensitive block scalar: mask until indentation
        # returns to or above the value-key column, or the line is empty.
        if in_block:
            # A unified-diff marker is at most ONE leading char (+/-/space).
            # Do NOT greedily eat a run of dashes — content like "-----BEGIN"
            # starts with dashes that are data, not diff markers (v2.5.0 H3).
            marker_len = 1 if line[:1] in "+- " else 0
            rest = line[marker_len:]
            content_indent = len(rest) - len(rest.lstrip())
            if line.strip() == "":
                out.append(line)
                continue
            if content_indent > block_indent:
                out.append(_mask_block_line(line))
                continue
            in_block = False  # dedented out of the block -> normal handling

        fm = flow_re.match(line)
        if fm:
            inner = fm.group(2)
            # find name: X and value: Y inside the flow mapping
            nmatch = re.search(r'name\s*:\s*([^,}\s]+)', inner)
            if nmatch and _SENSITIVE_KEYS.search(_unquote(nmatch.group(1))):
                inner2 = re.sub(r'(value\s*:\s*)([^,}]+)', r'\1[REDACTED]', inner)
                out.append(f"{fm.group(1)}{inner2}{fm.group(3)}")
            else:
                out.append(line)
            continue

        nm = name_re.match(line)
        if nm:
            last_name_sensitive = bool(_SENSITIVE_KEYS.search(_unquote(nm.group(1))))
            out.append(line)
            continue

        vm = value_re.match(line)
        if vm:
            marker, indent_ws, val = vm.group(1), vm.group(2), vm.group(3)
            key_col = len(marker) + len(indent_ws)
            if last_name_sensitive:
                if val.strip() == "" or _is_block_scalar_opener(val):
                    # Block scalar opener (incl. a trailing comment), or a bare
                    # `value:` whose content is on the next lines: keep the
                    # `value:` line, mask the body.
                    out.append(line)
                    in_block = True
                    block_indent = key_col
                else:
                    # Inline value: mask it. Both '-' old and '+' new lines hit
                    # here while last_name_sensitive stays True.
                    out.append(f"{marker}{indent_ws}value: [REDACTED]")
            else:
                out.append(line)
            continue

        # A non-name, non-value, non-continuation line ends this env-var block.
        if line.strip() != "":
            last_name_sensitive = False
        out.append(line)
    return "\n".join(out)


def _fence_safe(text: str) -> str:
    """Neutralize triple-backtick sequences so untrusted rendered content
    cannot break out of the ```diff code fence it is placed in.

    v2.5.19 (R4, community-research round): a value in a rendered manifest
    (e.g. a ConfigMap holding a Markdown MOTD) can contain ```, which closes
    the bot's own code fence and lets the rest of that value render as live
    Markdown in the PR comment — enough to inject a fake "Status: SUCCESS"
    line or hidden content that a reviewer reads as the bot's own words. We
    insert a zero-width space between the backticks: the fence sequence is
    broken (three separate spans, not a fence token) while the text still
    reads as ``` to a human. Applied to every body placed inside a fence.
    """
    return text.replace("```", "`\u200b`\u200b`")


def _show_cr(text: str) -> str:
    """Make carriage returns visible in a display diff.

    v2.5.19 (E3): a PR that only flips CRLF<->LF in a value file produces
    rendered -/+ pairs that look byte-identical (the \\r is invisible), which
    reads as a broken diff. Replace a trailing \\r with a visible symbol (␍,
    U+240D) so the real change is obvious. Only trailing \\r is touched; a
    bare \\r mid-line would be unusual in helm output and is left alone.
    """
    return text.replace("\r\n", "\u240d\n").replace("\r", "\u240d")


def _mask_block_line(line: str) -> str:
    """Replace the content of a block-scalar continuation line with a marker,
    preserving the diff marker and indentation for readability (v2.5.0 H3)."""
    # A unified-diff marker is at most one leading char; do not eat data dashes.
    marker_len = 1 if line[:1] in "+- " else 0
    prefix = line[:marker_len]
    rest = line[marker_len:]
    indent = len(rest) - len(rest.lstrip())
    return f"{prefix}{' ' * indent}[REDACTED]"


def _redact_for_display(hdr: str, body: str) -> str:
    """Redact a diff section before it is posted to Bitbucket.

    v1 Secret sections get whole-value masking; everything else gets the
    same key-name based redaction the AI prompt has always had, PLUS the
    two-line k8s env-var pass (FIX D). Before v2.4.3 only the AI path
    redacted - the Bitbucket comment published rendered manifests verbatim,
    including Secret data blocks. Kinds merely containing "Secret"
    (ExternalSecret, SealedSecret) hold references, not values, and are NOT
    whole-masked.
    """
    if re.search(r"/Secret[\s/]", hdr + " "):
        return _redact_secret_section(body)
    return _redact_k8s_env_pairs(_redact_sensitive(body))


def _redact_error_detail(detail: str) -> str:
    """Redact secret-looking values from a helm/render error before it can
    reach a PR comment or a build status.

    v2.5.19 (R2, community-research round): helm's YAML errors echo the
    offending source line verbatim ("yaml: line 5: password: hunter2 ..."),
    so a parse failure on a value file leaked whatever was on that line into
    the comment — the same class as Argo CD CVE-2025-23216 (secrets shown in
    error messages and the diff view). This masks the value after any
    `<sensitive-key>:` or `<sensitive-key>=` token while keeping the key name
    and the surrounding message intact for diagnosis. Fail-safe: on any regex
    trouble, returns a generic string rather than risking the raw detail.

    v2.5.21 (F1, ReDoS): the caller truncated with [:400] AFTER this ran, so
    the regex saw the full untruncated helm stderr. The `[A-Za-z0-9_.\\-]*`
    prefix backtracks quadratically on a long dashed near-miss run
    (`aaa-aaa-...`) — content an attacker can put in a values file that helm
    then echoes. Measured 80KB -> 132s of CPU pinning a worker thread. The
    fix is to bound the input to a few KB BEFORE the regex: the caller only
    keeps [:400] anyway, and a much larger head still leaves ample context
    for masking. The bound makes the quadratic term a small constant.
    """
    if not detail:
        return detail
    # Bound BEFORE the regex — this is the actual ReDoS fix. Well above the
    # caller's own [:400] so diagnostics are unaffected.
    if len(detail) > _REDACT_DETAIL_MAX_CHARS:
        detail = detail[:_REDACT_DETAIL_MAX_CHARS]
    try:
        # key: value  and  key=value  where the key looks sensitive.
        def _mask(match):
            return f"{match.group(1)}{match.group(2)}[REDACTED]"
        pattern = re.compile(
            r'(?i)\b([A-Za-z0-9_.\-]*'
            r'(?:password|passwd|pwd|secret|token|key|auth|credential|bearer'
            r'|jwt|dsn|session|cookie|connection[-_]?string)'
            r'[A-Za-z0-9_.\-]*\s*[:=]\s*)(["\']?)\S.*',
        )
        return pattern.sub(_mask, detail)
    except Exception:
        return "(error detail withheld — could not be safely redacted)"


def _redact_sensitive(text: str) -> str:
    """Redact secret-like values from diff text before sending to Vertex AI.

    Matches lines where the key name looks sensitive (password, token, key,
    secret, etc.) and replaces the value with [REDACTED]. Operates on the
    rendered diff lines ('+'/'-' prefixed) so structural diff context is kept.

    Only the VALUE portion (after ':', '=', or quoted assignment) is redacted;
    key names and diff markers are preserved for context.

    A sensitive key whose value is a YAML block scalar (`private-key: |`) has
    its continuation lines masked too: matching only the opener line here left
    the indented body to leak on any non-Secret, non-env resource (e.g. a
    ConfigMap or CRD holding a PEM key or token), since neither this pass nor
    _redact_k8s_env_pairs covered that shape. Same block-tracking as the other
    two redactors.
    """
    # Idempotence guard (property-test finding, inputs like '0\r\r' or
    # '0\x85'): splitlines-plus-join drops trailing terminators one per
    # pass and normalizes exotic ones to \n, so content-bearing text
    # ending in ANY of splitlines' terminators changed on every pass.
    # Drop all trailing terminators up front: one stable fixed point,
    # same documented rule that a trailing newline is dropped.
    # Terminator-only input keeps its shrink-by-one behaviour, pinned by
    # test_terminator_only_input_shrinks_by_design.
    _terms = "\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"
    if text.strip(_terms):
        text = text.rstrip(_terms)
    redacted_lines = []
    in_block = False
    block_indent = 0
    for line in text.splitlines():
        if in_block:
            marker_len = 1 if line[:1] in "+- " else 0
            rest = line[marker_len:]
            content_indent = len(rest) - len(rest.lstrip())
            if line.strip() == "":
                redacted_lines.append(line)
                continue
            if content_indent > block_indent:
                redacted_lines.append(_mask_block_line(line))
                continue
            in_block = False  # dedented out of the block -> normal handling
        # Match key: value or key=value patterns (YAML / env-style).
        m = re.match(r'^([+\- ]*)([\w.\-/]+\s*[:=]\s*)(.+)$', line)
        if (m and _SENSITIVE_KEYS.search(m.group(2))
                and not _is_scheduling_field(m.group(2))):
            redacted_lines.append(f"{m.group(1)}{m.group(2)}[REDACTED]")
            if _is_block_scalar_opener(m.group(3).strip()):
                marker_len = 1 if line[:1] in "+- " else 0
                rest = line[marker_len:]
                block_indent = len(rest) - len(rest.lstrip())
                in_block = True
        else:
            redacted_lines.append(line)
    return "\n".join(redacted_lines)
