# acme-diff-preview

![CI](https://github.com/appspace-cloud/acme-diff-preview/actions/workflows/ci.yml/badge.svg)
![Release](https://github.com/appspace-cloud/acme-diff-preview/actions/workflows/release.yml/badge.svg)
![Coverage](badges/coverage.svg)
![Version](badges/version.svg)

ACME Diff Preview service for Appspace. A long-running Kubernetes Deployment
that does two distinct jobs:

1. **PR diff comments** — watches Bitbucket PRs on the configured config repos
   and, for every affected app, renders the chart with `helm template` for
   both the PR and the `main` revision, diffs the two locally, and posts a
   formatted comment with a Vertex AI Gemini summary. Multi-repo (COPS-2507):
   the path map is partitioned by each Application's git source, so a PR can
   only ever match, fetch from, and comment on its own repo. Repos are
   configured via `DIFF_REPOS` (chart value `diff.repos`), e.g.
   `acme-config-dev;acme-config-stage`. Production runs both `acme-config-dev`
   and `acme-config-stage` with no scope restriction — every ArgoCD app in
   the repo is reachable, GCP or Azure alike, and any tree the repo has that
   ArgoCD does not manage (e.g. a legacy-pipeline path) simply matches zero
   apps and gets the "No ArgoCD apps affected" comment rather than a diff.
   `acme-config-prod` isn't wired in yet — pending its own ArgoCD onboarding.
   An optional `:scopes` suffix on a repo entry (e.g. `acme-config-stage:gcp/`)
   can still fully hide an in-repo tree regardless of app matching, if a repo
   ever needs that; a PR with zero in-scope files is then skipped in full
   silence instead of getting the "no apps affected" comment. New-environment
   evaluation resolves `appspace.version` through the config.yaml hierarchy
   at the PR sha, since most environments inherit it from a cohort-level file.

2. **JFrog OCI webhook** — receives push events from JFrog when CI publishes
   a new Helm chart to `helm-oci-dev`, finds every dev/QA ArgoCD app tracking
   that chart version, and hard-refreshes them to bypass the OCI cache.

A CronJob runs a full hard-refresh of all dev/QA apps every 30 minutes as a
fallback safety net.

---

## How the diff works (pure helm template, no agent round-trips)

ArgoCD is used **only** for discovery: at startup (and every 5 min) a single
`argocd app list` builds an in-memory map of each app's chart name, target
revision, OCI registry, value files and namespace. The diff itself never touches
a spoke agent. For each affected app the service:

1. `helm pull oci://<registry>/<chart> --version <X> --untar` for both the PR and
   the `main` chart version (cached locally, pulled once per pod lifetime).
2. Fetches the app's value files from Bitbucket at the PR sha and the main sha.
3. Runs `helm template` for each side and diffs the rendered YAML resource by
   resource in Python.

This is entirely local. Typical latency is ~4-6s/app with a warm chart cache vs
20-360s when diffs went through the agents. When the PR bumps `appspace.version`
(the OCI chart `targetRevision`), the new version is read from the PR config file
and used for the PR render so the diff shows the real image changes.

## Diff outcomes and debugging

Every diff resolves to one of these outcomes:

| Outcome | Meaning | PR comment |
|---|---|---|
| `diff` | The rendered manifests differ | ⚠️ N resource(s) will change |
| `no_diff` | Manifests match (or only noise/checksum changes) | ✅ No manifest changes |
| `indeterminate` | The diff could **not** be computed | ❔ diff unavailable (reason) |
| `error` | Unexpected per-PR exception | ❌ error |

`indeterminate` is the important one: it is **never** rendered as a green
"no changes". Each indeterminate carries a short reason (set directly by
`_run_one_diff`, no stderr guessing). The full detail is in the pod logs at
`LOG_LEVEL=DEBUG`:

| Reason | Retry? | Cause |
|---|---|---|
| `oci_not_found` | no (permanent) | the chart version does not exist in the registry — posts a **FAILED** build status because the deployer would fail the same way |
| `oci_pull_failed` | yes | `helm pull` / `helm registry login` failed (network or credentials) |
| `metadata_pending` | yes | the app was added since the last 5-min discovery refresh |
| `render_failed` | no (soft) | `helm template` failed to render the chart with these values |
| `timeout` | yes | a pull/fetch/render step exceeded `DIFF_TIMEOUT` |

Only `oci_not_found` blocks the PR. Every other reason is a soft "diff
unavailable" (build status stays SUCCESSFUL so a transient blip never blocks a
merge), and the PR is left **un-seen** so the next loop re-evaluates it — once
the OCI/Bitbucket path recovers the comment flips to the real diff.

To see exactly why a diff failed:

```bash
kubectl -n argocd logs deploy/acme-diff-preview | grep '"outcome"'
# or, for full per-step detail, set logLevel: DEBUG in the Helm values
```

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

### Tuning knobs (env vars / Helm values)

| Env | Helm value | Default | Purpose |
|---|---|---|---|
| `MAX_APPS_PER_RUN` | `diff.maxAppsPerRun` | `800` | Hard cap on apps diffed per PR |
| `DIFF_WORKERS` | `diff.workers` | `16` | Parallel per-app diffs within one PR |
| `PR_WORKERS` | `diff.prWorkers` | `3` | PRs processed in parallel |
| `DIFF_TIMEOUT` | `diff.timeout` | `120` | Seconds per diff step |
| `DIFF_RETRIES` | `diff.retries` | `5` | Attempts per diff (backoff + jitter) |
| `WARM_WORKERS` | `diff.warmWorkers` | `4` | Parallel chart warm-up pulls |
| `WARM_THRESHOLD` | `diff.warmThreshold` | `8` | Min apps before warm-up kicks in |
| `KUBE_VERSION` | `kubeVersion` | `1.30.0` | `--kube-version` passed to `helm template` |
| `BB_API_CONCURRENCY` | — | `30` | Max concurrent Bitbucket API calls |
| `HELM_CACHE_MAX_CHARTS` | — | `60` | Max pulled chart versions kept on disk |
| `AI_MAX_APPS` | — | `40` | Max changed apps included in the AI summary prompt |
| `DIFF_OCI_SELFCHECK_INTERVAL` | — | `900` | Seconds between periodic OCI self-checks (`helm show chart` against a known-good ref). First check ~60s after start; `0` disables. Result in `/diff-preview/stats` as `oci_selfcheck` |
| `DIFF_OCI_SELFCHECK_REF` | — | *(last successful pull)* | Optional fixed reference `registry/chart:version` for the self-check |
| `DIFF_OCI_FAIL_ERROR_THRESHOLD` | — | `3` | Consecutive systemic chart-pull failures after which failures log at ERROR instead of WARNING |
| `DIFF_IGNORE_RESOURCES` | — | *(empty)* | Extra comma-separated resource-name substrings to hide from every diff, on top of the built-in `micro-versions-info` |
| `DIFF_HTTP_POOLING` | — | `on` | HTTP keep-alive pooling: one persistent TLS connection per worker thread and host. `off` routes every request through plain `urlopen`. Auto-defers to `urlopen` when a proxy is configured. Visible in `/diff-preview/stats` (`http_pool_reuses`/`http_pool_fresh_conns`/`http_pool_fallbacks`) |
| `DIFF_UI_ENABLED` | `diffUi.enabled` | `true` | Full-diff web UI (see below). Persists artifacts + serves `/diff/*` in-cluster; safe by default since no ingress path exposes it externally |
| `DIFF_UI_BASE_URL` | `diffUi.baseUrl` | *(empty)* | External base URL the build status deep-links to. Empty = status keeps linking to the comment |
| `DIFF_UI_DIR` | `diffUi.dir` | `/tmp/acme-diff-ui` | Artifact directory (bounded, pruned oldest-first) |
| `DIFF_UI_MAX_ARTIFACTS` | `diffUi.maxArtifacts` | `500` | Max stored artifacts before pruning |
| — | `diffUi.ingress.enabled` | `false` | Externally reachable, IAP-gated Service + BackendConfig for the UI (see below) |

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

## Repository layout

```
acme-diff-preview/
├── src/
│   ├── diff_preview.py        Main service (Deployment)
│   ├── diff_ui.py             Full-diff artifact store + web UI (stdlib only)
│   └── dev_hard_refresh.py    Full hard-refresh of all dev/QA apps (CronJob)
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

## HTTP endpoints

All endpoints are served on port **8080** inside the pod.

| Method | Path | Description |
|---|---|---|
| `POST` | `/diff-preview/webhook` | Bitbucket PR webhook (wakes the diff loop) |
| `GET` | `/diff-preview/stats` | Diff outcome counters, main-render cache hits/misses, last iteration timing (JSON) |
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
  exposes `is_leader` per replica.
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
kubeVersion: "1.30.0"

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
  schedule: "*/30 * * * *"

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
