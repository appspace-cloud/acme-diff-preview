# Golden comments (COPS-2565)

These files are the exact comment bodies `format_comment` produces for a set of
scenarios. They are the regression net for the thing reviewers actually read.

## Why these exist

The four bug tickets before this one (COPS-2552, 2554, 2563, 2564) were all
found in production, on a real PR, by a human noticing the comment looked
wrong. In every case the diff engine was correct and the *classification or
presentation* of an edge case was not. Unit tests did not catch any of them,
because each one only shows up in the assembled comment.

Each golden below maps to a real incident:

| File | Guards against |
|---|---|
| `minus_only_no_deletion_block.md` | COPS-2563. Live PR 3829 announced "110 RESOURCE(S) DELETED" for Deployments whose only change was a removed `replicas:` line. |
| `true_deletion_shouts.md` | The other half of COPS-2563: a real deletion must stay loud. A "fix" that silenced this would be worse than the bug. |
| `schema_failure_readable.md` | COPS-2564. 53 schema violations were cut mid-path at 400 characters, ending in `definitions/a`. |
| `failed_app_not_green.md` | The most dangerous failure mode in the product: a computation failure rendered as "no changes". |
| `new_env_rides_along.md` | v2.5.4 Finding 4: a clean existing-app diff must not show a green check while an unvalidated new environment rode in on the same PR. |
| `large_pr_summary_table.md` | The PR 3837 shape. Must degrade to a table and stay inside Bitbucket's 245KB limit. |
| `ordinary_version_bump.md` | The 83.7% case. If this changes shape, everything else is suspect. |
| `version_downgrade.md` | Downgrades must be flagged, not shown as an ordinary change. |
| `all_clean.md` | The all-green baseline. |

## Updating them

```bash
UPDATE_GOLDEN=1 ./venv/bin/python -m pytest tests/test_cops2565_golden_comments.py
```

**Read the diff before you commit it.** A golden corpus that gets regenerated
on every red build protects nothing, and that is the specific failure mode this
setup is designed to resist:

- Regeneration is never automatic and never a side effect of a normal test run.
- The goldens are committed markdown, so a change shows up in review as readable
  prose. A reviewer can see "the deletion block disappeared" without running
  anything.
- If a golden changes and the PR description does not explain **why**, that is
  the signal to stop and investigate, not to re-run the generator.

## What is frozen and what is not

Frozen: the whole comment body, byte for byte. Resource counts, section
ordering, the deletion block, the schema block, the traffic light and the footer
are all part of the contract, because each of them has been wrong at least once
in production.

Normalised, because they are not behaviour:

- the footer timestamp, pinned to a fixed instant;
- the AI summary, stubbed off (a non-deterministic remote call, already behind
  an operator switch since COPS-2555).
