# Testing this repo

How to run the suite fast, why it is fast, and the rules that keep it
trustworthy. Written for engineers and for AI assistants (Claude, Cursor)
working in this repo.

`acme-diff-preview` is a review tool: its comment is what an engineer reads
before merging a change that reaches roughly 1000 production ArgoCD
Applications. The suite is the thing that stops us shipping a wrong comment.
Everything below optimises how *fast* the same assertions run. Nothing below
makes the suite prove less.

---

## The commands

```bash
# The release gate. Serial, ~2.5 minutes. This is the one to use.
python3 -m pytest tests/ -q

# One file while iterating (seconds)
python3 -m pytest tests/test_something.py -q

# Parallel: ~30s, but see the warning below before trusting it
python3 -m pytest tests/ -q -n auto --dist loadfile

# Regenerating golden files -- MUST be serial (see below)
UPDATE_GOLDEN=1 python3 -m pytest tests/test_cops2565_golden_comments.py -q
```

The release gate in `RELEASING.md` is the **full suite**, every time. Parallel
is fine for that; a subset is not.

---

## Why it used to take 20 minutes

Measured, not guessed. 1,245 tests, 80 files, ~1,224s. `--durations=40`
showed the cost was **not** spread evenly:

> The 40 slowest tests were ~1,087s of the 1,224s total. **89% of the runtime
> in 3% of the tests.**

They clustered at oddly uniform values (~26s, ~29s). `cProfile` on one of
them found 27.0s of its 29.7s inside `time.sleep`, plus real TLS handshakes.
Tracing each sleep to its caller gave the answer:

| Sleep origin | Time in ONE test |
|---|---|
| `_bb_fetch_cached` to `_bb_fetch_status` | 18.0s |
| `_pr_chart_revision_checked` to `_bb_fetch_status` | 12.0s |
| `_gcp_access_token` to `http` | 3.0s |

Those tests were escaping the `urlopen` mock seam, reaching the **real**
Bitbucket and GCP endpoints, failing, and retrying with hardcoded backoff
(`(attempt + 1) * 2`, `2 ** attempt`). The waits are hardcoded, so they could
not be turned down from outside.

A first guess that turned out to be **wrong**, recorded so nobody re-tests it:
the exponential `DIFF_BACKOFF_BASE=3` retry looked like the obvious culprit.
Running a 29.4s test with `DIFF_BACKOFF_BASE=0 DIFF_BACKOFF_CAP=0` gave
29.36s. Not it.

---

## What was changed

### 1. `time.sleep` is neutralised during tests (`tests/conftest.py`)

An autouse fixture replaces `time.sleep` with a no-op for the duration of each
test. `monkeypatch` reverts it at teardown, so nothing leaks between tests.

**The retry loops still run in full**: same attempts, same branches, same log
lines, same assertions. The only thing removed is wall-clock spent waiting for
a network the test never intended to touch.

If your test needs **real elapsed time** -- real threads that must make
progress during the wait, leader-election races, anything where another thread
has to get somewhere -- opt out:

```python
@pytest.mark.realtime
def test_two_replicas_hand_over_the_lease():
    ...
```

### 2. `pytest-timeout` as a guard

A per-test cap means a future non-hermetic test **fails fast and visibly**
instead of quietly costing 30 seconds or hanging CI. This is a safety net, not
a speed trick: a test that suddenly needs 60s is telling you something.

### 3. `pytest-xdist` with `--dist loadfile` (installed, NOT yet the default)

**Status: use with care.** The parallel run finishes in ~30s versus ~2.5 min
serial, but it currently produces around a dozen failures that the serial run
does not. Those are **not** caused by parallelism: each affected file passes on
its own under xdist. They are pre-existing **cross-file test pollution** —
tests that depend on state another file happened to leave behind, which the
serial ordering silently satisfied. xdist exposes the coupling, it does not
create it.

Until that is fixed, **the serial run is the gate**, in CI and before release.
Do not paper over the failures by reordering files, pinning an order, or
retrying. Fixing them means finding the shared state and making each file
hermetic — which is worth doing, and is tracked separately.

The design below is why `loadfile` is the right mode once the coupling is gone.


`src/diff_preview.py` carries module-level mutable state that tests clear in
setup and teardown: `_seen`, `_force_recompute`, `_main_render_cache`,
`_app_chart_map`, `_app_chart_revision_map`, `_vf_cache`, `_yaml_cache`,
`_retry_backoff`.

xdist runs **separate processes**, so each worker gets its own copy of every
global. One worker calling `_seen.clear()` cannot affect another. That is why
process-based parallelism is safe here and a thread-based runner would not be.

`--dist loadfile` keeps every test in a file on **one** worker, so a file's
own setup/teardown discipline still holds for all of its tests. Do not switch
to the default `--dist load` without thinking: it scatters a file's tests
across workers.

---

## Rules that keep the suite honest

**Golden files are regenerated serially, never in parallel.** Two workers
writing `tests/golden/*.md` at once corrupt each other. Read-only comparison
is parallel-safe; `UPDATE_GOLDEN=1` is not. Always diff a regenerated golden
before accepting it -- never regenerate blindly to make red go green.

**No shared fixed paths.** Use `tmp_path` / `tmp_path_factory`. A hardcoded
`/tmp/thing` collides across workers.

**Tests must be zero-network by assertion, not by accident.** Mock at the
`urlopen` seam. If a test reaches the real internet it is broken even when it
passes: it is slow, flaky, and dependent on someone else's uptime.

**A flaky test is a bug report, not an inconvenience.** This service has real
concurrency (leader election, locks). Do not add `pytest-rerunfailures` or
retry-until-green: that discards exactly the signal we most need.

---

## Guidance for AI assistants working in this repo

Read this section before changing tests or test config.

1. **Run the full suite serially: `python3 -m pytest tests/ -q`.** It is ~2.5
   minutes, not twenty. There is no longer an excuse to skip it, and
   `RELEASING.md` requires it before every release. Do **not** use `-n auto`
   as the gate yet: it is faster but currently red for reasons unrelated to
   your change (see the xdist section above).
2. **The suite takes long enough to exceed a single tool-call timeout.** Run
   it in the background and poll:
   ```bash
   nohup python3 -m pytest tests/ -q -n auto --dist loadfile > /tmp/suite.log 2>&1 &
   # then poll: tail -3 /tmp/suite.log
   ```
3. **Write the failing test first and confirm it is red** before implementing.
   A test that has never failed has not been shown to test anything.
4. **Never make a test faster by making it prove less.** Deleting a slow test,
   weakening an assertion, loosening a golden comparison or over-mocking the
   logic under test are all forbidden here regardless of the time saved. Speed
   comes from removing dead waiting, not from removing verification.
5. **If a test is unexpectedly slow, profile it, do not guess.** The last
   confident guess was wrong. The method that worked:
   ```bash
   python3 -m pytest tests/ -q --durations=40          # rank the offenders
   python3 -m cProfile -o /tmp/p.out -m pytest "<test>" -q
   python3 -c "import pytest_stats"                    # then sort by tottime
   ```
   `tottime` is the one that tells you where wall-clock actually burns;
   `cumtime` will just point at the test runner.
6. **Do not regenerate golden files to resolve a failure** unless you have
   read the diff and can explain why the new output is correct.
7. **If you add a test that genuinely needs real time**, mark it
   `@pytest.mark.realtime` rather than removing the sleep fixture.
