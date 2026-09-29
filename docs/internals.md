# acme-diff-preview internals

Reference material moved out of the README to keep the front page short. Nothing
here is required to use the tool; it is for people changing it or debugging it.

## Contents

- [Why a YAML slip is blocked](#why-a-yaml-slip-is-blocked)
- [Why a clone without its `--aec1` token is blocked](#why-a-clone-without-its---aec1-token-is-blocked)
- [Why renaming a live environment is blocked](#why-renaming-a-live-environment-is-blocked)
- [Why a teardown is blocked](#why-a-teardown-is-blocked)
- [Why removing a cohort `config.yaml` is blocked](#why-removing-a-cohort-configyaml-is-blocked)
- [Why an environment without a chart version is blocked](#why-an-environment-without-a-chart-version-is-blocked)
- [Why waking an AEC clone is blocked](#why-waking-an-aec-clone-is-blocked)
- [Why a copied clone ashn is blocked](#why-a-copied-clone-ashn-is-blocked)
- [Why a new env needs an ApplicationSet glob](#why-a-new-env-needs-an-applicationset-glob)
- [Why a key in a file the ApplicationSet does not read is blocked](#why-a-key-in-a-file-the-applicationset-does-not-read-is-blocked)
- [Why turning the legacy Helm writer back on is blocked](#why-turning-the-legacy-helm-writer-back-on-is-blocked)
- [Why a move that turns noCore off is blocked](#why-a-move-that-turns-nocore-off-is-blocked)
- [Handling mass version bumps](#handling-mass-version-bumps-hundreds-of-apps-in-one-pr)
- [The two surfaces: comment and page](#the-two-surfaces-comment-and-page)
- [Which resources make it into the comment body](#which-resources-make-it-into-the-comment-body)
- [What one app shows when it changes hundreds of resources](#what-one-app-shows-when-it-changes-hundreds-of-resources)
- [Superseding an in-flight render](#superseding-an-in-flight-render)
- [Secret-leak and comment-integrity hardening](#secret-leak-and-comment-integrity-hardening)
- [Full-diff web UI](#full-diff-web-ui-atlantis-style)
- [Why a toleration with a wrong operator or effect is blocked](#why-a-toleration-with-a-wrong-operator-or-effect-is-blocked)

---

### Why a YAML slip is blocked

<a id="why-an-empty-microservicesdefinitions-is-blocked"></a>

A YAML slip changes config, and no diff line shows it clearly. So a PR to
`main` is blocked (COPS-2766) when it adds one of these to a changed `*.yaml`
or `*.yml` file:

- **A duplicate key**: the same key twice in one map. YAML keeps only the last
  copy and drops the first one with no error. acme-config-prod #3583 lost a
  login whitelist for 53 h this way (COPR-31148).
- **A bare key**: `key:` with no value. It is null, and Helm then deletes that
  key from the chart defaults, for example the probes or the HPA policies. An
  explicit `null` or `~` is not a slip: it removes a chart default on purpose.
- **An empty `microservices.definitions`**: the key is present, but it is not
  a map with children (null, `{}`, `[]`, `""`, a number). It deletes every
  image name the chart ships (see below).

Helm and ArgoCD read only the first YAML document of a file, so the check reads
only that one. A `definitions:` before a second `---` document is a wipe too.
2.121.0 missed it, and it also missed `definitions: []` and `definitions: ""`.

**Only the slips the PR adds.** A duplicate or bare key counts only in a value
file under `gcp/`, `azure/` or `aws/` (not the pipeline YAML under `.ci/`), and
only when the PR adds it. The check counts each key path in the file at the
merge preview and in the same file on `main` (the old name for a move), and
blocks only on the difference. So a file with an old slip stays green, and a
third copy of an old duplicate is new. A key path is dotted and a list item is
`[]`, so moving list items does not make an old slip look new. `<<` merge keys
are skipped here, but the wipe follows them, as Helm does. A new file counts
every slip. A move that Bitbucket did not pair is compared with the deleted
file of the same env folder and name. Over 6 months of acme-config-prod this
would have blocked 6 PRs, all real slips (#3245, #3289, #3299, #3411, #3583
and #4322). Without the compare with `main` it would have blocked 52.

**The wipe counts at head alone**, in every changed YAML file, as in 2.121.0,
so that guard never gets weaker. ArgoCD merges an environment's Helm value
files in order, with the per-env `cicd-versions.yaml` **last**. A file shaped
like

```yaml
appspace:
  microservices:
    definitions:        # <- key present, no children => YAML null
```

collapses the **entire** `microservices.definitions` map to null in Helm's
`merge`, wiping every per-service `image.name` override the chart ships
(`appspace-platformservice`, `appspace-webhookservice`, `appspace-screenshot`,
…). Each affected service then falls back to the chart helper's derived
`appspace-<key>` name, a registry path that for these services has never held
an image, so the whole environment goes `ImagePullBackOff` on the next sync.
This is the COPR-31637 incident. A **missing** `definitions` key is safe (the
chart's own map is kept intact) and is not blocked. To remove per-env
overrides, delete the `definitions:` key entirely. See
[`docs/microservices-definitions-guard.md`](microservices-definitions-guard.md)
for the full incident write-up.

Limits:

- Without a merge preview the base is the tip of `main`, not the merge base.
  An old slip that `main` fixed later would then look new, so only the wipe is
  checked, and the skip is logged.
- A file that does not parse on `main` has no old slips to compare with, so
  only the wipe is checked, and the skip is logged. A PR that fixes the YAML
  is not blocked for the slips that were already there.
- A file that is absent at the PR side is skipped, and so is one that does not
  parse: the render reports bad YAML.
- A failed read is red and retried, never a pass.
- There is no override. The fix is always in the YAML: keep one copy of a
  duplicate key, give a bare key a value (or write `null`), or delete the
  `definitions:` line.

The red status names the file, the line and the key. When every slip is a
wipe, it keeps the text of the 2.12.0 guard:

```
BLOCKED: YAML slip in pv-orch-a/customer.yaml line 5: duplicate key appspace.zeroPods - see PR comment
BLOCKED: 1 file(s) empty out microservices.definitions (wipes image overrides)
```

### Why a clone without its `--aec1` token is blocked

A clone environment runs on the same cluster and cloud project as the
production tenant it copies. Its names must carry a token (`--aec1`, `--sbx1`),
or it reuses production's names: acme-config-prod PR #4671 cloned NBC with
`customerName: nbc` (COPR-32566).

A clone is a value file under `<cloud>/aec/` (`--aec1`) or `*/sandbox/`
(`--sbx1`), or in a folder with an `--aec<n>` / `--sbx<n>` token. Under `aec/`
or `sandbox/` the path sets the word; a folder token only sets the number
(`--aec2`).

The guard reads every changed clone value file (`cicd-versions.yaml` too: it is
merged last), and only its first YAML document, as Helm does. These must carry
the token when they are set:

- the folder name, for a `customer.yaml`
- `customerName`, `customerSubdomain` and `instanceName`
- `infra.deployLinuxServicesK8s.svc.instanceName`
- each name in `infra.deployLinuxServicesK8s.mongo.instances` and
  `rabbit.instances` (KCC and ASO give these names to the VMs)
- `microservices.acmePingScaler.pingHost`

A missing value, or one Helm treats as unset (`""`, `false`, `0`), is not
checked. The comment gives the fixed name for each value. The old side of a
move (a 404) is skipped. Any other failed read gives a red status that is
retried with the COPS-2546 backoff, never a pass. Every new commit is checked
again, and the same comment is edited.

The guard reads the file as it is after the merge, but it blocks only the misses
that are not on `main` yet. A miss already on `main` is a live environment, and
fixing it is a rename (see the next section). Direct pushes to `main` are not
checked.

### Why renaming a live environment is blocked

The private-cloud ApplicationSets name an environment's apps and namespace
`pv-<customerName>-<suffix>`, with suffix `a` when it is not set. They read the
two keys from `customer.yaml`, and from the `config.yaml` one folder up only when
`customer.yaml` does not have the key (even an empty value in `customer.yaml`
wins). So a change of one of them on a live environment is not an in-place
change. ArgoCD creates new apps in a new namespace and deletes the old apps
without pruning (`preserveResourcesOnDeletion: true`). Before ArgoCD removes the
old apps, they can sync the new values into the old namespace. The old namespace
keeps running, and its KCC resources manage the same GCP objects as the new
environment. acme-config-prod PR #4672 did this (`nbc` to `nbc--aec1`,
COPR-32565).

The guard (COPR-32578) checks every live `customer.yaml` under
`<gcp|azure>/<tier>/private-cloud/<spoke>/`. Live means it exists on `main` with
its cohort `config.yaml`. It blocks a PR to `main` when the file:

- changes its `customerName` or `suffix` (removing `suffix: b` renames the env
  to `-a`),
- moves to another cloud, tier or spoke folder (another ApplicationSet or
  cluster, like #4517), or moves to a folder where it gets another
  `customerName` or `suffix`,
- or gets a changed `customerName` or `suffix` from its cohort `config.yaml`.

A move between `prod` and `aec` in the same spoke keeps the cluster and the
namespace, so the new apps take over. It is blocked only with `decommission`.

It compares `main` with the merge preview, first YAML document only. A bad date
or an unknown tag is read as text, as ArgoCD does. A file without
`customerName`, or YAML that nothing can read, is left to the render, which
fails on it. A failed read is red and retried, never a pass.

A planned rename passes only as a migration:

1. A separate PR first adds `autosync: false` under `appspace:` (and
   `instanceName` if it is missing) and is merged. A pause added in the rename
   PR itself does not reach the old apps.
2. The rename PR keeps `autosync: false` and has a commit with the line
   `Confirm-Rename: <old namespace> -> <new namespace>`. For a move to another
   cluster, each side starts with the folder part that changes, like
   `na4-a/<namespace>`. The comment gives the exact command.

`decommission: true` or `decommissionPurgeData: true`, on `main` or in the PR,
always blocks: the old apps would delete GCP objects that the new environment
still uses.

The confirmation is a commit because Bitbucket Cloud PRs have no labels, and a
new commit is a new sha, so the check runs again by itself. Merging the pause PR
moves `main`, which also runs it again. The blocked comment lists the steps in
order: the 8 secrets to copy (without them the new environment gets new
passwords and keys), and for a `customerName` change the new, empty content
bucket and BigQuery dataset. When the rename passes, the comment shows the steps
after the merge.

Limits:

- An empty commit keeps earlier approvals (smart approval reset), so approve
  after the confirmation.
- Direct pushes to `main` are not checked. On prod the Azure DevOps build
  status also counts, so a merge in the seconds between a push and this check
  is possible.
- Deleting a cohort `config.yaml` that still has envs below it has the same
  effect. It is blocked too (next section).
- Bitbucket pairs renamed files by similar content. A PR that removes one env
  and adds a different, similar one can look like a rename: split it into two
  PRs.
- A live env that leaves the ApplicationSets is not a rename for this guard:
  a rename to `Customer.yaml`, a move out of `<cloud>/<tier>/private-cloud/`,
  or a delete + add with a new name that Bitbucket does not pair. ArgoCD then
  removes the apps without pruning, like any deletion without `decommission`.
  The delete + add is a teardown of the old env: with no cascade armed it is
  blocked until `Confirm-Teardown: <env>`, and the panel says it looks like a
  rebuild or a rename (next section). A move to a folder without a cohort `config.yaml` is
  blocked by COPS-2552.
- Without a merge preview the guard reads the branch tip, which can only
  over-block.

**Always use a PR for these changes.** A direct push to `main` skips every
check, and ArgoCD applies it at once. So never push these to `main` directly:
renaming an environment (`customerName`, `suffix`, or a folder move), deleting
a `customer.yaml`, or deleting a cohort `config.yaml`. The pipeline commits on
`main` only change versions (`version`, `cicd-versions.yaml`): they never
touch these keys, and never add, delete or move a file.

### Why a teardown is blocked

Removing an environment folder is Phase 3 of `acme-components`
`documentation/decommission-environment.md`, the only destructive step. Up to
2.121.0 the comment could say DO NOT MERGE while the build stayed green, and a
green tick outranks a red paragraph. Since COPS-2766 every case where the
teardown does not do what the reader expects is a merge gate. The build is
FAILED, the comment still shows the diff, and the merge summary starts with one
⛔ line per open gate.

| Check | When | Lifted by |
|---|---|---|
| No cascade | a private-cloud folder removal with no `appspace.decommission` on `main`. The Applications go and every workload keeps running, unmanaged. | `Confirm-Teardown: <env>` |
| Public cloud | any `cl-*` folder or block removal. Public cloud has no cascade (COPS-2700), so nothing is deleted by itself. | `Confirm-Teardown: <constellation>` |
| Shared user content | the purge is armed, and a surviving environment uses the same user content bucket and DNS record | `Confirm-Teardown: <env>` |
| 7-day hold | the cascade is armed, and `zeroPods` or `decommission` has been true on `main` for less than 7 days | `Confirm-Decommission: <env>` |
| Cascade not live | the cascade is armed in config, but ArgoCD has not put `resources-finalizer.argocd.argoproj.io` on the Applications | nothing, it clears itself |
| Flag typo | a teardown flag that is misspelled or in the wrong place, so Phase 2 reads pending | nothing: fix the key on `main` in a separate PR, then rebase the removal |

The same mechanism has three gates outside a teardown:

- `Confirm-IP-Release: <env>`: a `ComputeAddress` or `DNSRecordSet` leaves the
  render of a live env with no explicit `deletion-policy: abandon`, so GCP
  releases the IP. For these two kinds a new name is a new IP, so it is never
  read as a rename.
- `Confirm-Rename: <old dir> -> <new dir>`: a live `cl-*/config.yaml` is
  renamed or moved, which renames every Application of the constellation. The
  two sides are the full folder paths. The rename guard above uses the same
  trailer with namespaces.
- A disk shrink: nothing lifts it, because GCP cannot shrink a disk in place.

How the line is read:

- `<env>` is the name the gate line shows: the pv folder name, or the
  constellation for `cl-*`, so one line covers every block of a constellation.
  The ⛔ line in the comment and the red status both give the exact line to add.
- It can be in any commit message of the PR, on its own line. Case does not
  matter. Backticks, a final `.` and a leading `>` or `*` are ignored, and `→`
  reads as `->`. An empty commit is fine:
  `git commit --allow-empty -m "Confirm-Teardown: pv-x-a"`.
- One line lifts every open gate with the same trailer and env.
  `Confirm-Teardown: pv-x-a` lifts the no-cascade and the shared user content
  gates of `pv-x-a`, never its hold.
- A lifted gate stays in the merge summary as a ☑️ `Confirmed in a commit`
  review line, so the override is visible. The build is then green, so that
  finding shows ⚠️ like every other line.
- It is a commit for the same reasons as the rename: Bitbucket Cloud PRs have
  no labels, and a new commit is a new sha, so the check runs again. Earlier
  approvals stay (smart approval reset), so approve after the confirmation.

It fails closed:

- The commit messages are read only when an open gate has a trailer, so a PR
  with no gate never reads them. They come from the git mirror first, then from
  the Bitbucket API with every page and a check that the list already has the
  new head. When they cannot be read, the status is red and a blip is
  retried. It is never a lift.
- The hold reads the first-parent history of the removed `customer.yaml` on the
  git mirror, newest first, so the time of a commit is when it landed on
  `main`. A history that cannot be read is never "hold met". When the mirror
  cannot answer yet (a commit not fetched, a git error), the status says
  `Waiting` with `[transient]` and the check runs again, so a blip never asks
  for the override. When no retry can help (the mirror off by config, an old
  version that does not parse), the gate says it could not confirm the hold
  and still needs `Confirm-Decommission`.
- The finalizer is read from ArgoCD. Not live is a red `Waiting` status with
  the token `[transient]`: the poll loop retries it with the usual backoff
  (COPS-2546), and it clears itself after ArgoCD syncs. When the lookup fails, the panel
  shows one ⚠️ line that asks you to check `argocd app get`, and the build stays
  green: a red build on every failed lookup would teach people to skip the
  panel.

A pause on `main` (`appspace.autosync: false`) is not a gate. Live on
pv-qa88-a (2026-09-29), the Applications kept
`resources-finalizer.argocd.argoproj.io` while auto-sync was off, and ArgoCD
runs the cascade on delete whatever the auto-sync is. The risk is different: a
change made on `main` during the pause (zeroPods, the purge policy) may not be
live yet. So the panel has one ⚠️ line next to the phase table, the merge
summary has a ⏸️ review item, and the build stays green.

When the PR also adds an environment, the decommission panel adds one 💡 line:
it looks like a rebuild or a rename of the old env, so arm decommission on the
old env, or use `git mv` with `Confirm-Rename`. On `cl-*` the flag arms
nothing, so there the line only names `git mv`. A `git mv` that the rename
guard lets through (`Confirm-Rename`, paused on `main`) needs no
`Confirm-Teardown` for its old folder: the Planned rename note already says
the old namespace keeps running.

Replay on the acme-config-prod history: the teardown gates would have
stopped one PR that was fine, of 525 (#4396). About one Phase 3 PR a month
needs `Confirm-Decommission`, because most of them arm and remove on the same
day, and one was a real catch: #4298 removed `pv-fordpoc-a` 37 minutes after
the PR that armed it. In six months the IP gate would have stopped two PRs
that were fine (#3222 and #3668, planned IP cutovers).

Direct pushes to `main` are not checked, as for every guard.

### Why removing a cohort `config.yaml` is blocked

The private-cloud ApplicationSets make an environment's apps from its
`customer.yaml` and the `config.yaml` one folder up (its cohort; for an env
right under a spoke, the spoke `config.yaml`). When that file is missing, they
make no apps for the environments below it, and ArgoCD deletes their apps.
Without `decommission: true` it keeps their resources, so the namespaces keep
running with nobody to manage them, like a rename (COPR-32565). The `customer.yaml` files stay in the repo, so the
repo still says the environments exist.

So a PR to `main` is blocked (COPR-32578) when it removes a private-cloud
`config.yaml` (deletes or moves it) while live environments below it stay:
their `customer.yaml` has live apps and is still there in the merge preview.
There is no override. History has 4 such cases (acme-config-prod #768 and
#1354, acme-config-stage #1996, and a direct push in acme-config-dev), all by
mistake inside a change about something else, found between 32 hours and 18 months
later. The 46 real cohort removals moved or removed their environments in the
same change, which this check allows.

The fix is to keep the file where it is. If those environments move, move
them in the same PR. If they go away, remove the file in the same PR that
removes them, after their decommission. Do not leave the file empty instead:
most environments take `appspace.version` from the cohort, and without it their
apps stop syncing. This check does not block an empty file or bad YAML,
because then ArgoCD does not delete the apps. The render reports bad YAML.

Limits:

- The live environments come from the app list, which refreshes every few
  minutes (`PATH_MAP_TTL`). An environment merged to `main` just before may not
  be counted yet.
- Public-cloud `cl-*/config.yaml` files are not part of this check. Removing
  one gets the public-cloud teardown panel.

### Why an environment without a chart version is blocked

The ApplicationSets set the chart `targetRevision` to `appspace.version`, taken
from the generator file: `customer.yaml` over the `config.yaml` one folder up.
For public cloud, the constellation apps (ms, ss) read `cl-*/config.yaml`
alone, and each numbered GLB app reads `cl-*/appN/customer.yaml` over it (the
fixed GLB apps read `cl-*/config.yaml` alone). When there is
no version, the template writes `watch-only`, a value that never resolves. The
apps go Sync Unknown and stop: no sync, no self-heal, and no later change
reaches them. Nothing is deleted. This happened on acme-config-prod #4042 (15
apps, 3 h 53 min). The render of this service would use the old chart and look
normal, so the check runs before it.

A PR to `main` is blocked (COPR-32578) when, after the merge, a generator file
has no usable version. A key present in `customer.yaml` wins even when it is
empty, so `version: ""`, `version:`, `~`, `false` and `0` all hide the cohort
value. The literal `watch-only` counts too. It is also blocked when a
`customer.yaml` is empty or its `appspace` is not a map: then the whole
ApplicationSet of the spoke stops. There is no override.

It checks the changed or moved `customer.yaml` files, new private-cloud
environments, and, when a cohort `config.yaml` has no version after the PR, the
live environments below it. In public cloud it checks only files the generator
really reads and that have live apps: `cl-*/config.yaml`, and
`cl-*/appN/customer.yaml` of a live `<cl-*>-appN-glb` app (not
`constellation/customer.yaml`, which is only a Helm values file). A removed
environment is not checked: that is a decommission.

### Why waking an AEC clone is blocked

An AEC clone (a folder with an `--aec<n>` token) starts with a copy of the
production data of the tenant it copies. When its pods run, it can call the
customer live integrations: SSO, webhooks, mail and connected apps
(AE-15507). So the runbook
(https://appspace.atlassian.net/wiki/spaces/cops/pages/66093626) creates a
clone asleep with `zeroPods: true`, restores and cleans the data, and wakes it
in a later PR. A replay of 6 months of acme-config-prod found 18 PRs that woke
47 clones, about 3 a month (#4660 woke 13 at once).

The `clone_wake` merge gate (COPS-2766) fails the build when a PR:

- adds a clone `customer.yaml` whose values do not set `zeroPods: true`,
- or takes a live clone from `zeroPods: true` to `false` or unset. The change
  can be in its own `customer.yaml` (a move reads the old path on `main`) or
  in a `config.yaml` above it.

The value is the one the chart sees: the `config.yaml` files above the
environment, then its `customer.yaml`, and the last one wins. It is read at
the head and on `main`. A version bump on a clone reads only the own
`zeroPods` of the file, on both sides. The whole chain is read only when that
key changes, when the file moves, or when a `config.yaml` with live clones
below it changes its own `zeroPods`.

The comment lists the Mongo collections that must be empty on the clone.
Write their counts in the PR, then push an empty commit with
`Confirm-Clone-Sanitized: <env>`, one line per clone. The line is read like
the other confirmation lines: in any case, with or without backticks.

It fails closed. Values that cannot be parsed are a gate too, and the same
line lifts it. A failed Bitbucket read is not a gate: the build is red with
`[transient]` and the PR is checked again after the backoff.

Limits:

- `zeroPods` stops the pods, not the Core VM, so this gate does not cover the
  Core VM.
- A wake written in `cicd-versions.yaml` is not seen, because the chain does
  not read that file. The pipeline writes only versions there.
- Only `--aec<n>` folders count as clones here, not `--sbx<n>` sandboxes.
- Direct pushes to `main` are not checked.

### Why a copied clone ashn is blocked

`appspace.ashn` names an environment in Customers and PDNS. acme-config-prod
#4042 made four AEC clones with the ashn of the production environment each
one copies (heb, universal with the ashn of universalhollywood, blackrock and
pfizer). So each clone and its original look like one environment there.
`main` has no duplicate ashn today (294 environments set one).

The `ashn_copy` merge gate (COPS-2766) looks at each changed clone
`customer.yaml` (a folder with `--aec<n>`) whose own `appspace.ashn` is new or
changed against `main`. A move reads the old path on `main`. It fails the
build when an environment with another `customerName` has the same ashn: the
own `customer.yaml` of a live environment on `main`, or another file of this
PR. The b and c clones of one customer share a `customerName`, so they are no
hit. A move or a delete is not a copy, because the old path is left out.

Nothing lifts it. Give the clone its own ashn (runbook step 2: change ashn,
suffix, customerName and instanceName). The tier token guard runs first, so a
clone that misses `--aec1` gets that block before this one.

The live environments are read only when a clone ashn changes: the own
`customer.yaml` of each one on `main`, from the git mirror first. Only a
complete read is kept, and only for the last `main` of each repo. A file that
cannot be read or parsed is not checked, and a ⚠️ line says how many. When
the ArgoCD app list is not loaded, a ⚠️ line says that the check is
unavailable. Neither one is a gate. A failed read of a file of the PR retries
the PR.

### Why a new env needs an ApplicationSet glob

ArgoCD makes the apps of an environment from the ApplicationSets on the hub.
Their git files generators list the identity files they read as globs, for
example `gcp/dev/private-cloud/ap1/**/customer.yaml`. A new environment, or a
live one that moves, in a folder that no glob reads gets no apps. Nothing
deploys on merge, and nothing says so. A replay against today's 135
ApplicationSets found one case in 6 months, acme-config-prod #3423 (the
nachaos spoke).

The `appset_miss` merge gate (COPS-2766) runs `argocd appset list -o json`
with the ArgoCD token of the service. It keeps the git files paths of every
generator, nested ones too, whose `repoURL` is this repo (ssh or https, with
or without `.git`). A path with `{{` is a template, and an exclude path
deploys nothing, so both are left out. The list is cached for
`PATH_MAP_TTL`. On a miss it is listed again without the cache, so an
ApplicationSet applied a minute ago counts.

It checks the identity files of each new environment, and the new side of
each move: a private-cloud `customer.yaml`, a `cl-*/config.yaml` and a
`cl-*/app<N>/customer.yaml`. The other `cl-*` folders (`api`, `cloud`,
`constellation`, `user-content`) are rendered by the `cl-*/config.yaml` sets,
so they are not checked. A move whose old side no glob reads is no miss: it
deployed nothing on `main` either. The legacy pipeline deploys `aws/`, not
ArgoCD, so a new `aws/` environment gets a ⚠️ check line instead.

The matcher reads `**/` as zero or more folders, and `*` and `?` do not match
`/`. The hub does not set
`applicationsetcontroller.enable.new.git.file.globbing`, so ArgoCD uses the
old globbing, where `*` also matches `/`. For today's globs and the checked
shapes, both give the same result. As a self-check, every live identity file
of a checked shape must match a glob. When one does not, or the list fails,
is not JSON or has no glob (a token that may not list them gets an empty
list), the check is not proven. Then a new environment gets a ⚠️ check line
and a move gets a note, there is no gate, and the leader log says
`ApplicationSet check unavailable`.

Nothing lifts the gate. Check the cloud, tier and spoke folders. If the
ApplicationSet is being added in acme-infrastructure, apply it first, then
push again here (an empty commit is enough). A move to another cloud, tier or
spoke is a rename of a live environment, so that guard stops it first. This
gate shows for a move after the rename is confirmed.

### Why a key in a file the ApplicationSet does not read is blocked

The ApplicationSets take `appspace.version` (the chart `targetRevision`),
`autosync` and `decommission` only from the files their generators list, not
from every Helm value file. Written anywhere else, `version` and `autosync` do
nothing: no new chart, no pause. The diff stays quiet, and the PR looks done.
COPS-2684 was `autosync: false` in a constellation `customer.yaml`.
`decommission` in such a file adds no cascade finalizer, but the chart still
reads it (the static IP deletion policy, and the data purge with
`decommissionPurgeData`), so the teardown is only half armed.

So a PR to `main` is blocked (COPS-2766) when it sets one of these keys in a
file that no generator reads:

| Tree | Read by the generator | Not read |
|---|---|---|
| Private cloud, `<gcp or azure>/<tier>/private-cloud/<spoke>/` | every `customer.yaml` below the spoke, and the cohort `config.yaml` one folder up (the spoke `config.yaml` for an env right under the spoke) | `cicd-versions.yaml` and the other value files, a `config.yaml` next to a `customer.yaml`, and every `config.yaml` above the spoke (`gcp/config.yaml`, the tier file) |
| Public cloud, `gcp/<tier>/public-cloud/<spoke>/` | `cl-*/config.yaml` (the whole environment) and `cl-*/appN/customer.yaml` (one numbered GLB app) | `cl-*/constellation/customer.yaml`, the fixed GLB folders (`api`, `cloud`, `user-content`) and `cicd-versions.yaml` |
| `aws/` | not checked: it has no ApplicationSet | |

A private-cloud `config.yaml` with no `customer.yaml` next to it counts as
read even when no env uses it yet, so a fleet bump stays green.

It is also blocked when `appspace.version` is not a string, in any file under
`gcp/` or `azure/`. The ApplicationSet prints a YAML number as a number, so
`2604.0` becomes `2604`, and ArgoCD can ask for a chart that does not exist.
The fix is to quote it. A `version` that is a map gets a tip: the chart's app
versions are `appspace.versions` (acme-config-dev #6845 wrote
`version.AppVersion`, so the upgrade did nothing).

Rules:

- Only a key the PR adds or changes counts, compared with `main` (the old name
  for a move). A removed key, or one with the same value on `main`, is not a
  hit.
- `decommission` in a public-cloud file is left out. It arms nothing there, and
  the decommission panel keeps its warning.
- An empty version in a generator file (`version: ""`, `~`, `false`, `0`) is
  the chart version check's (previous section).
- A failed read is red and retried, never a pass.
- There is no override. The fix is always to move, quote or delete the key.

The replay found no hit in 1023 acme-config-prod PRs (6 months) and 525
acme-config-stage PRs, and one in 525 acme-config-dev PRs (#6845).

```
BLOCKED: appspace.autosync in cl-prod-b/constellation/customer.yaml is never read by the ApplicationSet - see PR comment
BLOCKED: appspace.version in pv-x-a/customer.yaml is not a string - see PR comment
```

### Why turning the legacy Helm writer back on is blocked

ArgoCD owns the `ms`, `ss` and `glb` releases. The old ADO pipeline is still
there: `helm_deploy.sh` in `acme-components` reads
`appspace.infra.deployGLB`, `deployMicroservices` and
`deploySupportingServices`, and on the literal `true` it runs `helm upgrade` on
the same releases again. Two writers on one release fail with invalid
ownership metadata, or undo each other. COPS-2560 turned the old writer off,
and the root `config.yaml` says to keep it off. No chart reads these keys, so
the diff shows nothing.

So a PR to `main` is blocked (COPS-2766) when it sets one of the three keys to
`true` in a value file under `gcp/`, `azure/` or `aws/`, and the key was not
`true` on `main` (the old name for a move). A new file counts every `true`.
`helmForceUpgrade` is not a hit, because the stage and dev roots set it to
`true`. When a live env's `customer.yaml` changes it and nothing renders
differently, the 💤 Edits with no effect panel says that only the legacy
pipeline reads it.

An environment that really needs the old writer adds
`Confirm-LegacyHelm: <env>` to a commit message of the PR, and pushes. `<env>`
is:

- the `cl-*` folder, in public cloud,
- the env folder, for a `customer.yaml` or a `cicd-versions.yaml`,
- else the folder of the file, for example
  `gcp/prod/private-cloud/na1-a/monthly` for a cohort `config.yaml`, or `gcp`
  for `gcp/config.yaml`.

The comment and the red status give the exact line:

```
BLOCKED: deployGLB: true in pv-x-a turns the legacy Helm writer back on. To merge anyway, add Confirm-LegacyHelm: pv-x-a to a commit
```

The line is read by the same parser as `Confirm-Rename`: on its own line in any
commit message, and case does not matter. Then the check lets the PR through.
When the PR has a diff or only adds environments, the merge summary keeps a
☑️ `Confirmed in a commit` review line, so the override is visible. The commits are read only when there
is a hit. A commit list that
cannot be read is red and retried, never a lift, as for `Confirm-Rename`.

The replay found no hit in 1023 acme-config-prod PRs (6 months). Stage had 3
PRs and dev 1 (#6509), all from before the legacy retirement in July 2026.
Direct pushes to `main` are not checked, as for every guard.
### Why a move that turns noCore off is blocked

`appspace.infra.noCore` is more than a load balancer flag. On GCP it moves
the URL map default from the Windows Core VM to `<env>-bs-agw`, about 109
Deployments restart, and the v1 API moves to v3. On Azure and AWS it switches
the nginx-frontend upstream. Turning it off deletes the noCore backends `bs-pcs`
and `hc-pcs`. The rendered diff shows none of this as a risk:
acme-config-prod #4565 turned it on in 57 environments with a silent comment,
and #4684 gave 48 minutes of 502 because the Core VM was stopped before the
apps were Synced (COPS-2758).

So every PR compares the effective value of each live environment, `main`
against the merge preview. The effective value is the last one set in the
ancestor `config.yaml` chain, root first, and then in the environment's
`customer.yaml`: the same files `_merged_kcc_flat_for_env` reads, in helm `-f`
order. They differ from the ApplicationSet value files only in
`cicd-versions.yaml`, which never sets `infra`. A null counts as unset,
because Helm drops a null key, and a file whose first document is not a map
(a cohort with only comments) reads as empty. A move is read at its old path
on `main` and at its new path in the PR. A changed `config.yaml` checks the
live environments below it, but only when the key differs in that file, so a
PR that does not touch the key costs two cached reads per changed file. Every
flip is a ⚠️ review item with the Core VM rule, and the build stays green.

One shape fails the build: a move of a live environment after which noCore is
off only because the moved `customer.yaml` does not set it. That is
acme-config-prod #4667. pv-myschroders-a moved from `gb1-b/weekly`, where the
cohort sets noCore, to `gb1-b/hardcoded/weekly`, a cohort with only comments.
noCore went off, the URL map went back to the Core VM and the backends were
deleted: a 5 h outage. The spoke and the identity did not change, so the
rename guard lets it pass by design. The check is a merge gate
(`nocore_lost`), not an early stop, so the comment keeps the full diff with
the deletions. A replay over acme-config-prod `main` since 2026-03-29 (1465
commits, 78 `customer.yaml` moves) found 2 moves that flip noCore (#4684 on,
#4667 off) and one gate hit, #4667.

There is no trailer, because the fix is always a config change: set
`appspace.infra.noCore` in the moved `customer.yaml`, `true` to keep noCore
or `false` if Core must come back. A value there (not null) lifts the gate.
When a cohort at the destination sets `false`, the fix line names that file.
A move into noCore (#4684) is only a warning.

The reads fail closed. A value file Bitbucket cannot serve retries the PR,
never "no flip". A value file that is not valid YAML gives a `noCore check
unavailable` review line. The check never breaks the comment: a bug in it
gives the same line for the whole PR, with the error in the service log.

### Handling mass version bumps (hundreds of apps in one PR)

Bumping a chart `version:` across many clusters in a single PR is a normal
operation. Because the diff is a local `helm template` render with no agent
round-trips, the fan-out is cheap and the only shared resource is the Bitbucket
API used to fetch value files. What keeps it fast and reliable:

- **Chart-cache warm-up** — one representative app per distinct OCI chart is
  pulled first, so the rest reuse the local tarball (`WARM_WORKERS`/`WARM_THRESHOLD`).
- **Bitbucket API rate limiting + safe caching** — a global semaphore
  (`BB_API_CONCURRENCY`) caps concurrent calls; value files are cached by
  immutable `(commit_sha, path)`, and a transient error is never cached as
  "missing" so one app's rate-limit blip can't poison the others.
  A 429 is a property of the *token*, not of the one request that received it,
  so the pause is **shared**: the first caller to be rate limited publishes a
  deadline (`Retry-After` when Bitbucket sends it, `BB_RATELIMIT_FALLBACK`
  when it does not, capped at `BB_RATELIMIT_MAX_PAUSE`) and every other
  Bitbucket call brakes with it, waiting *outside* the semaphore so a sleeping
  thread does not hold a concurrency slot. This covers both the value-file
  path and the poll loop; non-Bitbucket hosts (Vertex AI, the GCP metadata
  server) keep their own per-request backoff and never trip the shared gate.
  429s log at `WARNING` **with the endpoint**, and a value file that could not
  be read is reported separately from one that is genuinely absent — the two
  used to share a single `debug()` line, which made rate limiting
  indistinguishable from a real gap in the values hierarchy. A 404 that says
  `Commit not found` is a failed read too: the merge preview exists only in
  the local mirror, so when the mirror cannot answer, Bitbucket does not know
  the commit. It is never cached as an absent file, and the PR is retried.
- **Retry with backoff + jitter** — transient reasons (`oci_pull_failed`,
  `metadata_pending`, `timeout`) retry in-process up to `DIFF_RETRIES` times.
- **AI summary at scale** — only the `AI_MAX_APPS` apps with the most changed
  resources go into the prompt (with a "+N omitted" note); the deterministic
  headline still covers every app.
- **Comment truncation preserves the footer** — an oversized comment is cut
  in the middle, never at the end, so the machine-readable `[clean|permanent|
  transient]`/`[base:...]` tokens always survive for SHA dedup.
- **Timeout hygiene** — a diff that hits `DIFF_TIMEOUT` cancels every subtask
  it had queued on the shared pool, so retries can't amplify congestion when
  the registry or renders are already slow.
- **Over the cap is permanent for that commit** — beyond `MAX_APPS_PER_RUN`
  affected apps, that commit's overflow set is never evaluated (FAILED status,
  comment names the knob); raise the cap or split the PR.

The on-disk chart cache is bounded (`HELM_CACHE_MAX_CHARTS`) and pruned at the
start of each iteration so a long-lived pod cannot fill node ephemeral storage.

> **Known trade-off — retries sleep in-worker.** A transient failure retries
> with backoff inside the same `DIFF_WORKERS` slot, so during a registry blip
> on a mass PR a worker spends most of its wall time sleeping. Simple and
> correct; a requeue-based design would raise throughput but is a larger
> change. Revisit only if blip-storms during mass bumps become common.

### The two surfaces: comment and page

Since 2.35.0 (COPS-2612) one function, `format_comment`, renders two different
artifacts, and which one it is rendering is carried by a `RenderProfile`
rather than inferred. `COMMENT_PROFILE` is the PR comment; `FULL_PROFILE` is
the full-diff page.

The split is not "short version / long version". It is **decision** versus
**evidence**:

| | `COMMENT_PROFILE` | `FULL_PROFILE` |
| --- | --- | --- |
| YAML hunks | no | always |
| Config-changes panel | no | yes |
| AI analysis | no | yes |
| Clean applications | one count | named, one per line |
| Byte-identical sections grouped | yes | no |
| Version-transition noise folded | yes | no |
| Per-resource body cap | 6,000 chars | none |
| Verdicts, deletions, VM facts, downgrades | **all, with names** | all |

#### The switches, and why they resolve at render time

`COMMENT_INLINE_DIFFS` (default `false`), `COMMENT_INPUT_PANEL` (`false`),
`COMMENT_INLINE_EVIDENCE_LINES` (`0`) and `FULL_PAGE_UNCAPPED` (`true`) are
read inside `RenderProfile.resolved()`, once per render, never snapshotted as
dataclass defaults at import.

This is not a style choice. `COMMENT_INLINE_DIFFS=true` is the one-variable
rollback to the pre-2.35.0 comment, and an import-time snapshot would make it
decorative: flipping it on a running pod would change nothing. The same trap
was hit twice during the COPS-2607 phases before the rule was written down.

`FULL_PROFILE` **pins** those three rather than resolving them. The page is
the complete record, so a comment-shape switch must not be able to empty it —
otherwise the rollback switch would delete the very thing it exists to fall
back on.

#### The rule that makes removing YAML safe

`format_comment` forces `inline_diffs` and `input_panel` back on when
`artifact_url` is empty. No URL means the artifact save failed or the UI is
off, so there is no page to hold the evidence, and the comment keeps it and
says why. Without that, a failed save would produce a comment with no
evidence anywhere.

The clearest proof it works is accidental: the four `test_cops2565` goldens
render without a URL, take this path, and still match the pre-2.35.0 comment
byte for byte.

#### Evidence moves, conclusions do not

The subtler rule, and the one that cost four bugs during COPS-2612. A diff
block contains both proof and conclusions, and only the proof belongs on the
page. The comment still states:

* how many of an app's resources are a version transition only, and **which
  one is not** (`Changed for another reason: ...`);
* every deletion, zeroed replica, downgrade, decommission and VM fact, by
  name;
* the real resource count per app, never `len(sections)`.

When adding anything to a diff block, the question to ask is which of the two
it is. If a reader would use it to decide, it stays in the comment.

#### `is_complete_record`

A `RenderProfile` field, not a check on `profile.name`. It means "this surface
IS the record, not a pointer to one", and it drives two behaviours: the page
renders no pointer to itself (with no URL to hand, that pointer degraded into
the page announcing that the page could not be produced — live for two
versions), and when the storage cap trims an app the page owns the shortfall
instead of directing the reader elsewhere.

It is a behaviour field precisely so a profile derived with `replace()` under
another name keeps behaving like the page.

#### Rollback order

`COMMENT_INLINE_DIFFS=true` **first**, then `FULL_PAGE_UNCAPPED=false`. The
other order leaves the comment without YAML *and* the page truncating, so the
information is gone. A rollback switch whose safe order is not written down is
a trap.

---

### Which resources make it into the comment body

An app can change hundreds of resources. Sections are stored up to
`FULL_SECTIONS_MAX_PER_APP` (5,000 since COPS-2610, memory-bounded, not an
arbitrary display cutoff). The counts in the headline and in the
`N resource(s) changed` line are always the real totals.

The cap is applied at **storage** time, so what it drops is missing from both
surfaces. At its old value of 400 it did that silently, while the comment's
own note claimed the remainder was "only in the full diff view" — exactly
where it was not. Hitting it now increments `section_cap_trims`, logs a
warning, and makes the page state the shortfall rather than claim to be
complete. That counter should stay at zero; if it moves, raise the cap.

Two rules decide what is shown, in order:

- **Detection runs on the full list, before any cap.** Deletions and
  replica zeroings are found by `_detect_deleted_resources` and
  `_detect_replicas_zeroed` inside `_package_sections`, on every section.
  A deletion sitting at position 111 of a mass diff is still named
  (the PR-6773 lesson, v2.5.26).
- **Risk sections get a reserved share of the display order**
  (`RISK_SECTION_RESERVE`, COPS-2567). Sections arrive sorted by resource
  key, so `/apps/Deployment` always sorts before
  `/autoscaling/HorizontalPodAutoscaler`. Taking a plain prefix meant that on
  acme-config-prod PR 3845 the ten display slots went to Deployments and none
  of the five HPA deletions the comment shouted about were visible anywhere in
  the body. The block told the reviewer to verify five deletions and showed no
  evidence for any of them, which reads exactly like a false positive.

`_prioritise_risk_sections` reorders and never drops, so `n_res` and any
consumer of the full section list are unaffected; only the caps remove
anything. The reserve is deliberately a share, not the whole budget: a PR that
deletes 200 resources must still show some ordinary changes. When no deletion
and no zeroing exist, the list is returned untouched, so the ordinary case is
byte for byte what it was before.

The truncation note reflects this. It says `Showing first N of M` only while
the slice really is a prefix, and names the risk-first ordering otherwise.
With the storage cap this generous, real apps almost never truncate at all
now; the note mostly exists for the pathological case.

### What one app shows when it changes hundreds of resources

The rules above pick which resources of a fleet are shown. They do nothing
for the shape that turned out to be the most common one in
`acme-config-prod`: a platform version bump applied to a SINGLE environment.
A census of the last 100 prod PRs found that 80 of them touch exactly one
environment, that the version bump is the dominant operation, and that 10
percent of the comments sat exactly on the Bitbucket 245KB hard cap. PR 3884
is one app with 185 resource sections and PR 3891 has 473 across 30 apps.
The readability budget shipped in COPS-2605 could not help there, because it
is checked once per app before rendering that app, so the first app always
renders in full. With one app, the budget never engages at all.

Worse than the size: PR 3891 added a brand new
`cnrm.cloud.google.com/reconcile-interval-in-seconds` annotation to its KCC
resources. It was in the comment, and no reviewer could ever have found it
inside 473 near-identical hunks.

Three layers now run inside each app, in this order.

**1. Version-transition fold (`_classify_version_fold`).** The changed lines
of a bump section come from a tight vocabulary: image tags, chart labels,
checksum annotations, version-carrying env values and deploy timestamps.
The classifier pairs every changed line of a section by YAML key, and folds
the section only if EVERY pair classifies as one of those. A pure addition,
a pure deletion, an unbalanced key or one unknown line makes the whole
section a needle that stays inline. Env `value:` pairs are the ambiguous
case, so they are only accepted when the same transition was already seen on
an unambiguous carrier (an image tag, a chart label, `targetRevision`, or the
app-level chart `version_change`). That is why a `MAX_WORKERS: 4 -> 16`
change can never fold. Fewer than `_VERSION_FOLD_MIN` foldable sections
means no fold at all, because one fold line costs more attention than the
two hunks it would hide. An image tag that goes down (`_image_tag_downgrade`)
never classifies, so its section stays inline and the merge summary names it
(COPS-2766, #4679). The rule is off when the app's chart moves and does not
go up (`_images_may_go_down`): a chart downgrade takes every image down with
it and has its own finding, and another tag of the same version is not a
downgrade. Then the images fold as before.

Like every other safety fact, this is computed in `_package_sections` on the
FULL pre-cap list, so what folds never depends on a display cap. Sections
already claimed by a deletion, a zeroing or a VM fact are exempt by
construction, and the needles join those facts in the
`_prioritise_risk_sections` reservation, so the one interesting resource
survives the storage cap instead of being dropped at position 300.

**2. Repeat grouping (`_group_repeated_sections`).** One identical change
applied to many resources (the annotation above, added to every KCC member)
is one fact, not 12. Sections are keyed by the signature of their changed
lines only, so context lines and resource names do not split a group. The
first section of a group renders its hunk in full and the rest are named
under a `same change` line. Risk sections are never grouped: a reviewer
verifying a deletion needs to see that deletion, not a pointer to a sibling.

**3. Intra-app readability budget.** Whatever survives the first two layers
is still bounded. A running byte counter (`used`) grows as chunks are
appended, and once it passes the room left in the budget the remaining
ordinary sections are named in one `omitted` line instead of inlined. Risk
sections are exempt from this cut, and the first section of an app always
renders, so a block is never reduced to just its headline.

Every one of the three points at the full-diff page, which is rendered with
the budget disabled and grouping off, and therefore never folds, groups or
omits anything. Folding in the comment is only ever safe because that page
is the complete record.

On the two worst PRs in the census, the comment goes from the 245KB hard cap
to about 32KB, with the reconcile-interval needle visible in both.

### Grouping apps with an identical diff (COPS-2579)

A shared ancestor file (for example `gcp/config.yaml`) can change one thing
for every environment in a fleet at once. Before COPS-2579, each app's
sections were capped at 10 for storage (a limit meant only for the AI
summary prompt, reused by mistake as the comment's own storage cap), and the
comment showed a fixed top few apps inline and a one-line summary for the
rest. On acme-config-prod PR #3837 (removing a Spot compute-class override
from 248 apps, 67 resources changed per app) this meant 60 of 16616 real
diff sections were ever shown, repeated as 6 arbitrary, mutually duplicate
copies, and the scheduling fields under review were masked by an unrelated
redaction bug on top of that.

Each app's DiffResult now carries a `fingerprint`: a stable hash of its full
(pre-cap) section list, independent of section order and of the app name
itself (`_fingerprint_sections`). `format_comment` groups changed apps by
this fingerprint (`_group_changed_apps_by_fingerprint`) and renders ONE full
representative diff per distinct fingerprint, with the complete list of
member environments named above it, instead of one diff per app. The
overview table still lists every app individually (so per-environment
visibility is not lost) and labels which diff group it belongs to.

Apps with no fingerprint (a legacy or hand-built `DiffResult`, for example in
a test that constructs one directly instead of going through
`_package_sections`) always form their own singleton group and never merge
with anything, so this is fully backward compatible with any code path that
does not compute a real fingerprint.

Total output size is now bounded by the number of DISTINCT diffs in a PR, not
by the number of apps: a PR where 248 apps share one change produces one full
diff; a PR where 248 apps each have a genuinely different change still
produces up to 248, protected the same way it always was by the per-body
(`DISPLAY_BODY_MAX_CHARS`) and whole-comment (`MAX_COMMENT_BYTES`)
truncation.

COPS-2679: that collapse is COMMENT-only (`group_repeats=True`). The FULL
page (`is_complete_record`, `group_repeats=False`) keeps one block per app
so overview deep links (`#app-…`) and the Index cover every environment.
Persisting a collapsed body made "see the full diff view" a dead end on
fleet PRs like acme-config-prod #4316.


### Superseding an in-flight render

Two cases, one mechanism. A render is worth aborting when the snapshot it
started from is already dead — whether that is because the PR's **own** branch
moved (COPS-2575) or because the **destination** branch moved under it
(COPS-2617).

#### The destination-branch case

A merge on `main` invalidates every open PR against it. Before COPS-2617 that
was only noticed *after* a render finished, by comparing the `[base:]` token
on the already-published comment — a full re-render rather than an abort.
Measured on acme-config-prod, four merges in ~8 minutes: 6 passes across two
large PRs, **4 of them against a base commit that was already dead**, and a
564-app comment rewritten 3 times purely from unrelated merges.

Three properties of the destination hint, each deliberate:

* **Keyed by `(repo, base_branch)`, not per PR.** One merge invalidates every
  open PR against that branch, and a burst has to cost one extra pass in
  total, not one per merge. Last writer wins.
* **Peek, never pop** — the one place it differs from `_arm_supersede`. That
  hint belongs to a single PR, so consuming it is right. This one is shared,
  so the first PR to read it must not consume it for the others. It clears
  naturally once a render starts from the new base.
* **Only `pullrequest:fulfilled` arms it.** A push to a PR's own branch must
  never read as "main moved", or every other open PR would abort on every
  unrelated push.

**A hint only counts if it arrived after the poll that produced the snapshot
(COPS-2633).** Comparing the hint and `base_sha` for plain inequality cannot
tell a hint that is *newer* than the snapshot (a real supersede) from one the
snapshot has already moved *past* (a leftover), and the second case is
permanent rather than rare: the config repos take direct pushes to `main`
from release automation, which fire no `pullrequest:fulfilled` event, so
`main` advances beyond the last merge commit and nothing corrects the hint
again. Measured on acme-config-stage #2802: the hint sat at an ancestor of
`main`, and every PR in the repo was skipped three times — about 3 minutes of
delay before its first comment — until the livelock guard released it.

So hints carry a sequence number, and the poll loop publishes the tip it
actually read (`_note_base_observed`) before processing any PR. That read is
ground truth about where the branch is, so it retires any hint recorded
earlier — which covers what a webhook cannot: a direct push, a squash that
rewrote the commit, or a `fulfilled` event that never arrived. A sequence
counter rather than a clock, because two `monotonic()` reads can be equal and
"equal" would have to be resolved one way or the other, silently making one of
the two cases wrong. The trade-off is deliberate: if Bitbucket's refs read lags
a merge it has already announced, the hint is ignored and the PR renders
against a base a few seconds old — the pre-COPS-2617 behaviour, still caught
after the render, and far cheaper than every PR waiting out the livelock guard
on a hint that will never be correct.

Both cases share `_supersede_lock`, `_sha_eq` normalisation, and the
`SUPERSEDE_MAX_CONSECUTIVE_ABORTS` livelock guard — without that ceiling a
merge train would starve a large PR out of ever publishing anything, which is
worse than publishing slightly stale.

#### A PR that is already merged (COPS-2766)

A merged PR can still be in the open-PR list when `main` already has its merge
commit. The poll then rendered the PR against its own merge commit and replaced
the verdict the reviewers saw: 7 of 525 merges were re-rendered this way, and
#4504 got a false FAILED. So right after the destination check, `process_pr`
asks the mirror `git merge-base --is-ancestor pr_sha base_sha`. On True the PR
is skipped: no comment, no status, no `_seen`, and one
`pr_skipped_already_merged` log line per head sha. False and None (mirror off,
sha not fetched yet, git error) render as before. Only True and False are
cached, because a missing sha can arrive with the next fetch.

Limits. A squash merge makes a new commit, so the head is not an ancestor and
the PR renders as before (acme-config-dev had 5 of those since 2026-08-01,
stage and prod none). A PR merged while it renders still posts its pre-merge
verdict, which is honest. An open PR whose head is already in `main` (a stacked
branch) keeps its last status, and merging it changes nothing.

#### The PR's-own-branch case

Two pushes landing on the same PR inside one render window used to mean the
first render ran to completion against a commit that was already dead,
published that diff into the shared PR comment, and only then did the real
commit get rendered. Measured on acme-config-prod PR 3837: 6m17s from the
final push to the final result, 3m10s of it rendering a commit nobody would
ever merge, and for ~10s the PR carried a build status for one commit next to
a comment describing another.

The webhook already knew. `pullrequest.id`, `pullrequest.source.commit.hash`
and `repository.full_name` all arrive in the body, which `do_POST` read,
HMAC-verified, and then discarded, deciding purely on the `X-Event-Key`
header.

**How it works now**

- The webhook handler records a hint, `(repo, pr_id) -> newest sha`, under its
  own lock (`_seen_lock` already guards three structures and is held by the PR
  workers; this one is written from HTTP handler threads).
- `process_pr` **arms** on entry by atomically popping any pending hint. A pop,
  not a clear: a webhook that lands while the PR is still queued behind others
  (`PR_WORKERS=3`, minutes on a busy iteration) writes its hint *before*
  `process_pr` runs, and clearing would destroy the only signal that the
  snapshot is already stale. Popping also means a hint the snapshot already
  reflects, typically the very webhook that started this iteration, is consumed
  without aborting a perfectly correct run.
- Three check points: at entry, inside the `as_completed` loop in
  `process_batch` (where the wasted minutes are actually saved, cancelling the
  queued futures exactly like the SIGTERM drain does), and once more after the
  batch so a supersede detected on the final future still prevents publication.

**Abort semantics.** A superseded run writes nothing: no comment, no build
status, and crucially no `_seen` entry, so the PR is re-rendered rather than
skipped. The backoff is deliberately not fed either, since a supersede is not
a transient failure and must not slow the retry down. The `INPROGRESS` already
posted sits on a sha that is no longer the tip, so Bitbucket shows only the
new tip's statuses and it is inert. No change is needed to the wake path:
`_wake` was already set by the superseding webhook and is only cleared after
the iteration, so the next pass starts immediately on the new sha.

**Livelock guard.** A PR pushed to faster than it can render would abort
forever and never publish anything. After `SUPERSEDE_MAX_CONSECUTIVE_ABORTS`
consecutive aborts the run is allowed to finish; the counter resets whenever a
run publishes a real result.

**Two details worth keeping straight**

- The repo key comes from `repository.full_name` (`workspace/slug`), never
  `repository.name`, which is a *display* name and can differ from the slug
  entirely. Keying off the display name would make hints silently never match
  and the whole feature a no-op with no error anywhere.
- Hints are recorded only for `pullrequest:created` and `pullrequest:updated`.
  Comment, approval and decline events also start with `pullrequest:` and also
  embed a full pull request entity, but their sha is just the current tip.
  All of them still wake the loop, exactly as before.

**The wake path is sacred.** This is the first thing that ever parses the
webhook body, so it is the first thing that could break the wake, and a broken
wake fails *silently*: the service just degrades to the 60s safety-net tick
and everything feels sluggish until somebody notices. So `_wake.set()` is the
first statement in the `pullrequest:` branch, before anything touches the
payload; `_maybe_record_supersede_hint` is total (every parse failure, missing
key, wrong type or unknown repo simply means "no hint"); and
`tests/test_cops2575_supersede.py` asserts the ordering on the handler source
itself plus a table of hostile payloads that must each still return 200 and
still wake the loop.

Hints are only trusted when HMAC verification actually ran. `_verify_bb_hmac`
is permissive when `BB_WEBHOOK_SECRET` is empty, and an unauthenticated POST
that can abort in-flight renders is a cheap denial of service.

**Observability.** `/diff-preview/stats` now reports `bb_webhook` counters
(`received`, `rejected_hmac`, `rejected_format`, `wakes`, `hints_recorded`,
`supersedes_triggered`, `base_hints_stale_dropped`, `last_received_at`, plus
`hmac_strict` and `supersede_enabled`), and `_diff_stats` tracks whether each
iteration was started by a webhook or by the safety-net tick. That ratio is
what catches the failures no unit test can: the hook deleted or disabled in
Bitbucket, the URL changed, an ingress rule dropping the POST, or the secret
drifting out of sync after a rotation. In all of those the code is perfectly
correct and the service is quietly running on the 60s poll.

`base_hints_stale_dropped` is the COPS-2633 equivalent for the destination
hint: it counts hints retired because the poller had already seen `main` move
past them. A steady climb is expected on any repo that takes direct pushes to
`main`; it is here because that condition was invisible for as long as the bug
lasted.

### Secret-leak and comment-integrity hardening

The rendered diff and the AI summary both derive from PR-controlled content,
so several layers guard what reaches the Bitbucket comment:

- **Kind-aware, structural redaction** — `kind: Secret` bodies are whole-masked;
  other kinds get key-name redaction that also handles YAML block scalars and
  the `- name:/value:` env-var shape, applied at display time so the diff
  engine still compares real values.
- **Error details are redacted too** — a `helm template` YAML error echoes the
  offending source line; that gets masked before it can reach the comment.
- **Comment-injection is neutralized** — triple-backtick sequences in rendered
  values can't break out of the bot's diff fence to inject a fake status line.
- **AI output is sanitized** — the model summary has Markdown images, raw
  HTML, and HTML comments stripped before posting; the AI never sets the
  build status itself.
- **Isolated helm pulls** — each `helm pull` runs with a private `HELM_*`
  home so concurrent pulls of different chart versions can't corrupt helm
  3.x's unlocked shared OCI blob cache.


---

### Full-diff web UI (Atlantis-style)

The PR comment has a hard Bitbucket size limit (`MAX_COMMENT_BYTES`, ~245KB):
an oversized comment is cut in the middle and, until now, the complete output
only existed in the pod logs. With `DIFF_UI_ENABLED` (**on by default**, see
below), the service persists the COMPLETE, untruncated comment body for the
PR (already redacted, the exact text the comment would carry), together with
the same at-a-glance context the comment header shows (base commit, apps
evaluated, per-outcome breakdown), and serves it on the health port.

#### Completeness, and the two caps that remain (COPS-2610)

The page never cuts a resource body. Before that was enforced it was quietly
failing its own promise: one stored production artifact carried **981**
occurrences of `... (diff truncated for display)`. The 6,000-char body cap is
a *comment* protection — one giant ConfigMap rewrite would push the comment
past `MAX_COMMENT_BYTES`, whose blunt global cut would chop off the footer and
the status token the poll loop parses. On the page it was only a lie.

Two caps survive, neither silent:

* **Visible rows**, default 20,000. Everything past that is still in the HTML
  behind a `show full output` button, and `/raw` is byte-exact. This is a
  browser-survival number, not a policy: the largest real artifact is 786,150
  lines and 113MB of HTML, so laying out every row on first paint is a hung
  tab rather than completeness.
* **Stored sections**, `FULL_SECTIONS_MAX_PER_APP` — see above; it counts and
  logs when it bites.

#### Retention lives in the bucket, not in the prune

`_prune` only walks the local artifact directory, which is a **cache**. The
durable copy is the GCS bucket, and its object lifecycle is what decides how
long a page opens: 365 days, set in `acme-infrastructure`
(`shared/infrastructure/acme-diff-preview-artifacts`). Raising
`DIFF_UI_MAX_ARTIFACTS` would not extend retention by a day.

Because the artifact is rewritten on every commit, the age clock runs from the
PR's last diff run. The local cache also has a byte budget
(`DIFF_UI_MAX_BYTES`, 400MiB) because the directory is an emptyDir whose
`sizeLimit` the kubelet enforces by **evicting the pod**, and a count is the
wrong unit for files spanning 2KB to tens of MB.

#### Navigation and anchors (COPS-2611 / COPS-2622)

An index lists every application with its resource count, collapsed per
application via `<details>` so 345 of them are a list rather than a wall, with
a client-side filter that narrows the index and never the body.

Structure is parsed from the markers `format_comment` already emits, not from
a change to the stored artifact, so older artifacts stay readable and `/raw`
is untouched. The parser is defensive by construction: anything unrecognised
falls through and renders exactly as before, and one row is emitted per source
line either way. A page that lost a line to gain an index would undo the work
above.

**`diff_ui.app_anchor` is the single owner of the per-application anchor
shape**, and the PR comment imports it to build its deep links. Two copies of
that logic would drift on the first change and every deep link would 404 *in
silence*. It is deliberately order-independent, unlike the de-duplicated ids
inside `build_outline`: the comment cannot know the page's application order,
so a position-dependent id could not be reproduced. That moves
collision-freedom onto the input, which holds because fleet application names
are already `[a-z0-9-]`; `build_outline` keeps its numeric suffix as backstop.

Anchors are scoped per application because resource names repeat across
environments constantly, and an index entry pointing into the collapsed
overflow reveals it before jumping — otherwise clicking it would do nothing on
exactly the large pages that need the index most.

Every value on the page is PR-controlled, so all of it is escaped, anchor ids
are **sanitised** rather than escaped (they land in `id=` and `href="#..."`
where escaping still leaves a quote to break out of), the filter only toggles
a class and never writes body-derived strings back into the DOM, and the page
ships zero external assets.

Before COPS-2579 this claim was only partly true: the persisted body was the
SAME body posted as the comment, and that body was itself capped per app
(10 sections) and per PR (a fixed top few apps shown inline). For a large
ancestor-file change the page ended up byte for byte identical to the
truncated comment, not a real "full output" (measured on acme-config-prod PR
#3837: 60 of 16616 real diff sections visible in both places). Now that
`format_comment` stores each app's full (memory-bounded) sections and groups
apps with an identical diff instead of picking an arbitrary top-N (see
"Grouping apps with an identical diff" above), the exact same persistence
call carries the complete content automatically, with no changes needed to
this module.

Like the
PR comment itself, there is exactly one live artifact per `(repo, pr)`: each
new commit overwrites the previous diff in place (atomic write), so the page
always reflects the latest generated output, and a build-status link that
embeds an older commit sha still resolves to the PR's current diff rather than
404. The page is rendered Azure DevOps-style (dense line-numbered diff table,
GitHub diff palette, sticky header) with a Light / Auto / Dark appearance
switch (persisted per browser; Auto follows the OS). Very large diffs render a
capped, scrollable window first with a "show full output" control that reveals
the rest in place (nothing is dropped; `/raw` is always byte-exact). Every
page names the service explicitly ("acme-diff-preview" wordmark, "ACME Diff
Preview" label) so a reviewer landing here from a build status link never has
to guess which tool posted it:

| Method | Path | Description |
|---|---|---|
| `GET` | `/diff/<repo>/<pr>/<sha>` | Full diff rendered as HTML (everything escaped); serves the PR's latest diff even if `<sha>` is stale |
| `GET` | `/diff/<repo>/<pr>/<sha>/raw` | Exact plain-text body |

**Why the default is on and why that is safe.** The ArgoCD hub Ingress
`extraPaths` forward exactly two paths to this Service, `/jfrog-webhook` and
`/diff-preview/webhook`, never a wildcard (defined in `acme-infrastructure`,
not this chart). Turning `DIFF_UI_ENABLED` on does not add a new externally
reachable path: `/diff/*` is only reachable in-cluster or via
`kubectl port-forward`, exactly like `/diff-preview/stats` already is.
`DIFF_UI_BASE_URL` stays empty by default, so the Bitbucket build status
keeps linking to the comment, never to a host with no access control in
front of it.

**Reaching it from a browser, behind SSO.** This is a SEPARATE, explicit
opt-in: `diffUi.ingress.enabled` (default `false`). When set, the chart
renders a second Service, `<release>-acme-diff-preview-ui`, selecting the
exact same pods on the exact same port, with a GKE `BackendConfig`
(`cloud.google.com/backend-config` annotation) enabling Google
Identity-Aware Proxy on that Service only. The primary Service (the
webhooks) is never touched, since JFrog and Bitbucket authenticate with an
HMAC signature and could never complete an interactive Google login. This
mirrors how ArgoCD itself is protected here (`argocd-dex-server` + Google
OAuth, COPS-2479): the same Google identity gates this page.

Turning it on is simpler than it sounds. GKE 1.29.4-gke.1043000+ supports
IAP with a Google-managed OAuth client, and this cluster runs 1.35.x
(verified live). The default path needs just:

1. Set `diffUi.ingress.enabled: true`. No custom OAuth client, no secret
   to provision, GKE manages the client itself.
2. Grant access to real people/groups: Cloud Console → Security →
   Identity-Aware Proxy → select this backend → Add principal →
   "IAP-secured Web App User". This step is unavoidable either way, it is
   the actual access-control layer, independent of the OAuth client.
3. Wire the new `<release>-acme-diff-preview-ui` Service into the hub
   Ingress' host/path rules and the TLS certificate for that host: both
   live in `acme-infrastructure`, tracked as follow-up work in the ticket,
   not in this chart.

A custom OAuth client is also supported, for orgs that specifically need
one instead of the Google-managed client: set both
`secrets.iapOauthClientIdKey` and `secrets.iapOauthClientSecretKey` (both
empty by default) to GCP Secret Manager key names, which makes the chart
create the `ExternalSecret` that fills the `BackendConfig`'s
`oauthclientCredentials`. Leaving either one empty keeps the
Google-managed path.

One project-level prerequisite either path shares, per Google's own docs:
the GKE service agent needs the `compute.backendServices.update` IAM
permission. This is granted automatically on almost every GCP project
(it is part of the default `Kubernetes Engine Service Agent` role) and is
project-level IAM, not something this chart can set or verify; if IAP
enablement silently does not take effect, check this first.

If step 3 is not done yet while `diffUi.ingress.enabled` is `true`, the
BackendConfig and Service simply exist unused; nothing else in the
cluster, and no other Application, is affected. Once the host is live, set
`DIFF_UI_BASE_URL` to it (e.g. `https://acme-diff-preview.appspace.com`, the
same `acme-diff-preview` slug this chart already uses for the Service name)
so the Bitbucket build status becomes the permalink to that exact commit's
page, mirroring how the Atlantis commit-status "Details" link opens the
full plan output. The comment stays as the summary either way. Storage v1
is a bounded local directory (atomic writes, oldest-pruned); a durable GCS
backend is separate follow-up work tracked in the ticket.

---


---

### Why a toleration with a wrong operator or effect is blocked

Kubernetes takes only these values in a pod toleration, and it is
case-sensitive: `operator` is `Equal` or `Exists`, and `effect` is
`NoSchedule`, `PreferNoSchedule` or `NoExecute`. Empty or null means the
default. Helm does not check this. It renders any string, so the diff looks
normal, and the API rejects the Deployment when ArgoCD syncs. On
acme-config-prod #4681, `operator: equal` froze the app until someone fixed
the value.

The check (COPS-2766) reads the tolerations in the PR render. It reads only
the resources that are new or changed in this PR, and it leaves out a bad
value that `main` already has in the same resource. So an old mistake never
blocks an unrelated PR, for example a fleet chart bump. An affinity
`operator: In` is not in a `tolerations:` block, so it is never read.

It fails the build as a render error. The comment shows it as SCHEMA
VALIDATION FAILED, but each line says that the Kubernetes API rejects the
value, and a hint says that helm and `values.schema.json` do not check it. The
red status names the resource and the value, and it is not retried. There is
no trailer, because the fix is a change of the value: in `customer.yaml`, or
in the cohort `config.yaml`. When the bad value comes from the chart itself,
for example in a chart bump, the fix goes in the chart.
