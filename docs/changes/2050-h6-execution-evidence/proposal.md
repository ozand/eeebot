# H6: separate proposer activity from execution-cycle counts

**Status:** design proposal only; not an implementation or acceptance of #2050.
**Issue:** eeebot #2050, H6. The issue's acceptance criteria are unchanged.

## Problem

H6 says not to show proposer attempts as cycles. The ledger includes proposer
proposal/rejection evidence and bridge execution evidence, while dashboard #390 /
PR #392 only corrected the status label for a proposal-only row. A label that
says execution was not observed does not itself remove that row from execution
feed/history counts. Counting candidate-generation attempts as execution cycles
misstates execution activity, especially during proposer-model unavailability.

## Proposed behavior

In a future, separately authorized dashboard change, exclude a ledger group from
execution feed/history rows and their row counts only when available evidence
confirms it is proposer-only: proposer evidence exists and no positive execution
or terminal evidence exists. Retain proposer attempts and model-call activity in
their proposer-activity surface. Do not modify or suppress the runtime ledger
writer to achieve this projection.

Treat a write-ahead `phase=started` as positive execution-start evidence. Retain
such cycles in the execution feed regardless of rest/no-plan, failure, kill, or
terminal outcome; started is not equivalent to success. A legacy terminal
outcome without `started` is ambiguous: retain it as `UNKNOWN`, but do not count
it as confirmed-started in a started-cycle denominator. Missing, malformed, or
incomplete evidence is never proof of proposer-only activity; retain/flag it as
unknown rather than drop it or invent an execution result.

## Acceptance proposal

- Confirmed proposer-only groups are absent from execution rows and row counts,
  while proposer activity remains visible in its separate surface.
- Started groups remain execution rows across rest/no-plan and all outcomes.
- Terminal-without-start and malformed/unknown groups are retained and marked
  unknown; they are not reported as confirmed starts.
- Counts state their evidence scope and coverage; unavailable history is
  `UNKNOWN`, never zero or a claim of exhaustive history.
- No producer event, event identity/schema, host behavior, or #2047 fallback
  semantics change.

## Scope and gates

This artifact interprets the existing H6 wording; it does not rewrite #2050 AC,
claim H6 is met, accept ADR-035, or authorize implementation in another repo.
Dashboard repository authority is unknown. Any dashboard issue/PR, edit, test,
comment, or publication requires separately established authority and review.
The work here is only an eeebot-owned design handoff; host/runtime actions and
#2047's proposer fallback behavior are out of scope.
