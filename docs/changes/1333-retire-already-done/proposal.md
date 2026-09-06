# Retire fuzzy `already_done` bookkeeping

## Context

Host ledger measurement through 2026-09-05 found 163 fuzzy `already_done` outcomes, all between 2026-07-12 and 2026-07-15. Only one was demand-linked, and no row has appeared for 52 days. The path compares title words with recent git subjects, can falsely classify renamed work as complete, mutates `memory/MEMORY.md`, and may push that bookkeeping before the normal integration gate.

The current in-repo request writer is `llm_proposer.write_request`. Demand-driven mode is default-on and demand items carry exact demand traceability. The same bridge still needs `_request_serves_demand` after this retirement for escalation-model selection, so that helper is not part of the removal.

## Change

Remove the fuzzy whole-log/path-log completion checks and their pre-spawn result/outcome/push branch. Keep exact `cycle-<id>-success` tag replay protection, recent-failure suppression, existence-index suppression, historical reason readers, and unrelated post-integration bookkeeping.

## Decommission duty map

| Former duty | Disposition | Evidence / owner now |
|---|---|---|
| Fuzzy recent-git completion decision | **dropped** | Zero live hits since 2026-07-15; false positives are worse than a spawned verification. |
| Path-scoped fuzzy completion decision | **dropped** | A path existing does not prove the requested extension is complete. |
| Exact replay of the same successful cycle ID | **superseded** | Existing `cycle-<id>-success` tag guard remains unchanged and emits `already_done_tag`. |
| Semantic duplicate of an existing different artifact | **superseded** | Existence index remains the bounded semantic backstop. |
| Repeat of recently failed/rejected work | **superseded** | `_recent_failure_match` remains the bounded failure backstop. |
| Mark `memory/MEMORY.md` done from a fuzzy match | **dropped** | Coordinator-era coupling; current request writer has no canonical MEMORY backlog completion contract. |
| Ungated push of that fuzzy mark-done commit | **dropped** | No replacement needed; removing the writer removes the unsafe path. |
| Historical ledger/result reason `already_done` | **orphaned, labelled history retained** | Scorecard/cycle-ledger readers keep parsing the old reason; no active writer remains. |
| Result terminal status `already_done` | **superseded for active writing by exact tag** | Exact-tag result keeps the status for schema compatibility; its reason/classification is explicitly `already_done_tag`. |
| `_request_serves_demand` | **not decommissioned** | Still selects escalation model after request selection. |
| `_extract_target_path` | **not decommissioned** | Still feeds recent-failure precision and result telemetry. |
| `_try_mark_backlog_done` and `_diff_against_remote_touches_only` | **not decommissioned globally** | Still serve post-integration memory/lesson/archiver duties; only the fuzzy caller is removed. |

## Reader audit

The removed gate wrote no unique state path: it wrote existing bridge result, ledger, dedup-decision, git-tag, and `memory/MEMORY.md` surfaces. Those surfaces retain other active writers. Literal searches across the product and companion repositories must leave only historical docs/tests and explicit historical-reader vocabulary, not a current fuzzy decision symbol.

## Acceptance

- No `_task_already_done`, `_task_already_done_for_path`, or `_duplicate_check_title` function remains.
- No active ledger writer emits reason `already_done`.
- Exact success-tag replay remains tested with reason `already_done_tag`.
- Historical scorecard and ledger fixtures containing `already_done` remain readable.
- Full pytest has no regression beyond the documented Windows baseline.
