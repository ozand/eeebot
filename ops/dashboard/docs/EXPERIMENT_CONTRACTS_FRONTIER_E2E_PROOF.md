# Experiment Contracts + Frontier E2E Proof

Last updated: 2026-04-21 UTC

Goal:
Add autoresearch-style execution discipline to the existing Nanobot self-improvement loop without replacing HADI/WSJF/task-plans.

Implemented:
- experiment contract artifact per bounded experiment
- explicit outcome: `keep`, `discard`, `crash`, `blocked`
- metric fields:
  - `metric_name`
  - `metric_baseline`
  - `metric_current`
  - `metric_frontier`
- existing hourly budget remains authoritative

Producer-side artifacts:
- `workspace/state/experiments/contracts/<experiment_id>.json`
- `workspace/state/experiments/<experiment_id>.json`
- `workspace/state/experiments/latest.json`
- `workspace/state/experiments/history.jsonl`

Decision rules in this first slice:
- `BLOCK` -> `blocked`
- `ERROR` -> `crash`
- first successful experiment with no baseline -> `keep`
- successful experiment with current >= baseline -> `keep`
- successful experiment with current < baseline -> `discard`

Metric used in first slice:
- `reward_signal.value`

Live verification performed:
1. wrote/confirmed a fresh local approval gate
2. ran a fresh bounded local cycle
3. recollected dashboard data
4. verified over live HTTP:
   - `/experiments` shows `outcome=`
   - `/experiments` shows `metric=reward_signal.value`
   - `/experiments` shows `frontier=`
   - `/experiments` shows `contracts/`
   - `/api/experiments` shows `outcome`
   - `/api/experiments` shows `metric_frontier`
   - `/api/experiments` shows `contract_path`

Tests:
- Nanobot producer tests passed
- Dashboard tests passed

Bounded conclusion:
Nanobot now has a measurable experiment-contract layer that complements the current HADI + WSJF process instead of replacing it.
