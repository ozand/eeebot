---
title: Report the hypothesis loop's verdict yield, and separate unverdictable from undecided
status: accepted
date: 2026-09-12
authors: [eeebot maintainers]
related: ["#1510", "#1345", "#1346", "#1457", "#822", "#878", "#1328", "#1335"]
tags: [hypotheses, observability, runtime]
---

# Status

Accepted — filed with #1510, ahead of any implementation. Complements ADR-007, which governs claim identity; this record governs what the loop reports about its own yield.

# Context

The hypothesis loop is the RSI experiment stage built across #878, #999 and #879. Its design holds up under inspection:

```text
strategist (daily)       -> hypotheses/durable.json      DURABLE_MAX_ENTRIES = 20, claim-keyed (ADR-007)
demand._hypothesis_items -> limit=1                      one experiment at a time
demand priority          -> defect > goal-gap > hypothesis > decay
hypothesis_verdict       -> microbench improvement_pct >= 5.0 -> supported / refuted   (authoritative)
                            confirmed usage, CONFIRM_WINDOW_DAYS = 14
                            no measured signal                 -> inconclusive
supported_hypotheses     -> tech_tree.maybe_mint_node (SUPPORTED_TOP_N = 3), goal_review candidate
lifecycle_counts         -> scorecard + dashboard, "VISIBILITY ONLY, never fed into fitness/targets/gaps"
```

The verdict is derived by the harness from a measurement, not asserted by the agent that produced the work. `hypothesis_verdict`'s module docstring states the trust boundary directly: a forged or tampered sidecar "costs at worst a wasted retry or a rejected priority candidate — it can never fabricate an integration by itself." That is the property the surveyed self-improving systems most often lack.

What the subsystem does not do is report its own yield. `hypotheses.html` renders a **Goal Gap Futility Tracking** section — 89 categories with attempt budgets, plus "126 active" and "59 stale" — and no verdict distribution at all. The two quoted population figures cannot both describe `durable.json`, which is bounded at 20; `lifecycle.json` is the unbounded candidate store, and since #1346 it separately tracks `stale`, `orphaned` and `evaluated_last_pass`.

Two consequences follow, and neither is visible today. Hypotheses sit third in demand priority with `limit=1`, so the share of cycles that ever serve one is unknown. And `inconclusive` is a single bucket holding two different states: a row filed yesterday that has not had its window, and a row that cannot ever receive a verdict because verdict source 2 requires the cycle to have touched a `scripts/` artifact — a condition inherited from #822's sidecar allowlist that a hypothesis about prompt assembly, context budgeting or path policy will never satisfy.

This is the third instrument in sequence that omits the number answering its own question: Tier 2 reported an empty corpus against 38 skills on `origin/main`; `lessons.html` reports `v2 Entries: 0` against 41 v2 rows; this page reports attempts instead of verdicts.

# Decision

**The hypothesis loop reports its yield, and the report distinguishes states that today look identical.** Four reporting obligations, no change to any decision rule.

1. **Verdict distribution is a first-class output.** `supported` / `refuted` / `inconclusive` over a stated window, alongside the age of the `inconclusive` rows. `lifecycle_counts` already computes it and `scorecard._hypothesis_loop_snapshot` already carries it; the gap is display, not derivation.

2. **`durable` and `lifecycle` are reported as separate populations.** Each with its own total, with #1346's `stale`, `orphaned` and `evaluated_last_pass` as their own columns. A single word covering two stores with different bounds and different lifecycles is not a summary, it is an error surface.

3. **The selection rate is counted.** Cycles serving a `hypothesis` demand over total terminal cycles, on the same window as the other scorecard rates. Third priority with `limit=1` is currently an assumption; this makes it a decision with a number behind it.

4. **`inconclusive` splits into three independently reported buckets:** `inconclusive_within_window` for a qualifying completion record inside `CONFIRM_WINDOW_DAYS`, `inconclusive_aged` for one past that window without a measured signal, and `inconclusive_undatable` when the row cannot be aged. The undatable population additionally reports its reasons: no qualifying artifact was touched, or no matching completion record exists (with invalid timestamps remaining an explicit partial/unavailable case). Undatable is never folded into either age bucket. The undatable population — not the aged bucket — is the input to the decision whether the `scripts/` condition needs widening. Without this split, a structurally unverdictable hypothesis is indistinguishable from a fresh one for as long as the corpus lives.

This decision is reporting only. It does not change `classify_hypothesis_verdict`'s rules or thresholds, `append_hypotheses` identity (ADR-007), `DURABLE_MAX_ENTRIES`, `TOP_N`, `SUPPORTED_TOP_N`, `CONFIRM_WINDOW_DAYS`, demand priority order, or the `limit=1` experiment concurrency. Nothing it adds is read by fitness, targets or gaps.

# Consequences

## What gets easier

The subsystem becomes answerable. "Are hypotheses useful" resolves to a distribution rather than an impression, and the answer arrives before anyone proposes changing the verdict rules to improve it.

`inconclusive_undatable_no_qualifying_artifact` is the evidence needed to decide whether verdict source 2's qualifying-directory condition is too narrow. `inconclusive_aged` remains a separate, valid-but-unresolved population and may be zero until rows actually age. The decision currently has no data and therefore cannot be made responsibly; after this it has an explicit count and reason breakdown.

A low selection rate, if that is what the count shows, is a cheap finding with a cheap fix — priority order and `limit` are configuration. Today it would be invisible for as long as the defect stream stays busy.

## What gets harder

Two more populations and two more buckets to keep coherent. #1346 already showed that lifecycle rows can become fossils that no input evaluates; adding columns to a store with that property means the columns must state which rows they were computed over, or they will quietly describe a shrinking subset.

A published yield invites the reflex to improve the number rather than the loop. The `lifecycle_counts` visibility-only contract is the guard against that reflex, and this ADR does not weaken it.

## What does not change

Verdict derivation and its two sources, the 5.0% microbench threshold, the 14-day confirmation window, claim identity, the 20-entry durable bound, `TOP_N` and `SUPPORTED_TOP_N`, `tech_tree.maybe_mint_node`, the `goal_review` citable-candidate path, demand priority, and the exclusion of all of it from fitness.

# Alternatives considered

- **Feed hypothesis outcomes into fitness so the loop is accountable for its yield.** Rejected. `lifecycle_counts` excludes them deliberately, and that exclusion is the same boundary #1457 examines: a loop that can improve its own score by improving its own statistics has an incentive to read and then shape its evaluator. Accountability here comes from the number being visible to the operator, not from it being wired into the loop's objective.

- **Widen verdict source 2 beyond its qualifying artifact directories now.** Rejected as premature. The condition may well be too narrow, but #1328 is the precedent: every pre-LLM cooling key looked reasonable and would have blocked 27–40% of would-be successes. `inconclusive_undatable_no_qualifying_artifact` is the measurement that turns this from a plausible fix into a justified one; `inconclusive_aged` is a separate age outcome, not evidence that the directory condition itself is unreachable.

- **Retire `inconclusive` and force a binary verdict after the window.** Rejected. It would convert "we did not measure this" into "this did not work", which is precisely the fabrication the harness-derived verdict exists to prevent, and it would corrupt `supported_hypotheses` — the one path that reaches the tech tree.

- **Render verdicts and drop the attempt budgets.** Rejected as over-correction: the futility attempt counts serve `goal_gap_futility`, a different mechanism with its own readers. The defect is that verdicts are absent, not that attempts are present.

- **Fix the render only, in the dashboard repo.** Rejected as insufficient. Items 3 and 4 are new measurements that do not exist in any store yet; a display cannot render a number nobody computes.

# Test Contract

- A fixture containing both `durable` and `lifecycle` rows reports two totals that reconcile independently with `DURABLE_MAX_ENTRIES` and `lifecycle_counts`; neither figure absorbs the other.
- A lifecycle fixture with rows past `CONFIRM_WINDOW_DAYS`, rows within it, no qualifying artifact, and no completion link splits into `inconclusive_aged`, `inconclusive_within_window`, and `inconclusive_undatable`, with the undatable reason counters identifying each cause; a row with a measured verdict appears in none of the three.
- An `orphaned` row from #1346 is counted in the population it belongs to and is not silently included in `evaluated_last_pass`.
- The selection-rate counter over a ledger fixture with known `serves: hypothesis` rows returns the expected ratio, and returns a labelled unavailable rather than zero when the ledger is absent — per the #1173 reader contract.
- A test asserts `classify_hypothesis_verdict`'s thresholds and sources are unchanged by this work.
- No new key added here is read by any fitness, target or gap computation; the assertion is on the reader side, not the writer's intent.

# References

- #1510 — the implementing issue.
- ADR-007 — deterministic hypothesis claim identity; governs the writer, this record governs the report.
- #1345 — claim-keyed novelty: 20 durable entries were about four ideas.
- #1346 — lifecycle fossils: `stale`, `orphaned`, `evaluated_last_pass`.
- #1457 — the loop can read its own evaluator; the boundary this ADR declines to cross.
- #822 — the sidecar allowlist precedent for verdict source 2's qualifying artifact condition.
- #1328 — measure a suppression rule against outcomes before building it.
- #1335 — enhancement-shaped cycles on scripts nothing runs.
- #878 — RSI stage 4: hypothesis → experiment → report loop.
- `nanobot/runtime/hypothesis_verdict.py` — verdict sources, thresholds, trust boundary.
- `nanobot/runtime/hypothesis_backlog.py` — bounds, `lifecycle_counts`, `supported_hypotheses`.
- `nanobot/runtime/demand.py` — `_hypothesis_items`, priority order.
