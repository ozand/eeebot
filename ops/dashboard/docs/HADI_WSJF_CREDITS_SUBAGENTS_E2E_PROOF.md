# HADI + WSJF + Credits + Subagent Correlation E2E Proof

Last updated: 2026-04-21 UTC

## Goal

Prove that the remaining operator gaps are now closed end-to-end across Nanobot producer state and the local dashboard consumer state:
- HADI backlog model
- explicit WSJF surface
- credits ledger
- durable subagent/task correlation visibility

## Producer-side changes

Implemented in Nanobot:
- hypothesis backlog entries now include `hadi`
- hypothesis backlog entries now include explicit `wsjf`
- backlog root now records `model = HADI`
- credits ledger now writes to:
  - `state/credits/latest.json`
  - `state/credits/history.jsonl`
- subagent telemetry now carries correlation fields:
  - `goal_id`
  - `cycle_id`
  - `report_path`
  - `current_task_id`
  - `task_reward_signal`
  - `task_feedback_decision`

## Consumer-side changes

Implemented in the dashboard:
- `/hypotheses` now renders explicit HADI + WSJF fields
- `/api/hypotheses` now exposes:
  - `model`
  - `selected_hypothesis_wsjf`
  - `selected_hypothesis_hadi`
- `/credits` now exists as an explicit credits ledger page
- `/api/credits` now exists
- `/experiments` now surfaces the current credits ledger summary
- `/subagents` now surfaces task correlation fields:
  - current task
  - reward signal
  - feedback decision

## Live verification performed

A real local Nanobot bounded cycle was executed into:
- `/home/ozand/herkoot/Projects/nanobot/workspace/state`

Then the local dashboard collector was run again and the web service was restarted.

Verified over live HTTP:
- `/hypotheses` contains `HADI`
- `/hypotheses` contains `WSJF`
- `/credits` contains `Credits ledger`
- `/experiments` contains `Current credits ledger`
- `/subagents` contains `Current task`
- `/subagents` contains `Reward signal`
- `/subagents` contains `Feedback decision`
- `/subagents` contains `Goal / cycle`

## Test proof

Nanobot producer-side tests:
- `tests/test_runtime_coordinator.py`
- `tests/test_task_cancel.py`

Dashboard consumer-side tests:
- full `python3 -m pytest -q`

All passed after the changes.

## Bounded conclusion

The previously open operator gaps are now closed for the bounded contract:
- HADI is explicit
- WSJF is explicit
- credits ledger is explicit
- durable subagent/task correlation is explicit

What is still not claimed:
- that WSJF is the final or only scoring model forever
- that credits are a full economic system beyond the current durable ledger
- that every future host/runtime will emit the same exact file schema without versioning
