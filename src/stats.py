"""The diff counters, and the lock that guards them.

Exposed at GET /diff-preview/stats and written from nineteen places in the
hub. They live here because the main render cache writes hit and miss counts
into them, and the cache cannot leave the hub while its counters stay behind.

Unlike `log` or the sub-task pool, these are re-exported rather than reached
through this module object. The difference is rebinding: a name that gets
replaced must be resolved through the namespace the replacement lands in,
but this dict is only ever mutated, so a second reference to it is the same
object and a mutation through either is visible through both. Nothing
declares `global _diff_stats`, and a test asserts that stays true.
"""
import threading


# Diff operation counters — exposed at GET /diff-preview/stats
_diff_stats:      dict          = {
    "prs_processed": 0,      # PRs where we ran at least one diff
    "apps_diff": 0,          # apps with real changes
    "apps_no_diff": 0,       # apps confirmed unchanged
    "apps_indeterminate": 0, # diffs that could not be computed
    "apps_oci_not_found": 0, # permanent OCI version missing
    "apps_render_failed": 0, # helm template failed (bad values/chart) — bughunt N4
    "apps_timeout": 0,       # a diff step exceeded DIFF_TIMEOUT — bughunt N4
    "main_render_cache_hits": 0,   # reused a parsed main-side render — bughunt N4
    "main_render_cache_misses": 0, # had to render main fresh — bughunt N4
    "main_render_cache_shadow_mismatches": 0,  # COPS-2631 shadow audit failures
    # COPS-2645: hits split by tier. A single hit counter cannot tell "the
    # cache works" from "the cache works only inside one pod's life", which
    # is exactly the confusion that hid the 0% hit rate for two releases.
    "main_render_cache_hits_memory": 0,
    "main_render_cache_hits_disk": 0,
    "main_render_cache_hits_gcs": 0,
    "main_render_cache_gcs_stores": 0,
    "main_render_cache_gcs_store_failures": 0,
    # COPS-2647: artifact bucket outcomes. A failed upload leaves the
    # PREVIOUS commit in the bucket while the leader serves the current
    # one, and load_artifact sends a replica to the bucket whenever its
    # local sha does not match -- so this counter rising means the two
    # pods may now present different diffs for the same URL.
    "artifact_gcs_upload_ok": 0,
    "artifact_gcs_upload_failed": 0,
    "artifact_gcs_upload_retries": 0,
    "artifact_gcs_download_failed": 0,   # 404 is a miss, NOT a failure
    "artifact_gcs_pending": 0,           # gauge: uploads awaiting reconcile
    # v2.5.19 (M8): visibility into the v2.5.18 scale machinery — are these
    # paths firing in production, and how often?
    "comments_truncated": 0,       # comments that exceeded MAX_COMMENT_BYTES
    # COPS-2575: webhook-triggered vs safety-net-triggered iterations. If the
    # webhook silently dies, safety_net climbs and webhook flatlines.
    "iters_webhook_triggered": 0,
    "iters_safetynet_triggered": 0,
    # COPS-2576: a standby's loop passes, counted apart. Its 5s reactive
    # wait is a leadership-handoff poll, not the 60s safety net, so mixing
    # it into the pair above made the safety-net-to-webhook ratio useless
    # on a standby (it climbed ~12x faster and meant nothing).
    "iters_standby_wait": 0,
    "last_iteration_trigger": None,
    # COPS-2577: largest number of apps a single run was asked to evaluate.
    # The cap is a hard merge block once crossed (over-cap apps are not
    # evaluated, so the status is FAILED rather than a false green), which
    # means an undersized cap first shows up as a blocked production PR.
    # This is the demand before truncation, so the gap to MAX_APPS_PER_RUN
    # is the real remaining headroom.
    "max_affected_apps_seen": 0,
    "ai_prompt_capped": 0,         # AI prompts capped at AI_MAX_APPS
    # COPS-2609 (phase B): the shape of the comment we post. Phases C-E are
    # verified against these numbers, so they have to exist before those
    # phases move anything. comment_bytes is the last comment rendered;
    # comment_max_bytes is the high-water mark, which is the one that can
    # answer phase E's "no comment ever reaches MAX_COMMENT_BYTES": a
    # last-value gauge only ever describes whichever PR rendered last.
    "comment_bytes": 0,            # size of the most recent comment body
    "comment_max_bytes": 0,        # largest comment body rendered so far
    "comment_fences": 0,           # diff fences in the most recent comment
    # Comments posted with the hunks inlined because the full-diff page was
    # not available. Harmless today; from phase E on, every one of these is
    # a reviewer reading a comment that lost its backing page.
    "comment_fallback_inline": 0,
    # Times the storage cap trimmed an app's section list (COPS-2610). Every
    # increment is content missing from BOTH surfaces; after phase E, from
    # everywhere. Should stay 0 -- if it moves, raise
    # FULL_SECTIONS_MAX_PER_APP.
    "section_cap_trims": 0,
    "diff_retries": 0,             # per-diff transient retries performed
    "futures_cancelled": 0,        # subtask futures cancelled on abnormal exit
    # v2.5.20 (E1): HTTP connection-pool observability. reuses vs fresh
    # tells whether keep-alive is actually paying off in production;
    # fallbacks counts requests the pool could not serve (redirects,
    # double connection failures, non-HTTPS) and re-routed to urllib.
    "http_pool_reuses": 0,         # requests served on an existing connection
    "http_pool_fresh_conns": 0,    # new HTTPSConnections opened
    "http_pool_fallbacks": 0,      # requests re-routed to plain urlopen
    # v2.5.25 (post-403-incident L1/L2): OCI-path health, previously
    # invisible — a pod could be Ready with 100% of pulls failing.
    "oci_selfcheck": None,         # ok / failed / skipped — periodic helm show chart
    "oci_selfcheck_at": None,      # ISO timestamp of the last self-check
    "oci_consecutive_pull_failures": 0,  # systemic pull failures since last success
    "last_iteration_s": None,# seconds taken by most recent iteration
    "last_iteration_at": None,
    # COPS-2631 stage 0: per-stage cumulative wall time on the hot path.
    # Seconds + count so /metrics and /diff-preview/stats can prove the
    # render-cache work (stage 3) moved the needle, instead of guessing
    # from end-to-end PR duration. Keys are declared here AND in
    # _PROM_REGISTRY (a metric is a contract; inventing one by accident
    # is forbidden — see COPS-2627).
    "stage_pull_seconds": 0.0,     # chart ensure + value-file fetch
    "stage_pull_count": 0,
    "stage_render_seconds": 0.0,   # helm template wall clock
    "stage_render_count": 0,
    "stage_parse_seconds": 0.0,    # _parse_manifest_resources
    "stage_parse_count": 0,
    "stage_diff_seconds": 0.0,     # _diff_resources
    "stage_diff_count": 0,
    "stage_store_seconds": 0.0,    # full-diff artifact save
    "stage_store_count": 0,
}
_diff_stats_lock: threading.Lock = threading.Lock()
