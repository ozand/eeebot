# Nanobot Experiment Contract + Frontier Implementation Plan

> For Hermes: implement this as an additive layer on top of the existing HADI/WSJF/task-plan/experiment system. Do not replace the current process.

Goal:
Add an autoresearch-style execution discipline to Nanobot eeepc/local runtime:
- explicit experiment contract artifact
- explicit experiment outcome (`keep`, `discard`, `crash`, `blocked`)
- baseline/current/frontier metric visibility
- keep everything inside the existing bounded hourly experiment budget

Architecture:
- Producer side in `nanobot/runtime/coordinator.py`
- Reader side in `nanobot/runtime/state.py`
- Consumer/dashboard side in `nanobot-ops-dashboard/src/nanobot_ops_dashboard/app.py` and templates
- Existing experiment budget remains the controlling budget
- This adds measurement/decision artifacts, not a separate planner

Metric choice for first slice:
- use existing `reward_signal.value` as the primary comparable metric for the current self-improvement lane
- baseline = latest previous experiment reward value if present
- current = current experiment reward value
- frontier = max reward value seen in experiment history

Outcome rules for first slice:
- `BLOCK` result_status -> `blocked`
- `ERROR` result_status -> `crash`
- `PASS` with no baseline -> `keep` (establish baseline/frontier)
- `PASS` with current >= baseline -> `keep`
- `PASS` with current < baseline -> `discard`

Contract artifact:
- `workspace/state/experiments/contracts/<experiment_id>.json`

Contract fields:
- schema_version
- experiment_id
- cycle_id
- goal_id
- current_task_id
- selected_tasks
- task_selection_source
- contract_type = "bounded-hourly-self-improvement"
- run_budget (copy of experiment budget)
- success_metric
- baseline_ref
- keep_rule
- discard_rule
- crash_rule
- blocked_rule
- mutation_scope summary

Producer tasks:
1. Write failing tests for contract artifact + outcome/frontier fields
2. Implement contract builder and writer
3. Extend experiment snapshot with:
   - outcome
   - metric_name
   - metric_baseline
   - metric_current
   - metric_frontier
   - contract_path
4. Update runtime state reader with those new fields

Dashboard tasks:
5. Extend experiment parsing with new fields
6. Update `/experiments` page to show:
   - outcome
   - baseline/current/frontier
   - contract path
7. Extend `/api/experiments` with the same fields
8. Add failing dashboard tests first, then make them pass

Verification:
- Nanobot tests green
- Dashboard tests green
- Run a fresh local bounded cycle
- Re-collect dashboard data
- Verify live `/experiments` and `/api/experiments`
