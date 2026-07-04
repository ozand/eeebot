# Change: skip already-done goal_text.json priorities using the existing git-log heuristic

- **change-id:** goal-text-done-detection
- **issue:** #575
- **capability:** `docs/specs/self-evolving-runtime`
- **role / workstream:** role:developer / workstream:runtime

## Problem

`_parse_backlog_task_from_goal_text` (`coordinator.py:452-497`, added in #568/#569)
parses `state/goals/goal_text.json`'s `"Current priority targets:"` section
with **no done-detection** — it always returns the lowest-numbered priority
found, regardless of whether it was already completed. Meanwhile
`_parse_backlog_task_from_memory`/`_curriculum_level`
(`coordinator.py:2502-2554`) already has exactly this heuristic for the
MEMORY.md backlog: search `eeebot-self-evolving`'s `git log --oneline
--since=14 days ago`, extract 4+ char words from the priority title, and
treat it as done if ≥2 of those words appear in the log.

On host: `goal_text.json`'s "Priority 5 — Write scripts/cycle_logger.py" was
completed 2026-06-22 and re-confirmed as already-done at least 5 times since
(bridge commits like `chore: confirm Priority 5 (cycle_logger.py) verified
for cycle-...`). The keyword-match heuristic would already catch this: title
words `write/scripts/cycle/logger` match `cycle`/`logger` substrings in the
confirmation commit messages, satisfying the existing `matches >= 2` rule —
but `_parse_backlog_task_from_goal_text` never runs this check, so the same
priority keeps getting re-derived as a fresh dispatchable task every cycle,
producing duplicate `bounded_execution` requests (10 of 13 queued requests
observed to be exact duplicates).

## Intended change

Extract the git-log keyword-match heuristic currently embedded in
`_curriculum_level`'s loop body (lines 2544-2552) into a standalone helper,
e.g. `_title_already_done_in_git_log(title: str, repo_root: Path) -> bool`.
`_curriculum_level` calls this helper instead of inlining the logic (pure
refactor, no behavior change — existing MEMORY.md/curriculum tests must keep
passing unmodified).

Give `_parse_backlog_task_from_goal_text` an optional
`selfevo_repo_root: Path | None = None` parameter. When iterating candidate
priorities (currently just taking the lowest-numbered match), skip any whose
title the new helper reports as already-done, and return the lowest-numbered
**not-done** priority, or `None` if all found priorities are done.

Update the call site in `_write_materialized_improvement_artifact`
(`coordinator.py:2673`) to pass the already-in-scope `_selfevo_root`
(computed at line 2665, before this call) into
`_parse_backlog_task_from_goal_text(state_root, selfevo_repo_root=_selfevo_root)`.

## Acceptance

- [ ] A `goal_text.json` priority whose title keywords (≥2 matches, 4+ chars)
      appear in `eeebot-self-evolving`'s recent git log is skipped, not
      re-derived as a fresh task (test).
- [ ] When all found `goal_text.json` priorities are already done, the
      function returns `None` (falls through to the next fallback tier —
      `todo.md`/research feed — unchanged existing behavior).
- [ ] Regression guard: `_curriculum_level`'s existing behavior (MEMORY.md
      `[Done]`-marker + keyword-match gating) is unchanged after the
      refactor — all existing curriculum/backlog tests pass unmodified.
- [ ] Regression guard: a genuinely open (not-yet-done) `goal_text.json`
      priority is still correctly routed — existing #568/#569 tests
      (`test_parse_backlog_task_from_goal_text_real_host_format`, etc.) pass
      unmodified.
- [ ] Full test suite green; deployed to eeepc and verified — the duplicate
      "Priority 5" request churn stops.

## Out of scope

- Redesigning `goal_text.json`'s schema, adding a `[Done]`-marker convention
  to it, or making the deploy-time seeding mechanism smarter — this only
  wires in the existing done-detection heuristic, it doesn't change how
  `goal_text.json` itself is authored/seeded.
- The bridge crash bug (#574) — unrelated root cause, fixed separately.
