# Proposer fallback on explicit authorized routes

## Problem

A proposer restricted to one model route cannot generate a candidate while that route is in supplier cooldown. The proposer is optional and its failure does not establish that the planner or executor stopped.

## Proposed change

Consider ADR-054's default-disabled, same-gateway, at-most-one alternate for affirmative supplier cooldown. Preserve request/auth failure classification, privacy, input equivalence and finite time/cost bounds. No route is selected by this proposal.

## Acceptance

Independent design review and effective ADR acceptance precede implementation. Focused synthetic tests cover eligibility, no silent fallback, attempt attribution/privacy, failure and bounds. Alternate route authorization, budget equivalence and host activation remain separate owner decisions. Existing #2047 item 2 is not reimplemented; broader role fallback is not claimed complete.
