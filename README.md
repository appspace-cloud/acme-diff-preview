# acme-diff-preview

![CI](https://github.com/appspace-cloud/acme-diff-preview/actions/workflows/ci.yml/badge.svg)
![Release](https://github.com/appspace-cloud/acme-diff-preview/actions/workflows/release.yml/badge.svg)
![Coverage](badges/coverage.svg)
![Version](badges/version.svg)

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
| 🗑️ RESOURCE(S) DELETED | Resources disappear from the rendered output entirely. |
| 🗑️ ENVIRONMENT DECOMMISSION | A whole environment is being removed. Read the block: by default its workloads are **orphaned, not deleted**. |

The one rule the tool never breaks: **a failure is never reported as "no changes".**
If a diff could not be computed, the status says so and the PR is not marked clean.

Only a slice of an app's changed resources is printed with a full diff block.
Deletions and replica zeroings get a reserved share of that slice, so the
resources the shouty blocks name are the ones you actually see first. Details
in [docs/internals.md](docs/internals.md#which-resources-make-it-into-the-comment-body).

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

See [docs/internals.md](docs/internals.md) for the reasoning behind each guard,
how mass version bumps are handled, the secret-leak hardening, and the
full-diff web UI.

---

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
| `SUPERSEDE_ABORT_ENABLED` | `diff.supersedeAbortEnabled` | `true` | Let a newer push abort a render already in flight ([details](docs/internals.md#superseding-an-in-flight-render)). Off = the pre-COPS-2575 behaviour, a dead commit is rendered to completion and published |
| `SUPERSEDE_MAX_CONSECUTIVE_ABORTS` | `diff.supersedeMaxConsecutiveAborts` | `3` | Livelock guard: after this many consecutive aborts on one PR, the run finishes regardless so the PR always eventually gets a comment |
| `KUBE_VERSION` | `kubeVersion` | `1.35.5` | `--kube-version` passed to `helm template`. Matches the real GKE clusters; keep it in step with them |
| `BB_API_CONCURRENCY` | — | `30` | Max concurrent Bitbucket API calls |
| `BB_RATELIMIT_FALLBACK` | — | `15` | Shared pause after a 429 that carries no `Retry-After`. Sized to Bitbucket's ~60s window, not to a single retry |
| `BB_RATELIMIT_MAX_PAUSE` | — | `60` | Cap on the shared pause, so a broken `Retry-After` cannot stall a PR |
| `HELM_CACHE_MAX_CHARTS` | — | `60` | Max pulled chart versions kept on disk |
| `AI_MAX_APPS` | — | `40` | Max changed apps included in the AI summary prompt |
| `DIFF_OCI_SELFCHECK_INTERVAL` | — | `900` | Seconds between periodic OCI self-checks (`helm show chart` against a known-good ref). First check ~60s after start; `0` disables. Result in `/diff-preview/stats` as `oci_selfcheck` |
| `DIFF_OCI_SELFCHECK_REF` | — | *(last successful pull)* | Optional fixed reference `registry/chart:version` for the self-check |
| `DIFF_OCI_FAIL_ERROR_THRESHOLD` | — | `3` | Consecutive systemic chart-pull failures after which failures log at ERROR instead of WARNING |
| `DIFF_IGNORE_RESOURCES` | — | *(empty)* | Extra comma-separated resource-name substrings to hide from every diff, on top of the built-in `micro-versions-info` |
| `DIFF_HTTP_POOLING` | — | `on` | HTTP keep-alive pooling: one persistent TLS connection per worker thread and host. `off` routes every request through plain `urlopen`. Auto-defers to `urlopen` when a proxy is configured. Visible in `/diff-preview/stats` (`http_pool_reuses`/`http_pool_fresh_conns`/`http_pool_fallbacks`) |
| `DIFF_UI_ENABLED` | `diffUi.enabled` | `true` | Full-diff web UI ([details](docs/internals.md#full-diff-web-ui-atlantis-style)). Persists artifacts + serves `/diff/*` in-cluster; safe by default since no ingress path exposes it externally |
| `DIFF_UI_BASE_URL` | `diffUi.baseUrl` | *(empty)* | External base URL the build status deep-links to. Empty = status keeps linking to the comment |
| `DIFF_UI_DIR` | `diffUi.dir` | `/tmp/acme-diff-ui` | Artifact directory (bounded, pruned oldest-first) |
| `DIFF_UI_MAX_ARTIFACTS` | `diffUi.maxArtifacts` | `500` | Max stored artifacts before pruning |
| — | `diffUi.ingress.enabled` | `false` | Externally reachable, IAP-gated Service + BackendConfig for the UI ([details](docs/internals.md#full-diff-web-ui-atlantis-style)) |



## Repository layout

```
acme-diff-preview/
├── src/
│   ├── diff_preview.py        Main service (Deployment)
│   ├── diff_ui.py             Full-diff artifact store + web UI (stdlib only)
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

---


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
