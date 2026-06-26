# acme-diff-preview

ACME Diff Preview service for Appspace. A long-running Kubernetes Deployment
that does two distinct jobs:

1. **PR diff comments** — watches `acme-config-dev` Bitbucket PRs, runs
   `argocd app diff` against every affected app, and posts a formatted diff
   comment with a Vertex AI Gemini summary.

2. **JFrog OCI webhook** — receives push events from JFrog when CI publishes
   a new Helm chart to `helm-oci-dev`, finds every dev/QA ArgoCD app tracking
   that chart version, and hard-refreshes them to bypass the OCI cache.

A CronJob runs a full hard-refresh of all dev/QA apps every 30 minutes as a
fallback safety net.

---

## Diff outcomes and debugging

Every `argocd app diff` call resolves to one of four outcomes
(see `classify_diff_error` in `src/diff_preview.py`):

| Outcome | Meaning | PR comment |
|---|---|---|
| `diff` | A real manifest diff was produced | ⚠️ N resource(s) will change |
| `no_diff` | Clean exit 0 — manifests match | ✅ No manifest changes |
| `indeterminate` | The diff could **not** be computed | ❔ diff unavailable (reason) |
| `error` | Unexpected / unknown failure | ❌ error |

`indeterminate` is the important one: it is **never** rendered as a green
"no changes". It covers the failures that come from the ArgoCD-agent / proxy /
Redis / OCI topology, with a short reason in the comment and the full ArgoCD
stderr in the pod logs at `LOG_LEVEL=DEBUG`:

| Reason | Cause |
|---|---|
| `oci_login` | repo-server `helm registry login` to the OCI registry failed (e.g. 401 Bad Credentials) on a manifest cache miss |
| `manifests_5xx` | repo-server returned 5xx (often the visible symptom of a failed/slow OCI render) on `GetManifests` |
| `redis_timeout` | spoke Redis unreachable via `argocd-agent-redis-proxy` |
| `managed_no_cache` | live state not cached for an agent-managed app |
| `server_unavailable` | ArgoCD server / app-controller restarting or busy |
| `canceled` | request cancelled / deadline exceeded |
| `permission` | the `diff-preview` account lacks RBAC for the app |
| `auth` | ArgoCD session expired (a background re-login is triggered) |

PRs with any `indeterminate` or `error` app are **not** marked "seen", so the
next loop re-evaluates them — once the underlying OCI / Redis path recovers the
comment automatically flips from "diff unavailable" to the real diff.

To see exactly why a diff failed:

```bash
kubectl -n argocd logs deploy/acme-diff-preview | grep '"outcome"'
# or, for full ArgoCD stderr, set logLevel: DEBUG in the Helm values
```

### Handling mass version bumps (hundreds of apps in one PR)

Bumping a chart `version:` across many clusters in a single PR is a normal
operation. Naively diffing every app at once means every app is a repo-server
cache miss racing to pull and render the same OCI chart, which saturates the
repo-server (5xx / `manifests_5xx`) and floods the argocd-agent principal. Two
mechanisms keep this reliable:

1. **Per-agent concurrency cap (serialize per spoke, parallelize across spokes).**
   Every app on a spoke shares one argocd-agent and the principal's resource-proxy
   connection to it. A single `argocd app diff` of a large app (hundreds of
   resources) fans out into hundreds of live-resource requests to that one agent,
   so running several diffs at once on the same spoke overruns the agent's response
   window; the principal then drops the late responses ("resource response not
   tracked") and the diff fails as `redis_timeout`. Diffs are gated by a global
   per-agent semaphore (`AGENT_MAX_CONCURRENCY`, default `1`) and interleaved across
   agents, so each spoke is hit one diff at a time while total throughput still
   scales with the number of spokes. Measured on a 24-app mass bump: `1` -> 26/27
   clean and 0 principal panics; `3` -> 11 failures plus `send on closed channel`
   panics on the principal.
2. **Chart-cache warm-up.** Before the parallel fan-out, one representative app
   per distinct OCI chart is diffed first (`_select_warm_apps`). That single
   render warms the repo-server chart cache so the remaining apps reuse the pull
   instead of stampeding it. Controlled by `WARM_WORKERS` / `WARM_THRESHOLD`.
3. **Retry with exponential backoff + jitter.** Transient burst errors
   (`manifests_5xx`, `code = Unknown desc = POST`, redis-proxy timeouts) are
   retried in-process up to `DIFF_RETRIES` times so a brief blip during the
   burst never surfaces as "diff unavailable".

This is paired with hub-side capacity in `acme-infrastructure`
(`reposerver.parallelism.limit`, a 100-processor principal, redis-ha headroom).

### Tuning knobs (env vars / Helm `diff.*` values)

| Env | Helm value | Default | Purpose |
|---|---|---|---|
| `MAX_APPS_PER_RUN` | `diff.maxAppsPerRun` | `800` | Hard cap on apps diffed per PR |
| `DIFF_WORKERS` | `diff.workers` | `16` | Parallel diffs within one PR |
| `PR_WORKERS` | `diff.prWorkers` | `3` | PRs processed in parallel |
| `DIFF_TIMEOUT` | `diff.timeout` | `120` | Seconds per diff attempt |
| `DIFF_RETRIES` | `diff.retries` | `5` | Attempts per diff (backoff + jitter) |
| `WARM_WORKERS` | `diff.warmWorkers` | `4` | Parallel chart warm-up diffs |
| `WARM_THRESHOLD` | `diff.warmThreshold` | `8` | Min apps before warm-up kicks in |
| `AGENT_MAX_CONCURRENCY` | `diff.agentMaxConcurrency` | `1` | Max concurrent diffs per agent/spoke (serialize per spoke) |

---

## Repository layout

```
acme-diff-preview/
├── src/
│   ├── diff_preview.py        Main service (Deployment)
│   └── dev_hard_refresh.py    Full hard-refresh of all dev/QA apps (CronJob)
├── tests/
│   └── test_diff_preview.py   Unit tests (no external dependencies)
├── charts/
│   └── acme-diff-preview/     Helm chart
│       ├── Chart.yaml
│       ├── values.yaml
│       └── templates/
│           ├── deployment.yaml
│           ├── service.yaml
│           ├── serviceaccount.yaml
│           ├── externalsecret.yaml
│           └── cronjob.yaml
├── docs/
│   └── runbooks/
│       └── jfrog-webhook-secret-rotation.md
├── Dockerfile                 python:3.12-slim + argocd CLI
├── RELEASING.md               How to cut a release (read before pushing tags)
└── .github/workflows/
    ├── ci.yml                 PR gate: tests, helm lint, docker build (no push)
    ├── release.yml            Push to main: publish Helm chart to GitHub Pages
    └── docker.yml             Push of v* tag: build + push image to JFrog
```

---

## HTTP endpoints

All endpoints are served on port **8080** inside the pod.

| Method | Path | Description |
|---|---|---|
| `POST` | `/diff-preview/webhook` | Bitbucket PR webhook (wakes the diff loop) |
| `POST` | `/jfrog-webhook` | JFrog OCI push webhook (triggers hard-refresh) |
| `GET` | `/jfrog-webhook/stats` | Webhook counters (JSON) |
| `GET` | `/healthz` | Liveness probe |
| `GET` | `/readyz` | Readiness probe |

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

---

## CI/CD

| Trigger | What runs |
|---|---|
| PR to `main` | Tests, helm lint, docker build (no push) |
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

Deployed via Terraform in `acme-infrastructure`:

```
deployments/appspace-com/gcp/appspace-devops/shared/infrastructure/gke/na1-a/config/terragrunt.hcl
```

Key Helm values configured from `acme-infrastructure`:

```yaml
image:
  repository: us-central1-docker.pkg.dev/appspace-devops/artifact/acme-diff-preview
  tag: "1.4.0"

# Set to DEBUG to log full ArgoCD stderr + per-attempt diff classification.
logLevel: INFO

argocd:
  server: argocd.appspace.com
  username: diff-preview

bitbucket:
  workspace: appspace-cloud
  repo: acme-config-dev

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
| `acme-repo-password` | JFrog pull credentials for GAR proxy and CI |
| `argocd-jfrog-webhook-shared-secret` | HMAC key for the JFrog webhook endpoint |

To rotate the JFrog webhook secret, follow the runbook at
`docs/runbooks/jfrog-webhook-secret-rotation.md`.
