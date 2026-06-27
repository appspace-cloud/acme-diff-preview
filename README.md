# acme-diff-preview

ACME Diff Preview service for Appspace. A long-running Kubernetes Deployment
that does two distinct jobs:

1. **PR diff comments** — watches `acme-config-dev` Bitbucket PRs and, for every
   affected app, renders the chart with `helm template` for both the PR and the
   `main` revision, diffs the two locally, and posts a formatted comment with a
   Vertex AI Gemini summary.

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
API used to fetch value files. Three mechanisms keep it fast and reliable:

1. **Chart-cache warm-up.** Before the parallel fan-out, one representative app
   per distinct OCI chart is pulled first (`_select_warm_apps`) so the remaining
   apps reuse the local tarball instead of all pulling it at once. Controlled by
   `WARM_WORKERS` / `WARM_THRESHOLD`.
2. **Bitbucket API rate limiting + safe caching.** A global semaphore
   (`BB_API_CONCURRENCY`, default 30) caps concurrent Bitbucket calls; value files
   are cached by immutable `(commit_sha, path)` and transient errors are **never**
   cached as "missing", so one app's rate-limit blip cannot poison the others.
3. **Retry with exponential backoff + jitter.** Transient reasons
   (`oci_pull_failed`, `metadata_pending`, `timeout`) are retried in-process up to
   `DIFF_RETRIES` times so a brief blip never surfaces as "diff unavailable".

The on-disk chart cache is bounded (`HELM_CACHE_MAX_CHARTS`) and pruned at the
start of each iteration so a long-lived pod cannot fill node ephemeral storage.

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
├── Dockerfile                 python:3.12-slim + argocd CLI + helm CLI
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
  tag: "1.9.0"

# Set to DEBUG to log full per-step diff detail.
logLevel: INFO

# Kubernetes version helm renders against (--kube-version).
kubeVersion: "1.30.0"

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

`OCI_USER` / `OCI_PASS` are **required** for the diff to work: without them every
`helm pull` fails and the comment shows "diff unavailable" for every app. The pod
logs an ERROR at startup when `OCI_PASS` is empty.

To rotate the JFrog webhook secret, follow the runbook at
`docs/runbooks/jfrog-webhook-secret-rotation.md`.
