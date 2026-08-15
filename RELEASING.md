# Release process

## Mandatory: regression tests before every change ships

Every change to this service — a bugfix, a hardening pass, a refactor, even a
"trivial" one-liner — must go through the full cycle, no exceptions:

1. **Write the regression test(s) first**, encoding the bug/requirement.
   Run them and confirm they FAIL against the current code (red) — a test
   that never failed never proved anything.
2. Implement the fix.
3. Run **that test file** until it's green, then run the **entire suite**
   (`python3 -m pytest tests/ -q` — ~2.5 minutes; see TESTING.md) — a change that only touches one function
   can still break an unrelated test elsewhere (shared globals, locks,
   caches). Never ship on a partial run.
4. For anything touching concurrency, timing, or a background thread
   (heartbeat, locks, retries), also do a real **local behavioral check**
   beyond unit assertions where practical — e.g. actually start the
   component and observe it over a few real ticks — the way v2.5.2's C2 fix
   was confirmed by running the heartbeat loop live and watching staleness
   grow under a simulated wedge, not just asserting a pure function's
   return value.
5. After deploying, verify the running pod: image tag, clean startup in the
   logs, no restarts, and (when relevant) a real PR against
   `acme-config-dev` exercising the exact scenario that was fixed.

This is not optional or size-dependent — it applies the same way whether the
change is one line or a full hardening pass. The goal is the same as any QA
process: prove the new thing works AND prove it did not break anything that
already worked.

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

**Step 5 comes before step 6, and the reason is not tidiness.** Merging to
`main` does not build the image; only the tag push does. If infra is applied
first, the kubelet tries to pull a tag that does not exist yet, and the
Artifact Registry proxy **caches that 404**. From then on the pull keeps
failing for the negative-cache TTL even after the image lands in JFrog, so the
deploy is blocked for a while by a stale answer rather than by a missing image.
The service stays up throughout (`atomic = false`, `cleanup_on_fail = false`
leave the old pods running), and the Helm release is left `failed` until the
next apply, but the rollout stalls.

If it happens anyway: confirm the image really is in JFrog (the `docker.yml`
run log ends with `image.name: docker-dev.repo.appspace.com/...`), then poll the
proxy manifest as described below until it answers `200` and re-run the apply.
Do not force anything, and do not push the image manually: that would collide
with the tag build (see below). Always test with a known-good tag as a control,
otherwise a broken check looks exactly like a missing image.

## Rolling back a bad release

There was no written procedure for this until COPS-2668, which meant the one
thing you need under pressure had to be reconstructed from memory. It is
simpler than it looks, because **the image tag is the rollback**.

**1. Roll back the running workload first.** Point `image.tag` (and the chart
version, if the chart itself changed) at the previous release in
`acme-infrastructure` and apply. The old tag is already in JFrog and already
cached by the Artifact Registry proxy, so the pull is immediate — none of the
negative-cache trap above applies to a tag that has been pulled before.

Faster still, if infra apply is not available to you and the service is
actively doing damage:

```bash
kubectl -n argocd set image deploy/acme-diff-preview \
  diff-preview=us-central1-docker.pkg.dev/appspace-devops/artifact/acme-diff-preview:<previous-tag>
```

That is a stopgap, not the fix: the periodic reconciler will restore the
tag from `main` within the hour, so the infra change still has to land.

**2. Do not delete or re-push the bad tag.** JFrog cannot overwrite tags, and
a deleted tag makes the incident harder to reconstruct afterwards. Leave it.

**3. Consider whether the render cache is holding bad output.** If the bad
release changed render behaviour, the durable tier can still be serving its
results after the rollback. Bump `MAIN_RENDER_CACHE_SALT` in the rollback
release, or the fix ships and the wrong renders keep being served.

**4. Revert the code on `main` with a normal PR.** `git revert` the merge
commit rather than force-pushing. `release.yml` republishes the chart, and
the next release proceeds normally from a clean history.

**What NOT to do:** do not force-push `main`, do not push an image by hand
(it collides with the tag build — see below), and do not reuse the bad
version number. Roll forward to `N+1` with the revert in it.

## Where the image actually lives: JFrog, then GKE pulls it through a proxy

`docker.yml` pushes the image to **JFrog** (`docker-dev.repo.appspace.com`)
and nowhere else. GKE never talks to JFrog directly. The chart's
`image.repository` default, and every real infra pin, point at
`us-central1-docker.pkg.dev/appspace-devops/artifact/acme-diff-preview`,
a Google Artifact Registry **remote repository** configured as a
pull-through proxy in front of JFrog.

This matters right after a tag push. `gcloud artifacts docker tags list`
on that Artifact Registry path can show nothing for the brand new tag,
because the proxy only caches a tag once something has actually pulled it,
even though the JFrog push already succeeded and `docker.yml` reported
success. Reading that gap as "the image did not get pushed" costs real
debugging time, and it has already happened during a real release.

**To confirm a fresh tag is actually resolvable, check the manifest
through the proxy directly, not the tag-listing command:**

```bash
TOKEN=$(gcloud auth print-access-token)
curl -s -o /dev/null -w "%{http_code}\n" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/vnd.docker.distribution.manifest.v2+json" \
  "https://us-central1-docker.pkg.dev/v2/appspace-devops/artifact/acme-diff-preview/manifests/<tag>"
```

`200` means the tag is pullable, whether or not it shows up in the tag
list yet.

**Run this check only after `docker.yml` has reported success.** The
negative-cache trap above is not limited to a premature infra apply: this
very curl poisons the cache in exactly the same way. Artifact Registry
caches Docker tag metadata for **about an hour** by default, and that
includes the "not found" answer. Asking for a tag while the build is still
failing therefore blocks the deploy for the rest of that hour, after the
image has landed and through any number of retries.

Seen for real on the 2.61.0 release: `docker.yml` failed twice on a
transient `curl: (56)` while downloading the argocd and helm binaries, the
manifest was checked in between, and the tag then answered `404` for the
next hour even though the third build pushed the image successfully.

If you are already in that hole, **query by digest to tell the two cases
apart**:

```bash
DIGEST=$(gh run view <docker-run-id> --log \
  | grep -oE '"containerimage.digest": "sha256:[a-f0-9]+"' \
  | head -1 | grep -oE 'sha256:[a-f0-9]+')
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $TOKEN" \
  "https://us-central1-docker.pkg.dev/v2/appspace-devops/artifact/acme-diff-preview/manifests/$DIGEST"
```

`200` by digest with `404` by tag means the image is present and only the
tag lookup is stale. Wait out the TTL; do not rebuild, do not retag, and do
not push anything by hand.

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
| `docker.yml` | Push of `v*` tag | Builds and pushes Docker image to JFrog. GKE never pulls from JFrog directly; it goes through the Artifact Registry proxy, see above |

## GitHub Releases (human-readable, one per `v*` tag)

Every `v<version>` tag (e.g. `v2.5.2`) should be an **annotated** tag, and its
message IS the GitHub Release body verbatim — so it must be written for that
page, not as an internal design doc. Two hard rules, learned from v2.5.2
rendering badly:

1. **Keep it short — a few bullets, not an essay.** Aim for under ~120 words:
   a one-line summary, then 2-5 short bullets (one per fix/change), then an
   optional one-line test/verification note. The full technical story (root
   cause, code paths, before/after numbers, live-verification detail) belongs
   in the **commit message** instead — that has no length or rendering limits
   and isn't shown on the Releases page.
2. **Never hand-wrap a line inside a paragraph or bullet.** GitHub's Release
   renderer shows every single `\n` as a hard line break — it does NOT
   reflow a hard-wrapped paragraph to the page width like a text editor
   would. A tag message written like a wrapped commit message (each source
   line ~72-80 chars) renders as a wall of short, choppy lines instead of a
   normal paragraph (exactly what v2.5.2 looked like before it was fixed).
   Write each paragraph/bullet as ONE continuous line (let your editor
   soft-wrap it, but do not insert a literal newline until the
   paragraph/bullet actually ends), and use real blank lines between
   paragraphs and real `- ` markdown bullets for lists.

Template:
```
vX.Y.Z: <one-line summary>

- **<short label>:** <what changed and why, one sentence, one line>.
- **<short label>:** <what changed and why, one sentence, one line>.

Tests: N passing (M new).
```

`release.yml`'s `github-release` job runs on every push to `main` and creates
a GitHub Release for any `v*` tag that doesn't have one yet, using **the
tag's own annotation message** (`git for-each-ref refs/tags/<tag>
--format='%(contents)'`) as the release notes verbatim. The job is idempotent
(`gh release view` check before creating), so:

- A normal release just needs `git tag -a vX.Y.Z -F <message-file>` + `git
  push origin vX.Y.Z` (or push it along with the `main` merge, as usual) —
  the Release appears automatically on the next `release.yml` run.
- It also backfills any past tag that never got a Release (this is how
  v2.4.9 through v2.5.1 got their Releases retroactively).

**Do not confuse this with the chart-releaser-action releases** named
`acme-diff-preview-X.Y.Z` (no `v` prefix) — those carry the actual `.tgz`
chart artifact that the published Helm repo index points to. ArgoCD/helm
depend on them to pull the chart; never delete or rename them.

### Fixing an already-published Release's notes

The underlying git tag is immutable in practice (never force-move an
already-pushed tag). If a Release's notes need correcting after the fact
(wrong content, or written before this template existed), edit the
**Release**, not the tag: commit a markdown file to
`.release-notes/<tag>.md` with the corrected notes and push it to `main`.
The `fix-release-notes.yml` workflow watches that path and overwrites the
matching Release's notes from the file — this exists because a personal
PAT only has read access to this repo (`push: false`), so editing an
existing Release needs the workflow's own `GITHUB_TOKEN`, and even
`workflow_dispatch` needs admin rights the PAT also lacks. A plain
`git push` over SSH is the one write path that works, hence the
push-triggered design.
