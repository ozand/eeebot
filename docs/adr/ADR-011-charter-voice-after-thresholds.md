---
title: The charter keeps a voice after every threshold is met
status: proposed
date: 2026-09-14
authors: [eeebot maintainers]
related: ["#1596", "#1597", "#1600", "#879", "#878", "#765", "#761", "#1328", "#1455", "#1457", "#1510"]
tags: [self-improvement, demand, goals, runtime]
---

# Status

Proposed — filed with #1596, #1597 and #1600, ahead of any implementation. Governs how the operator's charter reaches demand; ADR-012 governs what the charter may ask for.

# Context

The loop selects one task per cycle from a priority-ordered demand queue. Measured over the rendered cycle history (2 423 of 2 709 rows parsed, 2026-07-13 → 2026-09-14), the source of that task has inverted:

```text
                    2026-07   2026-08   2026-09
goal-gap              24.0%     44.8%      0.5%
decay                 16.0%     10.0%      0.7%
reflection + defect    3.4%     37.5%     92.2%
hypothesis             0.0%      2.0%      2.7%
```

In September 92% of cycles take their task from the loop's own previous output. The channel that carries the operator's charter — `goal-gap` — is effectively silent.

Nothing failed. The live scorecard is HEALTHY: `repeat_failure_rate` 0.298 against a 0.35 ceiling, held-out 4/4, five feeds fresh. The mechanism behind the silence is explicit in the code:

```text
nanobot/runtime/goal_review.py:402  _collect_evidence() draws from three sources
                                      scorecard "gaps"          (#765)  — needs a breached threshold
                                      usage/decay stale         (#761)  — needs a stale artifact
                                      supported hypotheses      (#878)  — 43 served cycles in 2 423

nanobot/runtime/goal_review.py:747  if not evidence:
                                        _record_review(state_dir, "no_gaps")
                                        return []

nanobot/runtime/goal_review.py:766  #879 Direction — a stable reorder of the LLM's
                                    candidate list, applied AFTER the return above
```

Every evidence source is breach-driven or near-empty in the satisfied regime, so `goal_review` no-ops. The tech-tree **Direction** — the loop's declared investment aim, live today as lever `loop.confirmed_integration_ratio`, aim higher — is a *sorter*, not a *source*: when the list it sorts is empty, the operator's stated aim reaches nothing.

`_TARGETS` cannot rescue this on its own. Six of its seven entries are ceilings or floors that fall silent once met. The seventh, `tokens_per_integration`, is typed `"direction": "trend"` and `_trend_gap` fires only when the value worsens past `_TREND_WORSEN_FACTOR × mean(prior window)` — a regression alarm, not an improvement pressure. A metric flat at 1.5M tokens per integration produces no gap, ever.

The result is a steering vocabulary consisting entirely of *"do not get worse"*, and a loop that, once it is not getting worse, is driven only by its own echo.

# Decision

**The charter's aim is an evidence source in its own right, not a tiebreak over evidence that a breach produced.** Three rules.

## 1. A declared aim is citable evidence

When a Direction is set and no breach-driven evidence exists, the Direction's lever and aim enter `_collect_evidence` as a fourth source, in the same shape as the other three. A candidate raised from it is cited, validated, deduped and capped by exactly the existing machinery — this is the #878 integration point, not a parallel mint path. A Direction-sourced priority carries its provenance so it is distinguishable in the ledger from a gap-sourced one.

With no Direction set and no other evidence, behaviour is byte-identical to today: `no_gaps`, zero LLM calls.

## 2. A threshold is never moved to manufacture demand

Raising a ceiling so that a satisfied metric reads as breached produces demand that is an artefact of the instrument. It also corrupts every rate computed over the window, and it destroys the ceiling's actual job, which is to catch regression. The absence of a gap is correct information; the defect is that correct information leaves the charter mute, and that is fixed at the source, not at the threshold.

## 3. A rule that creates demand is replayed before it is built

Any new gap-producing rule — a ratchet direction being the live candidate — is first replayed against recorded history and reported on: how many cycles it would have fired on, its longest consecutive run, the demand share it would have claimed, and whether the metric it ratchets is gameable by the loop's own output shape. #1328 established this for a rule that *suppressed* demand, where every plausible cooling key would have blocked 27–40% of would-be successes. A rule that *creates* demand carries the same burden in the other direction: the failure mode is a treadmill rather than a blockade, and it is equally invisible without the replay.

This decision does not change `_TARGETS`, any threshold, `_trend_gap`, `validate_priority`, `_MAX_PRIORITIES`, demand priority order, `limit`, or the #879 reorder.

# Consequences

## What gets easier

The loop stops being silently unsteerable. Today an operator who sets a Direction has no way to tell whether it did anything, because its only effect is a reorder that may have had nothing to order. After this, a Direction either produces a citable candidate or the ledger says why it did not.

The charter and the thresholds separate cleanly. Thresholds go back to being what they are good at — catching regression — and stop being asked to double as the expression of intent, which is a job a bound cannot do once it is met.

Rule 3 makes the ratchet question answerable cheaply. The replay is a report over `scorecard/history.jsonl`; it either supports the rule or closes the idea, and either outcome is worth more than the argument.

## What gets harder

A fourth evidence source means `goal_review` can now mint in a regime where it previously never ran, so its rejection reasons and dedup become load-bearing in a case they have never been exercised in. The mitigation is that the source is the only new thing: everything downstream of `_collect_evidence` is unchanged and already tested.

A Direction that is set and left stale becomes a standing demand generator pointed at an aim nobody re-examined. The offsetting discipline is that the Direction is operator-owned and visible on the front page; a stale one is a visible stale one, which is not the case today.

## What does not change

`_TARGETS` and every threshold in it. `_trend_gap`. The three existing evidence sources and their semantics, including the #761 decay epoch and protect-list. `validate_priority`, the `_MAX_PRIORITIES` cap, and the #860 canon split between `goal_text.json` and `derived_priorities.json`. Demand priority order and `limit=1`. The #1457 boundary: none of this is read by fitness, targets or gaps.

# Alternatives considered

- **Raise `repeat_failure_rate`'s ceiling (or any other) so a gap appears.** Rejected under rule 2. It manufactures the observation it wants to act on, and it disables the regression detector that the ceiling exists to be.

- **Let Direction bypass `goal_review` and emit demand items directly.** Rejected. It would create a second path into the queue with none of `validate_priority`'s duplicate, scope and citation checks, which is precisely the machinery that keeps derived priorities from degenerating. #878 took the narrow integration point for the same reason and it held.

- **Ship a ratchet target now instead.** Rejected as ordering, not as substance — see rule 3 and #1597. A ratchet may well be right; a ratchet that fires every cycle is a treadmill, and only the replay separates the two.

- **Accept the self-referential regime as healthy.** Rejected, but not dismissed: a loop repairing its own defects is doing legitimate work, and 29% of cycles being deliberately skipped as duplicates or recent failures shows the brakes work. The objection is narrower — a system whose entire steering is "do not get worse" has no expression for "get better", and cannot be pointed anywhere by its operator once the gauges are green.

- **Raise the exploration rate instead, so hypotheses fill the gap.** Rejected here and deferred to #1600, which is parked on #1455: raising exploration before experiment results are readable buys unmeasurable experiments and fills a corpus ADR-009 exists to keep legible.

# Test Contract

- With every `_TARGETS` entry satisfied and a Direction set, `goal_review` produces at least one citable candidate; with the Direction absent and all else equal, it records `no_gaps` and makes zero LLM calls — byte-identical to the current path.
- A Direction-sourced accepted priority is distinguishable from a gap-sourced one in `derived_priorities.json` and in the `phase: "goal_review"` ledger row.
- A Direction-sourced candidate that fails `validate_priority` is rejected with a reason recorded, exactly like a gap-sourced one; no new acceptance path exists.
- A test asserts `_TARGETS`, every threshold, and `_trend_gap` are unchanged by this work.
- The ratchet replay (#1597) produces its report without adding any rule to `_TARGETS`; a test asserts the replay is read-only with respect to `state/`.
- No key added by any of this is read by a fitness, target or gap computation; the assertion is on the reader side.

# References

- #1596 — the implementing issue for rules 1 and 2.
- #1597 — the ratchet replay required by rule 3.
- #1600 — exploration budget, parked on #1455; the other lever on the same problem.
- ADR-012 — what the charter may ask for; this record governs how it is heard.
- #879 — tech-tree Direction as a soft reorder of candidates.
- #878 — supported hypotheses as an evidence source; the integration-point precedent.
- #765 / #761 — the gap and decay evidence sources.
- #1328 — replay a demand-shaping rule against recorded outcomes before building it.
- #1455 — the experiment ledger has no valid design under the measured baseline drift.
- #1457 — the loop can read its own evaluator; the boundary none of this crosses.
- `nanobot/runtime/goal_review.py` — `_collect_evidence`, the `no_gaps` return, the #879 reorder.
- `nanobot/runtime/scorecard.py` — `_TARGETS`, `_trend_gap`.
