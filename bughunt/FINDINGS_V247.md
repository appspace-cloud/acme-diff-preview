# v2.4.7 — one minor thread-safety fix + two user-requested improvements

## Minor: _gcp_access_token() race (found in the last deep-diagnosis round)

`_gcp_access_token()` cached the GCP metadata-server token in module globals
without a lock. `generate_ai_summary` runs per-PR under `MAX_PR_WORKERS`
concurrent threads, so two threads racing near expiry could both trigger a
redundant fetch, or (narrower window) end up with a token from one fetch
paired with the expiry timestamp from a different concurrent fetch. Neither
produces an invalid/unsafe token — real-world impact was always low — but
added a lock to remove the race entirely. Confirmed with a 20-thread
concurrent test: exactly 1 fetch, all callers see the same token.

## Improvement 1: build status link pointed at ArgoCD, not the diff

The Bitbucket build status badge always linked to `https://{ARGOCD_SERVER}`
— the ArgoCD login page for the acme-diff-preview Application itself, which
tells a reviewer nothing about the actual diff and requires separate ArgoCD
access to even load. The full diff is already in the PR comment. Changed
`post_build_status` to link back to the PR itself
(`https://bitbucket.org/{workspace}/{repo}/pull-requests/{pr_id}`) wherever
`pr_id` is available — all 12 call sites in `process_pr` and
`fix_stuck_inprogress` now pass it. Falls back to the old ArgoCD link only
if no `pr_id` is given (defensive default, not expected to fire in the
normal flow).

## Improvement 2: OCI-not-found error hid which package was missing

When a chart version doesn't exist in the OCI registry, the internal error
(`OciChartNotFound`) already captures the exact `chart:version` and
registry — but `format_comment` never showed it, only a generic hint
("Chart version not found in OCI registry — check that the version
exists"). A reviewer had no way to tell WHICH package to go publish or fix
without digging into pod logs. Now the specific `REASON_OCI_NOT_FOUND` case
gets a bold, prominent callout with the exact missing chart/version/registry
string; every other indeterminate reason keeps the existing generic-hint
format unchanged.

Tests: 126 passing (was 119). New: tests/test_v247_improvements.py (7 tests).
