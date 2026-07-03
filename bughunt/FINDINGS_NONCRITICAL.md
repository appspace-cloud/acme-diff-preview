# Non-critical findings — acme-diff-preview (2026-07-03)

> **STATUS: ALL APPLIED in v2.4.5.** Proof/regression tests in
> `tests/test_v245_improvements.py`. Measurement-driven probes that led here
> lived in `bughunt/nc_probe*.py` (removed after use, findings kept here).

Five non-critical angles, each measured empirically before fixing:

## N1 — `JFROG_REFRESH_WORKERS` read with two different meanings
Same env var controlled both the webhook-dispatch pool (default 4) and the
per-event app-refresh fan-out (default 8). Lowering it to calm the ArgoCD API
throttled both at once, multiplicatively. Split into `JFROG_DISPATCH_WORKERS`
and `JFROG_REFRESH_FANOUT`, each independently tunable.

## N2 — Large-PR summary table listed every unchanged app
Measured: 300 changed + 300 unchanged apps produced a 601-row table, half of
it "no changes" noise. Now the table lists only changed/error/indeterminate
apps and closes with a single `(+N more) no changes` row.

## N3 — A typo in any numeric env var crash-looped the pod
Measured: `DIFF_WORKERS=sixteen` raised `ValueError` at import with no hint
which variable was at fault (21 numeric vars exposed to this). Added
`_env_int(name, default)`: invalid values log a WARNING naming the variable,
the bad value, and the default used, and the pod starts normally.

## N4 — `/diff-preview/stats` missing actionable counters
Added `apps_render_failed`, `apps_timeout` (split from the reason codes
already computed per PR), and `main_render_cache_hits` /
`main_render_cache_misses` (cache effectiveness, previously invisible).

## N5 — `find_existing_comment` re-paginated every comment every iteration
Our comment updates in place and never moves position; on a heavily
discussed PR, every ~60s iteration re-scanned every human comment to find
it. Added `_comment_id_cache` (pr_id -> comment_id): a single GET-by-id
replaces full pagination once the id is known; self-healing on a 404
(comment deleted) via one fallback full scan; pruned alongside `_seen` for
closed PRs.

## N7 (partial, by design) — mixed print()/log() for operational lines
On inspection, most `print()` lines are an intentional human-readable
per-PR narrative (for `kubectl logs -f`), coexisting with structured
`log()` summaries at the points that matter. Converting all 24 wholesale
would bloat the diff and could flood a log aggregator (e.g. per-app retry
lines, up to ~3000/iteration at 600 apps) for marginal benefit. Converted
only the one line with real alerting value: the F1 "main advanced"
recompute trigger is now a structured `log()` event
(`event=main_advanced_recompute`), queryable and alertable.

Tests: 110 passing (was 98).
