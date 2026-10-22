# Promoted overlays and subagent gates

Overlay loading, smoke gating, and fitness acceptance are separate trust steps.

## Promoted overlay loading

[[nanobot/runtime/promoted_overlay.py#install_promoted_overlay]] refuses untrusted trees and loads active, verified entries.

It checks root ownership and permissions through [[nanobot/runtime/promoted_overlay.py#_boundary_ok]], then revalidates denied paths, slice shape, and hashes while loading through [[nanobot/runtime/promoted_overlay.py#_load_one_module]]. A bad entry is skipped and an overall loader error returns no loaded modules, but this is not transactional rollback of arbitrary effects from code already executed. Call early: references imported beforehand remain bound to installed code. Assertions: [[tests/test_promoted_overlay.py#TestBoundarySelfCheck#test_boundary_ok_false_on_stat_exception]], [[tests/test_promoted_overlay.py#TestManifestEntryValidation#test_deny_set_module_is_refused_even_if_manifest_claims_active]], [[tests/test_promoted_overlay.py#TestManifestEntryValidation#test_sha256_mismatch_is_refused]], [[tests/test_promoted_overlay.py#TestManifestEntryValidation#test_one_bad_entry_never_blocks_a_good_sibling_entry]].

The runtime slice is separately calculated by [[nanobot/runtime/promoted_overlay.py#effective_runtime_slice]].

The gate consumes it during runtime-surface classification. See [[tests/test_runtime_slice.py#test_slice_drops_deny_even_if_listed]] and [[tests/test_runtime_slice.py#test_classify_runtime_file_not_in_slice_is_violation]]. This is repository behavior, not a claim about the live host.

## Subagent execution and gate

The executor runs bounded work and records terminal outcomes.

The source in [[nanobot/agent/subagent.py]] sets checkpointing to opt-in (`checkpoint_commits=False`) and defaults the expected branch to unset; [[nanobot/agent/subagent.py]] skips when that branch is absent. The executor loop observes iteration limits, wall-clock deadline, and progress watchdog; cancellation records telemetry before propagating. Tests verify configured iteration limits in [[tests/test_subagent_manager.py#test_subagent_manager_honors_configured_max_iterations]], main-branch refusal in [[tests/test_agent_chooses.py#test_checkpoint_commit_never_lands_on_main]], and different-cycle branch/CAS/index behavior in [[tests/test_agent_chooses.py#test_checkpoint_bound_to_expected_branch]]. That test's docstring specifies the three scenarios; it does not establish the default-off or unset-branch cases. Lat resolves the checkpoint helper anchor, but not the nested `_run_subagent` method, so the source file is linked for that method.

Execution completion is not integration. The bridge's [[nanobot/runtime/bridge.py#_run_smoke_tests]] and gate checks are separate. Mutation validation and bounded test selection use [[nanobot/runtime/gate.py#_validate_mutation_surfaces]] and [[nanobot/runtime/gate.py#_select_gate_tests]]. Gate failure refuses integration: see [[tests/test_bounded_gate.py#TestFailSafe#test_harness_exception_fails_closed]] and [[tests/test_bridge_cycle_branch.py#TestSmokeGateFailSafe]].

Test-weakening detection is additional to the main smoke gate. [[nanobot/runtime/test_guard.py#evaluate]] treats test deletion with non-test changes, net assertion/raises loss, and newly added skip/xfail as hard signals; parametrization shrink and renamed-file assertion loss are soft signals. Git, timeout, or parse trouble deliberately fails open. Assertions: [[tests/test_test_guard.py#TestHardSignalDeletion#test_deleted_test_file_with_non_test_change_blocks]], [[tests/test_test_guard.py#TestHardSignalAssertLoss]], [[tests/test_test_guard.py#TestHardSignalSkipMarker]], [[tests/test_test_guard.py#TestSoftSignals]], [[tests/test_test_guard.py#TestFailOpen]]. An unblocked detector verdict is not smoke-gate success.

## Evidence, fitness, and promotion authority

Candidate and cycle evidence is not itself acceptance.

The separation is summarized in [[evidence]] and [[gates-and-promotion]].

Harness-owned fitness boundaries are specified by [[docs/specs/self-evolving-runtime/spec.md]] (R26) and [[docs/specs/subagent-bridge/spec.md]]; the instance does not own the scorecard or held-out checkers. Source checks and smoke results do not prove fitness targets or host release activation.

No ADR acceptance is asserted by this page. The linked current specs define the applicable behavior; this page records source-grounded navigation only and does not amend runtime behavior or architectural decisions.
