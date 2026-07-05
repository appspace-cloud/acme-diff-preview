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
   the Docker image to JFrog. **Do not push the image manually first — see below.**
6. Update `acme-infrastructure` config with the new chart version and image tag,
   open a PR, and let Atlantis apply it.

## Critical: never push the image manually AND push a git tag for the same version

If you push a Docker image manually with `docker push` and then also push the
matching git tag, the CI workflow will try to push the same image again.
JFrog will reject it because it cannot overwrite an existing tag:

```
Artifact deletion error: Not enough permissions to delete/overwrite all artifacts
```

**Pick one path — never both:**

| Path | When to use |
|---|---|
| Push git tag only | Normal releases — CI builds and pushes the image |
| Push image manually | Emergency hotfix only — do NOT also push a git tag for that version |

The `docker.yml` workflow uses the Docker Registry HTTP API to check if the tag
already exists before attempting a push. If you pushed manually and CI still
fails, re-run the workflow — the check will detect the existing tag and exit
cleanly without error.

## GitHub Actions workflows

| Workflow | Trigger | What it does |
|---|---|---|
| `ci.yml` | PR or push to `main` | Tests + helm lint + docker build (no push) |
| `release.yml` | Push to `main` | Publishes Helm chart to GitHub Pages via chart-releaser, and ensures every `v*` tag has a matching GitHub Release (see below) |
| `docker.yml` | Push of `v*` tag | Builds and pushes Docker image to JFrog Artifact Registry |

## GitHub Releases (human-readable, one per `v*` tag)

Every `v<version>` tag (e.g. `v2.5.1`) should be an **annotated** tag whose
message is the full release story: what broke, why, the fix, and how it was
verified — see any existing `v*` tag for the expected level of detail
(`git tag -n99 v2.5.1`). This is the same convention used in `acme-mcp`.

`release.yml`'s `github-release` job runs on every push to `main` and creates
a GitHub Release for any `v*` tag that doesn't have one yet, using **the
tag's own annotation message** (`git for-each-ref refs/tags/<tag>
--format='%(contents)'`) as the release notes — not the commit message the
tag points to, which is usually shorter and less useful as a changelog entry.
The job is idempotent (`gh release view` check before creating), so:

- A normal release just needs `git tag -a vX.Y.Z -F <message-file>` + `git
  push origin vX.Y.Z` (or push it along with the `main` merge, as usual) —
  the Release appears automatically on the next `release.yml` run.
- It also backfills any past tag that never got a Release (this is how
  v2.4.9 through v2.5.1 got their Releases retroactively).

**Do not confuse this with the chart-releaser-action releases** named
`acme-diff-preview-X.Y.Z` (no `v` prefix) — those carry the actual `.tgz`
chart artifact that the published Helm repo index points to. ArgoCD/helm
depend on them to pull the chart; never delete or rename them.
