# FINDINGS: traffic-light status coherence (semaforo)

> **STATUS: RESOLVED.** Every finding in this document was implemented and
> shipped in **v2.5.4/v2.5.5** (the indeterminate-means-FAILED rule, the
> two-color GREEN/RED model, the machine-readable `[token]` footer) and is
> live in production. Kept for historical reference like the other files in
> `bughunt/` — the "Do NOT implement yet" note below described the state on
> 2026-07-05 and no longer applies.

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


---

## SESSION 2 (2026-07-05, same day, follow-up campaign) — folder rename/move/delete

Marcos asked specifically for real-config-prod-style folder restructuring
tests (renames, moves between "cadence" folders, decommissions), since
`acme-config-prod`'s git history shows this happens routinely (customers
moved between `weekly`/`monthly`/`hardcoded`/`sandbox`/`monthly-friday`
folders, typo fixes in folder names, cross-region customer migrations,
full decommissions). Confirmed via `git log --diff-filter=R` and
`--diff-filter=D` on acme-config-prod: dozens of real examples.

`acme-config-dev` does not have the cadence-folder pattern, but DOES have
the same underlying mechanism: `gcp/dev/private-cloud/*/custom/pv-dev-NN-a/`
customer folders, each backed by its own ArgoCD Applications
(`pv-dev-NN-a-ms/-ss/-glb`) whose `helm.valueFiles` are STATIC paths
pointing at that exact folder (confirmed via `argocd app get`). This is
structurally identical to a prod customer folder, so it's a faithful
substitute for testing the rename/move/delete family.

## FINDING 6 (HIGH SEVERITY, confirmed live 3x: PR #6647, #6648, #6649) —
renaming, moving, or deleting a customer folder makes the bot report a
generic render failure and silently swallows any real change bundled with it

Root cause chain, confirmed step by step:

1. The live ArgoCD Application for a customer (e.g. `pv-dev-06-a-ms`) has
   `helm.valueFiles` hardcoded to the OLD path (`.../custom/pv-dev-06-a/
   customer.yaml`, `.../cicd-versions.yaml`) — this is how ArgoCD is
   currently configured and won't change until the PR merges.
2. `get_pr_changed_files` correctly captures BOTH old and new paths for a
   git-detected rename (FIX C, v2.4.9) — so `_match_files_to_apps` DOES
   correctly identify `pv-dev-06-a-*` as affected, via the OLD path. This
   part works.
3. But when `_run_one_diff` renders the PR side, it fetches the app's
   STATIC valueFiles list at `pr_sha`. The OLD path is gone at `pr_sha`
   (confirmed directly: `git show <pr-branch>:<old-path>` →
   "fatal: path ... exists on disk, but not in <branch>"), so
   `_bb_fetch_status` returns 404 for it.
4. Per the existing (deliberate, documented) logic in `_run_one_diff`: a
   changed file that 404s at `pr_sha` is treated as "deleted by this PR"
   and OMITTED from the value files used to render — not backfilled from
   `main`. This is CORRECT behavior for a genuine deletion of an override,
   but here the file didn't disappear, it MOVED, and its content (often
   including `appspace.version`, `cloudShortName`, and every service
   override for that customer) is simply gone from the render inputs.
5. Helm then fails to render the chart at all — typically "Missing
   required value" for a field customer.yaml used to provide — producing
   the SAME generic `render_failed` / "helm template failed to render the
   chart with these values" message you'd get from an actual unrelated
   bug. There is no distinction between "this customer's config file
   moved" and "this customer's config is broken."

Confirmed live, three ways:
- **Pure rename, zero content change** (PR #6647, `pv-dev-05-a` →
  `pv-dev-05-renametest-a`): render fails for all 3 apps. A completely
  safe, no-op operation looks identical to a broken deploy.
- **Rename + a real version bump in the same commit** (PR #6648,
  `pv-dev-06-a` → `pv-dev-06-renametest-a` + a real tag change on
  `user-background`): SAME render failure. The AI summary literally says
  "0 app(s) updated · 0 resource(s) changed" and "No critical changes
  detected" — the deliberate version bump is 100% invisible. A reviewer
  sees a clean-looking (if oddly empty) green check for what is actually
  an unverified, unreviewed real change.
- **Full decommission** (PR #6649, deleted `pv-dev-07-a` entirely,
  replicating real prod pattern COPR-30708 "Decommission PV-Raytheon"):
  same generic render failure, giving zero signal that this is an
  intentional, expected teardown rather than a bug.

All three currently resolve to **green** (Finding 1's bug), which makes
this compound with Finding 1: not only is the color wrong, but even once
Finding 1 is fixed and this correctly shows red, the message itself will
still be the unhelpful, generic "helm template failed" with no indication
that a rename/move/delete is the actual, common, everyday cause. Given how
frequently this operation happens in production (dozens of examples in
`acme-config-prod` history), this is likely one of the most operationally
disruptive gaps found in the whole campaign: it means the tool cannot
currently produce a trustworthy diff for one of the most routine change
types customers/operators make.

This also interacts with **Finding 4** (new-env candidates only checked
when `affected` is empty): since the rename IS matched via the old path
(`affected` is non-empty), the new path's files never get a chance to be
considered as "a new environment" either — even if they did, `affected`
being non-empty would block that path per Finding 4. So a rename currently
falls into a gap between both mechanisms: not treated as "the same app
moved" (which would need new logic) and not treated as "a new env"
(blocked by Finding 4).

Fix direction (harder than Findings 1-3, needs real design work): the tool
needs an explicit concept of "this app's config file moved within the same
PR" — detect it from the old/new path pairs already available in the raw
Bitbucket diffstat (currently collapsed into a flat file list by
`get_pr_changed_files`, losing the pairing), and when detected, fetch the
value file content from its NEW path instead of treating the old path's
404 as a deletion. This is different from a real deletion (old path 404,
no corresponding new path) which should keep today's "omit it" behavior.
Suggest tackling this together with Finding 4 in the same design session,
since both are about the app-to-file identity model breaking when paths
change within a single PR.

## Repro recipes added this session

- PR #6647: `git mv gcp/dev/private-cloud/ap1/custom/pv-dev-05-a
  gcp/dev/private-cloud/ap1/custom/pv-dev-05-renametest-a`, no content
  change. Push, open PR, wait ~45-90s for the bot.
- PR #6648: same `git mv` pattern on `pv-dev-06-a`, PLUS a real tag change
  on one service in the moved `cicd-versions.yaml` before committing.
- PR #6649: `git rm -r` a full customer folder (e.g. `pv-dev-07-a`),
  replicating a decommission PR.
- Remember to `git checkout main` + restore the moved/deleted dev sandbox
  folders are automatically fine since these are throwaway branches never
  merged — `main` was never touched, no cleanup needed on the real dev
  environments themselves, only the test branches (already deleted).

## Updated suggested order for next phase

1. Finding 1 + 3 together (main fix: any indeterminate reason → FAILED,
   including in fix_stuck_inprogress). Cheapest, highest-value, unblocks
   correct coloring everywhere else.
2. Finding 6 (rename/move/delete handling). HIGH severity and HIGH
   frequency in production — arguably should be prioritized above Finding
   4 despite being harder, precisely because it happens constantly in
   acme-config-prod and currently produces zero useful signal.
3. Finding 4 (mixed PR blind spot for brand-new environments). Related
   design work to #2 (same underlying "path identity" gap), consider
   doing both in the same session.
4. Finding 5 (new-env default-to-green). Still last — needs production
   log sampling before writing the allow-list.


---

## SESSION 3 (2026-07-05, same day, second follow-up) — more real prod-style
operational patterns

Marcos asked to keep digging for operational patterns that could break the
tool, using more real acme-config-prod examples as inspiration. 6 more real
PRs (#6650-#6655), all declined after capture, no merge.

### CONFIRMED WORKING CORRECTLY (no new findings — good news, listed for
completeness so these aren't re-tested from scratch next time)

- **PR #6650 — fan-out change on shared `private-cloud/config.yaml`**
  (real pattern: prod's "Unify HPAs configuration" / "Refactor Public and
  Private Configuration" commits). Added a label at the shared config
  level inherited by all 10 dev private-cloud customers (30 apps total:
  10 customers × ms/ss/glb). All 30 apps correctly evaluated, all
  correctly show "no manifest changes" since the test label isn't
  consumed by any template. Confirms the fan-out mechanism itself
  (discover_path_app_map, path-prefix matching) scales correctly and
  doesn't drop or duplicate any app.

- **PR #6651 — valid, complete, brand-new customer added alone (happy
  path)**. Real `appspace.version`, complete `customer.yaml`, empty
  `cicd-versions.yaml`. Correctly detected as new env, correctly shows the
  documented "resource preview not available for new environments...
  provisioned after first deployment... this is expected" message with
  **SUCCESSFUL** status. This is the control test for Findings 4/5 — when
  a new env is evaluated in isolation (no existing app touched in the same
  PR), the classification logic works exactly as designed.

- **PR #6653 — junk binary `.DS_Store` file alongside a real change**
  (real precedent: `acme-config-prod` has actual committed `.DS_Store`
  files at `gcp/`, `azure/`, `aws/`, `.cursor/`). The binary file is
  silently ignored (doesn't match any value-file pattern), the real tag
  bump on `cl-dev11-a`'s `poll` service is correctly diffed. No crash, no
  interference.

- **PR #6655 — bulk multi-customer bump, 5 unrelated customers changed in
  one commit** (real pattern: prod's "NA, CA, AP Weekly customers going to
  2601.4.6" style mass-update commits). All 5 customers correctly
  detected, each shows exactly its own changed resource, zero
  cross-customer contamination in the diff sections.

### FINDING 4 — STRENGTHENED (confirmed with a VALID new environment, not
just a broken one)

**PR #6652** repeated PR #6646's shape but with a twist: the new customer
(`pv-dev-98-a`) was **completely valid** — same shape as PR #6651, which
in isolation correctly renders the "new environment detected, expected"
message. Bundled with a real, unrelated tag bump on an already-existing
customer (`pv-dev-01-a`) in the same commit, the result was identical to
PR #6646: **zero mention of `pv-dev-98-a` anywhere.** The comment, AI
summary, and status are 100% built from `pv-dev-01-a`'s change alone.

This upgrades Finding 4's severity assessment: it is not just that a
*broken* new environment goes unnoticed (bad enough on its own) — **any**
new environment, valid or not, is completely invisible whenever the same
PR also touches an already-known app. Given how often bulk/combination PRs
happen in production (Session 3's own PR #6655 shows 5-customer bulk edits
are routine), this blind spot likely triggers far more often in practice
than the narrower "broken new env" framing from Session 2 suggested. No
change to the recommended fix direction (already documented under
Finding 4) — this is additional live evidence to weigh when prioritizing
it, not a new root cause.

### FINDING 6 — CONFIRMED with a case-only rename variant

**PR #6654**: renamed `pv-dev-04-a` to `PV-DEV-04-a` (case-only, zero
content change; a Linux/Bitbucket-valid distinct path, even though a
default macOS filesystem treats them as the same file — had to use a
two-step `git mv` through a temp name locally to produce the rename at
all). Result: identical `render_failed` outcome as every other rename
variant in Finding 6 (PR #6647/#6648/#6649). No new root cause — this
confirms Finding 6's mechanism is purely path-string-based (old path 404s
at `pr_sha` regardless of *why* the path changed), which is useful
confirmation that the eventual fix (resolve renamed value files to their
new path) doesn't need any case-sensitivity-specific handling — the
general old-path/new-path pairing fix already scoped for Finding 6 covers
this variant too.

### Updated finding count: 6 total, now 8 real-PR confirmations across
Findings 1, 2, 4 (x2), 6 (x4), plus 4 clean/correct confirmations (fan-out,
happy-path new-env, junk file, bulk multi-customer) that do NOT need any
fix and don't need re-testing in a future session.

No new findings requiring a NEW fix category this session — Session 3
was mostly a stress-test / breadth pass confirming Session 1-2's findings
generalize (or don't) across more shapes of the same root causes, plus
several explicit "this works fine" confirmations that narrow the actual
fix surface for next phase (the tool's core matching/fan-out/bulk-diff
machinery is solid; the problems are specifically in status
classification (Findings 1/2/3), new-env path isolation (Finding 4), and
path-rename resolution (Finding 6)).


---

## RESOLUTION (2026-07-05, same day) — all 6 findings implemented, tested,
deployed, and re-verified live

Marcos gave full autonomy to implement everything at once. Done end to end:
local tests first (28 new, all red against a real repro before the fix,
green after), full suite (224 -> then 228 after a hotfix), real local
behavioral check with the actual acme-config-dev repo + a real helm binary
(bypassing only OCI auth, which needs the pod's production secret),
release v2.5.4, deploy, and live re-verification with the exact same real
PRs that found each bug.

**Shipped as v2.5.4 (commit a260368) + v2.5.5 hotfix (commit 6365dd9).**
Pod running 2.5.5, 0 restarts, clean logs.

### Finding 1 (+2): traffic-light rule — RESOLVED
Any indeterminate reason now blocks (not just oci_not_found), in both the
main status cascade and fix_stuck_inprogress. invalid_yaml/invalid_version
also now correctly stop the retry loop (a second bug found while fixing
this one — before, only oci_not_found did, so an invalid_yaml PR was
silently re-diffed forever).
Verified live: PR #6656 (invalid_yaml) -> FAILED. PR #6659 (render_failed)
-> FAILED, "will retry automatically if transient".

### Finding 3: fix_stuck_inprogress — RESOLVED
Same fix as above, same session, same token vocabulary.

### Finding 5: new-env default-to-red — RESOLVED
_new_env_status inverted to an allow-list: only "missing required value"
stays green, everything else (chart pull failures, registry login
failures, generic render_failed) is red by default now.

### Finding 6: rename/move resolution — RESOLVED (the flagship fix)
get_pr_changed_files now returns (files, renames); the old->new pairing is
threaded through to both _run_one_diff's value-file fetch (the core fix)
and _pr_chart_revision_checked (so a version bump bundled with a rename
isn't missed either). Verified live: PR #6657 replicated PR #6648's exact
scenario (folder rename + a real version bump in the same commit) and now
correctly shows "1 resource(s) will change" with the real diff, instead
of the old generic "helm template failed" that hid everything.

### Finding 4: mixed-PR blind spot — RESOLVED
_detect_new_env_candidates now runs unconditionally; format_comment gained
new_env_lines/new_env_structural/new_env_desc to splice a bundled new
environment into the same comment and force red on a structural problem
even with an otherwise-clean existing-app diff. Verified live: PR #6658
replicated PR #6652's exact scenario (valid new customer + real change on
an existing customer, same commit) and now shows BOTH sections in one
comment with the correct green status.

### Bonus finding, caught during v2.5.4's own live re-verification, fixed
in the v2.5.5 hotfix before declaring done
Combining Finding 4 (always run new-env detection) with Finding 6 (follow
renames) created a genuine interaction bug: a rename's NEW path isn't yet
in path_map, so it also looked like a brand-new environment and got
double-evaluated through the wrong code path (_render_new_env_diff, which
doesn't know "this app already exists, it just moved"), producing a false
structural-problem red status for the WRONG reason on top of the correct
diff. A second, independent bug in the same area: _render_new_env_diff's
old 120-char truncation could cut "Missing required value" off a long
error before Finding 5's allow-list ever saw it. Both confirmed live on
PR #6657 before the hotfix, both fixed and re-verified live after.

### Lesson for next time
Live-verifying a fix with the exact real PR that found the original bug
is what caught this interaction bug — a fix that looks correct in
isolation (and passed 28 new unit tests) can still misbehave when
combined with another fix touching adjacent code. Always re-run the real
repro PRs after deploying, not just the unit suite, before considering a
hardening round done.
