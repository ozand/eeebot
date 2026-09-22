---
name: run-tests
description: Trigger: Run bounded verification tests via scripts/run_all_tests.py before committing.
version: "1.3.0"
tags: [testing, validation, ci]
---

# run-tests

Run bounded verification tests for the files you changed and report pass/fail
in one call. The canonical runner is `scripts/run_all_tests.py`, which
discovers test targets across the `scripts/` self-tests, the `tests/` unit
suites, and the repository validators, executes each in a bounded subprocess,
and prints a consolidated report. Use this before committing to catch
regressions without burning extra tool calls.

## Command triggers

Prefer the unified runner over ad-hoc `pytest`/`unittest` invocations. Every
command below is bounded: pipe through `tail -N` so a large sweep cannot OOM
the eeepc host.

```bash
# Default gate: scripts/ self-tests + integrity validators (fast)
python3 scripts/run_all_tests.py 2>&1 | tail -40

# Scoped to one changed script (preferred for single-file cycles)
python3 scripts/run_all_tests.py --target <script_stem> 2>&1 | tail -40

# Scoped to one changed test file (no full sweep)
python3 scripts/run_all_tests.py --test-file tests/test_<module>.py 2>&1 | tail -40

# One suite only
python3 scripts/run_all_tests.py --suite scripts 2>&1 | tail -40
python3 scripts/run_all_tests.py --suite tests 2>&1 | tail -40
python3 scripts/run_all_tests.py --suite all 2>&1 | tail -60

# Name/pattern filters
python3 scripts/run_all_tests.py --pattern "*<keyword>*" 2>&1 | tail -40
python3 scripts/run_all_tests.py --exclude "*hang*" 2>&1 | tail -40

# Machine-readable report (for parsing, not for humans)
python3 scripts/run_all_tests.py --json 2>&1 | tail -60

# Discovery only (no execution) — confirm what would run
python3 scripts/run_all_tests.py --list 2>&1 | tail -40
python3 scripts/run_all_tests.py --list-suites 2>&1 | tail -20
```

## Bounded execution procedure

1. Identify changed files:
   ```bash
   git diff --name-only HEAD
   ```
2. If you changed exactly one script, run its scoped tests first (fastest
   feedback):
   ```bash
   python3 scripts/run_all_tests.py --target <script_stem> 2>&1 | tail -40
   ```
3. If you changed a test file, run just that file:
   ```bash
   python3 scripts/run_all_tests.py --test-file tests/test_<module>.py 2>&1 | tail -40
   ```
4. Only after the scoped run passes, run the default gate (scripts +
   integrity) to confirm no cross-cutting regression:
   ```bash
   python3 scripts/run_all_tests.py 2>&1 | tail -40
   ```
5. Reserve `--suite all` for pre-integration sweeps; it is the slowest path
   and should be the last check, not the first.

## Loop-breaker handling (stop repeated diagnostic execution)

When the tool executor returns a loop-breaker / safety-stop alert, or when you
are about to submit a test or diagnostic command whose inputs are identical to
one that already produced the same output, **halt the repetition immediately
and transition to source editing or verification in the same turn**. A
deterministic test command with unchanged inputs cannot produce a different
result; re-running it verbatim is pure waste, and the loop-breaker warning is
the harness's explicit signal that the current approach is stuck.

This is the test-execution form of the `handle-loop-breaker` skill and of
`lessons/errors/ERR-2026-09-06-001.md` (loop breaker warnings ignored —
subagent re-ran the same failing test command repeatedly). It addresses the
cycle-9ff792579a7a pattern, where the agent repeated the identical diagnostic
snippet five times, twice after the breaker fired, and ended with zero files
changed. [Lesson LESS-REF-9ff792579a7a-eef8]

On a loop-breaker alert (or two consecutive byte-identical test outputs):

1. **Halt.** Do not submit the next identical test/diagnostic call. The very
   next tool call must differ from the one that triggered the alert.
2. **Name the stuck command.** In one sentence, state which test command is
   repeating and what it produced. Do not treat it as a transient hiccup.
3. **Change at least one input** before any re-run: the target path, the
   arguments/flags, the runner (`run_all_tests.py` → `pytest` or vice versa),
   or your hypothesis about why it failed.
4. **Transition to a different strategy class.** If the repeated call was a
   test run, switch to reading the failing source or the prior output to form
   a new hypothesis, then edit the source or the test. If it was a diagnostic
   read, switch to the edit or the scoped test.
5. **Act, don't re-diagnose.** Proceed to the file edit or the scoped test in
   the same session; do not end the turn after re-diagnosis without an edit,
   a test, or a commit.

## Early transition checkpoint (test-expansion cycles)

Test-expansion cycles (adding or extending `test_*` methods) fail when the
worker spends its turn budget on exploration — reading history, lessons,
reflections, and unrelated suites — and reaches the iteration limit with no
test file written and no test executed. The turn count accumulates with zero
file modifications, so the cycle ends with nothing to commit.

Checkpoint: once the skip check and the target test file read are done, the
next tool call must be `edit_file`/`write_file` on the test file, not another
read. Concretely:

1. **Write the test method first.** Within the first 10-15 iterations, land
   the new/changed `test_*` method in the target file. Do not keep reading
   other suites, ledger entries, or reflector output to "gather context" —
   the task prompt already carries the relevant excerpts.
2. **Execute it immediately after writing.** Run the scoped command for that
   file right away so a pass/fail exists before the budget runs out:
   ```bash
   python3 scripts/run_all_tests.py --test-file tests/test_<module>.py 2>&1 | tail -40
   ```
   (or `python3 -m pytest tests/test_<module>.py -q 2>&1 | tail -30` for
   standalone-function suites — see Runner selection).
3. **Commit the test before widening scope.** A committed, passing test is a
   concrete artifact the next cycle can build on; an uncommitted one is
   discarded when the turn ends. Only after the scoped run passes, widen to
   the default gate (step 4 of the procedure above).

If you find yourself past iteration 15 with no `edit_file`/`write_file` on
the test file, stop reading and write the test now — a partial, committed
test beats a complete, uncommitted one.

## Runner selection (unittest vs pytest)

- `scripts/run_all_tests.py` runs `tests/` unittest suites via
  `python3 -m unittest <module>` and `scripts/` self-tests via their `--test`
  flag. It is the default and always available (stdlib only).
- If a test file defines standalone `test_*` functions with plain `assert`
  statements (the pytest convention, no `unittest.TestCase`), `python3 -m
  unittest` collects 0 tests and silently exits 0. For those files run pytest
  directly: `python3 -m pytest tests/test_<module>.py -q 2>&1 | tail -30`.
- When in doubt, prefer the unified runner; it handles both conventions and
  reports per-target status.

## Constraints

- Python 3.11 stdlib only; the runner needs no third-party packages.
- Keep output bounded: always pipe through `tail -N` to avoid OOM on eeepc.
- Exit 0 = all pass; non-zero = failures printed above the tail.
- Per-test timeout defaults to 30s; raise with `--timeout N` only when a
  target legitimately needs it, and cap the whole sweep with
  `--max-seconds 120` so a slow suite cannot hang the cycle.
- If the runner is unavailable, fall back to `python3 -m py_compile <file.py>`
  as a syntax check.

## When to use

- Before `git commit` on any changed `.py` file.
- After a repair-cycle edit to confirm the fix does not break existing tests.
- To verify a new test file catches the expected failure before integration.
