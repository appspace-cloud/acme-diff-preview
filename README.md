# acme-diff-preview

ArgoCD Diff Preview — posts Kubernetes manifest diffs as Bitbucket PR comments with AI-generated summaries powered by Vertex AI Gemini (COPS-2494/2496/2497/2498).

## What it does

On every open PR in `acme-config-dev`, the service:
1. Detects changes within seconds via Bitbucket webhook
2. Runs `argocd app diff --revisions <PR_SHA>` against all affected apps
3. Posts a comment with the full diff and an AI summary of what changed
4. Updates the Bitbucket build status (INPROGRESS → SUCCESSFUL/FAILED)

## Repository structure

```
acme-diff-preview/
├── src/                        Application source code
│   ├── diff_preview.py         Main service (long-running Deployment)
│   └── dev_hard_refresh.py     Hard-refresh script (CronJob every 2h)
├── tests/                      Unit tests
├── charts/
│   └── argocd-diff-preview/    Helm chart for all Kubernetes resources
├── Dockerfile                  Single image: python:3.12-slim + argocd CLI
└── .github/workflows/
    ├── ci.yml                  PR gate: syntax, tests, helm lint, docker build
    └── release.yml             Tag v*: push GHCR image + publish Helm chart
```

## Helm chart

Hosted at: `https://appspace-cloud.github.io/acme-diff-preview`

```bash
helm repo add acme-diff-preview https://appspace-cloud.github.io/acme-diff-preview
helm install argocd-diff-preview acme-diff-preview/argocd-diff-preview \
  --namespace argocd \
  -f values-override.yaml
```

See [docs/argocd/README.md](https://bitbucket.org/appspace-cloud/acme-infrastructure/src/main/docs/argocd/README.md) in acme-infrastructure for full operational documentation.

## Docker image

`ghcr.io/appspace-cloud/acme-diff-preview:<version>`

Single unified image for both the Deployment and the hard-refresh CronJob.
No `gcloud` — credentials arrive via ESO-managed Kubernetes Secret.

## CI/CD

- **On every PR:** Python syntax check, pytest, `helm lint`, Docker build (no push)
- **On `v*` tag:** Build + push image to GHCR, package + publish Helm chart to GitHub Pages

## Related tickets

- [COPS-2494](https://appspace.atlassian.net/browse/COPS-2494) — Convert CronJob to Deployment
- [COPS-2496](https://appspace.atlassian.net/browse/COPS-2496) — AI diff summaries
- [COPS-2497](https://appspace.atlassian.net/browse/COPS-2497) — Bitbucket webhook
- [COPS-2498](https://appspace.atlassian.net/browse/COPS-2498) — This: extract to GitHub repo + Helm chart
