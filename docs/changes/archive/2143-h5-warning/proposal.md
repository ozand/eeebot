# Change: Per-task repeated-start warning evidence

- **change-id:** 2143-h5-warning
- **issue:** #2143
- **capability:** runtime health/reporting
- **role / workstream:** runtime / H5 warning design

## Problem

The proposed per-task H5 warning has unresolved identity, success-reset, and evidence-retention semantics. Existing start rows omit `retry_key`; therefore a warning cannot safely infer task attribution or report a low count from incomplete history.

## Intended change

Amend proposed ADR-039 with a conservative exact-key contract and bounded effective coverage horizon. This change implements the proposed behavior after the independent design review; ADR-039 remains Proposed. Persist the already-computed key on start, compute warning evidence on the existing health/reporting surface, and disclose aggregates only.

Use only exact equality of canonical `retry_key`: candidate ID verbatim, otherwise `self-` plus hash of the trimmed first plan-title line. An edited self-directed title may form a new identity. Do not add lineage, fuzzy matching, or a time/cycle-nearest join. Immediate success resets only the exact same key. A late `pushed_late` success without exact key lineage is UNKNOWN and cannot be assigned to a task.

Warn at 12 exact unsuccessful started attempts; 11 does not warn when coverage is known. A below-threshold count is publishable only when relevant start/reset coverage is known. A fully evidenced run of at least 12 exact starts after an unknown reset independently proves a warning, though affected-task count may remain unknown. Otherwise missing/expired/partial evidence that could conceal a reset makes the result UNKNOWN, not a guessed count.

Use existing `CYCLE_LEDGER_RETENTION_DAYS` as the effective coverage horizon (default 90). This is not a physical-pruning guarantee for the append-active ledger file; pruning is append-driven, and older/missing archive coverage is UNKNOWN. Extend the existing operator health/reporting surface; introduce no new notifier or service. Output must never contain raw keys. The warning has no action at 13 and does not block, wake, prioritize, mutate retry/handled state, or rollback. Keep the global #2050 tripwire separate and unchanged.

## Acceptance

- [x] ADR-039 remains Proposed and explicitly distinguishes delegated technical design from personal operator/ADR acceptance.
- [x] Exact retry-key identity, edited-title identity boundary, and no-inference rule are implemented.
- [x] Missing start/reset evidence and late-push without exact key are UNKNOWN; documented chronology distinguishes proven thresholds from unknown subthreshold counts.
- [x] Effective retention is the existing 90-day-default horizon without claiming active-file pruning.
- [x] Informational-only behavior, aggregate-only public output, and unchanged #2050 semantics are explicit.
- [x] Focused tests are written and passed; ADR-039 maps implemented claims to the test names.
- [x] #2088 acceptance walk/history and #2050 are unchanged.

## Out of scope

Changing #2088 criteria/history or #2050; new task-lineage identity, fuzzy matching, inferred late-push resets, new notifier/service, host/provider/private-history activity, deployment, ADR acceptance, issue closure, push, or merge.
