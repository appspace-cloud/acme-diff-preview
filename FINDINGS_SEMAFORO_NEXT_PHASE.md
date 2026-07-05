# FINDINGS: traffic-light status coherence (semaforo) — for next phase

Generated 2026-07-05. Do NOT implement yet — this is the saved analysis to
work from in the next session. All findings below were confirmed either by
direct code reading or by a real PR against acme-config-dev (declined after
capture, no merge).

## Ground rule agreed with Marcos (2026-07-05)

No orange/warning status. Two colors only:
- GREEN (SUCCESSFUL) = the diff was actually computed (with or without
  changes) and can be trusted.
- RED (FAILED) = anything else: any error, any reason, transient or
  permanent. No exceptions. A transient error self-heals on the next
  ~60s iteration once the retry succeeds (existing RETRYABLE_REASONS /
  "unseen" mechanism already does this) — only the COLOR changes, not the
  retry behavior.

---

## FINDING 1 (confirmed live, PR #6645) — render_failed shows GREEN

Location: `process_pr`, main status-decision block (~line 3872-3899).

`has_blocking_indet` only looks at `oci_not_found_count`. Every other
REASON_* in an indeterminate outcome (`render_failed`, `timeout`,
`oci_pull_failed`, `metadata_pending`, `unexpected_error`, and even
`invalid_yaml`/`invalid_version` which ARE in `PERMANENT_REASONS`) falls
through to the `elif any_unknown:` branch, which posts **SUCCESSFUL** with
"Diff unavailable for N app(s)".

Confirmed live: injected a value with valid YAML but wrong type (`image:
"string"` instead of a map) → helm template fails → `render_failed` →
PR #6645 posted **SUCCESSFUL**, comment said "NOT confirmed unchanged".

Fix direction: any `n_unknown > 0` should route to FAILED, not just
`oci_not_found`. Collapse the `elif sections_total > 0` (with the
"(N unavailable)" suffix) and `elif any_unknown` branches: if `any_unknown`
is true at all, result must be FAILED regardless of whether some other apps
did produce a real diff.

## FINDING 2 (confirmed live, PR #6644) — invalid_yaml: green in existing
env, red in new env (inconsistent by path, not by cause)

`_new_env_status` already treats YAML-parse errors as `structural` → FAILED.
The main existing-env path does not — it is subsumed into Finding 1's bug
(invalid_yaml is in PERMANENT_REASONS but PERMANENT_REASONS is only
consulted for classification hints in the comment text, never actually
checked in the status-decision `if/elif` chain; only `REASON_OCI_NOT_FOUND`
is special-cased).

This is the same root cause as Finding 1. Once Finding 1's fix lands
(any indeterminate → FAILED), this inconsistency disappears on its own:
invalid_yaml will be FAILED on both paths.

## FINDING 3 (code-confirmed, not yet live-tested) — fix_stuck_inprogress:
transient token → SUCCESSFUL

Location: `fix_stuck_inprogress`, the `elif _token == "transient":` branch
sets `state, desc = "SUCCESSFUL", "Diff unavailable - review comment"`.

Same bug as Finding 1, in the pod-crash-recovery path. Once a comment is
generated with the "transient" token (which will exist as long as
Finding 1 exists upstream in `_extract_status_token`/`format_comment`),
recovering a stuck INPROGRESS status maps it to green. Must become FAILED
for consistency, and to stay correct even after Finding 1 is fixed (a
transient outcome must NEVER map to green, in any code path that reads the
token).

Not yet reproduced live (requires killing a pod mid-diff at the right
moment) — code-level fix should be applied together with Finding 1 since
it reads the same token vocabulary; add a unit test that constructs a real
comment via format_comment with a transient outcome and feeds it through
fix_stuck_inprogress, asserting FAILED.

## FINDING 4 (confirmed live, PR #6646) — HIGH SEVERITY: mixed PR silently
skips a broken new environment entirely (not a wrong color — no evaluation
at all)

Location: `process_pr`, ~line 3491: `_detect_new_env_candidates` is only
called inside `if not affected:`. Docstring of
`_detect_new_env_candidates` confirms this is by design: "The caller should
only invoke this after confirming get_affected_apps() returned an empty
set."

Consequence: if a single PR touches files for an EXISTING environment
(so `affected` is non-empty) AND ALSO adds a brand-new environment's
config/customer.yaml in the same commit, the new-environment files are
never passed to `_detect_new_env_candidates` at all. They are not rendered,
not diffed, not mentioned in the comment, and do not affect the status in
any way — not even a debug log line.

Confirmed live: PR #6646 bumped a real tag on `cl-dev11-a` (existing) AND
added `cl-zztest99-a/config.yaml` (new env, deliberately missing
`appspace.version`, which `_new_env_status` classifies as `structural` →
FAILED *if the new-env path ever ran*). Real bot output: **SUCCESSFUL**,
"1 resource(s) will change", comment mentions ONLY `cl-dev11-a`. Zero
mention of `cl-zztest99-a` anywhere — not in the AI summary, not in the
per-app list, not in the status description.

This is worse than a wrong color: a reviewer sees a clean, confident green
check and has no way to know a second, unvalidated environment was added in
the same PR. It could ship broken on merge with no warning at any point.

Fix direction: decouple new-env detection from `if not affected:`. Always
run `_detect_new_env_candidates` on the full changed-file list (it already
filters out files that match `path_map`, i.e. already-known apps, via
`if f in path_map_keys: continue`), independent of whether `affected` is
empty. Merge both result sets (existing-app diffs + new-env renders) into
one comment and one status decision, so a mixed PR is fully covered by a
single evaluation instead of an if/else that silently drops one branch.
This needs careful design: the two paths currently produce different
comment structures (structured diff sections vs "all resources are new"
narrative) and post `upsert_comment` independently with early `return`
statements — merging them is a real refactor, not a one-line fix.

## FINDING 5 (code-confirmed via re-use of Finding 1's repro, not yet
live-tested for the new-env path specifically) — new-env classifier
defaults UNKNOWN errors to green, not red

Location: `_new_env_status`. The function has a hardcoded allow-list of
`structural` (red) substrings (`"error converting yaml"`, `"no
appspace.version"`, `"not found in oci"`, `"chart not found"`, `"invalid"
+ "version"`, `"could not fetch"`). ANYTHING that does not match one of
these falls through to `return "SUCCESSFUL", True` — i.e. the *default* for
an unrecognized error is green ("expected first-time-env case"), not red.

Two concrete gaps found by re-reading `_render_new_env_diff`'s error
strings against the allow-list:

- `"chart pull failed: {str(e)[:120]}"` (generic exception during
  `_ensure_chart`, e.g. a transient network blip, disk issue, or an actual
  bug) — does not match any structural substring → green.
- `"chart pull returned None (registry login may have failed)"` — does not
  match → green.
- Most importantly: `"helm template failed: {err[:120]}"` where `err` is a
  genuine template-execution error UNRELATED to missing first-deploy
  credentials (e.g. the same `render_failed`-class error reproduced for
  Finding 1: `can't evaluate field tag in type interface {}` from a
  type-mismatched value) — does not contain any YAML-parse substring, so it
  is NOT caught by the `invalid_yaml` sub-check, and is NOT in the
  structural list → falls through to green, "expected first-time-env
  case", even though the actual cause has nothing to do with missing
  post-deploy secrets and may be a real, permanent config bug in the new
  environment's files.

This is a "default-to-safe should be red, not green" problem: the function
assumes any unrecognized helm-template failure is the well-understood
"missing credentials that only exist after first deploy" case, but that is
only one specific, narrow failure mode among many that produce a similar
error shape.

Fix direction: invert the default. Keep a SHORT allow-list of the specific,
well-understood "expected first-deploy" error shapes (the missing
credential/constellation-file pattern is the only one currently documented
as legitimate), and treat everything else — including "chart pull failed",
"registry login may have failed", and any non-whitelisted helm template
failure — as `structural` (red) by default. This is a larger behavior
change than Findings 1-3 and needs the real "expected" error text sampled
from production logs first, to build an accurate allow-list without
turning every legitimate new-environment PR red.

---

## Suggested order for next phase

1. Finding 1 (main fix: any indeterminate reason → FAILED). This is the
   trunk fix; Finding 2 disappears automatically once this lands.
2. Finding 3 (fix_stuck_inprogress transient → FAILED), same session,
   same token vocabulary, cheap to do together.
3. Finding 4 (mixed PR blind spot). Bigger refactor, own session/PR,
   needs careful design for merging the two comment-building paths.
4. Finding 5 (new-env default-to-green). Needs production log sampling
   of real "expected" first-deploy error text before writing the
   allow-list, to avoid false reds on legitimate new environments.

## Test/verification assets already produced this session (not saved as
permanent test files yet — recreate when implementing)

- PR #6645 repro recipe: valid YAML, `image: "string"` instead of a map,
  on an existing service in cicd-versions.yaml → render_failed → today
  green, should be red.
- PR #6644 repro recipe: tab-indented invalid line appended to an existing
  env's cicd-versions.yaml → invalid_yaml → today green, should be red.
- PR #6646 repro recipe: bump a real tag on an existing env (e.g. `poll`)
  AND simultaneously add `gcp/dev/public-cloud/ap1/cl-zztest99-a/config.yaml`
  with no `appspace.version` in the same commit → new env silently never
  evaluated, full green on the existing-env diff only.
