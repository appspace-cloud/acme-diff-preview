# acme-diff-preview

ArgoCD Diff Preview — posts Kubernetes manifest diffs as Bitbucket PR comments with AI-generated summaries powered by Vertex AI Gemini (COPS-2494/2496/2497/2498).

## What it does

On every open PR in `acme-config-dev`, the service:
1. Detects changes within seconds via Bitbucket webhook (COPS-2497)
2. Runs `argocd app diff --revisions <PR_SHA>` against all affected ArgoCD apps
3. Posts a formatted diff comment on the Bitbucket PR with a Vertex AI summary
4. Updates the Bitbucket build status (INPROGRESS → SUCCESSFUL/FAILED)

## Repository structure

```
acme-diff-preview/
├── src/
│   ├── diff_preview.py         Main service (long-running Deployment)
│   └── dev_hard_refresh.py     Hard-refresh all dev/QA apps (CronJob every 2h)
├── tests/
│   └── test_diff_preview.py    Unit tests (syntax, key functions, no-gcloud)
├── charts/
│   └── acme-diff-preview/      Helm chart — all Kubernetes resources
│       ├── Chart.yaml          version: 1.0.0, appVersion: "1.1.0"
│       ├── values.yaml         All tuneable parameters with defaults
│       └── templates/
│           ├── deployment.yaml      Long-running diff-preview service
│           ├── service.yaml         NodePort :8080 for ArgoCD Ingress backend
│           ├── serviceaccount.yaml  WIF annotation → argocd@appspace-devops GSA
│           ├── externalsecret.yaml  ESO → acme-diff-preview-creds K8s Secret
│           └── cronjob.yaml         Hard-refresh, runs every 2h
├── Dockerfile                  python:3.12-slim + argocd CLI v3.4.3
└── .github/workflows/
    ├── ci.yml                  PR gate: syntax, pytest, helm lint, docker build
    └── release.yml             Tag v*: push image to JFrog + publish Helm chart
```

## Docker image

Images are pushed to JFrog Artifactory and pulled by GKE via the existing
**Google Artifact Registry remote proxy** in `appspace-devops`:

| Registry | URL |
|---|---|
| Source (JFrog) | `docker-dev.repo.appspace.com/acme-diff-preview:<tag>` |
| GKE pull URL (GAR proxy) | `us-central1-docker.pkg.dev/appspace-devops/artifact/acme-diff-preview:<tag>` |

GKE node pools pull from the GAR proxy URL — no `imagePullSecrets` needed
since the node service account already has IAM access to `appspace-devops`
Artifact Registry.

## Helm chart

The chart is published to GitHub Pages via `chart-releaser-action`:

```
https://appspace-cloud.github.io/acme-diff-preview
```

## CI/CD

| Trigger | What runs |
|---|---|
| PR to `main` | Python syntax check, pytest, `helm lint`, `docker build` (no push) |
| Tag `v*` | Build + push image to JFrog, package + publish Helm chart to GitHub Pages |

### Adding GitHub Actions secrets

The release workflow needs two repository secrets:

| Secret | Value |
|---|---|
| `JFROG_USER` | `acme-repo` |
| `JFROG_PASSWORD` | Contents of GCP SM secret `acme-repo-password` in `appspace-devops` |

To set them:
```bash
gh secret set JFROG_USER --body "acme-repo" --repo appspace-cloud/acme-diff-preview
PASS=$(gcloud secrets versions access latest --secret=acme-repo-password --project=appspace-devops)
gh secret set JFROG_PASSWORD --body "$PASS" --repo appspace-cloud/acme-diff-preview
```

## Installation (via acme-infrastructure)

The service is installed as a `helm_release` in:
```
deployments/appspace-com/gcp/appspace-devops/shared/infrastructure/gke/na1-a/config/terragrunt.hcl
```

Key values passed from acme-infrastructure:
```yaml
image:
  repository: us-central1-docker.pkg.dev/appspace-devops/artifact/acme-diff-preview
  tag: "1.1.0"
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
secrets:
  externalSecretStore: argocd-gcp-sm
serviceAccount:
  annotations:
    iam.gke.io/gcp-service-account: argocd@appspace-devops.iam.gserviceaccount.com
```

The ArgoCD Ingress `extraPaths` for `/diff-preview/webhook` is configured
in the ArgoCD Helm values (not in this chart).

## GCP Secrets Manager keys

| Secret name | Used for |
|---|---|
| `argocd-diff-preview-bb-user` | Bitbucket username |
| `argocd-diff-preview-bb-token` | Bitbucket app password |
| `argocd-diff-preview-admin-pass` | ArgoCD `diff-preview` account password (plaintext, stable) |
| `argocd-diff-preview-password` | Bcrypt hash for `accounts.diff-preview.password` in ArgoCD (Terraform) |
| `acme-repo-password` | JFrog pull credentials (for GAR proxy + CI) |

## Related tickets

- [COPS-2494](https://appspace.atlassian.net/browse/COPS-2494) — Convert CronJob to Deployment
- [COPS-2496](https://appspace.atlassian.net/browse/COPS-2496) — Vertex AI diff summaries
- [COPS-2497](https://appspace.atlassian.net/browse/COPS-2497) — Bitbucket webhook
- [COPS-2498](https://appspace.atlassian.net/browse/COPS-2498) — Extract to GitHub repo + Helm chart (this)
