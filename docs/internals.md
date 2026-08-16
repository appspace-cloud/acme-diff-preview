# acme-diff-preview internals

Reference material moved out of the README to keep the front page short. Nothing
here is required to use the tool; it is for people changing it or debugging it.

## Contents

- [Why an empty `microservices.definitions` is blocked](#why-an-empty-microservicesdefinitions-is-blocked)
- [Handling mass version bumps](#handling-mass-version-bumps-hundreds-of-apps-in-one-pr)
- [The two surfaces: comment and page](#the-two-surfaces-comment-and-page)
- [Which resources make it into the comment body](#which-resources-make-it-into-the-comment-body)
- [What one app shows when it changes hundreds of resources](#what-one-app-shows-when-it-changes-hundreds-of-resources)
- [Superseding an in-flight render](#superseding-an-in-flight-render)
- [Secret-leak and comment-integrity hardening](#secret-leak-and-comment-integrity-hardening)
- [Full-diff web UI](#full-diff-web-ui-atlantis-style)

---

### Why an empty `microservices.definitions` is blocked

ArgoCD merges an environment's Helm value files in order, with the per-env
`cicd-versions.yaml` **last**. A file shaped like

```yaml
appspace:
  microservices:
    definitions:        # <- key present, no children => YAML null
```

collapses the **entire** `microservices.definitions` map to null in Helm's
`merge`, wiping every per-service `image.name` override the chart ships
(`appspace-platformservice`, `appspace-webhookservice`, `appspace-screenshot`,
…). Each affected service then falls back to the chart helper's derived
`appspace-<key>` name — a registry path that for these services has never
held an image — so the whole environment goes `ImagePullBackOff` on the next
sync. This is the COPR-31637 incident.

The guard flags a `definitions` key that is present but null/empty. A
**missing** `definitions` key is safe (the chart's own map is kept intact) and
is deliberately **not** blocked. To remove per-env overrides, delete the
`definitions:` key entirely — never leave it present but empty. See
[`docs/microservices-definitions-guard.md`](docs/microservices-definitions-guard.md)
for the full incident write-up and the exact detection rule.

### Handling mass version bumps (hundreds of apps in one PR)

Bumping a chart `version:` across many clusters in a single PR is a normal
operation. Because the diff is a local `helm template` render with no agent
round-trips, the fan-out is cheap and the only shared resource is the Bitbucket
API used to fetch value files. What keeps it fast and reliable:

- **Chart-cache warm-up** — one representative app per distinct OCI chart is
  pulled first, so the rest reuse the local tarball (`WARM_WORKERS`/`WARM_THRESHOLD`).
- **Bitbucket API rate limiting + safe caching** — a global semaphore
  (`BB_API_CONCURRENCY`) caps concurrent calls; value files are cached by
  immutable `(commit_sha, path)`, and a transient error is never cached as
  "missing" so one app's rate-limit blip can't poison the others.
  A 429 is a property of the *token*, not of the one request that received it,
  so the pause is **shared**: the first caller to be rate limited publishes a
  deadline (`Retry-After` when Bitbucket sends it, `BB_RATELIMIT_FALLBACK`
  when it does not, capped at `BB_RATELIMIT_MAX_PAUSE`) and every other
  Bitbucket call brakes with it, waiting *outside* the semaphore so a sleeping
  thread does not hold a concurrency slot. This covers both the value-file
  path and the poll loop; non-Bitbucket hosts (Vertex AI, the GCP metadata
  server) keep their own per-request backoff and never trip the shared gate.
  429s log at `WARNING` **with the endpoint**, and a value file that could not
  be read is reported separately from one that is genuinely absent — the two
  used to share a single `debug()` line, which made rate limiting
  indistinguishable from a real gap in the values hierarchy.
- **Retry with backoff + jitter** — transient reasons (`oci_pull_failed`,
  `metadata_pending`, `timeout`) retry in-process up to `DIFF_RETRIES` times.
- **AI summary at scale** — only the `AI_MAX_APPS` apps with the most changed
  resources go into the prompt (with a "+N omitted" note); the deterministic
  headline still covers every app.
- **Comment truncation preserves the footer** — an oversized comment is cut
  in the middle, never at the end, so the machine-readable `[clean|permanent|
  transient]`/`[base:...]` tokens always survive for SHA dedup.
- **Timeout hygiene** — a diff that hits `DIFF_TIMEOUT` cancels every subtask
  it had queued on the shared pool, so retries can't amplify congestion when
  the registry or renders are already slow.
- **Over the cap is permanent for that commit** — beyond `MAX_APPS_PER_RUN`
  affected apps, that commit's overflow set is never evaluated (FAILED status,
  comment names the knob); raise the cap or split the PR.

The on-disk chart cache is bounded (`HELM_CACHE_MAX_CHARTS`) and pruned at the
start of each iteration so a long-lived pod cannot fill node ephemeral storage.

> **Known trade-off — retries sleep in-worker.** A transient failure retries
> with backoff inside the same `DIFF_WORKERS` slot, so during a registry blip
> on a mass PR a worker spends most of its wall time sleeping. Simple and
> correct; a requeue-based design would raise throughput but is a larger
> change. Revisit only if blip-storms during mass bumps become common.

### The two surfaces: comment and page

Since 2.35.0 (COPS-2612) one function, `format_comment`, renders two different
artifacts, and which one it is rendering is carried by a `RenderProfile`
rather than inferred. `COMMENT_PROFILE` is the PR comment; `FULL_PROFILE` is
the full-diff page.

The split is not "short version / long version". It is **decision** versus
**evidence**:

| | `COMMENT_PROFILE` | `FULL_PROFILE` |
| --- | --- | --- |
| YAML hunks | no | always |
| Config-changes panel | no | yes |
| AI analysis | no | yes |
| Clean applications | one count | named, one per line |
| Byte-identical sections grouped | yes | no |
| Version-transition noise folded | yes | no |
| Per-resource body cap | 6,000 chars | none |
| Verdicts, deletions, VM facts, downgrades | **all, with names** | all |

#### The switches, and why they resolve at render time

`COMMENT_INLINE_DIFFS` (default `false`), `COMMENT_INPUT_PANEL` (`false`),
`COMMENT_INLINE_EVIDENCE_LINES` (`0`) and `FULL_PAGE_UNCAPPED` (`true`) are
read inside `RenderProfile.resolved()`, once per render, never snapshotted as
dataclass defaults at import.

This is not a style choice. `COMMENT_INLINE_DIFFS=true` is the one-variable
rollback to the pre-2.35.0 comment, and an import-time snapshot would make it
decorative: flipping it on a running pod would change nothing. The same trap
was hit twice during the COPS-2607 phases before the rule was written down.

`FULL_PROFILE` **pins** those three rather than resolving them. The page is
the complete record, so a comment-shape switch must not be able to empty it —
otherwise the rollback switch would delete the very thing it exists to fall
back on.

#### The rule that makes removing YAML safe

`format_comment` forces `inline_diffs` and `input_panel` back on when
`artifact_url` is empty. No URL means the artifact save failed or the UI is
off, so there is no page to hold the evidence, and the comment keeps it and
says why. Without that, a failed save would produce a comment with no
evidence anywhere.

The clearest proof it works is accidental: the four `test_cops2565` goldens
render without a URL, take this path, and still match the pre-2.35.0 comment
byte for byte.

#### Evidence moves, conclusions do not

The subtler rule, and the one that cost four bugs during COPS-2612. A diff
block contains both proof and conclusions, and only the proof belongs on the
page. The comment still states:

* how many of an app's resources are a version transition only, and **which
  one is not** (`Changed for another reason: ...`);
* every deletion, zeroed replica, downgrade, decommission and VM fact, by
  name;
* the real resource count per app, never `len(sections)`.

When adding anything to a diff block, the question to ask is which of the two
it is. If a reader would use it to decide, it stays in the comment.

#### `is_complete_record`

A `RenderProfile` field, not a check on `profile.name`. It means "this surface
IS the record, not a pointer to one", and it drives two behaviours: the page
renders no pointer to itself (with no URL to hand, that pointer degraded into
the page announcing that the page could not be produced — live for two
versions), and when the storage cap trims an app the page owns the shortfall
instead of directing the reader elsewhere.

It is a behaviour field precisely so a profile derived with `replace()` under
another name keeps behaving like the page.

#### Rollback order

`COMMENT_INLINE_DIFFS=true` **first**, then `FULL_PAGE_UNCAPPED=false`. The
other order leaves the comment without YAML *and* the page truncating, so the
information is gone. A rollback switch whose safe order is not written down is
a trap.

---

### Which resources make it into the comment body

An app can change hundreds of resources. Sections are stored up to
`FULL_SECTIONS_MAX_PER_APP` (5,000 since COPS-2610, memory-bounded, not an
arbitrary display cutoff). The counts in the headline and in the
`N resource(s) changed` line are always the real totals.

The cap is applied at **storage** time, so what it drops is missing from both
surfaces. At its old value of 400 it did that silently, while the comment's
own note claimed the remainder was "only in the full diff view" — exactly
where it was not. Hitting it now increments `section_cap_trims`, logs a
warning, and makes the page state the shortfall rather than claim to be
complete. That counter should stay at zero; if it moves, raise the cap.

Two rules decide what is shown, in order:

- **Detection runs on the full list, before any cap.** Deletions and
  replica zeroings are found by `_detect_deleted_resources` and
  `_detect_replicas_zeroed` inside `_package_sections`, on every section.
  A deletion sitting at position 111 of a mass diff is still named
  (the PR-6773 lesson, v2.5.26).
- **Risk sections get a reserved share of the display order**
  (`RISK_SECTION_RESERVE`, COPS-2567). Sections arrive sorted by resource
  key, so `/apps/Deployment` always sorts before
  `/autoscaling/HorizontalPodAutoscaler`. Taking a plain prefix meant that on
  acme-config-prod PR 3845 the ten display slots went to Deployments and none
  of the five HPA deletions the comment shouted about were visible anywhere in
  the body. The block told the reviewer to verify five deletions and showed no
  evidence for any of them, which reads exactly like a false positive.

`_prioritise_risk_sections` reorders and never drops, so `n_res` and any
consumer of the full section list are unaffected; only the caps remove
anything. The reserve is deliberately a share, not the whole budget: a PR that
deletes 200 resources must still show some ordinary changes. When no deletion
and no zeroing exist, the list is returned untouched, so the ordinary case is
byte for byte what it was before.

The truncation note reflects this. It says `Showing first N of M` only while
the slice really is a prefix, and names the risk-first ordering otherwise.
With the storage cap this generous, real apps almost never truncate at all
now; the note mostly exists for the pathological case.

### What one app shows when it changes hundreds of resources

The rules above pick which resources of a fleet are shown. They do nothing
for the shape that turned out to be the most common one in
`acme-config-prod`: a platform version bump applied to a SINGLE environment.
A census of the last 100 prod PRs found that 80 of them touch exactly one
environment, that the version bump is the dominant operation, and that 10
percent of the comments sat exactly on the Bitbucket 245KB hard cap. PR 3884
is one app with 185 resource sections and PR 3891 has 473 across 30 apps.
The readability budget shipped in COPS-2605 could not help there, because it
is checked once per app before rendering that app, so the first app always
renders in full. With one app, the budget never engages at all.

Worse than the size: PR 3891 added a brand new
`cnrm.cloud.google.com/reconcile-interval-in-seconds` annotation to its KCC
resources. It was in the comment, and no reviewer could ever have found it
inside 473 near-identical hunks.

Three layers now run inside each app, in this order.

**1. Version-transition fold (`_classify_version_fold`).** The changed lines
of a bump section come from a tight vocabulary: image tags, chart labels,
checksum annotations, version-carrying env values and deploy timestamps.
The classifier pairs every changed line of a section by YAML key, and folds
the section only if EVERY pair classifies as one of those. A pure addition,
a pure deletion, an unbalanced key or one unknown line makes the whole
section a needle that stays inline. Env `value:` pairs are the ambiguous
case, so they are only accepted when the same transition was already seen on
an unambiguous carrier (an image tag, a chart label, `targetRevision`, or the
app-level chart `version_change`). That is why a `MAX_WORKERS: 4 -> 16`
change can never fold. Fewer than `_VERSION_FOLD_MIN` foldable sections
means no fold at all, because one fold line costs more attention than the
two hunks it would hide.

Like every other safety fact, this is computed in `_package_sections` on the
FULL pre-cap list, so what folds never depends on a display cap. Sections
already claimed by a deletion, a zeroing or a VM fact are exempt by
construction, and the needles join those facts in the
`_prioritise_risk_sections` reservation, so the one interesting resource
survives the storage cap instead of being dropped at position 300.

**2. Repeat grouping (`_group_repeated_sections`).** One identical change
applied to many resources (the annotation above, added to every KCC member)
is one fact, not 12. Sections are keyed by the signature of their changed
lines only, so context lines and resource names do not split a group. The
first section of a group renders its hunk in full and the rest are named
under a `same change` line. Risk sections are never grouped: a reviewer
verifying a deletion needs to see that deletion, not a pointer to a sibling.

**3. Intra-app readability budget.** Whatever survives the first two layers
is still bounded. A running byte counter (`used`) grows as chunks are
appended, and once it passes the room left in the budget the remaining
ordinary sections are named in one `omitted` line instead of inlined. Risk
sections are exempt from this cut, and the first section of an app always
renders, so a block is never reduced to just its headline.

Every one of the three points at the full-diff page, which is rendered with
the budget disabled and grouping off, and therefore never folds, groups or
omits anything. Folding in the comment is only ever safe because that page
is the complete record.

On the two worst PRs in the census, the comment goes from the 245KB hard cap
to about 32KB, with the reconcile-interval needle visible in both.

### Grouping apps with an identical diff (COPS-2579)

A shared ancestor file (for example `gcp/config.yaml`) can change one thing
for every environment in a fleet at once. Before COPS-2579, each app's
sections were capped at 10 for storage (a limit meant only for the AI
summary prompt, reused by mistake as the comment's own storage cap), and the
comment showed a fixed top few apps inline and a one-line summary for the
rest. On acme-config-prod PR #3837 (removing a Spot compute-class override
from 248 apps, 67 resources changed per app) this meant 60 of 16616 real
diff sections were ever shown, repeated as 6 arbitrary, mutually duplicate
copies, and the scheduling fields under review were masked by an unrelated
redaction bug on top of that.

Each app's DiffResult now carries a `fingerprint`: a stable hash of its full
(pre-cap) section list, independent of section order and of the app name
itself (`_fingerprint_sections`). `format_comment` groups changed apps by
this fingerprint (`_group_changed_apps_by_fingerprint`) and renders ONE full
representative diff per distinct fingerprint, with the complete list of
member environments named above it, instead of one diff per app. The
overview table still lists every app individually (so per-environment
visibility is not lost) and labels which diff group it belongs to.

Apps with no fingerprint (a legacy or hand-built `DiffResult`, for example in
a test that constructs one directly instead of going through
`_package_sections`) always form their own singleton group and never merge
with anything, so this is fully backward compatible with any code path that
does not compute a real fingerprint.

Total output size is now bounded by the number of DISTINCT diffs in a PR, not
by the number of apps: a PR where 248 apps share one change produces one full
diff; a PR where 248 apps each have a genuinely different change still
produces up to 248, protected the same way it always was by the per-body
(`DISPLAY_BODY_MAX_CHARS`) and whole-comment (`MAX_COMMENT_BYTES`)
truncation.

COPS-2679: that collapse is COMMENT-only (`group_repeats=True`). The FULL
page (`is_complete_record`, `group_repeats=False`) keeps one block per app
so overview deep links (`#app-…`) and the Index cover every environment.
Persisting a collapsed body made "see the full diff view" a dead end on
fleet PRs like acme-config-prod #4316.


### Superseding an in-flight render

Two cases, one mechanism. A render is worth aborting when the snapshot it
started from is already dead — whether that is because the PR's **own** branch
moved (COPS-2575) or because the **destination** branch moved under it
(COPS-2617).

#### The destination-branch case

A merge on `main` invalidates every open PR against it. Before COPS-2617 that
was only noticed *after* a render finished, by comparing the `[base:]` token
on the already-published comment — a full re-render rather than an abort.
Measured on acme-config-prod, four merges in ~8 minutes: 6 passes across two
large PRs, **4 of them against a base commit that was already dead**, and a
564-app comment rewritten 3 times purely from unrelated merges.

Three properties of the destination hint, each deliberate:

* **Keyed by `(repo, base_branch)`, not per PR.** One merge invalidates every
  open PR against that branch, and a burst has to cost one extra pass in
  total, not one per merge. Last writer wins.
* **Peek, never pop** — the one place it differs from `_arm_supersede`. That
  hint belongs to a single PR, so consuming it is right. This one is shared,
  so the first PR to read it must not consume it for the others. It clears
  naturally once a render starts from the new base.
* **Only `pullrequest:fulfilled` arms it.** A push to a PR's own branch must
  never read as "main moved", or every other open PR would abort on every
  unrelated push.

**A hint only counts if it arrived after the poll that produced the snapshot
(COPS-2633).** Comparing the hint and `base_sha` for plain inequality cannot
tell a hint that is *newer* than the snapshot (a real supersede) from one the
snapshot has already moved *past* (a leftover), and the second case is
permanent rather than rare: the config repos take direct pushes to `main`
from release automation, which fire no `pullrequest:fulfilled` event, so
`main` advances beyond the last merge commit and nothing corrects the hint
again. Measured on acme-config-stage #2802: the hint sat at an ancestor of
`main`, and every PR in the repo was skipped three times — about 3 minutes of
delay before its first comment — until the livelock guard released it.

So hints carry a sequence number, and the poll loop publishes the tip it
actually read (`_note_base_observed`) before processing any PR. That read is
ground truth about where the branch is, so it retires any hint recorded
earlier — which covers what a webhook cannot: a direct push, a squash that
rewrote the commit, or a `fulfilled` event that never arrived. A sequence
counter rather than a clock, because two `monotonic()` reads can be equal and
"equal" would have to be resolved one way or the other, silently making one of
the two cases wrong. The trade-off is deliberate: if Bitbucket's refs read lags
a merge it has already announced, the hint is ignored and the PR renders
against a base a few seconds old — the pre-COPS-2617 behaviour, still caught
after the render, and far cheaper than every PR waiting out the livelock guard
on a hint that will never be correct.

Both cases share `_supersede_lock`, `_sha_eq` normalisation, and the
`SUPERSEDE_MAX_CONSECUTIVE_ABORTS` livelock guard — without that ceiling a
merge train would starve a large PR out of ever publishing anything, which is
worse than publishing slightly stale.

#### The PR's-own-branch case

Two pushes landing on the same PR inside one render window used to mean the
first render ran to completion against a commit that was already dead,
published that diff into the shared PR comment, and only then did the real
commit get rendered. Measured on acme-config-prod PR 3837: 6m17s from the
final push to the final result, 3m10s of it rendering a commit nobody would
ever merge, and for ~10s the PR carried a build status for one commit next to
a comment describing another.

The webhook already knew. `pullrequest.id`, `pullrequest.source.commit.hash`
and `repository.full_name` all arrive in the body, which `do_POST` read,
HMAC-verified, and then discarded, deciding purely on the `X-Event-Key`
header.

**How it works now**

- The webhook handler records a hint, `(repo, pr_id) -> newest sha`, under its
  own lock (`_seen_lock` already guards three structures and is held by the PR
  workers; this one is written from HTTP handler threads).
- `process_pr` **arms** on entry by atomically popping any pending hint. A pop,
  not a clear: a webhook that lands while the PR is still queued behind others
  (`PR_WORKERS=3`, minutes on a busy iteration) writes its hint *before*
  `process_pr` runs, and clearing would destroy the only signal that the
  snapshot is already stale. Popping also means a hint the snapshot already
  reflects, typically the very webhook that started this iteration, is consumed
  without aborting a perfectly correct run.
- Three check points: at entry, inside the `as_completed` loop in
  `process_batch` (where the wasted minutes are actually saved, cancelling the
  queued futures exactly like the SIGTERM drain does), and once more after the
  batch so a supersede detected on the final future still prevents publication.

**Abort semantics.** A superseded run writes nothing: no comment, no build
status, and crucially no `_seen` entry, so the PR is re-rendered rather than
skipped. The backoff is deliberately not fed either, since a supersede is not
a transient failure and must not slow the retry down. The `INPROGRESS` already
posted sits on a sha that is no longer the tip, so Bitbucket shows only the
new tip's statuses and it is inert. No change is needed to the wake path:
`_wake` was already set by the superseding webhook and is only cleared after
the iteration, so the next pass starts immediately on the new sha.

**Livelock guard.** A PR pushed to faster than it can render would abort
forever and never publish anything. After `SUPERSEDE_MAX_CONSECUTIVE_ABORTS`
consecutive aborts the run is allowed to finish; the counter resets whenever a
run publishes a real result.

**Two details worth keeping straight**

- The repo key comes from `repository.full_name` (`workspace/slug`), never
  `repository.name`, which is a *display* name and can differ from the slug
  entirely. Keying off the display name would make hints silently never match
  and the whole feature a no-op with no error anywhere.
- Hints are recorded only for `pullrequest:created` and `pullrequest:updated`.
  Comment, approval and decline events also start with `pullrequest:` and also
  embed a full pull request entity, but their sha is just the current tip.
  All of them still wake the loop, exactly as before.

**The wake path is sacred.** This is the first thing that ever parses the
webhook body, so it is the first thing that could break the wake, and a broken
wake fails *silently*: the service just degrades to the 60s safety-net tick
and everything feels sluggish until somebody notices. So `_wake.set()` is the
first statement in the `pullrequest:` branch, before anything touches the
payload; `_maybe_record_supersede_hint` is total (every parse failure, missing
key, wrong type or unknown repo simply means "no hint"); and
`tests/test_cops2575_supersede.py` asserts the ordering on the handler source
itself plus a table of hostile payloads that must each still return 200 and
still wake the loop.

Hints are only trusted when HMAC verification actually ran. `_verify_bb_hmac`
is permissive when `BB_WEBHOOK_SECRET` is empty, and an unauthenticated POST
that can abort in-flight renders is a cheap denial of service.

**Observability.** `/diff-preview/stats` now reports `bb_webhook` counters
(`received`, `rejected_hmac`, `rejected_format`, `wakes`, `hints_recorded`,
`supersedes_triggered`, `base_hints_stale_dropped`, `last_received_at`, plus
`hmac_strict` and `supersede_enabled`), and `_diff_stats` tracks whether each
iteration was started by a webhook or by the safety-net tick. That ratio is
what catches the failures no unit test can: the hook deleted or disabled in
Bitbucket, the URL changed, an ingress rule dropping the POST, or the secret
drifting out of sync after a rotation. In all of those the code is perfectly
correct and the service is quietly running on the 60s poll.

`base_hints_stale_dropped` is the COPS-2633 equivalent for the destination
hint: it counts hints retired because the poller had already seen `main` move
past them. A steady climb is expected on any repo that takes direct pushes to
`main`; it is here because that condition was invisible for as long as the bug
lasted.

### Secret-leak and comment-integrity hardening

The rendered diff and the AI summary both derive from PR-controlled content,
so several layers guard what reaches the Bitbucket comment:

- **Kind-aware, structural redaction** — `kind: Secret` bodies are whole-masked;
  other kinds get key-name redaction that also handles YAML block scalars and
  the `- name:/value:` env-var shape, applied at display time so the diff
  engine still compares real values.
- **Error details are redacted too** — a `helm template` YAML error echoes the
  offending source line; that gets masked before it can reach the comment.
- **Comment-injection is neutralized** — triple-backtick sequences in rendered
  values can't break out of the bot's diff fence to inject a fake status line.
- **AI output is sanitized** — the model summary has Markdown images, raw
  HTML, and HTML comments stripped before posting; the AI never sets the
  build status itself.
- **Isolated helm pulls** — each `helm pull` runs with a private `HELM_*`
  home so concurrent pulls of different chart versions can't corrupt helm
  3.x's unlocked shared OCI blob cache.


---

### Full-diff web UI (Atlantis-style)

The PR comment has a hard Bitbucket size limit (`MAX_COMMENT_BYTES`, ~245KB):
an oversized comment is cut in the middle and, until now, the complete output
only existed in the pod logs. With `DIFF_UI_ENABLED` (**on by default**, see
below), the service persists the COMPLETE, untruncated comment body for the
PR (already redacted, the exact text the comment would carry), together with
the same at-a-glance context the comment header shows (base commit, apps
evaluated, per-outcome breakdown), and serves it on the health port.

#### Completeness, and the two caps that remain (COPS-2610)

The page never cuts a resource body. Before that was enforced it was quietly
failing its own promise: one stored production artifact carried **981**
occurrences of `... (diff truncated for display)`. The 6,000-char body cap is
a *comment* protection — one giant ConfigMap rewrite would push the comment
past `MAX_COMMENT_BYTES`, whose blunt global cut would chop off the footer and
the status token the poll loop parses. On the page it was only a lie.

Two caps survive, neither silent:

* **Visible rows**, default 20,000. Everything past that is still in the HTML
  behind a `show full output` button, and `/raw` is byte-exact. This is a
  browser-survival number, not a policy: the largest real artifact is 786,150
  lines and 113MB of HTML, so laying out every row on first paint is a hung
  tab rather than completeness.
* **Stored sections**, `FULL_SECTIONS_MAX_PER_APP` — see above; it counts and
  logs when it bites.

#### Retention lives in the bucket, not in the prune

`_prune` only walks the local artifact directory, which is a **cache**. The
durable copy is the GCS bucket, and its object lifecycle is what decides how
long a page opens: 365 days, set in `acme-infrastructure`
(`shared/infrastructure/acme-diff-preview-artifacts`). Raising
`DIFF_UI_MAX_ARTIFACTS` would not extend retention by a day.

Because the artifact is rewritten on every commit, the age clock runs from the
PR's last diff run. The local cache also has a byte budget
(`DIFF_UI_MAX_BYTES`, 400MiB) because the directory is an emptyDir whose
`sizeLimit` the kubelet enforces by **evicting the pod**, and a count is the
wrong unit for files spanning 2KB to tens of MB.

#### Navigation and anchors (COPS-2611 / COPS-2622)

An index lists every application with its resource count, collapsed per
application via `<details>` so 345 of them are a list rather than a wall, with
a client-side filter that narrows the index and never the body.

Structure is parsed from the markers `format_comment` already emits, not from
a change to the stored artifact, so older artifacts stay readable and `/raw`
is untouched. The parser is defensive by construction: anything unrecognised
falls through and renders exactly as before, and one row is emitted per source
line either way. A page that lost a line to gain an index would undo the work
above.

**`diff_ui.app_anchor` is the single owner of the per-application anchor
shape**, and the PR comment imports it to build its deep links. Two copies of
that logic would drift on the first change and every deep link would 404 *in
silence*. It is deliberately order-independent, unlike the de-duplicated ids
inside `build_outline`: the comment cannot know the page's application order,
so a position-dependent id could not be reproduced. That moves
collision-freedom onto the input, which holds because fleet application names
are already `[a-z0-9-]`; `build_outline` keeps its numeric suffix as backstop.

Anchors are scoped per application because resource names repeat across
environments constantly, and an index entry pointing into the collapsed
overflow reveals it before jumping — otherwise clicking it would do nothing on
exactly the large pages that need the index most.

Every value on the page is PR-controlled, so all of it is escaped, anchor ids
are **sanitised** rather than escaped (they land in `id=` and `href="#..."`
where escaping still leaves a quote to break out of), the filter only toggles
a class and never writes body-derived strings back into the DOM, and the page
ships zero external assets.

Before COPS-2579 this claim was only partly true: the persisted body was the
SAME body posted as the comment, and that body was itself capped per app
(10 sections) and per PR (a fixed top few apps shown inline). For a large
ancestor-file change the page ended up byte for byte identical to the
truncated comment, not a real "full output" (measured on acme-config-prod PR
#3837: 60 of 16616 real diff sections visible in both places). Now that
`format_comment` stores each app's full (memory-bounded) sections and groups
apps with an identical diff instead of picking an arbitrary top-N (see
"Grouping apps with an identical diff" above), the exact same persistence
call carries the complete content automatically, with no changes needed to
this module.

Like the
PR comment itself, there is exactly one live artifact per `(repo, pr)`: each
new commit overwrites the previous diff in place (atomic write), so the page
always reflects the latest generated output, and a build-status link that
embeds an older commit sha still resolves to the PR's current diff rather than
404. The page is rendered Azure DevOps-style (dense line-numbered diff table,
GitHub diff palette, sticky header) with a Light / Auto / Dark appearance
switch (persisted per browser; Auto follows the OS). Very large diffs render a
capped, scrollable window first with a "show full output" control that reveals
the rest in place (nothing is dropped; `/raw` is always byte-exact). Every
page names the service explicitly ("acme-diff-preview" wordmark, "ACME Diff
Preview" label) so a reviewer landing here from a build status link never has
to guess which tool posted it:

| Method | Path | Description |
|---|---|---|
| `GET` | `/diff/<repo>/<pr>/<sha>` | Full diff rendered as HTML (everything escaped); serves the PR's latest diff even if `<sha>` is stale |
| `GET` | `/diff/<repo>/<pr>/<sha>/raw` | Exact plain-text body |

**Why the default is on and why that is safe.** The ArgoCD hub Ingress
`extraPaths` forward exactly two paths to this Service, `/jfrog-webhook` and
`/diff-preview/webhook`, never a wildcard (defined in `acme-infrastructure`,
not this chart). Turning `DIFF_UI_ENABLED` on does not add a new externally
reachable path: `/diff/*` is only reachable in-cluster or via
`kubectl port-forward`, exactly like `/diff-preview/stats` already is.
`DIFF_UI_BASE_URL` stays empty by default, so the Bitbucket build status
keeps linking to the comment, never to a host with no access control in
front of it.

**Reaching it from a browser, behind SSO.** This is a SEPARATE, explicit
opt-in: `diffUi.ingress.enabled` (default `false`). When set, the chart
renders a second Service, `<release>-acme-diff-preview-ui`, selecting the
exact same pods on the exact same port, with a GKE `BackendConfig`
(`cloud.google.com/backend-config` annotation) enabling Google
Identity-Aware Proxy on that Service only. The primary Service (the
webhooks) is never touched, since JFrog and Bitbucket authenticate with an
HMAC signature and could never complete an interactive Google login. This
mirrors how ArgoCD itself is protected here (`argocd-dex-server` + Google
OAuth, COPS-2479): the same Google identity gates this page.

Turning it on is simpler than it sounds. GKE 1.29.4-gke.1043000+ supports
IAP with a Google-managed OAuth client, and this cluster runs 1.35.x
(verified live). The default path needs just:

1. Set `diffUi.ingress.enabled: true`. No custom OAuth client, no secret
   to provision, GKE manages the client itself.
2. Grant access to real people/groups: Cloud Console → Security →
   Identity-Aware Proxy → select this backend → Add principal →
   "IAP-secured Web App User". This step is unavoidable either way, it is
   the actual access-control layer, independent of the OAuth client.
3. Wire the new `<release>-acme-diff-preview-ui` Service into the hub
   Ingress' host/path rules and the TLS certificate for that host: both
   live in `acme-infrastructure`, tracked as follow-up work in the ticket,
   not in this chart.

A custom OAuth client is also supported, for orgs that specifically need
one instead of the Google-managed client: set both
`secrets.iapOauthClientIdKey` and `secrets.iapOauthClientSecretKey` (both
empty by default) to GCP Secret Manager key names, which makes the chart
create the `ExternalSecret` that fills the `BackendConfig`'s
`oauthclientCredentials`. Leaving either one empty keeps the
Google-managed path.

One project-level prerequisite either path shares, per Google's own docs:
the GKE service agent needs the `compute.backendServices.update` IAM
permission. This is granted automatically on almost every GCP project
(it is part of the default `Kubernetes Engine Service Agent` role) and is
project-level IAM, not something this chart can set or verify; if IAP
enablement silently does not take effect, check this first.

If step 3 is not done yet while `diffUi.ingress.enabled` is `true`, the
BackendConfig and Service simply exist unused; nothing else in the
cluster, and no other Application, is affected. Once the host is live, set
`DIFF_UI_BASE_URL` to it (e.g. `https://acme-diff-preview.appspace.com`, the
same `acme-diff-preview` slug this chart already uses for the Service name)
so the Bitbucket build status becomes the permalink to that exact commit's
page, mirroring how the Atlantis commit-status "Details" link opens the
full plan output. The comment stays as the summary either way. Storage v1
is a bounded local directory (atomic writes, oldest-pruned); a durable GCS
backend is separate follow-up work tracked in the ticket.

---


---

