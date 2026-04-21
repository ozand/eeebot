# eeebot audit remediation todo

Goal: bring live behavior and operator surfaces closer to the canonical operating contract.

## Priority order

- [ ] 1. Approval truth normalization
  - Problem: approval file can be expired while repo/dashboard surfaces still imply `fresh`.
  - Product changes:
    - recompute approval freshness from `workspace/state/approvals/apply.ok`
    - expose expiry/freshness/ttl fields in runtime + dashboard
    - make overview/cycles/approvals/system truthful at current time
  - Acceptance:
    - if approval is expired at audit time, UI/API say expired/stale
    - no page shows implied PASS/fresh solely from stale copied state

- [ ] 2. Experiment execution status vs evaluation outcome reconciliation
  - Problem: latest experiment can show `result_status=PASS` but `outcome=discard`, which is semantically valid but operator-misleading.
  - Product changes:
    - preserve execution status and evaluation outcome as separate fields
    - show both clearly in overview/experiments/API
  - Acceptance:
    - `/experiments` and `/api/experiments` clearly show PASS + discard as distinct dimensions

- [ ] 3. Canonical current control-plane summary
  - Problem: current blocker / current task / active execution truth is spread across multiple partial sources.
  - Product changes:
    - create one canonical summary object for current control-plane state
    - include goal, blocker, task, experiment, approval, execution, revert, stale flags
    - surface in overview, `/api/summary`, `/system`, `/api/system`
  - Acceptance:
    - operator can answer the key “what is happening now?” questions from one summary object

- [ ] 4. Stale execution control-state repair
  - Problem: active execution control state can be stale/null while project appears in progress.
  - Product changes:
    - tighten stale execution semantics in control snapshot/feed/dashboard
    - clearly separate live execution vs stale/blocked/waiting-for-dispatch
  - Acceptance:
    - no “in progress” active execution with null executor linkage presented as healthy

- [ ] 5. `/api/system` upgrade
  - Problem: `/system` page is useful, `/api/system` is too thin.
  - Product changes:
    - add richer system/control-plane payload to `/api/system`
    - include file previews/control summary useful to operators
  - Acceptance:
    - `/api/system` meaningfully reflects `/system`

- [ ] 6. Verification and proof
  - Run targeted tests and live checks after each slice.
  - Capture final proof note if all slices land cleanly.
