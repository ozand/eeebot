---
title: Direction comes from reflection over a span, never from an instantaneous error signal
status: proposed
date: 2026-09-16
authors: [eeebot maintainers]
related: ["#1596", "#760", "#765", "#815", "#878", "#1600"]
tags: [self-improvement, architecture, demand, honesty]
---

# Status

Proposed. Found by seeding an operator priority into the live canon on 2026-09-15 and watching where it went.

# Context

Two priorities were seeded into `state/goals/goal_text.json` so the loop would build a framebuffer writer and a palette optimisation. One was completed. The other has never reached the front of the queue, and the reason is structural rather than accidental.

The demand queue, read from the production `_priority_items` path:

```text
1. Priority 43 — Filter fallback candidates dedup      (V1, self-minted)
2. Priority 45 — Assert doc integration structure      (V1, self-minted)
3. Priority 17 — Write one frame to the real display   (V2, operator)
```

`goal_review` mints its own priorities from scorecard gaps (#765), and every one of them is V1. `demand._priority_items` stable-sorts V1 before V2 under #815, whose own comment reads *"bias the primary vector, never starve the secondary one"*. Ten self-minted priorities exist; five were added in the last two days, against roughly one completion per cycle. While the mint rate matches or exceeds the completion rate, a V2 item can never reach the front. The ordering is arithmetic, not judgement.

**What the loop mints, and why.** The scorecard holds exactly one open gap:

```text
repeat_failure_rate = 0.3641   against a target of 0.35
```

That single number is the entire evidence base for direction. Every minted priority attacks it, and the only lever the loop can reach is its own proposal pipeline — so it writes filters and quarantines: *filter futile candidates*, *filter candidate paths*, *dedup candidates*, *quarantine repeat defects*, *quarantine repeat defect patterns*. Five of ten are filters, four are quarantines.

**The loop that makes this self-defeating.** `repeat_failure_rate` counts terminal cycles. Work minted to reduce it produces new cycles and new code, which enter the same population the metric measures. The actuator sits inside its own sensor, so the correction is not negative feedback — it is a pump. Supporting figures from the same snapshot: 225 integrations of which **95 are unconfirmed (42%)**, 54 fallback cycles, and hypotheses served 10 of 411 cycles (2.4%, consistent with #1600).

**Why this component exists at all**, because it should not simply be deleted. Before #760 the loop was supply-driven: every ten minutes a model was asked to invent a task over a value-poor workspace, and it invented — two or three LLM calls per cycle on proposals its own dedup then rejected. The demand collector was built as the engine half of that inversion: a deterministic, LLM-free scan yielding structured items the model may *select and refine from, never invent beyond*. That inversion was correct and still is.

`goal_review`'s minting is a **second generator** that puts invention back one layer up, writing into the operator's own charter format. The thing demand collection was built to prevent reappeared wearing the operator's clothes.

**The asymmetry underneath all of it.** Two contours observe this system. One sees an instant and holds direction; the other sees a span and holds nothing:

```text
goal_review            sees one scorecard gap        writes PRIORITIES   -> what to do
reflector + curator    sees the lived span           writes lessons      -> how to do it
```

Knowledge influences method and never direction. A component that sees only a gauge reading can only reach for what moves the gauge; it cannot want a screen. That is the whole explanation of the filter-and-quarantine backlog, and no reordering fixes it.

# Decision

**Direction is produced by reflection over a span, bounded by the same evidence discipline as every other claim.** Four rules.

## 1. The gap-driven mint stops

`goal_review` no longer mints priorities from a scorecard gap. A gap remains what it always was — a reported number and an input to ranking — and stops being a source of work.

This is what takes the actuator out of the sensor. It also removes the only path by which the loop writes into the operator's charter format, which was never the intent of #765.

## 2. The night contour proposes candidates, with citations

The reflector and curator already read what the demand generator cannot: executor transcripts and days of reflection. They gain one obligation — to name what the lived span shows is worth doing — in the form the rest of the system already requires: **a candidate cites the ledger rows, commits or measurements it rests on, or it is not a candidate.**

This is the #878 integration point generalised, not a new mint path: candidates are evidence entering the existing pipeline, and they pass `validate_priority` like anything else.

## 3. The operator charter outranks anything self-minted

Not because the operator is senior, but because the charter is **the only demand source that can see outside this machine**. Every other source — the ledger, usage telemetry, hypotheses — observes the loop observing itself. An architecture where those sort as peers cannot reach an external goal, and the queue above is what that looks like in practice.

**Addendum (#1708):** ranking is not selection. `llm_proposer._select_assigned_demand` (#902) ran a least-recently-served rotation that ignored this sort — a never-served `reflection-*` id (minted fresh most cycles) always beat the operator head, so rule 3 held in the ranking and not in the loop that acts on it. Fixed: an eligible item with `provenance == "operator"` is now selected outright, ahead of rotation, unless cooling/futility/exhaustion has excluded it — those guards, and #902's stall protection, are unchanged.

## 4. The deterministic sources stay, and the executor still selects

Defects, decay, and existing priorities remain exactly as they are: those are facts, not invention, and #760's inversion is preserved. The executor continues to **select and refine from a bounded candidate set** rather than deciding direction from its whole corpus.

# Consequences

## What gets easier

An external goal becomes reachable at all. Today no evidence source can see a frame reaching `/dev/fb0` or a video an anonymous viewer can play, so no amount of prompting produces demand for one.

Direction stops being produced by the component least equipped to produce it. A span carries what a snapshot cannot: what was attempted, what was abandoned, what was repeated.

The self-minted backlog drains. Ten open items against one gap is accumulation, not control.

## What gets harder

The night contour becomes load-bearing for direction, so its model choice stops being an implementation detail. It currently runs `gemini-3.8-flash-high` — a cloud model at the fast-and-cheap tier, not the local Qwen and not a larger-context model. That is a deliberate decision to make, not a default to inherit.

A direction chosen nightly persists for a day, where a gap-driven one turned over in fifteen minutes. #1197 is the standing warning about failures that stay invisible for hours; a wrong direction now has a longer half-life and needs its own visibility.

Candidates must cite, which means the night contour cannot propose from impression. That is the point, and it will produce fewer candidates than the mint does.

## What does not change

`_TARGETS`, the scorecard, and gap computation. `MUTATION_POLICY` and the commit surface. The gate. The tech tree as a ranking input, never a scheduler (#879), and the #1457 boundary. The demand collector's deterministic sources and the LLM-free scan. The charter itself.

# Alternatives considered

- **Reorder the queue so V2 sorts first, or tag the operator's items V1.** This is the patch actually applied on the day, to stop the screen waiting a week. Rejected as the decision: the mint rate is unbounded, so any ordering rule is overtaken by the next burst. It treats the queue, which is the symptom.

- **Cap the self-minted queue and keep minting.** Better, and still wrong on its own: a bounded pump is still a pump, and the actuator stays inside the sensor. Worth doing as a safety bound; not worth mistaking for the fix.

- **Delete the demand generator and let the executor choose from Tier 1 and Tier 2.** Rejected on three grounds. With no demand items `llm_proposer.should_propose` makes zero LLM calls and the cycle records an idle heartbeat, so deletion yields idling rather than freedom. Memory and skills are retrieval surfaces — a lesson says *how* to do something, never *what* to do next — so Tier 2 is not a demand source without a new mechanism. And asking a local 27B model in a bounded cycle to pick direction over the whole corpus is precisely the supply-driven failure #760 removed.

- **Exclude self-minted work from the metric it targets, and change nothing else.** Rejected as insufficient, though it is the sharpest single lever and is folded into rule 1 by removing the mint entirely. On its own it leaves direction with the component that sees an instant.

# Test Contract

- No priority is written by the gap path; a fixture with an open gap produces a reported number and no new priority entry.
- A night-contour candidate without at least one citation is rejected at validation; a fixture drives an uncited candidate.
- Operator charter priorities sort ahead of every self-derived priority regardless of vector; a fixture with both asserts the order.
- The deterministic demand sources produce byte-identical items before and after this change; a fixture pins them.
- With no candidates at all the cycle records an idle heartbeat and makes zero LLM proposal calls, exactly as today — a test pins that this change cannot turn silence into invention.
- The self-derived queue depth is reported, so accumulation is visible whether or not a cap is later added.

# References

- #1596 — when every target is satisfied the charter has no path into demand; this is the same wound seen from the other side.
- #760 — the demand-driven inversion, and why the generator exists.
- #765 — scorecard gaps as demand; the path this record closes.
- #815 — the V1-before-V2 bias whose comment promises not to starve V2, and does.
- #878 — harness-supported hypotheses as evidence; the integration point rule 2 generalises.
- #1600 — exploration is a leftover, not a budget: 2.4% of cycles.
- ADR-011 — the charter keeps a voice after every threshold is met.
- ADR-021 — the same asymmetry applied to capability rather than direction.
- `nanobot/runtime/demand.py`, `nanobot/runtime/goal_review.py`.
