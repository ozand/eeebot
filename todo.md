# eeebot audit remediation todo

Goal: bring live behavior and operator surfaces closer to the canonical operating contract.

## Priority order

- [ ] 1. Approval truth normalization
  - Problem: approval file can be expired while repo/dashboard surfaces still imply `fresh`.
  - Product changes:
    - recompute approval freshness from `workspace/state/approvals/apply.ok`
    - expose expiry/freshness/ttl fields in runtime + dashboard
    - make overview/cycles/approvals/system truthful at current time
  - Acceptance:
    - if approval is expired at audit time, UI/API say expired/stale
    - no page shows implied PASS/fresh solely from stale copied state

- [ ] 2. Experiment execution status vs evaluation outcome reconciliation
  - Problem: latest experiment can show `result_status=PASS` but `outcome=discard`, which is semantically valid but operator-misleading.
  - Product changes:
    - preserve execution status and evaluation outcome as separate fields
    - show both clearly in overview/experiments/API
  - Acceptance:
    - `/experiments` and `/api/experiments` clearly show PASS + discard as distinct dimensions

- [ ] 3. Canonical current control-plane summary
  - Problem: current blocker / current task / active execution truth is spread across multiple partial sources.
  - Product changes:
    - create one canonical summary object for current control-plane state
    - include goal, blocker, task, experiment, approval, execution, revert, stale flags
    - surface in overview, `/api/summary`, `/system`, `/api/system`
  - Acceptance:
    - operator can answer the key “what is happening now?” questions from one summary object

- [ ] 4. Stale execution control-state repair
  - Problem: active execution control state can be stale/null while project appears in progress.
  - Product changes:
    - tighten stale execution semantics in control snapshot/feed/dashboard
    - clearly separate live execution vs stale/blocked/waiting-for-dispatch
  - Acceptance:
    - no “in progress” active execution with null executor linkage presented as healthy

- [ ] 5. `/api/system` upgrade
  - Problem: `/system` page is useful, `/api/system` is too thin.
  - Product changes:
    - add richer system/control-plane payload to `/api/system`
    - include file previews/control summary useful to operators
  - Acceptance:
    - `/api/system` meaningfully reflects `/system`

- [ ] 6. Verification and proof
  - Run targeted tests and live checks after each slice.
  - Capture final proof note if all slices land cleanly.

## CI stability

- [x] 7. Fix two pre-existing `main` test failures blocking all PR CI (done — 2026-06-23)
  - Problem: `main` CI is red on every PR due to two failures unrelated to the PR under test:
    - `tests/test_research_feed_backlog_fallback.py::test_auto_seed_adds_priorities_when_empty` — `NameError: _task_already_done` (the AST test-loader extracts `_auto_seed_backlog_from_research` but not its `_task_already_done` dependency).
    - `tests/test_runtime_coordinator.py::test_cycle_executes_configured_subagent_executor_and_consumes_completed_result` — `executed_count` stays 0 (`assert 0 == 1`): a configured subagent executor's completed result is not consumed.
  - Fix (minimal, no over-engineering):
    - Bug 1 (test-only): stub `_task_already_done -> False` in the one test that reaches it (real fn returns False for fresh titles in a temp repo; the function is not under test here). No change to the fragile AST loader.
    - Bug 2 (production): `nanobot/runtime/subagent_materializer.py:346` — add `"bounded_execution"` to the executor-eligible profile gate. The coordinator emits `bounded_execution` requests (coordinator.py:2056/2488/3083) but the gate dropped them, so materialization could never run on a configured executor. Gated on `configured_executor` being set, so hosts without the executor env are unaffected.
  - Proof: both target tests pass; full suite `993 passed` (was `991 passed, 2 failed`); zero new ruff findings on changed files.
  - Rollout: dev = canonical `eeebot` (PR); rollout to running `eeebot-self-evolving` (PR) — see commit/PR links in HISTORY.
