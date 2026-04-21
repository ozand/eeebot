# Discard + Revert Enforcement E2E Proof

Last updated: 2026-04-21 UTC

Goal:
Complete the autoresearch-style decision loop by enforcing a durable revert workflow when an experiment outcome is `discard`.

Implemented:
- when `metric_current < metric_baseline` on a PASS cycle, experiment outcome becomes `discard`
- Nanobot writes a revert record under:
  - `workspace/state/experiments/reverts/<experiment_id>.json`
- experiment snapshots now include:
  - `revert_required`
  - `revert_status`
  - `revert_path`
- dashboard `/experiments` surfaces:
  - `outcome=discard`
  - `revert=queued`
- overview `/` uses a status badge for the current experiment outcome

Live proof:
1. seeded previous experiment baseline with metric `2.0`
2. ran a fresh bounded local cycle that produced current metric `1.0`
3. recollected dashboard data
4. verified live:
   - `/experiments` contains `outcome=discard`
   - `/experiments` contains `revert=queued`
   - `/api/experiments` contains `"outcome": "discard"`
   - `/api/experiments` contains `revert_status`
5. confirmed revert file exists in:
   - `workspace/state/experiments/reverts/`

Result:
The keep/discard/crash/blocked discipline is now no longer passive reporting only. The system emits a durable revert work item automatically for discard outcomes.
