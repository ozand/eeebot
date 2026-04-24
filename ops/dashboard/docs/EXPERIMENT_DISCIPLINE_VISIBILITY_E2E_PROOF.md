# Experiment Discipline Visibility E2E Proof

Last updated: 2026-04-21 UTC

Goal:
Extend the first experiment-contract tranche with stronger operator visibility:
- experiment contract on `/plan`
- outcome/frontier/simplicity on overview
- frontier section on `/analytics`
- simplicity signal in `/experiments`

Producer-side addition:
- experiment snapshots now include:
  - `complexity_delta`
  - `simplicity_judgment`

Dashboard-side addition:
- `/` now shows:
  - outcome
  - metric
  - experiment frontier
  - simplicity
- `/plan` now shows:
  - experiment contract
  - outcome
  - frontier
- `/analytics` now shows:
  - experiment frontier section
- `/experiments` now shows:
  - outcome
  - metric
  - baseline/current/frontier
  - simplicity judgment
  - complexity delta

Live verification:
After a fresh local bounded cycle and dashboard recollection, the following were verified over HTTP:
- `/` contains `Outcome`, `Experiment frontier`, `Simplicity`
- `/plan` contains `Experiment contract`, `Outcome`, `Frontier`
- `/analytics` contains `Experiment frontier`, `reward_signal.value`
- `/experiments` contains `outcome=`, `complexity Δ`, `frontier=`
- `/api/experiments` contains `outcome`, `metric_frontier`, `contract_path`

Tests:
- Nanobot producer tests passed
- Dashboard tests passed

Conclusion:
Nanobot now has both the experiment-contract layer and the operator surfaces needed to understand its bounded keep/discard/crash/blocked discipline inside the existing hourly budget.
