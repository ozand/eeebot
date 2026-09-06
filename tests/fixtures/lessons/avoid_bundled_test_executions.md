# Avoid Bundled Test Executions

## Symptom

A verification step that chains several test suites or long-running scripts in a
single shell command (e.g. `pytest tests/ && python3 scripts/preflight.py &&
pytest tests/test_gate.py`) is killed by the tool's default 60-second timeout
before any suite finishes. The cycle then either retries the same compound
command (burning iterations) or skips verification entirely, leaving the commit
unvalidated. This was observed in cycle-e550dad85580, where a bundled
multi-suite pytest run plus a preflight script exceeded the default limit on
the slow i386 host.

## Root Cause

1. **Default tool timeout is 60 seconds.** The `exec` tool kills any command
   still running after 60s unless an explicit `timeout` parameter is passed.
   On this host (i386, 2 GB RAM, slow disk), a full `pytest tests/` pass alone
   can approach or exceed 60s; chaining multiple suites multiplies the wall
   time.
2. **Bundling hides per-step cost.** When N suites are joined with `&&`, the
   operator (and the agent) cannot see which step is slow, so the natural
   reaction is to re-run the whole bundle rather than isolate the slow step.
3. **No per-suite budgeting.** The agent treats "run the tests" as one atomic
   action instead of a set of independently runnable, independently timed
   units.

## Fix Applied

- **Run one suite per command.** Verify with a single targeted invocation
  (`python3 -m unittest tests.test_<target> -v` or
  `pytest tests/test_<target>.py -v`) instead of the whole `tests/` glob.
  The harness gate already runs the full suite; in-cycle verification only
  needs to prove the files this cycle touched.
- **Pass an explicit `timeout` for known-slow steps.** When a preflight or
  benchmark script legitimately needs more than 60s, invoke it with
  `timeout: 120` (or the smallest value that covers the measured runtime)
  instead of bundling it with other commands.
- **Isolate long-running preflight checks.** Run preflight/benchmark scripts
  in their own `exec` call, after the targeted test run, so a timeout in one
  does not discard the other's result.
- **Measure before bundling.** If two steps must both run, time each
  separately first (`time <cmd>` with a bounded cap) and only bundle them if
  the combined wall time is comfortably under 60s.

## Prevention

1. **Default to targeted, single-suite verification.** The in-cycle rule is
   "one test file per `exec` call"; the gate owns full-suite runs.
2. **Never chain a known-slow command with `&&`.** If any step in a chain
   has ever exceeded ~30s on this host, split the chain.
3. **Use the `timeout` parameter deliberately.** A 120s or 300s explicit
   timeout on a single isolated command is safe; a 60s default on a bundle is
   not.
4. **Treat a timeout kill as a signal to split, not to retry.** Re-running
   the same compound command after a timeout will time out again; isolate the
   slow step instead.
5. **Keep verification output bounded** (`| tail -n 40`) so that a long
   run's output does not also exhaust the context budget.
