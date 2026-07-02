# Bug-hunt findings — acme-diff-preview v2.4.3 (2026-07-02)

> **STATUS: ALL FIXED in v2.4.4.** The proof tests moved to
> `tests/test_bugfix_regressions.py` and run in CI.

Every finding below is proven by a failing test in `bughunt/test_findings.py`.
The tests assert the CORRECT behavior, so a failure = the bug exists today.
After fixing, this file must pass — it is also the acceptance suite.

Run: `python3 -m pytest tests/test_bugfix_regressions.py -v`

---

## F1 — Stale diff after main advances (correctness, HIGH)

**Scenario:** PR A has a clean diff comment. PR B merges to main and changes
values or the chart version for the same apps. PR A's comment now shows a
diff against an old main — exactly what the merge will NOT do — and stays
stale until the author pushes a new commit.

**Where:** `process_pr` — both dedups (in-memory `_seen[pr_id] == pr_sha`
and cross-pod `comment_sha == pr_sha[:8]`) compare only the PR source sha.
`process_pr` already receives `base_sha` and ignores it in the decision.

**Evidence:** `test_f1_recompute_when_main_advances` — second call with a
new `base_sha` is skipped, the diff never runs.

**Proposed fix:** include the base sha in both dedup keys:
`_seen[pr_id] = (pr_sha, base_sha)` and embed a short base-sha token in the
comment footer next to the existing `[clean|permanent|transient]` token,
e.g. `[acme-diff-preview [clean] [base:1a2b3c4d]]`. Legacy comments without
the token are treated as stale once. Cost: one extra recompute per PR each
time main moves — bounded by MAX_APPS_PER_RUN and the render caches.

---

## F2 — Duplicate comment on transient PUT failure (integrity, MEDIUM-HIGH)

**Scenario:** Bitbucket returns 5xx/429 on the comment PUT (after http()
retries). The fallback POSTs a NEW comment although the old one still
exists. Every storm adds one more duplicate; readers see N stale comments
plus the current one, and `find_existing_comment` behavior on duplicates
is undefined.

**Where:** `upsert_comment` — the `except Exception` fallback POST was
designed for the deleted-comment case (PUT 404) but fires on ANY error.

**Evidence:** `test_f2_no_duplicate_comment_on_transient_put_failure` —
PUT raising 502 produced a POST (`assert 1 == 0`).

**Proposed fix:** only fall back to POST when the PUT failed with HTTP 404.
Re-raise (or log-and-skip; next iteration retries) for anything else.

---

## F3 — Unbounded refresh threads on webhook bursts (resilience, MEDIUM)

**Scenario:** CI republishes dozens of distinct chart versions in a minute
(observed in production: the rev1 burst). One daemon thread per distinct
chart:version spawns and each runs ArgoCD hard-refreshes across matching
apps — an uncapped thundering herd against the ArgoCD API. The 15s dedup
window only collapses IDENTICAL chart:version events.

**Where:** the `/jfrog-webhook` handler — `threading.Thread(...).start()`
per event.

**Evidence:** `test_f3_webhook_burst_bounded_concurrency` — 24 distinct
pushes produced 24 concurrent `jfrog-refresh-*` threads (1:1).

**Proposed fix:** replace thread-per-event with a bounded
`ThreadPoolExecutor` (e.g. 4 workers) + a dedup on enqueue, created at
startup. Burst behavior becomes: enqueue fast, drain at a controlled rate.

---

## F4 — Retry-After ignored; total backoff ~3s (resilience, MEDIUM)

**Scenario:** Bitbucket rate-limit windows last ~60s and include a
Retry-After header. `http()` retries at 1s + 2s and gives up (~3s total),
so during a storm every call in the iteration fails through. Combined with
F2, each failed comment PUT also creates a duplicate.

**Where:** `http()` (src/diff_preview.py:648).

**Evidence:** `test_f4_http_honors_retry_after` — server mandated 30s, total
backoff was 3s.

**Proposed fix:** on 429, honor `Retry-After` (capped, e.g. 60s) before
retrying, and allow one extra attempt for 429 specifically. Keep the current
fast backoff for 5xx.

---

## F5 — Line-based manifest parser silently drops resources (correctness, MEDIUM)

**Scenario A (flow style):** a template emitting `metadata: {name: x}`
(valid YAML, produced by `toJson`-style helpers) is invisible to the parser
— the resource is skipped on BOTH sides, so a real change reports as
**no-diff**.

**Scenario B (duplicate keys):** if a render emits the same
(kind, ns, name) twice (umbrella charts merging subchart output), dict
insertion keeps only the last document; a PR change in the first copy is
invisible.

**Where:** `_parse_manifest_resources` (src/diff_preview.py:1674).

**Evidence:** `test_f5a_flow_style_metadata_not_dropped` and
`test_f5b_duplicate_resource_key_not_silently_overwritten` — both changed
renders produced an empty diff.

**Proposed fix:** parse each document with `yaml.safe_load` to extract
apiVersion/kind/metadata (keep the raw text as the diffed body — behavior
unchanged for the normal path, so existing diffs stay byte-identical);
fall back to the line scanner only when safe_load fails. For duplicates,
append a `#2`, `#3` suffix to the key so both documents stay diffable, and
log a WARNING naming the chart.

---

Verified healthy during the same audit (no action): Bitbucket pagination
(`next` + page cap), comment size truncation, `_is_checksum_only_section`
(mixed sections survive), HMAC via `hmac.compare_digest`, value-file 404 vs
transient-error classification, Vertex AI failures degrade gracefully to
"no summary", bounded render caches with pruning of `_seen` /
`_pr_chart_targets` / `_jfrog_recent`.
