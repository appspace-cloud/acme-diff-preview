# acme-diff-preview internals

Reference material moved out of the README to keep the front page short. Nothing
here is required to use the tool; it is for people changing it or debugging it.

## Contents

- [Why an empty `microservices.definitions` is blocked](#why-an-empty-microservicesdefinitions-is-blocked)
- [Handling mass version bumps](#handling-mass-version-bumps-hundreds-of-apps-in-one-pr)
- [Which resources make it into the comment body](#which-resources-make-it-into-the-comment-body)
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

### Which resources make it into the comment body

An app can change hundreds of resources, so only a slice of them is printed
with a full diff block. The counts in the headline and in the `N resource(s)
changed` line are always the real totals; the cap only controls what is shown.

Two rules decide the slice:

- **Detection runs on the full list, before any cap.** Deletions and
  replica zeroings are found by `_detect_deleted_resources` and
  `_detect_replicas_zeroed` inside `_package_sections`, on every section.
  A deletion sitting at position 111 of a mass diff is still named
  (the PR-6773 lesson, v2.5.26).
- **Risk sections get a reserved share of the display budget**
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

### Superseding an in-flight render

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
`supersedes_triggered`, `last_received_at`, plus `hmac_strict` and
`supersede_enabled`), and `_diff_stats` tracks whether each iteration was
started by a webhook or by the safety-net tick. That ratio is what catches the
failures no unit test can: the hook deleted or disabled in Bitbucket, the URL
changed, an ingress rule dropping the POST, or the secret drifting out of sync
after a rotation. In all of those the code is perfectly correct and the
service is quietly running on the 60s poll.

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
evaluated, per-outcome breakdown), and serves it on the health port. Like the
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

