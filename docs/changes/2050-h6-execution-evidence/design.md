# H6 execution-evidence projection design

## Boundary and predicate

This is a dashboard read-model rule, not a ledger-producer rule. For a feed
classification group, first gather all rows for the same cycle identity from the
available live and archived inputs, then classify the group. Do not classify a
partial page or one archive independently when other evidence for that identity
is present in the reader's available scope.

Let `S` mean at least one valid `phase=started` row; `T` mean at least one valid
terminal `phase=outcome` row; `P` mean at least one recognized proposer decision
row (`phase=proposed`, `phase=proposer_skip`, or `phase=proposer_reject`) with a
usable identity; `O` mean other recognized execution evidence; and `U` mean
missing, malformed, conflicting, or insufficiently covered input. Only these
explicit proposer-decision phases qualify as `P`; do not infer proposer-only
status from arbitrary ledger phases, telemetry rows, absent rows, or an `idle`
heartbeat (which has no cycle ID).

| Evidence | Execution row? | Execution row count? | Started-cycle denominator? | Treatment |
|---|---:|---:|---:|---|
| `S` (with or without `P`, `T`, or `O`) | yes | yes | yes | Preserve running/terminal classification; rest, no-plan, failure, or kill does not erase start. |
| `P` only, with complete available identity coverage and no `S`, `T`, or `O` | no | no | no | `proposed`, `proposer_skip`, and `proposer_reject` are examples; each is proposer activity, not execution start. Keep proposer activity visible separately. |
| `T` without `S` | yes, conservatively | yes | no | `UNKNOWN`; terminal evidence is not proof of a write-ahead start, but is enough not to discard as proposer-only. |
| `O` without `S` or `T` | yes, conservatively | yes | no | `UNKNOWN` unless existing semantics establish a more specific truthful classification. |
| `U`, including malformed rows or incomplete history coverage | retained/flagged where a row can be rendered | not silently treated as proposer-only or zero | no | Report coverage/classification as `UNKNOWN`; do not fabricate start or outcome. |

The discriminator is positive execution evidence, not absence of success or
presence of a proposer call. The `started` signal is the positive start for the
started-cycle denominator. Terminal-only legacy records remain visible for
history but are explicitly not asserted to be confirmed started cycles.

## Projection, counts, and history coverage

Apply classification after joining same-identity rows across all live/archive
inputs available to the dashboard reader, before pagination or row-count
calculation. Row count means rendered execution rows in the declared available
scope; started-cycle denominators mean rows with confirmed `started` evidence.
Do not label a bounded page count as a total-history count. If archive rotation,
retention, inaccessible files, pagination, or malformed input prevents complete
coverage, surface the coverage limitation and mark exhaustive totals unknown.
Do not turn missing records into zero.

Keep the proposer activity/call surface separate and unchanged. Do not remove
ledger writes, change event identity/schema, invent a terminal outcome, add a
collector/framework, or change host/runtime behavior. Proposer `llm_unavailable`
handling and fallback semantics under eeebot #2047 are separate work.

## Minimal future regression matrix

Use existing dashboard test fixtures and public feed/publisher-render path (no
new framework or synthetic host/runtime data):

1. Archived `proposed`-only group: absent from execution rows/count; proposer
   activity remains represented; not `running` or a fabricated terminal state.
2. `proposer_skip`-only group with a usable identity: absent from execution
   rows/count; skip remains proposer activity, not a started or successful run.
3. `proposer_reject`-only group with a usable identity: absent from execution
   rows/count; rejection remains proposer activity, not an execution failure.
4. Started active group: retained and `running`.
5. Started then rest/no-plan: retained as execution evidence, not success.
6. Started then failed/killed/terminal: retained with existing outcome or
   killed/incomplete classification.
7. Terminal without start: retained as `UNKNOWN`, excluded from confirmed-start
   denominator, not silently dropped.
8. Malformed/conflicting evidence or incomplete archive coverage: not classified
   as confirmed proposer-only; uncertainty/coverage is surfaced, not zeroed.
9. Matching identity rows split between live and archived inputs: join before
   classification and count once within the declared scope.

These are proposed tests, not existing coverage claims. Existing #390 regression
`test_390_archived_proposal_without_start_is_not_running_in_public_feed` verifies
a status label, not row/count exclusion; preserve its active and killed behavior.

## Evidence and authority

The source boundary is pinned to eeebot published main
`2bf78ba04c8978a1bef7f88bb0efe98dc06b4914`: `cycle_ledger.py` documents the
write-ahead `started` event; `llm_proposer.py` attaches proposer activity to a
cycle ID. A shared cycle ID is not itself proof of execution start. ADR-035 is
Proposed and is not accepted as authority for this design. The prior public
read-only dashboard audit pinned source at
`ozand/eeebot-ops-dashboard` master `6f022ef64fd8e2c6efd6e9f2ba4e297142611932`;
that snapshot grouped ledger rows into feed rows and counted rendered rows, and
#390 / PR #392 corrected status but did not establish exclusion/count behavior.

Dashboard repository authority remains UNKNOWN. This eeebot design receipt does
not authorize dashboard issues, comments, PRs, edits, tests, or publication. A
separate authorized dashboard task and independent review are required before
implementation or any claim of H6 completion. No ADR is created by this design
slice; if implementation proposes changing the producer/event contract or a
cross-reader public data contract beyond this projection, stop for architecture
review before proceeding.
