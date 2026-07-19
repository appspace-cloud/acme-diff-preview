# acme-diff-preview

![CI](https://github.com/appspace-cloud/acme-diff-preview/actions/workflows/ci.yml/badge.svg)
![Release](https://github.com/appspace-cloud/acme-diff-preview/actions/workflows/release.yml/badge.svg)
![Coverage](badges/coverage.svg)
![Version](badges/version.svg)

ACME Diff Preview service for Appspace. A long-running Kubernetes Deployment
that does two distinct jobs:

1. **PR diff comments** — watches Bitbucket PRs across the configured config
   repos (today: `acme-config-dev` and `acme-config-stage`; `acme-config-prod`
   once its ArgoCD onboarding lands) and, for every affected app, renders the
   chart with `helm template` for both the PR and the `main` revision, diffs
   the two locally, and posts a formatted comment with a Vertex AI Gemini
   summary. Multi-repo (v2.6.0, COPS-2507): the path map is partitioned by
   each Application's git source, so a PR can only ever match, fetch from,
   and comment on its own repo. Repos are configured via `DIFF_REPOS`
   (chart value `diff.repos`), e.g. `acme-config-dev;acme-config-stage:gcp/|azure/`.
   The optional `:scopes` suffix limits which path prefixes the service sees
   in that repo, tracking what ArgoCD actually manages there. Since v2.6.3
   the `azure/` tree in stage is in scope: `pv-stage-corporate-b` (AKS
   `az-prod-pv-na1-b`, COPS-2517) is ArgoCD-managed, so its PRs get the
   same diff comments as any GCP environment. The `aws/` tree stays with
   the legacy pipeline, so PRs touching only that are skipped in full
   silence (no comment, no build status). New-environment evaluation resolves
   `appspace.version` through the config.yaml hierarchy at the PR sha, since
   most stage/prod environments inherit it from a cohort-level config.yaml.

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

Four more scale behaviors are worth knowing on very large PRs (v2.5.18,
`bughunt/FINDINGS_SCALE.md`):

- **AI summary at scale.** Only the `AI_MAX_APPS` (default 40) apps with the
  most changed resources are included in the AI prompt, with an explicit
  "+N omitted" note to the model. Without the cap, a 300-app PR built a
  prompt past the model's context window and the summary silently failed on
  exactly the PRs that most need one. The deterministic headline (apps,
  environments, resource counts) always covers **all** apps.
- **Comment truncation preserves the footer.** Comments over the Bitbucket
  size limit (~245KB — an 800-app comment measures ~433KB) are cut in the
  middle, never at the end: the footer's machine-readable `[clean|permanent|
  transient]` and `[base:...]` tokens always survive, so SHA dedup and the
  main-advanced check keep working and a pod restart never re-diffs an
  unchanged mass PR.
- **Timeout hygiene under load.** A diff that hits `DIFF_TIMEOUT` returns at
  the timeout (chart pulls no longer block the worker until they finish in
  the background) and cancels every subtask it had queued on the shared
  pool, so retries cannot amplify congestion when the registry or renders
  are already slow.
- **Over the cap is permanent for that commit.** With more than
  `MAX_APPS_PER_RUN` affected apps, the same deterministic overflow set is
  never evaluated for that commit (the build status is FAILED and the
  comment names the knob). Raise `MAX_APPS_PER_RUN` (`diff.maxAppsPerRun`)
  or split the PR; retries will not cover them.

> **Known trade-off — retries sleep in-worker.** A transient failure
> (`oci_pull_failed`, `metadata_pending`, `timeout`) is retried with backoff
> inside the same `DIFF_WORKERS` slot, so during a registry blip on a mass PR
> a worker spends most of its wall time sleeping. This keeps the retry logic
> simple and correct; a requeue-based design would raise throughput but is a
> larger change. Revisit only if blip-storms during mass bumps become common.

### Secret-leak and comment-integrity hardening

The rendered diff and the AI summary both derive from PR-controlled content,
so several layers guard what reaches the Bitbucket comment (see
`bughunt/FINDINGS_IMPROVEMENTS.md` for the full analysis behind each):

- **Kind-aware, structural redaction.** `kind: Secret` bodies are whole-masked;
  other kinds get key-name redaction that also handles YAML block scalars
  (`key: |`) and the `- name:/value:` env-var shape. Redaction runs at display
  time, before truncation, so the diff engine still compares real values.
- **Error details are redacted too.** A `helm template` YAML error echoes the
  offending source line; those details are masked before they can reach the
  comment or build status.
- **Comment-injection is neutralized.** Triple-backtick sequences in rendered
  values can no longer break out of the bot's ```` ```diff ```` fence, so
  untrusted content cannot inject a fake status line or hidden Markdown.
- **AI output is sanitized.** The model summary (built from untrusted values)
  has Markdown images, raw HTML, and HTML comments stripped before posting,
  closing the zero-click image-exfiltration channel; the AI never sets the
  build status.
- **Isolated helm pulls.** Each `helm pull` runs with a private
  `HELM_*` home so concurrent pulls of different chart versions cannot corrupt
  helm 3.x's unlocked shared OCI blob cache.

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
| `AI_MAX_APPS` | — | `40` | Max changed apps included in the AI summary prompt (largest diffs kept; the headline still counts all apps) |
| `DIFF_OCI_SELFCHECK_INTERVAL` | — | `900` | Seconds between periodic OCI self-checks (`helm show chart` against a known-good ref, exercising the authenticated pull path). First check runs ~60s after start; `0` disables. Result in `/diff-preview/stats` as `oci_selfcheck`, failures log at ERROR |
| `DIFF_OCI_SELFCHECK_REF` | — | *(last successful pull)* | Optional fixed reference `registry/chart:version` for the self-check; when unset the last successfully pulled chart is used |
| `DIFF_OCI_FAIL_ERROR_THRESHOLD` | — | `3` | Consecutive systemic chart-pull failures (auth/network, not 404s) after which failures log at ERROR instead of WARNING, enabling log-based alerting |
| `DIFF_IGNORE_RESOURCES` | — | *(empty)* | Extra comma-separated resource-name substrings to hide from every diff, on top of the built-in `micro-versions-info` |
| `DIFF_HTTP_POOLING` | — | `on` | HTTP keep-alive connection pooling (v2.5.20): one persistent TLS connection per worker thread and host instead of a fresh handshake per call. Set `off` to route every request through plain `urlopen` again. Automatically defers to `urlopen` when a proxy is configured (`HTTPS_PROXY`/`HTTP_PROXY`), since the pool speaks directly to the host. Pool behavior is visible in `/diff-preview/stats` (`http_pool_reuses` / `http_pool_fresh_conns` / `http_pool_fallbacks`) |

---

## Repository layout

```
acme-diff-preview/
├── src/
│   ├── diff_preview.py        Main service (Deployment)
│   └── dev_hard_refresh.py    Full hard-refresh of all dev/QA apps (CronJob)
├── tests/                     367 tests, 3 layers — see "Tests" below
│   ├── test_diff_preview.py           Core unit tests
│   ├── test_v2*.py                    Per-release regression suites
│   └── test_coverage_*.py             Synthetic-infrastructure layer
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

## Tests

```bash
python3 -m pytest tests/ -q                                  # full suite (550 tests)
python3 -m pytest tests/ -q --cov=src --cov-report=term      # with the coverage report
```

**Coverage: 99.8% of `src/` overall — `dev_hard_refresh.py` at 100%, `diff_preview.py` at 99.8%** — measured over seven complementary layers, all of which run with **zero external infrastructure** (no cluster, no Bitbucket account, no registry, no real helm), on any machine and in CI:

- **Core unit tests** — the pure logic: diff parsing and filtering, secret redaction, comment formatting, rename/decommission/tier-move classification, the traffic-light coherence rules. Every rule that has ever been wrong in production has a regression test pinning the fix.
- **Per-release regression suites** (`test_v2*.py`) — each hardening release since v2.4.5 ships with the tests that first reproduced its bugs (red) and now guard them (green): webhook memory caps, HMAC non-ASCII crashes, identity-aware rename following, decommission warnings, downgrade visibility, Pages-deploy races.
- **Synthetic-infrastructure layer** (`test_coverage_*.py`) — the previously live-only surface, now deterministic: the REAL `http()` retry/backoff engine against a local stub server (5xx retries, 429 `Retry-After` honoring, network failures); both webhook endpoints driven with genuinely HMAC-signed requests against the real health server on an ephemeral port (including the dedup window and the "invalidation crashed but the webhook still answers" contract); ArgoCD discovery and the JFrog hard-refresh path through fake `argocd` binaries; the CronJob script end to end (auth, listing, per-app refresh, the timeout guard); the full Bitbucket comment/status layer (open-PR pagination, comment find/create/update with the size-limit truncation, stuck-INPROGRESS status repair, raw file fetches with the 404-vs-transient distinction, and the value-file cache's poison rule — a transient fetch error is never remembered as "missing"); the login readiness circuit-breaker, chart-republish cache invalidation, on-disk helm chart cache pruning, and the heartbeat thread (liveness refresh + clean shutdown); the AI summary call, mocked at the same `http()` boundary as every other external dependency; and — the centerpiece — **`process_pr`, the ~600-line orchestrator, walked end to end** through a scripted PR world: happy diffs, SHA dedup, republish-forced recomputes, the indeterminate-means-FAILED traffic-light rule, visible-but-non-blocking downgrades, new-environment rendering and structural blocks, the `MAX_APPS_PER_RUN` cap, invalid-version accounting, and the outer exception safety net that must never let one PR's crash affect the others.
- **Helm orchestration layer** (`test_coverage_helm_layer.py`) — a fake `helm` binary (the same technique as the fake `argocd` above) exercises everything OUR code does around the helm CLI: registry login success/failure/TTL caching, the pull-once per-chart-version locking, the memory-and-disk cache with dev-tag TTL semantics, permanent-vs-transient error classification from stderr (`OciChartNotFound` vs retry-and-give-up), the atomic tmp-dir rename and the v2.5.14 orphan-directory cleanup (asserted: an exhausted-retry pull leaves NOTHING behind under the cache root), chart subdir discovery, `helm template` argv/values-file plumbing, and — the payoff — **`_run_one_diff` end to end**: the fake helm emits manifests driven by the canned value files, so the *real* parse-and-compare pipeline (`_parse_manifest_resources` → `_diff_resources`) computes a genuine diff, a genuine no-diff, a render-failure classification, a permanent chart-not-found, and the effective-version-change report. The line that stays honest: helm's render *correctness* (what manifests a given chart actually produces) is helm's contract, not ours, and is deliberately NOT asserted — these tests validate our caching, locking, classification, and diffing logic, which treats helm output as opaque YAML.
- **Deep-branch sweep** (`test_coverage_deep_branches.py`) — driven by an exhaustive per-line gap analysis (coverage.py `analysis2`, grouped by function), this layer closes the error and fallback branches the happy paths never reach: `_run_one_diff`'s pull/render timeouts, executor-wrapped `OciChartNotFound` unwrapping, per-side pull failures, folder-move fetch failures, the corroborated per-file rename follow, the unchanged-file reuse rule (an untouched value file is never re-fetched at the PR sha), and the main-render cache cap; `_ensure_chart`'s dev-tag TTL eviction, the stale-build *parking* rule (never rmtree under an in-flight render), and the pull-lock re-check that reuses a concurrent thread's finished pull; `_render_new_env_diff`'s full error ladder plus its happy path; `process_pr`'s confirmed-decommission skip, the real chart pre-warm (with a not-found tolerated as a warning), the complete FAILED/SUCCESSFUL description matrix, and the SIGTERM mid-diff drain that must never mark the PR as seen; `main_iteration`'s discovery-failure recovery, non-fatal JWT refresh, stale-state eviction, and the outcome rollup; every `fix_stuck_inprogress` state derivation; `upsert_comment`'s deleted-comment (404-on-PUT) re-create fallback; the value-file singleflight *waiter* path; and `http()`'s malformed `Retry-After` tolerance. A standard `.coveragerc` (documented coverage.py defaults) excludes only the `if __name__ == "__main__":` entry guards, which by definition never run under pytest.
- **Last-mile sweep** (`test_coverage_last_mile.py`) — a re-audit after the deep-branch pass showed most of the remaining lines were ordinary error branches, plus three lines whose earlier tests had passed through *sibling* arms (an `HTTPError` where the uncovered arm wanted a generic network exception — and `_bb_fetch_status` turned out to use urllib directly, so the seam is `urlopen`, not `bb()`; a permanent reason where the loop-exhaustion arm wanted a retryable one; `time.time()` seeded into a `time.monotonic()` dedup map). This layer closes them: both raise contracts of `find_existing_comment` (fast-path transient and page-scan — either must skip the PR rather than post a duplicate comment), the JFrog hard-refresh's malformed-app-list guard and per-app failure counting, the degraded `/healthz` verdict, the dedup map's ancient-entry prune, unidentifiable YAML documents in the manifest parser, redaction passthrough lines, `process_pr`'s revision-future crash / legacy-incomplete-comment rerun / pre-warm metadata gaps and crash absorption / the oci-not-found-alone status, a diff crash inside `process_batch` surfacing as that app's error in the comment, and **`main()` itself in a single pass** — an iteration crash is caught and logged, the loop survives, and shutdown completes cleanly.
- **Precision sweep** (`test_coverage_precision.py`) — a second re-audit of the ~37 lines then documented as "genuinely unreachable" found that most were not races at all, just ordinary branches nobody had fed the right combination of inputs yet. Closing them surfaced the same lesson as the last-mile pass, twice over: a "final move step" race in `_ensure_chart` (another thread already created `chart_dir`) turned out to be reachable *single-threaded*, just by pre-creating an empty `chart_dir` before the pull — no real second thread required, since the check only cares whether the path exists; and `argocd_diff`'s post-loop fallback (`last_reason`/`last_detail` defaults) is unreachable through normal retry exhaustion (the last iteration always returns explicitly) but fires immediately with `DIFF_RETRIES=0`, where `range(0)` never executes the loop body at all. This pass also caught four of its OWN early drafts silently passing for the wrong reason — asserting on a helper fixture's `tmp_path` instead of the fixture's *actual* `HELM_CACHE_DIR` subdirectory, a stray file placed one directory level too deep, a dedup set that's only populated after a filter the test's empty `path_map` always failed, and a SHA comparison against the full hash where the source compares against its 8-char prefix — each one a reminder that a green assertion proves only what it checks, not what you meant to check. Real ground newly covered: the stale-dev-dir park failing with `OSError` (falls back to `rmtree`), the release-registry lookup for non-dev chart versions, a rename-target's cache-HIT path, a `kind: List` item whose continuation line isn't 2-space indented, the decommission-candidate dedup, a Bitbucket diffstat page-limit warning, the legacy pre-`[token]` comment format, and the fully-decommissioned-affected-set path into `process_batch`'s own empty-list guard.

Building the synthetic layers surfaced a real test-isolation bug, fixed along the way: four older regression files stubbed `generate_ai_summary`, `http`, or `post_build_status` by assigning straight to the module (`mod.x = ...`) instead of through `monkeypatch.setattr`, so the stub silently outlived its own test and broke any *later* test in the same pytest session that needed the real function. Converted to `monkeypatch.setattr` throughout — same behavior within each test, properly restored afterward.

The remaining 4 lines (0.16%) are genuinely dead defensive code, confirmed by direct analysis rather than assumed: the post-loop fallback in `http()` and in `_bb_fetch_status()` (both retry loops explicitly `return`/`raise` on every reachable exit — including the final iteration — so the line after the loop can never execute given the current exception handling); an `if not doc:` guard in `_parse_manifest_resources()` whose only caller, `_split_yaml_docs()`, already filters out empty documents before yielding them; and an `if not delta:` guard in `_diff_resources()` for two unequal strings producing an empty `difflib.unified_diff()` — verified empirically to be impossible, since `splitlines(keepends=True)` is injective enough that unequal input always yields at least one diff line. These are left uncovered on purpose rather than covered by contorting the code to make them reachable.

The discipline that keeps this trustworthy is in [RELEASING.md](RELEASING.md): every change ships with a regression test written first and confirmed failing, then the full suite, then live pod verification.

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
