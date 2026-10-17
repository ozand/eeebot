# Gates, integration, and promotion

A cycle gate can authorize integration to the instance repository; it is not the same as product promotion or host release activation.

## Cycle branch and gate

Cycle changes stay isolated and must pass bounded, fail-closed checks before integration.

[[nanobot/runtime/bridge.py#_setup_cycle_branch]] isolates cycle work on a branch. Before integration, [[nanobot/runtime/gate.py#_validate_mutation_surfaces]] checks the changed surface and [[nanobot/runtime/gate.py#_select_gate_tests]] chooses bounded smoke coverage. The bridge integrates only after its gate decision permits it, through [[nanobot/runtime/bridge.py#_integrate_cycle_to_main]]. A failing gate keeps `main` unchanged and preserves the cycle branch for evidence. See [[tests/test_bridge_cycle_branch.py#TestFullCycleFlow#test_failing_gate_keeps_main_untouched_and_branch_for_forensics]] and [[tests/test_bounded_gate.py#TestFailSafe#test_harness_exception_fails_closed]].

## Promotion is a separate authority path

Runtime-slice changes use a promotion candidate and review path rather than ordinary cycle integration.

The bridge records a candidate for separate verification and operator review. Promotion policy and release contracts are described in [[docs/specs/promotion-and-release/spec.md]] and [[docs/specs/subagent-bridge/spec.md]]. The verifier evaluates candidates and may promote or roll them back through its own evidence path; it is not the cycle smoke gate. See [[tests/test_runtime_slice.py#test_record_candidate_writes_pending_promotion]] and [[tests/test_promotion_verifier.py#TestEligibleCandidateSoaksAndPromotes#test_full_lifecycle_soak_then_promote]].

## Limit of repository evidence

A passing source-level cycle gate does not prove a host release was activated or is healthy. Deployment is separately governed by [[docs/specs/host-runtime/spec.md]] and checked-in host tooling; no live host state is asserted here.
