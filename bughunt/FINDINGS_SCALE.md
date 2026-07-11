# Scale / PR-scenario findings — acme-diff-preview (2026-07-11)

> **STATUS: RESOLVED in v2.5.18** (S1-S6 all fixed; S5 was a messaging/design
> clarification, not a code defect). Regression tests:
> `tests/test_v2518_scale_hardening.py` (13 cases, confirmed red on v2.5.17).
> New tuning knob: `AI_MAX_APPS` (default 40). Kept for historical reference
> like the other files in `bughunt/`.
>
> Original analysis below, unedited. Full-pass adversarial review of v2.5.17
> focused on PR scenarios at scale
> (600-1000+ apps per PR, retry storms, comment-size limits, pool saturation).
> Every sizing claim below was measured empirically with throwaway probes
> (probe_scale.py / probe_scale2.py, removed after use), not estimated.
> Reference numbers: MAX_APPS_PER_RUN=800, DIFF_WORKERS=16, PR_WORKERS=3,
> subtask pool=32, MAX_COMMENT_BYTES=245_000, pod limit 2Gi.

## S1 — AI prompt has NO cap on changed-app count (HIGH at scale)

`generate_ai_summary` caps sections per app (10) and chars per section
(1500), but iterates over ALL changed apps. Measured prompt sizes:

| changed apps | prompt size | ~tokens | gemini-2.5-flash (1M ctx) |
|---|---|---|---|
| 50  | 0.7 MB | ~181K  | fits |
| 100 | 1.4 MB | ~361K  | fits |
| 300 | 4.3 MB | ~1.08M | **exceeds** |
| 800 | 11.6 MB | ~2.9M | **exceeds** |

Consequences on a mass version-bump PR (the exact scenario COPS-2502 sized
the service for):
- Somewhere between ~100 and ~300 changed apps the Vertex call starts
  failing on context length. The failure is caught and the comment posts
  without AI ("AI summary absent") — silently, on exactly the PRs where a
  summary is most valuable to a reviewer.
- Before failing, the pod builds an 11.6 MB prompt string per PR
  (×3 concurrent PRs) and uploads it to Vertex over a 60s-timeout call —
  wasted memory, egress, and time on every recompute of a big PR.

Fix shape: cap the number of apps included in the prompt (e.g. top-N by
n_res, like LARGE_PR_INLINE_APPS does for the comment) and state
"+N more apps omitted" in the prompt; alternatively budget by total chars
(~700KB keeps ~180K tokens, safe margin).

## S2 — Comment truncation destroys the footer tokens (MEDIUM-HIGH)

An 800-app comment measures 433 KB pre-truncation; anything over 6 inline
apps × ~10 sections is already near the 245 KB limit, so mass PRs are
ALWAYS truncated. `upsert_comment` cuts from the END, so the footer —
which carries the two machine-readable tokens the whole dedup design
depends on — is always lost:

- `[clean|permanent|transient]` gone → `_extract_status_token` returns "".
- `[base:xxxxxxxx]` gone → the F1 main-advanced check can never match.

Measured replay of process_pr's decision on a truncated comment: with an
empty `_seen` (any pod restart), `rerun=True` — the pod re-diffs the whole
800-app PR from scratch even though the posted comment already covered the
exact same (pr_sha, base_sha). In-pod `_seen` caps it at ONE redundant
recompute per restart per oversized PR (4-5 min of hub/BB/OCI load each),
and cross-pod dedup (rolling deploys) is fully broken for these PRs.
Secondary: `fix_stuck_inprogress` on a truncated comment falls through all
its heuristics to the generic "SUCCESSFUL / No manifest changes" label.

Fix shape: reserve the footer. Truncate the MIDDLE of the body (or cap the
per-app blocks harder) and always re-append the real footer lines, so the
marker, sha header, status token and base token all survive any size.

## S3 — Chart-pull timeout returns FAR later than DIFF_TIMEOUT (MEDIUM)

`_run_one_diff`'s chart-pull block wraps a 2-worker `with
ThreadPoolExecutor(...)`. When `fut.result(timeout=DIFF_TIMEOUT)` raises,
the exception exits the `with` block, whose `__exit__` calls
`shutdown(wait=True)` — blocking until BOTH `_ensure_chart` calls actually
finish. `_ensure_chart` can legitimately take: 3 pull attempts × 120s
subprocess timeout + 5s/10s sleeps, plus an UNBOUNDED wait on the per-key
pull lock held by another thread doing the same. So a diff that "timed out
at 120s" can actually hold its DIFF_WORKERS slot for 6-7+ minutes before
the REASON_TIMEOUT return executes. Under a registry brownout across a
mass PR this compounds: 16 slots × minutes each, retries (5 attempts)
multiply it. Not a hang (all inner waits are bounded except the lock,
whose holder is bounded), but the DIFF_TIMEOUT contract is violated
exactly when the system is already degraded.

Fix shape: `ex.shutdown(wait=False, cancel_futures=True)` semantics —
restructure to submit on the shared pool (no `with`) or call
`fut.cancel()` + use a non-context-managed executor for this pair.

## S4 — Timed-out render futures are never cancelled on the SHARED pool (MEDIUM)

The render/fetch phase submits to the shared 32-worker subtask pool with
no `with` block, so a TimeoutError DOES return immediately — but the
abandoned futures stay queued/running in the shared pool. Each retry
(up to 5) submits fresh tasks on top. Saturation math: 3 PRs × 16 diff
workers = 48 waiters on 32 pool workers; with slow renders (big
micro-services chart) plus abandoned duplicates from timed-out attempts,
queueing delay alone can push later `result(timeout=120)` calls over the
limit — timeouts caused by the pool, not the work, which then retry and
add more load (classic congestion amplification). Normal operation has
comfortable headroom (measured diffs 4-6s warm); this only bites when
renders slow down across a mass PR.

Fix shape: `fut.cancel()` both futures in every timeout/error path
(cancels queued ones for free; running ones are already bounded by the
subprocess timeout).

## S5 — >800-app PR: the overflow apps are never evaluated, by design but silently sticky (LOW-MED, design)

`affected[:MAX_APPS_PER_RUN]` is deterministic (sorted), the status is
FAILED "N app(s) not evaluated", and — because a clean 800/800 run is
neither hard-error nor transient — `_seen[pr_id] = (pr_sha, base_sha)` IS
set. Net: a 1000-app PR is permanently FAILED with the SAME 200 apps never
evaluated, and no retry will ever cover them; only a new commit, a main
advance, or raising MAX_APPS_PER_RUN changes anything. The FAILED status
is the correct honest signal; the sticky part just deserves being written
down (and the status text could say "raise MAX_APPS_PER_RUN" explicitly).

## S6 — no-apps and error comment bodies re-broke `_extract_comment_sha` (LOW)

The three hand-built bodies in process_pr (no-apps, new-env-only was fine,
per-PR error) write the header as `` Commit `sha` vs `main` `` — plain,
no `**`. `_extract_comment_sha` requires `**Commit** \`sha\`` (bold), the
exact generated-vs-parsed drift its own docstring warns about (the v2.4.6
bug class). Measured: extraction returns "" for both bodies. Consequence
is small — cross-pod/post-restart dedup never matches these comments, so
each pod restart reprocesses every no-apps PR once (cheap: no diffs run)
and every errored PR once (re-runs the failing path). The regression test
from v2.4.6 only covers format_comment, which is why this reappeared.

Fix shape: one shared `_comment_header(pr_sha)` helper used by all four
body builders + extend the round-trip test to cover all of them.

## Verified healthy during the same pass (no action)

- format_comment CPU at scale: 0.07s for 800 apps — negligible.
- `_match_files_to_apps` worst case (10K files × 800 paths): sub-second.
- Memory: main-render cache bounded (200 entries, evict-half), vf cache
  bounded (5000), transient render strings ~16×10MB worst case — inside
  the 2Gi limit with margin.
- `_bound_vf_cache` eviction arithmetic correct (keeps MAX/2).
- Webhook dedup dict pruned; `_seen`/`_pr_chart_targets`/`_comment_id_cache`
  pruned to open PRs; identity-rename verdict cache bounded.
- SIGTERM mid-batch: `with` executor exit waits for queued futures (pod may
  be SIGKILLed after grace period), but the partial-results guard prevents
  any false comment — correctness holds, only the "graceful" label is
  optimistic.
- Pre-warm correctly skips stale dev charts (per-key lock serializes the
  re-pull); only a perf non-help, not a bug.
