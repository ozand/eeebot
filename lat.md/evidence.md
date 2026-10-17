# Cycle evidence, ledger, and fitness

Cycle records, verification evidence, and fitness summaries answer different questions and have different trust boundaries.

## Cycle identity and ledger

The ledger records cycle phases and terminal outcomes; it does not itself certify acceptance.

[[nanobot/runtime/cycle_ledger.py#append_event]] appends timestamped phase evidence best-effort; terminal outcomes are written through [[nanobot/runtime/cycle_ledger.py#record_cycle_outcome]]. A cycle ID ties phase rows, demand/proposal context, and a terminal result together. A recorded outcome is not by itself proof of a passing gate, integration, or promotion. Bounded readers can report incomplete or unavailable evidence; absence or an unreadable ledger is not equivalent to a measured zero. See [[tests/test_cycle_ledger.py#TestTypedHelpers#test_one_terminal_row_per_cycle]] and [[tests/test_cycle_ledger.py#test_run_window_pruned_store_is_unavailable_not_empty]].

## Scorecard and held-out checks

Scorecard summaries and held-out checks provide fitness evidence distinct from the per-cycle smoke gate.

[[nanobot/runtime/scorecard.py#compute_scorecard]] composes bounded loop, cost, quality, value, and held-out summaries from ledger rows and protected sidecars. Held-out evaluation is implemented by [[nanobot/runtime/heldout/__init__.py#run_heldout]] and applies harness-owned checkers to instance artifacts. Missing or unreadable inputs retain explicit status rather than being presented as confirmed zero. See [[tests/test_heldout.py#TestRunner#test_good_scripts_pass_and_results_shape]], [[tests/test_heldout.py#TestRunner#test_head_time_watermark_no_op]], and [[tests/test_scorecard.py#TestReaderStatus1179#test_missing_ledger_is_unavailable_not_empty]].

## Trust boundary

The instance cannot author its own fitness verdict or protected fitness inputs.

The bridge snapshots and compares protected fitness inputs around subagent spawn windows, recording detected changes as integrity events.

The instance cannot author fitness verdicts or protected inputs. [[nanobot/runtime/scorecard.py#fitness_sidecar_hashes]] computes the hash inventory; [[nanobot/runtime/bridge.py#_run_planning_session]] and [[nanobot/runtime/bridge.py#_main_impl_body]] perform pre/post comparisons and append integrity evidence when values change. See [[tests/test_bridge_fitness_integrity.py#TestSpawnBoundaryTamperDetection#test_subagent_sidecar_write_records_integrity_row]]. The repository describes this mechanism, not whether a host ran a check or what its current metrics are.
