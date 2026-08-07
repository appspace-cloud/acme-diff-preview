# Task: fold version-transition noise inside each app so the needle is visible

Operator iteration 2 of the comment redesign. Never commit this file.

## Evidence: census of the last 100 PRs in acme-config-prod

Corpus: PR ids 3797-3896 (2026-07-28 to 2026-08-06), 93 with a bot comment.
All 93 predate the v2.27 rollout, so they show the pre-redesign pain at
full strength, on real production traffic.

- Comment size: p50 8.7KB, p75 43KB, p90 240KB. Ten percent of all prod
  comments sit AT the Bitbucket hard cap (245KB), middle-cut.
- 80 of 100 PRs touch exactly ONE environment. Mass fleet rollouts are
  rare in prod; the common shape is one env, many resources.
- The dominant operation by far is the platform version bump
  (`appspace.version`, 159 mentions). Real examples:
  - PR 3884 "Updating to 2603.0.15-rev1": 1 env, 185 resource sections,
    4132 changed lines, comment 244,815 bytes (hard-capped).
  - PR 3891 "bumped preview and acc to 2603.1.10": 3 envs, 473 sections,
    comment 244,535 bytes (hard-capped).
- Inside PR 3891's noise there is a REAL needle: a new
  `cnrm.cloud.google.com/reconcile-interval-in-seconds: "3600"` annotation
  appearing on KCC resources. Today no reviewer can see it. This is the
  exact failure mode the fold must solve.
- The changed lines of a bump section belong to a tight, deterministic
  vocabulary: image tag lines, chart/version labels, checksum annotations,
  env-var `value:` lines carrying the version pair, and ISO-8601 deploy
  timestamp `value:` lines. Everything else is a needle.
- 7 PRs (3834-3842) have NO bot comment at all. They cluster in one
  2-hour window on 2026-07-30: a bot outage, not a coverage gap. PRs
  merged during downtime never get evaluated. Out of scope for the
  comment format; note it for a branch-protection follow-up.
- 6 comments ended in [permanent]. v2.27's merge summary already covers
  failed apps correctly (golden failed_app_not_green.md). No action.

## Why v2.27.1 does not solve the dominant case

The readable budget (COMMENT_READABLE_BYTES = 30,000) is checked per APP,
before rendering each app, in format_comment's per-app loop. Two gaps:

1. The FIRST app always renders in full. A single-env platform bump is
   ONE app with 185+ sections, so the budget never engages and the
   comment still hits the hard cap. This is the number one prod shape.
2. The routine-bump rollup folds identical version-only changes ACROSS
   environments. It has no concept of folding WITHIN one app.

Also: the budget check recomputes the byte size of every line on every
app iteration (quadratic on big comments). Replace with a running counter.

## What to build

### 1. Version-transition fold (section level, deterministic)

New classifier, own function (e.g. `_classify_version_fold`), running in
`_package_sections` on the FULL filtered section list, before any cap
(same design rule as deletions/VM: facts never depend on display caps).

Two passes:
- Pass 1 collects candidate version transitions (old, new) from
  unambiguous carriers: image tag pairs (same repo), chart /
  `helm.sh/chart` / `app.kubernetes.io/version` label pairs, and the
  app-level `version_change` (targetRevision) when present.
- Pass 2 classifies each section: FOLDABLE only if every changed +/-
  line pairs up by key and each pair is one of: checksum annotation
  (hex to hex), image tag pair in the candidate set, chart/version
  label pair in the candidate set, `value:` pair whose (old, new) is in
  the candidate set, or `value:` ISO-8601 timestamp pair. Any unpaired
  or unclassifiable changed line makes the section a needle (not
  foldable). Sections named by deleted / zeroed / renamed / VM facts
  are never foldable, by explicit exemption.

Result travels in DiffResult as a new field with a default (keep the
namedtuple pattern), holding: n_foldable, the dominant transition
label(s), and the foldable header set.

Non-foldable sections join the `_prioritise_risk_sections` reservation
(via `extra=`) so needles always survive the storage cap. Detecting the
needle is half the job; it must be VISIBLE in the comment.

### 2. Comment rendering

In the app block, when the budget is active and n_foldable >= 3:
- One fold line right under the app header, blockquote style, matching
  the existing rollup voice: count, transition label, what was folded
  (image, chart labels, checksums, deploy timestamps), link to the full
  diff view.
- Folded sections are skipped inline. Non-folded sections render as
  today. The "Showing first X of Y" note must tell the new truth.
- The full-diff page renders with the budget disabled and MUST NOT fold
  anything (it is the complete record; keep that contract).

### 3. Intra-app readable budget (hard guarantee)

While rendering an app's inline sections, stop once the running comment
size passes the budget and emit one pointer line: how many ordinary
sections were omitted, naming the first few, linking the full page.
Risk sections (deleted / zeroed / VM / downgrade) always render before
ordinary ones (the prioritisation already sorts them first) and are
never omitted. After this change the comment size is bounded for EVERY
PR shape, single-app included. Keep the footer dedup contract intact.

### 4. Running-size counter

Replace the per-iteration `sum(len(l.encode(...)))` with a counter
updated as lines are appended. Same behavior, linear cost.

## Invariants (unchanged from iteration 1)

- Deterministic facts only; no AI in anything safety-relevant.
- Anything risk-flagged is never folded, anywhere, at any size.
- The full-diff page always holds everything, folding disabled.
- A failure is never reported as "no changes".
- The machine footer `[clean|permanent|transient] [base:sha]` survives.

## How to work

1. Read the touched regions first: `_package_sections`,
   `_filter_diff_sections`, `_format_app_diff_block`, the per-app loop
   in `format_comment`, `_prioritise_risk_sections`, DiffResult.
2. RELEASING.md flow, red-first: write the failing tests, watch them
   fail, implement, then the full suite (~2.5 min, nohup + poll).
3. Golden fixtures: add `platform_bump_single_env.md` (all sections
   fold), `platform_bump_with_needle.md` (one needle inline, rest
   folded, needle survives storage cap), `intra_app_budget.md` (huge
   non-foldable app, tail omitted with pointer). Keep existing goldens
   green; update only if the new truth requires it, and say why.
4. Property test for the classifier (hypothesis); blacklist the unicode
   line separators (\x85, \x1e) from text alphabets, known flake.
5. Update README "Reading a comment" and the scannability section.
6. No ticket keys in code comments; explain the why directly.
7. Bump chart + version following the repo convention (this is a
   feature: minor). Release notes short, one continuous line each.
8. Verify against the real corpus before opening the PR: rebuild the
   comment for PR 3884 and PR 3891 shapes from the downloaded corpus
   data in /tmp/prod_pr_audit/ and confirm the fold line, the needle,
   and the size drop (target: well under COMMENT_READABLE_BYTES).

## Deliverable

A PR against main implementing all four points, tests proving each
behavior, a release, the acme-infrastructure bump (chart version AND
image tag together), live scenario PRs in acme-config-dev reproducing
the corpus shapes, and a browser pass over the rendered comments.
