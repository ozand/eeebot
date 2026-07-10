# Safety-shell invariants — existing test coverage map

- **Issue:** #703 (loop-redesign ticket D, stacked on #702)
- **Status:** evidence record — no code or tests changed by this document
- **story_id:** docs/specs/subagent-bridge/spec.md ("Immutable safety shell
  (loop-independent)")

## Purpose

This maps each frozen invariant in `docs/specs/subagent-bridge/spec.md`,
"Immutable safety shell (loop-independent)" (S1-S8), to the existing tests
that already verify it, so the freeze is demonstrably backed by hardening
work already merged (#653, #666, #678, #680, #686) rather than only by prose.
No new tests are added here; this is read-only evidence.

## Map

| Invariant | Implementing function(s) | Test file | Representative test(s) |
|---|---|---|---|
| **S1** Green-only integration (gate fails safe; `main` never advances on red/error/timeout) | `_run_smoke_tests`, `_run_smoke_tests_with_shrink_guard`, gate decision in `main()` | `tests/test_repair_loop.py` | `test_smoke_pass_when_all_tests_pass`, `test_smoke_fail_when_tests_fail`, `test_smoke_no_tests_dir_returns_false`, `test_smoke_no_tests_collected_returns_false`, `test_smoke_timeout_returns_false`, `test_smoke_pytest_not_found_returns_false` |
| **S1** (bounded-gate integration decision, end-to-end) | `main()` gate flow | `tests/test_bounded_gate.py` | `TestFailSafe` (`test_missing_core_and_no_affected_tests_fails_closed`, `test_missing_tests_directory_fails_gate`, `test_pytest_timeout_fails_gate`, `test_harness_exception_fails_closed`), `TestIntegrationFullCycle` (`test_scripts_change_with_passing_test_integrates`, `test_scripts_change_with_failing_test_does_not_integrate`) |
| **S1** (cycle-branch integration decision, end-to-end) | `_integrate_cycle_to_main` | `tests/test_bridge_cycle_branch.py` | `TestIntegrateCycleToMain.test_green_path_advances_and_pushes_main`, `TestIntegrateCycleToMain.test_stale_base_is_rejected_and_origin_main_untouched`, `TestFullCycleFlow.test_green_gate_integrates_and_pushes`, `TestFullCycleFlow.test_failing_gate_keeps_main_untouched_and_branch_for_forensics`, `TestSmokeGateFailSafe.test_missing_tests_directory_fails_gate`, `TestSmokeGateFailSafe.test_emptied_suite_fails_gate`, `TestSmokeGateFailSafe.test_harness_exception_fails_closed` |
| **S2** Protected paths / mutation surface (core `nanobot/`, `.github/`, `pyproject.toml`, bridge itself cannot land) | `_validate_mutation_surfaces`, `_ALLOWED_PATH_PREFIXES` | `tests/test_bridge_cycle_branch.py` | `TestMutationSurfaceHardBlock.test_violation_blocks_integration_and_main_stays_untouched`, `TestMutationSurfaceHardBlock.test_legit_surfaces_scripts_docs_memory_still_pass`, `TestRepairTurnSurfaceViolationIsCaught.test_clean_initial_then_repair_edits_core_is_blocked` |
| **S3** No-secret checks (`_BLOCKED_FILE_PATTERNS`) | `_validate_mutation_surfaces`, `_auto_commit_uncommitted_work` | `tests/test_bridge_cycle_branch.py` | `TestRepairTurnSurfaceViolationIsCaught.test_clean_initial_then_repair_adds_secret_is_blocked`, `TestBlockedPatternAcrossAllCommits.test_blocked_file_in_direct_commit_blocks_integration`, `TestAutoCommitUncommittedWork.test_blocked_pattern_file_excluded`, `TestAutoCommitUncommittedWork.test_only_blocked_pattern_files_commits_nothing` |
| **S4** Suite-shrink guard (cycle can't weaken the suite it's judged by; re-checked every repair retry) | `_run_smoke_tests_with_shrink_guard`, `_count_tests`, `_count_tests_at_ref` | `tests/test_bridge_cycle_branch.py` | `TestSmokeGateFailSafe.test_suite_shrink_guard_fails_when_test_count_drops`, `TestSmokeGateFailSafe.test_suite_shrink_guard_allows_growing_suite` |
| **S5** Git-verifiable rollback record (`main_sha_before == main_sha_after` iff not integrated) | `_write_bridge_completed_result`, `_cleanup_cycle_branch` | `tests/test_bridge_cycle_branch.py` | `TestCleanupCycleBranch.test_deletes_branch_after_integration`, `TestFullCycleFlow.test_failing_gate_keeps_main_untouched_and_branch_for_forensics`, `TestMutationSurfaceHardBlock.test_violation_blocks_integration_and_main_stays_untouched` (all assert `main` SHA stability / branch retention on non-integration) |
| **S5** (self-push cannot bypass isolation) | cycle-branch isolation | `tests/test_bridge_cycle_branch.py` | `TestSelfPushIsolation.test_plain_push_from_cycle_branch_does_not_advance_origin_main` |
| **S6** Concurrency lock (exclusive non-blocking `flock`) | `_acquire_bridge_lock` | `tests/test_bridge_locking.py` | `TestAcquireBridgeLock.test_acquires_when_free`, `test_second_acquire_in_same_process_is_contended`, `test_lock_releases_on_close_allowing_reacquire`, `test_falls_back_to_null_lock_when_fcntl_unavailable`, `test_contended_lock_via_monkeypatched_flock`, `TestMainHonoursLockContention.test_main_exits_cleanly_when_lock_held` |
| **S6** (HEAD-on-main precondition / exactly-one-bounded-subagent-per-cycle setup) | `_restore_to_main`, `_setup_cycle_branch` | `tests/test_bridge_locking.py`, `tests/test_bridge_cycle_branch.py` | `TestHeadOnMainPrecondition.test_restores_stray_cycle_branch_to_main`, `test_missing_repo_is_not_a_precondition_failure`, `test_restore_failure_would_trigger_abort_guard`; `TestSetupCycleBranch.test_creates_branch_off_origin_main`, `test_dirty_tree_blocks_setup_without_crash` |
| **S7** Stop-guard time/iteration budgets (subagent turn budget + repair-loop revision cap) | `config.agents.defaults.max_tool_iterations`, `stop_guards.REVISION_CAP_DEFAULT`, `stop_guards.revision_outcome` | `tests/test_bridge_repair_subagent_manager_kwargs.py` (adjacent — verifies the repair-turn spawn call site's kwargs are structurally valid, not the budget value itself); `tests/test_repair_loop.py` (repair-loop behavior exercised at the smoke-gate level) | `test_repair_subagent_manager_call_uses_only_valid_kwargs`, `test_repair_subagent_manager_call_passes_required_bus_kwarg`; repair-cycle smoke-gate tests above. **Weakest coverage of the eight invariants** — no test directly asserts `max_tool_iterations` or `REVISION_CAP_DEFAULT` bound a real run; see "Gaps" below. |
| **S8** Bounded gate sized to host per-cycle budget (`_select_gate_tests` selection, not full suite) | `_select_gate_tests`, `_CORE_SMOKE_TESTS`, import-smoke | `tests/test_bounded_gate.py` | `TestSelectGateTests.test_script_change_selects_its_test_plus_core`, `test_changed_test_file_selects_itself`, `test_no_matching_test_still_returns_core_set`; `TestImportSmoke.test_syntax_error_fails_gate_before_pytest_runs`; `TestAffectedAndCoreTests.test_affected_test_fails_gate`, `test_core_smoke_failure_fails_gate_even_with_unrelated_change` |
| **Auto-commit safety net** (adjacent to S1/S3 — dirty subagent work is captured, not silently discarded, before the gate runs) | `_auto_commit_uncommitted_work` | `tests/test_bridge_cycle_branch.py` | `TestAutoCommitUncommittedWork.test_dirty_tree_gets_committed`, `test_clean_tree_is_a_noop`; `TestFullCycleFlowWithAutoCommit.test_dirty_uncommitted_work_green_gate_integrates`, `test_dirty_uncommitted_work_failing_gate_keeps_forensic_branch` |
| **Scope-constrained bookkeeping pushes** (adjacent to S1/S2 — non-gated pushes outside the cycle-branch path are diff-constrained) | `_diff_against_remote_touches_only` | `tests/test_bridge_cycle_branch.py` | `TestAlreadyDonePushGate.test_memory_only_diff_is_pushable`, `test_non_memory_diff_blocks_push_and_main_stays_untouched`; `TestPostIntegrationBookkeepingPushGate.test_extra_file_in_diff_skips_push`, `test_intended_file_only_diff_allows_push` |

## Novelty / duplicate check (precheck P2 precursor)

`_task_already_done` (the git-log-based approximation the
`precheck-contract.md` P2 check is designed to reuse/replace with the #704
ledger) is exercised end-to-end via
`tests/test_decouple_generate_execute.py`:
`test_fresh_materialization_already_done_in_git_log_gets_no_request`,
`test_existing_live_request_for_the_same_artifact_is_not_duplicated`,
`test_stale_terminal_request_does_not_count_as_live`. These are planner/
materializer-side tests, not bridge-side, but are the closest existing
coverage for the "duplicate vs done-ledger" precheck rule.

## Gaps (informational, not a blocker for this freeze)

- **S7 is the weakest-covered invariant.** No test directly exercises
  `SUBAGENT_BRIDGE_MAX_REVISIONS` / `REVISION_CAP_DEFAULT` as a
  budget-exhaustion scenario (e.g. asserting the repair loop stops after N
  attempts), and no test asserts `config.agents.defaults.max_tool_iterations`
  actually bounds a subagent turn in the bridge specifically (as opposed to
  generically elsewhere in the codebase). Coverage today is indirect, via the
  repair-loop smoke-gate tests and a structural kwargs-validity test. This is
  a coverage gap worth a follow-up issue, not something this documentation
  ticket fixes (no code/tests are changed here).
- The precheck contract itself (P1-P3 in `precheck-contract.md`) has no
  dedicated tests because it does not exist as code yet — it is a design
  contract for #706/#707 to implement against. Once implemented, this table
  should be extended with its own row(s).

## Cross-links

- Invariant definitions: `docs/specs/subagent-bridge/spec.md`, "Immutable
  safety shell (loop-independent)" (S1-S8).
- Precheck contract these tests partially anticipate:
  `docs/changes/703-safety-shell-invariants/precheck-contract.md`.
- Architecture decision: `docs/changes/702-ledger-loop-architecture-decision/decision.md`.
