# Broad exception handlers — inventory

Produced for COPS-2650 (2026-08-11 audit). This is a **review artefact**,
not a rulebook. The audit found 77 `except Exception` handlers across the
service and had no cheap way to tell the deliberate ones from the
accumulated ones. This file records the classification so the next reader
starts from evidence instead of a grep.

## Counts

| Measure | Count |
| --- | --- |
| Broad handlers (`except Exception` / bare `except`) | 77 |
| Silent (body is a bare `pass`) | 17 |
| Silent **and** uncommented | 15 |
| Log or count something | 36 |
| Neither logged nor commented | 31 |

Files: `src/diff_preview.py`, `src/diff_ui.py`, `src/leader.py`.

## Why most of them are correct

This service prefers degraded output over a failed run, and that is the
right trade for what it does: a diff comment that is missing one app is
recoverable, a poll loop that dies because a temp file was locked is not.
The dominant patterns are all deliberate:

- **Cleanup paths** — `conn.close()`, `shutil.rmtree`, `os.remove` on a
  temp file. A failure here has no consequence; the object is being
  discarded anyway.
- **Connection-pool churn** — closing a stale socket, setting a timeout on
  a reused connection. Failure means falling back to a fresh connection,
  which is the same code path a cold start takes.
- **Best-effort durability** — bucket mirroring, cache pruning. The local
  copy is authoritative; these paths exist to make the next read cheaper.
- **Terminal error reporting** — posting a build status or an error comment
  after a diff has already failed. Raising here would replace a useful
  error message with a stack trace nobody sees.

## Changes made under COPS-2650

Two silent handlers sat on paths that reach the comment, which is the
category the ticket said must at minimum log. Neither was a bug; both were
unreadable.

**`_diff_decommission` VM arming state.** The handler kept `vm_armed` and
`declares_vms` at their `False` defaults on any parse error. That is
correct — the surrounding comment documents the fail-closed intent, so an
unparseable identity file renders Phase 1 as *pending* rather than falsely
claiming it is done. But a reader looking at a phase table that seems wrong
had no way to learn why. Now logs at debug and says it is failing closed on
purpose.

**Chart pre-warm futures.** A pre-warm failure costs latency, never
correctness: `_run_one_diff` pulls the chart itself if it is not on disk.
Now logs at debug, because "every diff is slow" was otherwise invisible
from that handler.

Behaviour is unchanged in both cases. No handler was narrowed.

## Deliberately not done

**Narrowing types wholesale.** Tempting and wrong. Turning a best-effort
path into a crash path to satisfy a lint metric would trade a real
property (the run survives) for a cosmetic one. Narrow only where the
narrower type is obvious and provable, one at a time, with a test.

**Chasing the count to zero.** 77 is not a target. The number that matters
is "silent handlers on correctness paths", and after this pass it is zero.

## For the next reader

Regenerate the counts with an AST walk over `ExceptHandler` nodes rather
than grep — grep cannot tell a bare `except:` inside a nested function
from a commented one, and it miscounts multi-line handlers. The audit
script filtered on `node.type is None or node.type.id in (Exception,
BaseException)` and checked whether the body was a single `ast.Pass`.
