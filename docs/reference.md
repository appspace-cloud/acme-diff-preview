# acme-diff-preview

![CI](https://github.com/appspace-cloud/acme-diff-preview/actions/workflows/ci.yml/badge.svg)
![Release](https://github.com/appspace-cloud/acme-diff-preview/actions/workflows/release.yml/badge.svg)
![Coverage](https://raw.githubusercontent.com/appspace-cloud/acme-diff-preview/badges/badges/coverage.svg)
![Version](https://raw.githubusercontent.com/appspace-cloud/acme-diff-preview/badges/badges/version.svg)

**Shows you what ArgoCD is about to change, before you merge.**

Open a PR on a config repo and this service replies with a comment listing every
Kubernetes resource that will change, per environment, as a real diff. The point
is a controlled PR model: nothing reaches a cluster that a reviewer has not seen
first.

```
PR opened on acme-config-*
        │
        ├─ which ArgoCD apps does this PR touch?        (app inventory, refreshed every 5 min)
        ├─ render each app at main         ──┐
        ├─ render each app at the PR sha   ──┴─ diff the two, resource by resource
        │
        └─ one comment on the PR  +  one build status (green / blocked)
```

Two things to know up front:

- **It renders locally.** `helm template` for both sides, diffed in Python. ArgoCD
  is used only to discover which apps exist and how they are configured, never to
  perform the diff. No spoke agent is contacted.
- **It compares desired against desired,** the PR against `main`, not against the
  live cluster. That is accurate here because every ApplicationSet runs with
  `selfHeal: true`, so live state tracks `main` closely (measured 2026-07-31:
  1020 of 1021 apps Synced).

It also runs a second, unrelated job: a **JFrog webhook** that hard-refreshes dev
and QA apps when CI publishes a new chart, so they pick it up past the OCI cache.

---

## Reading a comment

| Signal | Meaning |
|---|---|
| ✅ no manifest changes | Rendered output is byte-identical. Safe. |
| ⚠️ N resource(s) will change | Normal diff. Review it. |
| ❔ diff unavailable | **Not** the same as "no changes". Something failed and the app was NOT evaluated. |
| 🧭 Merge summary | **Always present, always first.** One verdict — ⛔ do not merge / ⚠️ review / ✅ routine — followed by one line per finding: decommissions, deletions, dangerous VM changes, downgrades, zeroed replicas, auto-sync toggles, new environments and the environments jumping version. It is built from the same deterministic facts as the panels below, so it can never disagree with the detail. |
| 🗑️ RESOURCE(S) DELETED | Resources disappear from the rendered output entirely, with **no replacement in this PR**. Sensitive kinds are always listed in full and never truncated away. |
| 🔄 resource(s) RENAMED | Deleted and recreated under a new name in the same PR, so nothing is lost. Typically a name carrying a content hash, or a resource moving to a new identity. These are deliberately kept **out** of the deletion count. |
| 🗑️ ENVIRONMENT DECOMMISSION | A whole environment is being removed. Read the block: by default its workloads are **orphaned, not deleted**. When the deletion is properly phased (cascade armed beforehand per `acme-components` `documentation/delete.md`), the complete redacted manifests the cascade removes are kept on the **full-diff page** for audit; the comment links to them instead of inlining hundreds of lines of rendered YAML. |
| 🆕 New Environment(s) Detected | A brand-new environment is added. The provisioning summary (chart version, resource counts, applications) comes first; the complete redacted manifest is kept on the **full-diff page** behind the build-status link; the comment links to it rather than inlining it. |
| ⏸️/▶️ Auto-sync PAUSED/RESUMED | `appspace.autosync` was toggled. Shown even when it is the ONLY change — no rendered manifest is touched, so the resource diff has nothing to say. |
| 🔒 DECOMMISSION ARMED / 🔓 DISARMED | `appspace.decommission` (and `decommissionPurgeData`) was toggled on a LIVE environment. Different from the block above: nothing is deleted yet, this is the flag that decides what happens the day the folder actually goes. |
| 🔒 DECOMMISSION PHASE 1 | `allowDeletion` was armed on this environment's Linux VMs, and nothing else. The first PR of a teardown, which used to show only the VM danger bullets: this panel carries the same Phase 1/2/3 table as the later phases, so the reviewer can see which steps are done and which are still ahead. |
| ⛔ STOP — teardown flag misspelled | A key that reads as `decommission`, `decommissionPurgeData`, `allowDeletion` or `confirmProdDeletion` but is not one the platform looks up at that depth — a dropped letter, wrong casing, or a VM arming flag under the wrong parent (e.g. `deployLinuxServicesK8s.allowDeletion` with no `defaults`/`svc`/… segment, or `confirmProdDeletion` under a role instead of `defaults`). Helm and the ApplicationSet match the key exactly, so the environment renders byte-identically and the PR would otherwise merge as a routine no-op with the operator believing a phase is done. **This one stops the comment**: the verdict, the key, the file and the rename, and nothing else. There is one correct response and the rest of a review is distance to it, so no diff, no VM panel and no phase table are rendered until the key is fixed. The build goes **FAILED** with the rename in the description, because a green tick outranks a red paragraph for anyone skimming the checks list. The full-diff page is exempt and still holds everything. Also shown on a folder-removal PR, where it explains why Phase 2 reads pending over a file that looks armed. Role-level `svc.allowDeletion` (and the other VM roles) is valid when spelled correctly — only paths the chart ignores are flagged as misplaced. |
| 🖥️ VM infrastructure | **Always present**, in a fixed place, so "did this PR touch VMs?" is answerable without reading anything else: `🖥️🚨 VM INFRASTRUCTURE CHANGES` when something dangerous is found, `🖥️ (routine)` for harmless changes, and an explicit `no changes` line when the domain is untouched. Covers KCC linux-services (`ComputeInstance`, `ComputeDisk`, `ComputeAddress`, snapshot-policy attachments); instance-type and disk-type changes are always highlighted, since both mean destroy-and-recreate. |
| ⬆️ Routine version bump | Several environments taking the same version-only change, folded into one line naming the transition and every environment it covers. Only ever applied to changes that are provably version-only. |
| ✂️ N more changed app(s) omitted | The readability budget folded ordinary diff blocks away. Nothing risk-flagged is ever folded; the link goes to the full-diff page, which always holds everything. |
| ⬆️ N of M changed resource(s) are the version transition | Inside ONE app, every resource whose only changed lines are version noise (image tags, chart labels, version env values, checksum annotations, deploy timestamps) folds behind that single line, which names the transition. Everything else in the same app stays inline, so a real change riding along with a bump cannot hide in it. Deletions, zeroed replicas and VM changes are never folded. |
| ♻️ N more resource(s) change exactly the same lines | One representative hunk is shown once and the identical siblings are named instead of reprinted. A one-line annotation added to hundreds of resources reads as one hunk plus a count. |
| ✂️ N more changed resource(s) omitted | Same readability budget, applied inside one app: ordinary resources past the budget are named instead of inlined. Nothing risk-flagged is ever omitted, and the full-diff page still holds every hunk. |

The one rule the tool never breaks: **a failure is never reported as "no changes".**
If a diff could not be computed, the status says so and the PR is not marked clean.

**Application-level state flags** (COPS-2584): `appspace.autosync` and
`appspace.decommission` change ArgoCD's behaviour for a whole environment
without touching a single template, so they get their own panel — the
headline, shown before even the config-change list — instead of blending
into an ordinary added/removed/changed key. Pausing or resuming, arming or
disarming, all name the environment and every Application it affects.
Resuming a long-paused environment also gets a reminder that the accumulated
diff applies immediately; arming decommission is explicit that nothing is
deleted by that PR alone. See `acme-components` `documentation/` for what each
flag actually does.

**Public cloud is different, on purpose.** A `cl-*` namespace is shared: one
constellation serves many customers from the same microservices, NEGs and
load balancers, so no delete is safe to automate for one of them. The cascade
gate was deliberately never ported there (COPS-2700), which means
`appspace.decommission` is a silent no-op and teardown is operator-driven end
to end. Every public-cloud panel says that reason, not just the mechanism, and
names the constellation rather than the block inside it — `cl-prod-b`, not
`constellation` or `app7` (COPS-2708). The manual checklist adapts to what is
going: removing the `constellation` block takes the shared workloads with it,
so `kubectl delete namespace` is right and the checklist says whose service
that ends; removing a single load-balancer block must **not** touch the
namespace, and the checklist says so instead.

**The teardown phases** (`acme-components`
`documentation/decommission-environment.md`): Phase 1 arms `allowDeletion`,
Phase 2 arms the cascade with `appspace.decommission` (the data purge is a
qualifier on it), Phase 3 removes the environment folder and is the only
destructive one. Phases 1 and 2 may share a PR; Phase 3 must not. Every PR in
the sequence renders the same three-row table with only the marks moving, so
"where am I" is answerable from the comment alone. A row reads `✅ this PR`
for work this diff performs, `✅ done` for work an earlier PR did,
`↩️ undone by this PR` when the diff takes a phase back, and
`⛔ broken by this PR` when the diff arms a phase while removing the config
that phase acts through (COPS-2660).

**Undo is part of the sequence** (COPS-2710). Removing `allowDeletion`,
removing `appspace.decommission` and softening the purge each render the same
table, because a rollback is when someone is recovering from a mistake and
most needs the map. None of them changes the verdict: `delete` going back to
`abandon` is the safe direction, and the table is positional context. Note
that removing `allowDeletion` is a **disarm, not a strip** — helm keeps
rendering every CR, so the COPS-2660 broken-arming panel deliberately does
not fire on it.

Because those flags are matched by exact key, a near miss arms nothing while
looking armed to any reader. `appspace.decomission: true` — one `m` — merged
green on acme-config-prod #4376 and left the following folder-removal PR
about to orphan 473 resources. Misspellings and casing slips of the three
teardown flags now fail the build and stop the comment (COPS-2707).

Apps whose full diff is byte-for-byte identical (a shared ancestor-file
change rolled out the same way to many environments, e.g. removing a
compute-class override across a whole fleet) are grouped: the comment shows
one complete representative diff plus the full list of environments it
applies to, instead of a separate, arbitrarily-truncated copy per app.
Deletions and replica zeroings get a reserved share of the display order, so
the resources the shouty blocks name are the ones you actually see first.
Details in [docs/internals.md](docs/internals.md#which-resources-make-it-into-the-comment-body).

The same idea then runs INSIDE each app, because the most common change in
`acme-config-prod` is a platform version bump to a single environment, and
that renders as one app carrying hundreds of near-identical resource
sections. Version-only sections fold behind one line, identical changes
repeated across many resources are shown once with a count, and whatever is
left is bounded by the readability budget. Needles (anything that is not
provably version noise) are pushed to the front of the display order, so the
one resource that also gained an annotation is visible instead of buried.
The full-diff page behind the build-status link never folds, groups or
omits anything: it stays the complete record for all three config repos
(dev, stage and prod).

**VM infrastructure changes.** GCP virtual machines (the KCC linux-services
resources rendered from `appspace.infra.deployLinuxServicesK8s`) are the
slowest thing on the platform to recover from when a change goes wrong, so
they get a dedicated panel right after the decommission warning, ranked by
severity. Flagged as dangerous: a deletion-policy moving to `delete` or
`deletionProtection` turning off (the next cascade can then really destroy
the VM in GCP), a `machineType` change without parking the VM
(`desiredStatus: TERMINATED`) first, a zone or disk-type change (both
immutable — destroy and recreate), a disk **shrink**, and a VM or
snapshot-policy attachment disappearing from the render. Disk growth, status
transitions and brand-new resources are reported quietly as routine.

Detection runs at **two levels**, because either one alone misses real cases:
the rendered manifests, and the value files themselves. The values level is
what catches a VM change on an environment where the templates do not render
at all — arming `allowDeletion` on an Azure environment produces no manifest
diff whatsoever, and used to show up as a plain green "no manifest changes".

**Scannability budget.** A comment that scrolls for 200KB is not read. Beyond
`COMMENT_READABLE_BYTES` of bulk diff content, ordinary per-app diff blocks
collapse into a single line pointing at the full-diff page, the overview
table caps its rows, and environments taking the same **provably version-only**
change fold into one `⬆️ Routine version bump` line naming the transition
and every environment it covers. Three invariants hold regardless of size:
every panel above the diffs always renders in full, anything risk-flagged
(deletions, downgrades, zeroed replicas, VM changes) is never folded, and the
full-diff page linked from the comment is always rendered with folding
disabled, so it holds everything the comment left out.

**The full-diff page is the complete record.** It is rendered separately from the comment with every budget disabled: no rollups, no collapsed apps, no table row cap, no per-resource body cap, and the configuration panel uncapped so every changed file is listed key by key, file by file. The comment is the summary; this page is the evidence.

Before this was enforced the page was quietly failing that promise: one production artifact (`acme-config-prod` #3887) carried 981 occurrences of `... (diff truncated for display)`, 981 places where the complete record told the reader to look somewhere else. The 6,000-character body cap is a comment protection, since one giant ConfigMap rewrite would otherwise push the comment past Bitbucket's limit and the blunt global cut would chop off the footer. On the page that same cap was only a lie.

Two caps remain, and neither is silent:

- **Visible rows** default to 20,000. Everything past that is still in the HTML, behind a `show full output` button, and `/raw` is always byte-exact. The ceiling is a browser-survival number, not a policy: #3887 is 786,150 lines and renders to 113MB of HTML, so laying out every row on first paint is a hung tab rather than completeness.
- **Stored sections** are bounded by `FULL_SECTIONS_MAX_PER_APP` (5,000) for memory safety, because the full list is retained per app across a whole run. What it drops is missing from *both* surfaces, so hitting it increments `section_cap_trims`, logs a warning, and the page states the shortfall rather than claiming to be complete.

**Rolling back the uncapping.** `FULL_PAGE_UNCAPPED=false` restores the old capped page. Since 2.35.0 the comment no longer carries inline YAML, so set `COMMENT_INLINE_DIFFS=true` **first**, then flip this. The other order leaves the comment without YAML *and* the page truncating, which means the information is simply gone. A rollback switch whose safe order is not written down is a trap.

### Two surfaces, one contract

Since 2.35.0 the service renders two different things from one function, and the split is deliberate:

| | PR comment | Full-diff page |
|---|---|---|
| Purpose | the **decision** | the **evidence** |
| YAML hunks | no | always |
| Config-changes panel | no | yes |
| AI analysis | no | yes |
| Clean applications | one count | named, one per line |
| Verdicts, deletions, VM facts, downgrades, decommissions | **all of them, with names** | all of them |
| Retention | as long as Bitbucket keeps the comment | 365 days from the PR's last diff run |

The rule that makes this safe: **the comment only drops the YAML when there is a page to hold it.** If the artifact could not be written, the comment renders the hunks inline and says why, so a failed save can never produce a comment with no evidence anywhere.

The second rule is subtler and cost three bugs to get right: what moves is *evidence*, never *conclusions*. The comment still tells you that six of seven resources are a version bump only, which the seventh is, which resources are being deleted, and what every guard decided. Only the proof moved.

**Retention.** The local artifact directory is a cache, not the record: `_prune` only walks it, and a pruned entry costs one re-download. The durable copy is the GCS bucket, and its object lifecycle is what actually decides how long a page opens. That lifecycle lives in `acme-infrastructure` (`shared/infrastructure/acme-diff-preview-artifacts`). Because the artifact is rewritten on every commit, the age clock runs from the PR's last diff run, not from when it was opened. A page that is gone says so explicitly instead of 404-ing into a dead end.

**The page is navigable.** An index at the top lists every application with its resource count, collapsed per application so 345 of them are a list rather than a wall, with a filter box that narrows the index as you type. Every application and every resource has a stable anchor, so a link to one resource can be pasted into a ticket: `.../diff/acme-config-prod/3899/<sha>#app-pv-bos-b-ms--compute-cnrm-cloud-google-com-computeinstance-pv-bos-svc-a`. Anchors are scoped by application because resource names repeat across environments, and an entry pointing into the collapsed overflow reveals it before jumping.

The index is derived from the markers the comment renderer already emits, not from a change to the stored artifact, so older artifacts stay readable and `/raw` is untouched. Anything the parser does not recognise still renders exactly as before: the page never loses a line to gain an index. The page still ships zero external assets, and every value on it, including anchor ids and index labels, is escaped or sanitised, because all of it is PR-controlled content.

**Every comment links to it, unconditionally.** The pointer renders twice, in
two fixed places: under the commit header, and again immediately above the
`Status:` line, because on a long comment the header has scrolled away. Until
now every link to the page was conditional (a truncation note, a per-app
"full hunks" pointer, a rollup line), so a comment where nothing was truncated
and nothing was folded had no way to reach the page at all.

If the page could not be written for a run, the comment says so in that same
place and keeps the hunks inline, rather than offering a link that 404s. That
fallback is checked, not assumed: the save reports whether it succeeded and the
comment is re-rendered before it is posted.

**Two render profiles.** `COMMENT` and `FULL` are what separate the two
surfaces: how much folding, grouping, and capping each one does. They are
values (`RenderProfile`) rather than a magic integer threaded through the
renderer, so a surface's behaviour can be read in one place. The older
`readable_budget=` argument still works and maps onto a profile.

## How the diff works

ArgoCD is used **only** for discovery: at startup, and every 5 minutes, one
`argocd app list` builds an in-memory map of each app's chart, target revision,
OCI registry, value files and namespace. Then, per affected app:

1. `helm pull` the chart for both the PR and the `main` version (cached per pod).
2. Read the app's value files at both shas, from a **local git mirror** of the
   config repo (COPS-2564). One `git fetch` per repo per iteration replaces what
   used to be one Bitbucket API call per file.
3. `helm template` each side and diff the rendered YAML, resource by resource.

Typical latency is 4 to 6 seconds per app with a warm chart cache. If the PR bumps
`appspace.version`, the new chart version is used for the PR side, so the diff
shows the real image changes.

**Which repos:** set by `DIFF_REPOS` (`diff.repos`), e.g.
`acme-config-dev;acme-config-stage;acme-config-prod`. The path map is partitioned
per repo, so a PR can only ever match and comment on its own repo. An optional
`:scope` suffix (`acme-config-stage:gcp/`) hides a tree entirely; a PR with no
in-scope files is skipped silently rather than getting a "no apps affected" reply.

---

## Merge-blocking guards

Beyond rendering diffs, the service also **blocks** a PR from merging (red
`FAILED` build status + an explanatory comment) when it detects a change that
is known to break an environment on merge. These are structural checks, run
before any diff, that no rendered diff would make obvious to a reviewer.

| Guard | What it catches | Why it is dangerous |
|---|---|---|
| Structural new-env failure | a new environment missing a required value (e.g. `appspace.version`) | the environment cannot render at all on merge |
| **Empty `microservices.definitions`** | a value file (typically `cicd-versions.yaml`) with `appspace.microservices.definitions` present but **null/empty** | silently deletes every microservice on merge ([details](docs/internals.md#why-an-empty-microservicesdefinitions-is-blocked)) |

**The red status names the failure, not its category** (COPS-2709). Bitbucket
shows the description and nothing else, so it is the whole message for anyone
who reads the checks list instead of opening the comment. A missing required
value, a schema violation, a template blowing up and a name over 63 characters
used to share one line, `N app(s): invalid config`, which named none of them.
Each now carries the same short error the comment headline has used since
COPS-2676, plus the environments and the action:

```
Missing Image Tag on => platform — pv-uwm-a — fix and push
at '/microservices/definitions/a': got string, want object — pv-uwm-a — fix and push
appspace-ms:2603.9.9 not found — pv-uwm-a — check the version or wait for the registry
```

A chart version missing from the registry gets a different action on purpose:
it is self-resolving, the poll loop keeps retrying it (COPS-2696), and telling
the author to fix and push would send them to change a version that is
probably correct. Apps failing the same way are grouped, so a fleet PR reads
as one problem with an environment count rather than fifty lines.

See [docs/internals.md](docs/internals.md) for the reasoning behind each guard,
how mass version bumps are handled, the secret-leak hardening, and the
full-diff web UI.

---

### Tuning knobs (env vars / Helm values)

| Env | Helm value | Default | Purpose |
|---|---|---|---|
| `MAX_APPS_PER_RUN` | `diff.maxAppsPerRun` | `1500` | Hard cap on apps diffed per PR. Crossing it blocks the merge (over-cap apps are not evaluated, so the status is `FAILED` rather than a false green). Sized at roughly 1.7x the largest fleet; `/diff-preview/stats` publishes `max_affected_apps_seen` beside `max_apps_per_run` so the remaining headroom is visible before it runs out |
| `DIFF_WORKERS` | `diff.workers` | `16` | Parallel per-app diffs within one PR |
| `PR_WORKERS` | `diff.prWorkers` | `3` | PRs processed in parallel |
| `DIFF_TIMEOUT` | `diff.timeout` | `120` | Seconds per diff step |
| `DIFF_RETRIES` | `diff.retries` | `5` | Attempts per diff (backoff + jitter) |
| `WARM_WORKERS` | `diff.warmWorkers` | `4` | Parallel chart warm-up pulls |
| `WARM_THRESHOLD` | `diff.warmThreshold` | `8` | Min apps before warm-up kicks in |
| `SUPERSEDE_ABORT_ENABLED` | `diff.supersedeAbortEnabled` | `true` | Let a newer push abort a render already in flight ([details](docs/internals.md#superseding-an-in-flight-render)). Off = the pre-COPS-2575 behaviour, a dead commit is rendered to completion and published |
| `SUPERSEDE_MAX_CONSECUTIVE_ABORTS` | `diff.supersedeMaxConsecutiveAborts` | `3` | Livelock guard: after this many consecutive aborts on one PR, the run finishes regardless so the PR always eventually gets a comment |
| `KUBE_VERSION` | `kubeVersion` | `1.35.5` | `--kube-version` passed to `helm template`. Matches the real GKE clusters; keep it in step with them |
| `BB_API_CONCURRENCY` | — | `30` | Max concurrent Bitbucket API calls |
| `BB_RATELIMIT_FALLBACK` | — | `15` | Shared pause after a 429 that carries no `Retry-After`. Sized to Bitbucket's ~60s window, not to a single retry |
| `BB_RATELIMIT_MAX_PAUSE` | — | `60` | Cap on the shared pause, so a broken `Retry-After` cannot stall a PR |
| `HELM_CACHE_MAX_CHARTS` | — | `60` | Max pulled chart versions kept on disk |
| `AI_MAX_APPS` | — | `40` | Max changed apps included in the AI summary prompt |
| `FULL_SECTIONS_MAX_PER_APP` | — | `5000` | Max changed resources **stored** per app, shared by the comment, the diff-group fingerprint and the full-diff page. A memory bound, not a display cutoff: what it drops is gone from both surfaces, so hitting it increments `section_cap_trims`, logs a warning, and makes the page state the shortfall instead of claiming completeness |
| `COMMENT_READABLE_BYTES` | — | `30000` | Readability budget for the **bulk** region of a comment. Past it, ordinary diff blocks fold into a pointer at the full-diff page and the overview table caps its rows. Panels and risk-flagged apps are never affected, and the full-diff page itself is always rendered with folding off. `0` disables folding entirely |
| `DIFF_OCI_SELFCHECK_INTERVAL` | — | `900` | Seconds between periodic OCI self-checks (`helm show chart` against a known-good ref). First check ~60s after start; `0` disables. Result in `/diff-preview/stats` as `oci_selfcheck` |
| `DIFF_OCI_SELFCHECK_REF` | — | *(last successful pull)* | Optional fixed reference `registry/chart:version` for the self-check |
| `DIFF_OCI_FAIL_ERROR_THRESHOLD` | — | `3` | Consecutive systemic chart-pull failures after which failures log at ERROR instead of WARNING |
| `DIFF_IGNORE_RESOURCES` | — | *(empty)* | Extra comma-separated resource-name substrings to hide from every diff, on top of the built-in `micro-versions-info` |
| `DIFF_HTTP_POOLING` | — | `on` | HTTP keep-alive pooling: one persistent TLS connection per worker thread and host. `off` routes every request through plain `urlopen`. Auto-defers to `urlopen` when a proxy is configured. Visible in `/diff-preview/stats` (`http_pool_reuses`/`http_pool_fresh_conns`/`http_pool_fallbacks`) |
| `DIFF_UI_ENABLED` | `diffUi.enabled` | `true` | Full-diff web UI ([details](docs/internals.md#full-diff-web-ui-atlantis-style)). Persists artifacts + serves `/diff/*` in-cluster; safe by default since no ingress path exposes it externally |
| `DIFF_UI_BASE_URL` | `diffUi.baseUrl` | *(empty)* | External base URL the build status deep-links to. Empty = status keeps linking to the comment |
| `DIFF_UI_DIR` | `diffUi.dir` | `/tmp/acme-diff-ui` | Artifact directory (bounded, pruned oldest-first) |
| `DIFF_UI_MAX_ARTIFACTS` | `diffUi.maxArtifacts` | `500` | Max artifacts in the **local cache** before pruning oldest-first. Not a retention policy: `DIFF_UI_GCS_BUCKET` is the durable copy and its bucket lifecycle sets how long a page really lives |
| `DIFF_UI_MAX_BYTES` | `diffUi.maxBytes` | `419430400` (400MiB) | Byte budget for the same local cache, enforced alongside the count. The directory is an emptyDir whose `sizeLimit` the kubelet enforces by **evicting the pod**, and artifacts range from ~2KB to tens of MB, so a count alone is measured in the wrong unit. A pruned entry costs one GCS re-download |
| `MAIN_RENDER_GCS_BUCKET` | `mainRenderCache.gcsBucket` | *(empty — tier off)* | Durable tier for the main-side render cache. Lookup is memory -> disk -> bucket -> render, so a replacement pod or a standby taking the lease warms from the bucket instead of re-rendering the fleet. Both local tiers live in an emptyDir that dies with the pod. **This bucket holds RAW renders, including cleartext `kind: Secret` values** — redaction is display-time by design, so its IAM is a security boundary. It must be a dedicated bucket: setting it to `DIFF_UI_GCS_BUCKET` (which is classified redacted-only) is refused and disables the tier. It used to inherit that bucket silently, which is the defect COPS-2668 removed. Empty disables the tier; writes are best-effort and off the diff path |
| `MAIN_RENDER_GCS_PREFIX` | — | `render-cache` | Object prefix, salt appended: `render-cache/<salt>/<key>.yaml.zst`. The bucket lifecycle deletes this prefix at 14 days; the entries are disposable, losing one costs a single render |
| `MAIN_RENDER_CACHE_MAX` | — | `2048` | In-process front cache size. Eviction is **LRU** and drops the memory entry only: disk owns its own caps and keeps serving evicted keys |
| `MAIN_RENDER_CACHE_SALT` | — | `cops2631-v1` | CacheVersion equivalent. Bump on any render-affecting code change: it is part of the content key **and** of the bucket object name, so a bump orphans every durable copy rather than serving it |
| `MAIN_RENDER_CACHE_SHADOW_RATE` | — | `0.01` | Fraction of cache hits that re-render and byte-compare. A mismatch discards the entry from **every** tier, bucket included, so a poisoned object cannot re-infect fresh pods, and increments `main_render_cache_shadow_mismatches` (must stay 0) |
| `DIFF_OCI_SELFCHECK_REF` | `ociSelfCheckRef` | *(empty)* | Chart reference (`registry/chart:version`) for the OCI self-check, used **only before any pull has succeeded**. The last real successful pull takes precedence because it is current by definition. Without it the self-check reports `skipped` for the whole duration of a credentials outage - exactly the incident it exists to detect |
| `COMMENT_INLINE_DIFFS` | — | `false` | Render the YAML hunks inside the PR comment. `false` (the default since 2.35.0) makes the comment a **decision summary**: verdicts, names and counts stay, the evidence lives on the full-diff page. `true` restores the pre-2.35.0 comment in one variable, and is also the **first** step of a phase C rollback — see below |
| `COMMENT_INPUT_PANEL` | — | `false` | Render the `Config changes in this PR` panel in the comment. Off by default; the page always keeps it |
| `COMMENT_INLINE_EVIDENCE_LINES` | — | `0` | With inline diffs off, still show this many lines of evidence for **risk-flagged** applications only (deletions, zeroed replicas, VM facts), so a reviewer never leaves the comment to see *why* something is dangerous — only to see the rest. `0` means the comment ships with no fenced block at all |
| `FULL_PAGE_UNCAPPED` | — | `true` | The full-diff page never cuts a resource body. `false` restores the pre-2.33.0 page (bodies cut at 6,000 chars with a marker). **Rollback order matters, see below** |
| — | `diffUi.ingress.enabled` | `false` | Externally reachable, IAP-gated Service + BackendConfig for the UI ([details](docs/internals.md#full-diff-web-ui-atlantis-style)) |



## Repository layout

```
acme-diff-preview/
├── src/
│   ├── diff_preview.py        Main service (Deployment)
│   ├── diff_ui.py             Full-diff artifact store + web UI (stdlib only)
│   ├── leader.py              Lease-based leader election (stdlib only)
│   ├── redact.py              Display-time redaction of sensitive values (stdlib only)
│   ├── vocabulary.py          Diff outcome and failure-reason vocabulary (stdlib only)
│   ├── comment_render.py      Merge summary and comment formatting helpers (stdlib only)
│   └── dev_hard_refresh.py    Full sweep of the mutable-tag projects (opt-in CronJob)
├── tests/                     Full pytest suite, 100% coverage of src/ — see "Tests" below
├── charts/
│   └── acme-diff-preview/     Helm chart
│       ├── Chart.yaml
│       ├── values.yaml
│       └── templates/
│           ├── deployment.yaml
│           ├── service.yaml
│           ├── ui-service.yaml         IAP-fronted Service (diffUi.ingress.enabled)
│           ├── ui-backendconfig.yaml   GKE BackendConfig enabling IAP on it
│           ├── ui-iap-externalsecret.yaml  IAP OAuth client creds
│           ├── serviceaccount.yaml
│           ├── externalsecret.yaml
│           └── cronjob.yaml
├── docs/
│   └── runbooks/
│       └── jfrog-webhook-secret-rotation.md
├── Dockerfile                 python:3.12-slim + argocd CLI + helm CLI
├── RELEASING.md               How to cut a release (read before pushing tags)
└── .github/workflows/
    ├── ci.yml                 PR gate: tests, helm lint, docker build (no push)
    ├── release.yml            Push to main: publish Helm chart to GitHub Pages
    └── docker.yml             Push of v* tag: build + push image to JFrog
```

### Splitting the service module

`diff_preview.py` is being reduced from one large file into cohesive
sibling modules, one extraction per release. Two rules hold for every step:

- **The arrow points one way.** An extracted module never imports
  `diff_preview` back. `tests/test_module_surface.py` fails the build if one
  does.
- **The hub keeps the name.** Whatever moves out is imported straight back
  in, so `diff_preview.<name>` still resolves for the roughly a thousand
  `monkeypatch.setattr` calls in the suite and for anything else addressing
  the service through that module.

The second rule matters more than it looks. Patching a name only affects
callers that resolve it through the same namespace, so moving a *caller*
into a new module silently disconnects the patch: the test stays green and
stops testing anything. `scripts/audit_seams.py` compares every patched
name against the modules that read it and fails when a patch can no longer
reach a caller. It runs in the suite, and standalone for a readable report:

```bash
python3 scripts/audit_seams.py
```

---

## Tests

```bash
python3 -m pytest tests/ -q                                  # full suite
python3 -m pytest tests/ -q --cov=src --cov-report=term      # with the coverage report
```

**Coverage: 100% of `src/`.** All tests run with **zero external infrastructure**
(no cluster, no Bitbucket account, no registry, no real helm) — every external
boundary (`http()`, `helm`, `argocd`) is faked or stubbed locally, so the suite
runs identically on any machine and in CI. The handful of excluded lines are
genuinely unreachable defensive guards, each documented inline with a
`# pragma: no cover` and a one-line reason, plus the standard
`if __name__ == "__main__":` entry guards.

The suite is organized in layers, roughly in the order they were built:

- **Core unit tests** (`test_diff_preview.py`) — the pure logic: diff parsing
  and filtering, secret redaction, comment formatting, rename/decommission/
  tier-move classification, the traffic-light coherence rules.
- **Per-release regression suites** (`test_v2*.py`) — each hardening release
  ships with the test that first reproduced its bug (red) and now guards it
  (green): webhook memory caps, HMAC crashes, rename following, downgrade
  visibility, HTTP pooling, and so on.
- **Synthetic-infrastructure layer** (`test_coverage_*.py`) — the previously
  live-only surface made deterministic: the real retry/backoff HTTP engine
  against a local stub server, both webhooks driven with genuinely
  HMAC-signed requests, ArgoCD/JFrog/helm CLIs replaced by fake binaries, and
  — the centerpiece — `process_pr`, `main_iteration`, and `_run_one_diff`
  walked end to end through scripted PR worlds (happy diffs, dedup,
  downgrades, new environments, timeouts, cancellation, mass-PR caps).
- **Coverage sweeps** (`test_coverage_deep_branches.py`, `_last_mile.py`,
  `_precision.py`, and `test_v264_coverage_*.py`) — closed the remaining
  error/fallback branches one per-line gap analysis at a time, finishing at
  100%. Along the way this process also caught and fixed a handful of real
  test bugs (assertions checking the wrong path, a stale cache key that
  silently skipped its own target) — a reminder that a green assertion only
  proves what it checks.

The discipline that keeps this trustworthy is in [RELEASING.md](RELEASING.md):
every change ships with a regression test written first and confirmed
failing, then the full suite, then live pod verification.

---


---

## HTTP endpoints

All endpoints are served on port **8080** inside the pod.

| Method | Path | Description |
|---|---|---|
| `POST` | `/diff-preview/webhook` | Bitbucket PR webhook (wakes the diff loop) |
| `GET` | `/diff-preview/stats` | Diff outcome counters, main-render cache hits split by tier (memory/disk/gcs) and misses, last iteration timing (JSON) |
| `POST` | `/jfrog-webhook` | JFrog OCI push webhook (triggers hard-refresh) |
| `GET` | `/jfrog-webhook/stats` | Webhook counters (JSON) |
| `GET` | `/healthz` | Liveness probe |
| `GET` | `/readyz` | Readiness probe |
| `GET` | `/diff/<repo>/<pr>/<sha>` | Full untruncated diff as HTML (404 unless `DIFF_UI_ENABLED`) |
| `GET` | `/diff/<repo>/<pr>/<sha>/raw` | Full untruncated diff as plain text (404 unless `DIFF_UI_ENABLED`) |

### JFrog webhook security

Every request to `/jfrog-webhook` must include an `X-JFrog-Event-Auth` header
with an HMAC-SHA256 signature computed from the request body using the shared
secret stored in GCP Secret Manager. Requests without a valid signature are
rejected with HTTP 401. Bodies over 64 KB are rejected with HTTP 413 before
the signature is even checked.

The stats endpoint returns something like:

```json
{
  "received": 42,
  "rejected_hmac": 1,
  "rejected_format": 0,
  "dedup_skipped": 3,
  "refreshes_ok": 87,
  "refreshes_failed": 0,
  "started_at": "2026-06-25T10:00:00+00:00"
}
```

---


---

## Docker image

Images are pushed to JFrog and pulled by GKE through the GAR remote proxy in
`appspace-devops`. No `imagePullSecrets` are needed — the node service account
already has IAM access to Artifact Registry.

| Registry | URL |
|---|---|
| Source (JFrog) | `docker-dev.repo.appspace.com/acme-diff-preview:<tag>` |
| GKE pull URL (GAR proxy) | `us-central1-docker.pkg.dev/appspace-devops/artifact/acme-diff-preview:<tag>` |

---

## Helm chart

Published to GitHub Pages on every merge to `main`:

```
https://appspace-cloud.github.io/acme-diff-preview
```

### High availability (2+ replicas)

Set `replicaCount: 2` and the service survives pod loss, node drains and
rolling deploys with no availability gap:

- The diff web UI and the webhooks are active-active: every replica serves
  them at all times. Artifacts live in the GCS bucket (`diffUi.gcsBucket`),
  so any replica can serve any diff page.
- The PR poll loop stays a singleton: only the replica holding a Kubernetes
  Lease runs it (`leaderElection.enabled`, on by default). The standby takes
  over in seconds when the leader dies, and instantly on a clean shutdown
  (the leader releases the lease on SIGTERM). `GET /diff-preview/stats`
  exposes `is_leader` per replica. Iteration-trigger counters are
  role-aware: `iters_webhook_triggered` and `iters_safetynet_triggered`
  count only on the leader, while a standby's short 5s handoff polls count
  in `iters_standby_wait`. The safety-net-to-webhook ratio (the
  webhook-health signal) is therefore meaningful on any pod, with no
  `is_leader` filter needed.
- The chart ships the rest of the disruption armor: `RollingUpdate` with
  `maxUnavailable: 0`, a PodDisruptionBudget (rendered only above one
  replica), node and zone topology spread, a `preStop` sleep for clean
  endpoint drain, and the Lease RBAC for the pod's ServiceAccount.

With one replica everything above is inert and behavior is exactly the
single-instance one: the pod trivially elects itself.

---

## CI/CD

| Trigger | What runs |
|---|---|
| PR to `main` | Tests + coverage report, helm lint, docker build (no push) |
| Push to `main` | Helm chart published to GitHub Pages |
| Tag `v*` | Docker image built and pushed to JFrog |

See [RELEASING.md](RELEASING.md) for the full release process and the rule
about never overwriting an existing image tag.

### GitHub Actions secrets required

| Secret | Value |
|---|---|
| `JFROG_USER` | `acme-repo` |
| `JFROG_PASSWORD` | GCP SM secret `acme-repo-password` in `appspace-devops` |

---

## Installation

Deployed via Terragrunt in `acme-infrastructure`:

```
deployments/appspace-com/gcp/appspace-devops/shared/infrastructure/gke/na1-a/config/terragrunt.hcl
```

That file is the source of truth for the live `image.tag` and `diff.repos` —
don't rely on the version numbers below, which are only illustrative:

```yaml
image:
  repository: us-central1-docker.pkg.dev/appspace-devops/artifact/acme-diff-preview
  tag: "<pinned in acme-infrastructure, kept in sync with Chart.yaml appVersion>"

# Set to DEBUG to log full per-step diff detail.
logLevel: INFO

# Kubernetes version helm renders against (--kube-version).
kubeVersion: "1.35.5"

argocd:
  server: argocd.appspace.com
  username: diff-preview

bitbucket:
  workspace: appspace-cloud
  repo: acme-config-dev   # legacy value, superseded by diff.repos (multi-repo)

vertex:
  project: appspace-devops
  location: us-central1
  model: gemini-2.5-flash

hardRefresh:
  enabled: false                # webhook-only by default; see above
  schedule: "0 6 * * *"
  projects: "appspace-dev,appspace-qa,appspace-stage"
  workers: 3

secrets:
  externalSecretStore: argocd-gcp-sm
  bbUserKey: argocd-diff-preview-bb-user
  bbTokenKey: argocd-diff-preview-bb-token
  argocdPassKey: argocd-diff-preview-admin-pass
  jfrogWebhookSecretKey: argocd-jfrog-webhook-shared-secret
  # OCI credentials are REQUIRED for the helm-template diff.
  ociUserKey: acme-repo-username
  ociPassKey: acme-repo-password

serviceAccount:
  annotations:
    iam.gke.io/gcp-service-account: argocd@appspace-devops.iam.gserviceaccount.com
```

The ArgoCD Ingress `extraPaths` for `/diff-preview/webhook` and
`/jfrog-webhook` are configured in the ArgoCD Helm values block inside
`acme-infrastructure`.

---

## GCP Secret Manager keys

All secrets are in the `appspace-devops` project.

| Secret | Used for |
|---|---|
| `argocd-diff-preview-bb-user` | Bitbucket username |
| `argocd-diff-preview-bb-token` | Bitbucket app password |
| `argocd-diff-preview-admin-pass` | ArgoCD `diff-preview` account password (plaintext) |
| `argocd-diff-preview-password` | Bcrypt hash for the ArgoCD accounts config |
| `acme-repo-username` | JFrog OCI username (`OCI_USER`) for `helm pull` |
| `acme-repo-password` | JFrog OCI password (`OCI_PASS`) for `helm pull`, GAR proxy, CI |
| `argocd-jfrog-webhook-shared-secret` | HMAC key for the JFrog webhook endpoint |
| `argocd-diff-preview-bb-webhook-secret` | HMAC key for the Bitbucket webhook endpoint (`BB_WEBHOOK_SECRET`), injected via the `secrets.bbWebhookSecretKey` chart value |

`OCI_USER` / `OCI_PASS` are **required** for the diff to work: without them every
`helm pull` fails and the comment shows "diff unavailable" for every app. The pod
logs an ERROR at startup when `OCI_PASS` is empty.

`BB_WEBHOOK_SECRET` protects the Bitbucket webhook endpoint but is **not required**
to run: `_verify_bb_hmac` falls back to permissive mode (accepts unsigned requests)
when it's empty, for backward compatibility during rollout. `secrets.bbWebhookSecretKey`
defaults to `""` in `values.yaml` (unlike `secrets.ociPassKey`, which ships with a real
default) — the pod logs a WARNING at startup when it's empty, so a broken or
never-wired ExternalSecret is visible in logs rather than silently reopening the
webhook to unsigned requests indefinitely.

To rotate the JFrog webhook secret, follow the runbook at
`docs/runbooks/jfrog-webhook-secret-rotation.md`.
