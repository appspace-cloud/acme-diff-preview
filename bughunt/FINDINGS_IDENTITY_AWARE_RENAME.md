# FINDINGS: rename following is path-based, not identity-based (next phase)

Generated 2026-07-06. Do NOT implement yet. This is the saved analysis to
work from in the next session. The finding was confirmed by direct code
reading, by a code-level repro against the real functions, and by mining
600 commits of acme-config-prod history for the exact input shape that
triggers it.

## Context corrected with Marcos (2026-07-06)

The real identity of an environment lives INSIDE customer.yaml, not in the
folder path:

- `customerName` is the true identity. It drives the namespace and related
  wiring. This is the important one.
- `suffix` is the variant (a / b / c ...). It can be declared locally in
  customer.yaml or inherited from a parent config.yaml higher up
  (`suffix: a`). It is part of the environment identity and affects the
  render (labels, hostnames, deploy targets).
- `instanceName` is ONLY for the virtual machines. It is NOT an identity
  discriminator and must be ignored for this purpose.

Two folder-rename situations exist in this repo, and they mean opposite
things:

1. Path correction (same identity): the folder name was wrong and gets
   aligned to the `suffix`/`customerName` already set inside customer.yaml.
   Content does not change, so Bitbucket reports it as a rename (R100).
   Following the rename and rebasing the value-file chain is CORRECT.
2. Rebuild / variant / migration (identity changed): a new environment is
   created in parallel with a new suffix (or a corrected customerName, or a
   new region) and the old one is later deleted. The new customer.yaml
   shares most of its content with the old one, so Bitbucket's content-
   similarity rename detector PAIRS them anyway, even though semantically
   this is "delete A, add B", two different environments.

## FINDING 7 (HIGH SEVERITY, code-confirmed + 49 real prod precedents) —
env-move detection trusts a content-similarity rename without checking that
the environment identity (customerName + effective suffix) actually stayed
the same

### Root cause

`_detect_env_move` (v2.5.8) and the per-file value fetch in `_run_one_diff`
decide "this app's folder moved" purely from the raw Bitbucket rename
pairing plus a basename check (`customer.yaml` / `config.yaml`). v2.5.9
tightened it so an ancillary-file-only pairing (cicd-versions.yaml) is not
trusted unless an identity-file rename corroborates it. But when the
IDENTITY file itself is the paired file, the guard is satisfied trivially:
`posixpath.basename(old_p) in _IDENTITY_BASENAMES` is True, so the move is
accepted with no check on what is INSIDE the file.

Nothing in the codebase ever reads `customerName` or `suffix`. Confirmed:
`grep -n "customerName\|suffix" src/diff_preview.py` shows only unrelated
hits (an `endswith(suffix)` cache-key helper, an app-name suffix regex).

### Impact (confirmed end to end in code)

When a Class 2 rename fires `_detect_env_move` for a still-live app
(e.g. `pv-manulife-a-ms`, whose ArgoCD valueFiles point at the OLD
`pv-manulife-a` folder):

1. `_rebase_value_files` rewrites the app's value files onto the NEW folder
   (`pv-manulife-b`).
2. `_fetch_value_files` fetches the NEW environment's customer.yaml /
   cicd-versions.yaml (suffix b, possibly different customerName/region).
3. `_effective_chart_version` reads the NEW chain, so `pr_rev` becomes the
   NEW environment's chart version.
4. `_helm_template` renders the PR side of the `-a` app with `-b`'s content.
   The diff is a mixed-identity artifact: it does not represent what merging
   will actually do to the `-a` app (which is being deleted), and it can
   attribute a spurious chart change or downgrade to the wrong environment.

Simultaneously, `_detect_env_decommission_candidates` SKIPS the old env
(`if clean in renames: continue`), so the loud, correct "ENVIRONMENT
DECOMMISSION" warning for `-a` is suppressed. The reviewer sees neither a
correct decommission notice nor a correct diff.

This is the same class of problem v2.5.9 fixed for ancillary files, but one
level up: v2.5.9 stopped trusting a coincidental cicd-versions.yaml pairing
by requiring identity-file corroboration; it did not consider that the
identity-file pairing itself can be a false positive when the two files are
content-similar but describe different environments.

### Real-world frequency (mined from acme-config-prod, last 600 commits)

Cross-suffix identity-file (`customer.yaml`) renames that git pairs at
R>=50% (approximating Bitbucket's detector):

- Class 1 (same customerName + suffix, folder path fix, rebase CORRECT): 17
- Class 2 (customerName or suffix CHANGED, rebase WRONG): 49

Class 2 samples (commit, similarity, old folder -> new folder, old
(customerName, suffix) -> new):

- 440e24d50 R53 pv-takeda-a -> pv-takeda-b  (takeda,a)->(takeda,b), also
  na3-a -> eu1-b region move ("takeda-eu-move")
- 0b8642daf R89 pv-asxo-a -> pv-asxo-b  (asxo,a)->(asxo,b)
- eb967073c R75 pv-asxo-b -> pv-asxo-c  (asxo,b)->(asxo,c)
- 292819c34 R97 pv-cpna-b -> pv-cpna-c  (cpna,b)->(cpna,c)
- 598010986 R97 pv-onr-a -> pv-onr-c  (onr,b)->(onr,c)  [suffix inherited,
  declared locally as b in customer.yaml]
- 6dfca5722 R99 pv-smbc-a -> pv-smbc-b  (smbc,a)->(smbc,b)
- aa4c97b2e R88 pv-authentic--aec1-a -> -b  (authentic--aec1,a)->(...,b)
- 421214f70 R90 pv-seagal-a -> pv-segal-a  (seagal,a)->(segal,a)
  [customerName typo fix, same suffix -> still a different identity]
- 13d0fe181 R73 pv-versantmedia-c -> pv-versant-c  (versantmedia)->(versant)
- 36635d50b R99 pv-bnym-b -> pv-bnym--aec1-b  (bnym--aec1)->(bny--aec1)

Class 1 control (correctly rebased today, must keep working):

- 655546c96 R100 pv-allianzna-a -> pv-allianzna-c  (allianzna,c)->(allianzna,c)
  ["Rename folders": folder name was wrong, internal suffix already c]
- 0e1dd52cc R100 pv-nike-a -> pv-nike-b  (nike,b)->(nike,b)

### Why the current tests pass anyway

`tests/test_v258_tier_move_downgrade.py` covers:
- the ancillary-only spurious pairing (v2.5.9) -> correctly ignored, and
- the legitimate same-identity move (customer.yaml renamed, content moved).

It never constructs the Class 2 case: an identity-file pairing where the
internal customerName/suffix differs between the two sides. So the suite is
green while the gap is open.

### All three rename-following paths confirmed exposed (live code repro)

The identity gap is not just in detection; every place that follows an
identity-file rename adopts the wrong environment's data for a Class 2
pairing. Confirmed by driving the real functions:

1. `_detect_env_move` returns the cross-identity (old_dir, new_dir) pair,
   so `_rebase_value_files` + `_fetch_value_files` render the old app's PR
   side with the new environment's content and `_effective_chart_version`
   takes the new env's version.
2. The per-file value fetch fallback in `_run_one_diff` follows the pairing
   the same way when `_detect_env_move` does not fire (no move but a paired
   changed file).
3. `_pr_chart_revision_checked` follows the pairing and adopts the new
   environment's `appspace.version` for the OLD app. Verified: an `-a` app
   at 2600.1.0-dev silently adopted the `-b` env's 2699.9.9-dev.

Any fix must apply the same identity check in all three, or a Class 2 pair
rejected in one path still leaks the wrong version/content through another.

### Related sub-case (same root cause) — shared config.yaml pairing wins

`config.yaml` is both a per-env identity file AND the shared defaults file
at ancestor levels (documented in the v2.5.7 note). When a tier/region
folder is restructured, git/Bitbucket pair the SHARED `config.yaml`
(e.g. `.../ap1/config.yaml` -> `.../ap2/config.yaml`, real prod precedents:
commits 10527e029, a1a509255, 662a57657, f43bf0d9d). `_detect_env_move`
accepts it (basename is config.yaml) and `_rebase_value_files` then rewrites
EVERY value file in the app's chain from ap1 to ap2, including the leaf
customer.yaml. In the common case the leaf moved too (rebase happens to be
correct), but two failure shapes exist:
- the shared config moved without the customers under it -> the rebased
  customer.yaml path 404s and the PR side loses the customer overrides;
- a PR moves BOTH the shared config (ap1->ap2) AND the leaf customer to a
  different suffix inside ap1 (pv-qa-40-a -> pv-qa-40-b): `_detect_env_move`
  returns the SHARED config pair first (dict iteration order of `renames`,
  i.e. Bitbucket diffstat order, non-deterministic to the user), rebasing
  the leaf to a non-existent ap2/pv-qa-40-a path. Confirmed at code level.
The identity-aware fix should also require that the paired file be a
per-env identity file (its directory maps to real apps in path_map), not a
shared ancestor default, before treating it as an app move.

### Fix direction (needs design, do NOT one-line it)

Make the move decision identity-aware instead of path-only:

1. When `_detect_env_move` finds a candidate (old_dir, new_dir) via an
   identity-file rename, fetch the OLD customer.yaml (at main_sha) and the
   NEW customer.yaml (at pr_sha) and compare their effective identity:
   `(customerName, effective_suffix)`.
   - Same identity -> trust the move, rebase as today (Class 1).
   - Different identity -> it is NOT a move of this app. Treat the old env
     as a genuine decommission (let `_detect_env_decommission_candidates`
     see it, i.e. do not swallow it as a rename) and let the new env go
     through `_detect_new_env_candidates` as a brand-new environment.
2. `effective_suffix` must be resolved like helm -f last-wins across the
   value-file chain (reuse the `_effective_chart_version` approach), because
   `suffix` is often inherited from a parent config.yaml and not declared in
   the leaf customer.yaml (confirmed: pv-onr-a's folder is -a but the
   customer.yaml sets suffix: b; other leaves omit suffix entirely).
3. Apply the SAME identity check at the two other places that follow an
   identity-file rename today, so the three paths stay consistent:
   - the per-file value fetch fallback in `_run_one_diff`
     (`renames.get(clean_path)` -> `renamed_vals`), and
   - `_pr_chart_revision_checked` (follows the rename to read the bumped
     version).
   Otherwise a Class 2 pairing would be rejected by `_detect_env_move` but
   still leak the wrong version/content through one of the other two.
4. `customerName` is the primary key; a change in customerName alone
   (seagal -> segal typo, bnym -> bny, versantmedia -> versant) is already
   enough to declare "different identity" even when the suffix matches.

### Repro recipe for the next session (dev sandbox, decline after capture)

acme-config-dev uses `.../custom/pv-dev-NN-a/` folders backed by real
ArgoCD apps, structurally identical to a prod customer folder.

- Class 2 repro: `git mv .../custom/pv-dev-06-a .../custom/pv-dev-06-b`,
  then edit the moved customer.yaml to change `suffix: a` -> `suffix: b`
  (and optionally bump a service tag) before committing. Push, open the PR.
  Expected AFTER the fix: the `-a` app shows a DECOMMISSION warning (not a
  rebased diff), and `-b` shows as a NEW environment. Expected TODAY
  (the bug): the `-a` app renders its PR side against `-b`'s content and no
  decommission warning appears.
- Class 1 regression guard: `git mv .../custom/pv-dev-07-a .../custom/pv-dev-07-b`
  and ALSO set `suffix: b` inside so the internal identity matches the new
  folder. This is a pure path fix and must keep rebasing correctly (real
  diff for the moved app, no false decommission).

Confirm the live ArgoCD app valueFiles for the target still point at the OLD
folder before testing (`argocd app get pv-dev-06-a-ms`), since that is what
makes the app get matched via the old path in the first place.
