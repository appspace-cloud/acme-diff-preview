# Comment-parsing bugs — acme-diff-preview (2026-07-03)

> **STATUS: ALL FIXED in v2.4.6.** Found via autonomous functional/performance
> testing (three-hat round: functional, performance, implementation review).
> Regression tests: `tests/test_comment_parsing_bugfix.py`.

Two regexes that read back the bot's OWN previously-posted comment could
**never** match what the bot actually writes. Both pre-date this session
entirely (present since v1.9.1, per the code's own comments); found while
building functional test batteries that exercise F1/N5 together for the
first time.

## Bug 1 — sha extractor never matches

Real header: `**Commit** \`{sha}\`` (bold, space before the backtick).
Old regex: `r'Commit \`([0-9a-f]{8})\`'` — missing `**` and the space.
**Confirmed empirically: 0/5 outcome scenarios matched.**

Consequence: `comment_sha` was always `""`, so the cross-pod dedup gate
(`comment_sha == pr_sha[:8]`) was permanently false. Every pod restart
(i.e. every release — we shipped 3 in one day) caused a full, unnecessary
re-diff of every currently open PR, even when the posted comment already
covered the exact same commit. The in-memory `_seen` dict masked this
within a single pod's uptime; only pod restarts exposed it.

## Bug 2 — status-token extractor never matches

Real footer: `{MARKER} [{token}]` (em-dash + space before the marker,
never a literal `[`). Old regex required a literal `[` immediately before
the marker. **Confirmed empirically: 0/5 outcome scenarios matched.**

Consequence: always fell back to legacy substring matching. By
coincidence this reproduced the intended clean/transient/error behavior,
but **not** for permanent errors: `oci_not_found`'s status text also
contains "Diff incomplete" (the transient-detection substring), so a
permanent, unfixable error was retried every iteration forever instead of
being marked done. Same broken regex was duplicated in
`fix_stuck_inprogress` (the pod-crash recovery path): a stuck-INPROGRESS
PR with a permanent error could resolve to a false `SUCCESSFUL` Bitbucket
status instead of `FAILED`, letting a hard-blocked PR look mergeable.

## Fix

Both regexes were duplicated at 2 call sites each (how the same bug
survived twice, unnoticed) — this is itself part of what let it hide.
Consolidated into two shared helpers, `_extract_comment_sha` and
`_extract_status_token`, each used everywhere a comment is read back.

## Testing discipline change

Every regression test in `tests/test_comment_parsing_bugfix.py` builds its
mock comment via the REAL `format_comment()` writer, never a hand-typed
approximation. Hand-typed mocks are exactly how both bugs went undetected
for so long — earlier tests in this session's own N5/F1 batteries also
had hand-typed mocks that (harmlessly) used the same wrong assumptions,
which is what led to their discovery in the first place.

Tests: 119 passing (was 110).
