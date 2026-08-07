# Task: redesign acme-diff-preview PR comments for operator scannability

## Context

`acme-diff-preview` is a webhook-driven Python service (`src/diff_preview.py`, ~9,800 lines, single file) that watches PRs across `acme-config-dev`, `acme-config-stage`, and `acme-config-prod`, renders the Helm chart diff for every affected environment (via `argocd_diff` / `_run_one_diff`), and posts a single Bitbucket PR comment summarizing what changed (`format_comment`, line ~8182, and `upsert_comment`, line ~6797).

Operators reviewing these PRs before merge are the audience. Two colleagues who review these comments daily gave three concrete pieces of feedback, below. Your job: analyze the current implementation in depth, then design and implement the fix. This is a real production tool used to gate merges into environments that are currently deploying to 259+ live customer environments, so correctness and not breaking the existing regression suite matter more than speed.

Repo: `~/gitprojects/acme-diff-preview` (current version `2.26.0`, chart at `charts/acme-diff-preview/Chart.yaml`).

## The three problems to solve

### 1. VM-related changes need their own unmistakable, separated section

Operators say the current comment doesn't make it obvious whether a PR touches virtual machine infrastructure — the thing they're most worried about breaking, because unlike a Kubernetes rollout, a botched VM change (wrong machine type, wrong disk, accidental deletion-policy flip) is slow and painful to recover from.

The relevant domain is `appspace.infra.deployLinuxServicesK8s` in `acme-components`, rendered by three Helm templates, one per role:
- `helm-charts/supporting-services/templates/kcc-linux-services/compute-instance-svc.yaml` (role `svc`)
- `helm-charts/supporting-services/templates/kcc-linux-services/compute-mongo.yaml` (role `mongo`)
- `helm-charts/supporting-services/templates/kcc-linux-services/compute-rabbit.yaml` (role `rabbit`)

Each emits (at minimum) a `ComputeInstance` and a `ComputeDisk` KCC resource. Read these three templates plus `compute-disk-svc.yaml`, `compute-address-svc.yaml`, and `compute-boot-disk-policy.yaml` in the same directory to get the complete field list. The fields operators care about most, concretely:
- `ComputeInstance.spec.machineType` (instance type — short name like `n2d-standard-4`)
- `ComputeInstance.spec.zone`
- `ComputeInstance.spec.desiredStatus` (RUNNING/TERMINATED — the runbook comment in the template explains a machine-type change requires stopping the VM first; a PR that changes `machineType` without going through TERMINATED first is itself worth flagging)
- `ComputeInstance.spec.bootDisk.initializeParams.size` / `.type` (only rendered when a new boot disk is being created)
- `ComputeInstance.spec.deletionProtection` and the `cnrm.cloud.google.com/deletion-policy` annotation (abandon vs delete — driven by `appspace.infra.deployLinuxServicesK8s.{defaults,svc,mongo,rabbit}.allowDeletion`)
- `ComputeDisk.spec.size` (GB) and `.spec.type` (`pd-ssd`/`pd-standard`/etc.)
- Whether `deployLinuxServicesK8s.enabled` or `<role>.enabled` flips at all (a VM appearing or disappearing from an environment for the first time)

Two levels of detection are both needed, and today neither exists as a dedicated panel:
- **Values-level**: a change under `appspace.infra.deployLinuxServicesK8s.*` in a `customer.yaml`/`config.yaml`, caught the same way `_summarize_input_changes` (line ~7856) already catches generic key changes — but today it's just another bullet point in that generic list, with no extra visual weight.
- **Rendered-manifest-level**: an actual diff in the rendered `ComputeInstance`/`ComputeDisk`/`ComputeAddress`/`ComputeDiskResourcePolicyAttachment` objects, which today would only show up buried inside the normal per-app resource diff (`_diff_manifests`, `_parse_manifest_resources`, `_section_kind` at line ~4875), with no special flagging — a `machineType` change looks exactly as visually important as a label change today.

**What to build**: a new, clearly separated markdown panel (own `##` header, own emoji, e.g. following the existing severity-panel style used by `_summarize_appspace_state_changes` at line ~7959 for autosync/decommission flags, or the chart-downgrade block in `format_comment` around line ~8280) that:
- Fires only when something in the VM domain actually changed (values-level or rendered-level, per above) — silent otherwise, per problem #3 below.
- Names the environment, the role (svc/mongo/rabbit), and the exact field(s) that changed, old → new.
- Distinguishes clearly between "routine" VM changes (e.g. a disk size increase) and genuinely dangerous ones (deletion-policy flipping to `delete`, `deletionProtection: false`, a live VM's `enabled` flag going false, a machine type change with no corresponding `desiredStatus` transition).
- Is inserted into `format_comment`'s panel assembly in the right priority position — look at the existing order (`appspace_state_lines` → `input_change_lines` → `decommission_lines` → chart-downgrade → deleted-resources → renamed-resources → AI summary → large-PR table → per-app diffs) and decide where VM changes should sit; operators asked for this to be unmistakable, so near the very top is likely right, but confirm against how `_prioritise_risk_sections` (line ~5046) already ranks risk so you're not inventing a second, inconsistent priority scheme.

Write this as its own function (e.g. `_summarize_vm_changes`), unit-testable independently, following the docstring/reasoning-comment style already used throughout the file (every non-trivial function here explains the *why*, often citing a bughunt finding or COPS ticket — match that).

### 2. Large output must go to the full artifact, never bloat the comment

There is already a mechanism for this that is under-used: `_save_diff_ui_artifact` (line ~6772) persists the full, pre-truncation comment body to a small web UI (`diff_ui.py`), gated by `DIFF_UI_ENABLED` (default true), and `_truncate_comment` (line ~6725) already knows how to cut from the middle of a comment while preserving the machine-readable footer.

The gap: `_truncate_comment` only engages at `MAX_COMMENT_BYTES = 245_000` (Bitbucket's hard limit, line ~266) — the actual comment-size ceiling for *readability* should be much lower than the platform's hard limit. Today a 150KB comment (well under the Bitbucket cap) sails through untouched even though no operator is going to read 150KB in a PR comment.

**What to build**: a second, much lower, proactive threshold (a new constant, something like `COMMENT_READABLE_BYTES` or similar — pick a number that fits maybe one screen-scroll of critical content, discuss/justify your choice, look at what a typical "small" PR comment is today for a sane baseline) that:
- Applies well before `MAX_COMMENT_BYTES`.
- When exceeded, keeps every high-priority panel in full (VM changes, decommission warnings, chart downgrades, deleted resources — anything already proven "critical" by an existing panel) and demotes/collapses the bulk material (ordinary per-app diff sections, the large-PR table body) with a clear pointer to the full artifact: `_save_diff_ui_artifact` already builds a URL via `DIFF_UI_BASE_URL` — use it, and reference `diff_ui.save_artifact`'s return value / URL construction so the comment can link directly to the full diff instead of just saying "see the pod logs."
- Must not break the existing dedup contract described in `_truncate_comment`'s docstring: the footer with `[clean|permanent|transient]` and `[base:xxxxxxxx]` tokens must always survive.
- Should reuse the existing per-section budget pattern already established for `_summarize_input_changes` (`_INPUT_CHANGES_MAX_LINES = 24`, line ~7772) rather than inventing a new capping mechanism from scratch — extend the same idea to the per-app diff section loop.

### 3. Comments should be short by default, and routine changes should be summarized, not enumerated

Beyond the size-based truncation in #2, operators want the comment to *read* short even when it fits comfortably under any byte limit. The specific example given: version/chart bumps across many services or environments should collapse into one line, not one line per service.

Look first at what already exists for this: `_rollup_by_service` (line ~7809) already groups identical key changes across services into `"...for N services: a, b, c"` inside `_summarize_input_changes`. Check whether this same rollup is applied everywhere it should be — in particular, whether ordinary chart-version bumps (as opposed to config-key changes) get the same treatment, or whether they still get enumerated per-app in the per-app diff section loop later in `format_comment`. There's also an existing `generate_ai_summary` (line ~7447) mechanism producing a model-written summary block — read it and decide whether it already covers "summarize the boring stuff" well, whether it should be leaned on more (e.g. promoted higher, or given an explicit instruction to compress routine bumps into one line), or whether a deterministic (non-AI) summarizer is more appropriate for something this load-bearing (the existing code philosophy elsewhere in this file strongly prefers deterministic facts over AI-only claims for anything safety-relevant — see the reasoning comment above `_detect_deleted_resources`'s deterministic-computation note around line 5), and match that philosophy rather than routing this through the AI summary if the rest of the codebase would consider that fragile.

**What to build**: a concrete rule (and code) for "routine" categories (plain version/chart bumps being the flagship example, but check if there are other repetitive-and-safe categories the current comment enumerates verbosely) that collapses them to one summary line per distinct change, with a count and a "+N more" style rollup consistent with `_rollup_by_service`'s existing UX, while anything already flagged critical by an existing panel (deletions, downgrades, decommission, and the new VM panel from #1) stays fully enumerated regardless of how "routine" it looks statistically — never silently fold a dangerous change into a summary line.

## How to work

1. Read `src/diff_preview.py` end to end for the areas above before writing any code — this file rewards reading the surrounding reasoning comments; do not guess at intent from function names alone.
2. Read `RELEASING.md` and follow it exactly: write the regression test first (confirm it fails against current behavior), implement, run that test file, then the full suite (`python3 -m pytest tests/ -q`, ~2.5 minutes). For anything touching timing/concurrency also do a real local behavioral check per that doc.
3. Check `tests/golden/` (10 existing fixtures) for the pattern used to golden-test comment output, and add/update golden fixtures for the new VM panel and the new truncation behavior rather than only asserting on substrings.
4. Update the README to reflect the new comment sections and truncation behavior — this repo's convention is that every release updates the README to match current behavior (see existing README conventions on multi-repo coverage).
5. Bump the chart version (`charts/acme-diff-preview/Chart.yaml`, currently `2.26.0`) following whatever the existing versioning convention is (check recent git log / CHANGELOG for the pattern — patch vs minor for this scope).
6. Write the GitHub/Bitbucket release notes short: one-line summary + 2–5 bullets + optional test note, under ~120 words, each bullet as one continuous line (no hard-wrapping — the renderer shows literal newlines as hard breaks).
7. Do not reference any Jira/COPS ticket key inside code comments or docstrings — this repo's convention is ticket keys live only in the commit message and PR title; in code, explain the *why* directly (this file's existing comments are full of good examples of that style — copy it).
8. Before opening a PR, sanity-check the new VM panel and the new truncation behavior against at least one real historical PR from `acme-config-prod` that touched `deployLinuxServicesK8s` (search recent history for one) and one large mass-rollout PR, so the new behavior is verified against real data, not just synthetic fixtures.

## Deliverable

A PR against this repo's default branch implementing all three changes, with:
- New/updated tests proving each of the three behaviors (a VM-domain change is flagged in its own panel and non-VM-domain changes never trigger it; a comment that would have exceeded the new readable-size threshold is trimmed with a working link to the full artifact while every critical panel survives intact; a batch of routine version bumps collapses to a rollup line while a single dangerous change among them still gets fully enumerated).
- An updated README section describing the new comment anatomy.
- A short summary at the end explaining what you found in the existing code that already solved part of the problem (so nothing gets reinvented) versus what was genuinely missing.
