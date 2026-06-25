# Release process

## Critical: never overwrite an existing image tag

JFrog does not allow overwriting a tag once pushed (it would require DELETE
permission on the artifact, which the CI service account does not have).
Trying to push to an existing tag results in:

  unauthorized: Not enough permissions to delete/overwrite artifacts

**Before building an image, always verify the tag does not already exist:**

```bash
curl -s \
  "https://docker-dev.repo.appspace.com/v2/acme-diff-preview/tags/list" \
  -u "acme-repo:PASSWORD" \
  | python3 -c "import json,sys; print(sorted(json.load(sys.stdin)['tags']))"
```

If the tag already exists, bump `appVersion` in `Chart.yaml` and
`image.tag` in `values.yaml` to the next patch before building.

## Version fields to update together

| File | Field | Example |
|---|---|---|
| `charts/acme-diff-preview/Chart.yaml` | `version` | `1.2.4` (Helm chart) |
| `charts/acme-diff-preview/Chart.yaml` | `appVersion` | `"1.3.4"` (Docker image) |
| `charts/acme-diff-preview/values.yaml` | `image.tag` | `"1.3.4"` |

Chart version and appVersion are bumped independently.
Chart version bumps when the chart templates or values change.
appVersion bumps when the Docker image changes.
They can be the same bump or different — keep them in sync with what changed.

## Workflow

1. Bump versions in `Chart.yaml` and `values.yaml` on a feature branch.
2. Verify the new tag does not exist in JFrog (see above).
3. Open a PR — CI runs tests and builds the image (no push on PR).
4. Merge to `main` — `release.yml` publishes the Helm chart to GitHub Pages.
5. Push tag `v<appVersion>` (e.g. `v1.3.4`) — `docker.yml` builds and pushes
   the Docker image to JFrog.
6. Update `acme-infrastructure` config with the new chart version and image tag,
   open a PR, and let Atlantis apply it.

## GitHub Actions workflows

| Workflow | Trigger | What it does |
|---|---|---|
| `ci.yml` | PR or push to `main` | Tests + helm lint + docker build (no push) |
| `release.yml` | Push to `main` | Publishes Helm chart to GitHub Pages via chart-releaser |
| `docker.yml` | Push of `v*` tag | Builds and pushes Docker image to JFrog Artifact Registry |
