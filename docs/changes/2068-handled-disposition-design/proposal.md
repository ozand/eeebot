# Change: Persist handled-marker disposition in the terminal ledger

- **change-id:** 2068-handled-disposition-design
- **issue:** [#2068](https://github.com/ozand/eeebot/issues/2068)
- **capability:** `docs/specs/self-evolving-runtime/spec.md` (proposed; no current spec delta authorized in discovery)
- **role / workstream:** runtime observability / persisted cycle ledger

## Problem

At source SHA `b3031531ff179728700db74f630c0cf6a042ba90`, the bridge computes a
bounded handled-marker disposition but discards the return before writing the
terminal cycle outcome. The ledger cannot report which existing result
(`retry`, retirement, supplier pause, or handled) occurred. This is disposition
observability only; it does not identify interruption cause or explain the
historical cycle-b56 `interrupted_defect` rows.

## Intended change

**Design proposal only for this issue.** Propose an optional `handled_disposition`
field on the terminal ledger outcome, populated only by carrying the exact
return value of `_decide_handled_marker`. Restrict values to the helper's
existing returns: `supplier_paused`, `handled`, `retired_state_lost`,
`retired_after_retries`, and `retry`. No decision, retry, fallback, budget, or
interruption behavior changes. Missing/unknown values are proposed to be
omitted; old rows remain valid. This is not approved implementation behavior.

## Acceptance

- [ ] Owner ozand explicitly approves field name/location and five-value enum semantics, including the broad `handled` value.
- [ ] Owner explicitly approves missing, unknown, and legacy-row behavior (proposal: omit; consumers treat absence as unknown).
- [ ] ADR-037 is reviewed and accepted by the owner before any implementation; this proposal remains Proposed until that decision.
- [ ] A later implementation plan maps each existing return to the same cycle's terminal outcome row and verifies compatibility/privacy without changing retry behavior.
- [ ] Accountable owner is ozand; design target is repository source/CI only. Any host rollout requires separate authorization and a staged rollback plan.

## Out of scope

No runtime code, schema/writer changes, tests, backfill, consumer/dashboard/public projection, cause attribution, interruption-path instrumentation, host change, model call, deployment, or rollout under #2068 discovery. This proposal does not cover separate open-increment interruption paths or explain cycle-b56 history.
