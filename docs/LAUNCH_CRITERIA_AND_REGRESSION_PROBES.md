# Launch Criteria and Regression Probes

Status: canonical launch-gate matrix for the current bounded eeebot runtime. This document locks the current launch criteria to concrete regression probes and operator proof surfaces.

## Launch criteria

1. Runtime state must be readable truthfully from the chosen authority.
   - Probe:
     - `tests/test_commands.py::test_status_can_report_host_control_plane_authority`
   - Operator proof:
     - `nanobot status --runtime-state-source ...`
     - `/system`

2. Control-plane summary must remain structurally valid.
   - Probe:
     - `tests/test_control_plane_summary.py`
   - Operator proof:
     - `/api/system`
     - `/system`

3. Self-evolving cycle must fail closed on missing approval and pass when gate is fresh.
   - Probe:
     - `tests/test_runtime_coordinator.py`
   - Operator proof:
     - report/outbox/control-plane summary

4. Governance records must stay schema-covered and replay/readiness-visible.
   - Probe:
     - `tests/test_promotion_workflow.py`
   - Operator proof:
     - `/system`
     - `/promotions`

5. Bounded safety signals must stay operator-visible.
   - Probe families:
     - `tests/test_capability_reporting.py`
     - `tests/test_host_resource_sensing.py`
     - `tests/test_app.py`
   - Operator proof:
     - `/system`

## Weak-host launch rule

Launch is not considered healthy if any of the following are true:
- runtime state cannot be read truthfully,
- control-plane summary validation fails,
- approval/human review boundary is closed for a path that requires it,
- governance coverage is action-required for a path expected to be replay-ready,
- regression probes for the bounded runtime contract are red.

## Rollback expectation

If a bounded slice breaks the green baseline:
- repair or rollback immediately,
- restore green tests,
- update the issue truthfully,
- only then continue to the next slice.
