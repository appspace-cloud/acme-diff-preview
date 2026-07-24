# Guard: empty `microservices.definitions` (COPR-31637)

## Summary

acme-diff-preview blocks any config-repo PR that sets
`appspace.microservices.definitions` to a **null or empty** map in a Helm
value file. Left unblocked, the change silently breaks image names across an
entire environment on merge, causing cluster-wide `ImagePullBackOff`.

## The incident

`pv-qa11-a` (GCP QA, cluster `gcp-qa-pv-ap1-a`, deployed by ArgoCD) went to
`ImagePullBackOff` on `platform`, `webhook`, and `screenshots`. The pods were
pulling:

- `appspace-platform` (0 images in the registry, ever)
- `appspace-webhook` (0 images)
- `appspace-screenshots` (0 images)

The correct, long-standing image names for these services carry a `-service`
suffix (or a shortened form):

- `appspace-platformservice` — 300+ images in prod since 2024-11
- `appspace-webhookservice` — 140+ images since 2024-12
- `appspace-screenshot`

So the build/push naming was never wrong. The chart was never wrong.

## Root cause

The `micro-services` chart ships a per-service `image.name` override in
`values.yaml` for exactly these services, because their published image names
intentionally differ from the chart helper's derived default:

```yaml
appspace:
  microservices:
    definitions:
      platform:
        image:
          name: appspace-platformservice
      webhook:
        image:
          name: appspace-webhookservice
      screenshots:
        image:
          name: appspace-screenshot
```

The chart helper `appspace.imageName` uses `image.name` verbatim when present,
otherwise it derives `appspace-<microservice-key>`. Without the override,
`platform` derives `appspace-platform`, which has no images.

ArgoCD renders each environment by merging its Helm value files in order, with
the per-env `cicd-versions.yaml` **last**. That file was committed as:

```yaml
appspace:
  microservices:
    definitions:
```

`definitions:` with no children parses to YAML `null`. In Helm's `merge`, a
null value at that key collapses the **entire** definitions map, so every
`image.name` override is wiped. All three services then derived the broken
`appspace-<key>` name and failed to pull.

Verified by rendering the exact OCI chart ArgoCD uses (`2602.4.15-dev`) with
the real merged values:

- populated `definitions` → `appspace-webhookservice` (correct)
- empty `definitions` → `appspace-webhook` (broken)

## Trigger commits (acme-config-dev)

- `454780d425` (2026-07-24) — removed the children under `definitions` but left
  the key, breaking `pv-qa11-a`.
- `1015bc622` (2025-09-19, "remove CICD") — a structurally identical earlier
  instance, confirming this is a repeatable mistake, not a one-off.

## Fix that resolved the incident

acme-config-dev PR #6909 commented the whole block out, so the `definitions:`
key no longer exists and the chart's own map (with the overrides) is kept.

## The rule the guard enforces

For every changed `*.yaml` / `*.yml` value file in a PR, fetched at the PR's
commit SHA:

- **Blocked** — `appspace.microservices.definitions` is present but its value
  is `null` or an empty mapping (`{}`).
- **Allowed** — the `definitions` key is **absent** (the chart's own map is
  kept intact), or it has real children, or the file is unparseable YAML (that
  fails elsewhere in the render; the guard never blocks on a parse error), or
  the fetch hit a transient error (never block a merge on a flaky read).

On a block, the service posts a `FAILED` Bitbucket build status (which prevents
merge where green builds are required) and a PR comment explaining the danger
and the fix.

## How to remove per-env overrides safely

Delete the `definitions:` key **entirely**. Never leave it present but empty:

```yaml
# safe: key removed, chart map kept
appspace:
  microservices:
    repository: some/repo
```

```yaml
# DANGEROUS: wipes every image.name override on merge
appspace:
  microservices:
    definitions:
```

## Where this lives in the code

`src/diff_preview.py`:

- `_values_wipes_definitions(body)` — the pure YAML-shape classifier.
- `_detect_wiped_definitions(changed_files, sha, repo)` — fetches each changed
  value file at the PR sha and returns those that wipe the map.
- `process_pr(...)` — runs the guard before any diff/app logic and blocks.

Tests: `tests/test_v2120_wiped_definitions_guard.py`.
