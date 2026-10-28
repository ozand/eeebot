# Runtime flow and persisted state

A bounded runtime cycle separates selected work, execution outcome, gate evidence, and durable state; this page maps selected seams rather than claiming full runtime coverage.

## Planning input is not execution authority

The planner chooses a plan or rest; candidate presence alone does not assign executor work.

Demand and proposer output are candidate inputs; the executor receives the planner's choice rather than an unselected candidate. [[nanobot/runtime/bridge.py#_run_planning_session]] coordinates that choice; [[nanobot/runtime/demand.py#collect_demand]] assembles demand. Regression assertions in [[tests/test_agent_chooses.py#test_planner_runs_first_and_its_plan_is_the_task]] and [[tests/test_agent_chooses.py#test_no_plan_does_not_fall_back_to_assignment]] distinguish plan authority from candidate presence. This is a selected contract, not an inventory of all candidate sources.

## Interrupted increment is carried, not completed

An interrupted increment remains pending for an explicit next-session decision, not a success claim.

A supply interruption can persist an open increment for the next planning session to explicitly keep, edit, or delete; it is not itself evidence of successful execution or integration. [[nanobot/runtime/open_increment.py#record_supply_interruption]], [[nanobot/runtime/open_increment.py#pending_open_increment]], and [[nanobot/runtime/open_increment.py#resolve]] implement that lifecycle. [[tests/test_agent_chooses.py#test_supply_interruption_carries_open_increment]] checks carry-forward and resolution, while [[tests/test_agent_chooses.py#test_open_increment_resolution_raising_fails_closed]] checks the bridge does not proceed as if resolution succeeded.

## Cycle ledger records transitions, not promotion

The cycle ledger records transitions; a row is not proof of promotion or release.

Cycle start, planning, gate decisions, and terminal outcomes are distinct ledger events written through [[nanobot/runtime/cycle_ledger.py#append_event]] and its record helpers. Reading these events is evidence of recorded transitions, not proof that a candidate passed promotion or was released. Promotion has a distinct boundary in [[gates-and-promotion#Cycle branch and gate]]. The selected event-writing symbols are not an exhaustive ledger schema.

## Persistence availability is part of the result

Bounded state reads distinguish unavailable inputs from partial evidence.

[[nanobot/runtime/state_access.py#ledger_window]] returns status-bearing bounded reads: a missing directory is unavailable, and a corrupt rotated archive can make an otherwise readable window partial rather than silently presenting complete evidence. Existing assertions: [[tests/test_state_access.py#test_ledger_window_missing_dir_is_unavailable]] and [[tests/test_state_access.py#test_ledger_window_corrupt_archive_is_partial]]. Separately, [[nanobot/runtime/open_increment.py#_save_state]] is followed by read-back verification in resolution; [[tests/test_agent_chooses.py#test_open_increment_resolution_raising_fails_closed]] covers a resolution failure, but does not independently establish atomic-write guarantees for every persistence path. Do not treat absent or unreadable state as an empty successful history.

## Dashboard recovery projection

The runtime dashboard reads planner recovery state for visibility only and omits private deferred-work identifiers.

[[scripts/eeebot_dashboard.py#read_no_plan_recovery]] accepts only the recovery schema and bounded flags/counts from the existing JSON state; labelled source failures and invalid state remain unavailable rather than zero. Existing metrics, health, and HTML presentation use [[scripts/eeebot_dashboard.py#collect_metrics_uncached]] and [[scripts/eeebot_dashboard.py#render_html]]. [[tests/test_dashboard_no_plan_recovery.py]] exercises those GET surfaces using the production collector with unrelated host/state probes stubbed, including omission of private payload fields and no recovery writes. A controlled temporary removal of the collector wiring fails the integration assertions; source bytes are restored afterward. This read-only projection does not add resume or recovery authority; ADR-035 remains Proposed.

## Spec and decision status

The spec describes product intent, while ADR-035 remains proposed; neither implies host deployment.

The current behavior should be read alongside [[docs/specs/self-evolving-runtime/spec.md]]; the spec's aspirational cycle contract is not evidence that every described artifact exists in this selected flow. [[docs/adr/ADR-035-the-planning-session-chooses-and-closes-hadi.md]] is marked proposed, not accepted: observed code and regression tests do not change that decision status. No deployment or current host state is asserted here.
