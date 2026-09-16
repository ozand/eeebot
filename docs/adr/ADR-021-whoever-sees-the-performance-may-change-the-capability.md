---
title: Whoever sees the performance may change the capability, and only after measuring whether it was used
status: proposed
date: 2026-09-16
authors: [eeebot maintainers]
related: ["#1505", "#1369", "#1595", "#1563", "#1314", "#1007"]
tags: [self-improvement, architecture, knowledge, honesty]
---

# Status

Proposed, alongside ADR-020. That record concerns *direction*; this one concerns *capability*. They are the same asymmetry in two places.

# Context

A coach watches performance and changes what the athlete can do. This system has the watcher and it has the change, attached to different components.

What actually runs, verified on the host:

```text
reflector    gemini-3.8-flash-high, every 30 min, 412 calls
             reads executor TRANSCRIPTS -- the turns, not only the outcome (#1007)
             drops whole turns, oldest first, when a record overruns (#1314);
             the recorder caps one record at 32 KiB
             writes -> a journal

curator      gemini-3.8-flash-high, nightly at 00:00, 37 calls
             reads days of reflection journals
             writes -> lessons/lessons.yaml

executor     local qwen3.8-27b, one bounded cycle, fresh process
             writes -> scripts, skills, tests, docs, memory, lessons
```

The observing half is built and working: 68 lesson files and 24 memory facts on `origin/main`, five lessons added on 2026-09-15, with retirement happening rather than pure growth. The reflector is a *transcript* reflector — it sees how a task was executed, which is exactly a coach's material.

**And it can only write text.**

| | sees | may change |
|---|---|---|
| reflector | the executor's turns | a journal |
| curator | days of reflections | lessons |
| executor | its own single cycle | skills, scripts, tests, everything committable |

All 32 skills in the instance were authored by `eeepc-agent` — the executor, from inside one cycle. That is a student writing the textbook from the impression of one lesson, while the observer who watched a hundred lessons is allowed a note in the margin.

A lesson is text in context. It cannot create a skill, repair one that misfires, or retire one that fires at the wrong moment.

**The gap that makes any authority here premature.** Nobody knows whether any of it is used. #1505 records that the citation store has never been written, so lesson usefulness is unmeasured — and the same is true of the 32 skills. A coach without feedback is not coaching; they are guessing with confidence.

**And the corpus only grows.** The skills catalogue has a budget (`_bound_skills_catalogue`, #1563) and automatic trimming has already damaged it once: #1369 blanked the trigger descriptions of eight skills and #1595 restored them by hand.

# Decision

**Capability changes belong to whoever sees the performance — after the usefulness of what exists has been measured, never before.** Five rules, and their order is part of the decision.

## 1. Measurement precedes authority

Until retrieval is recorded — which skills and lessons were actually pulled into a cycle, and whether pulling them correlates with the outcome — the trainer changes nothing. This is #1505's machinery, and it is the prerequisite rather than a nice-to-have.

The order matters more than any single rule here. Granting authority first produces a component confidently restructuring what it never measured, on a fast-tier model, from a partial record.

## 2. The trainer proposes; the gate holds

Skill changes from the night contour enter as proposals through the existing mutation surface and gate. No direct commit. This is ADR-018's boundary in a second place: the component with the wider view does not thereby get the wider hand.

**The two-author conflict story (#1666 phase 2).** Lesson mutations already have two authors: the executor writes lesson entries from inside its own cycle, through the full mutation gate and its own commit; the trainer (reflector → curator) proposes lesson cards from across many cycles, through the staging path (`_stage_lesson_cards` → `_pickup_staged_promotions` → `apply_staged_lesson_cards`). One rule resolves a same-id collision between them: **whichever author's id reaches the checkout first keeps it.** `_merge_card_into`'s exact-id branch already refused a second writer for the same id silently; the gate (`apply_staged_lesson_cards`) now also distinguishes a genuine conflict — the checkout already carries a *different* `problem`/`solution` under that id — from a harmless idempotent retry of an already-applied card, and records the former (`decisions.jsonl`, `mint_declined` / `trainer_proposal_declined_id_conflict`) instead of leaving it to read as an ordinary no-op. Near-duplicate folding (a different id, absorbed by content similarity — #1106) is unaffected: that is existing, tested curator behavior, not the executor/trainer collision this rule targets. Tested once, in `tests/test_trainer_capability_gate.py`.

## 3. Retirement is part of training

A trainer that may only add is a commentator. The authority granted under rule 1 explicitly includes removing a skill or lesson that measurement shows is never retrieved, or is retrieved and does not help.

Removal is where the damage lives, so it carries the same evidence burden as addition and is bounded per run — #1369 is the precedent for an automatic trim that quietly cost eight skills their triggers.

## 4. "This helped" is a claim and needs a row

A model's impression that a skill was useful is not evidence. A proposal to add, change or retire cites retrieval counts and outcomes, exactly as ADR-015 requires of a narrated claim and ADR-019 of an optimisation.

## 5. The trainer states what it did not see

The transcript the reflector reads is bounded, and on overrun whole turns are dropped oldest-first (#1314) under a 32 KiB per-record cap. A coach reviewing a truncated recording is systematically blind to the opening of the match.

Every reflection records how much of the transcript it saw. A reflection over a truncated record says so, and a proposal resting on one is marked as resting on a partial view. Either measure the truncation rate or admit it — silently reasoning over an abridged record is the failure this project keeps finding in other clothes.

# Consequences

## What gets easier

The corpus acquires a gardener. Today it is written by whoever happened to be in a cycle and pruned by nobody with evidence.

Capability improvement starts using the only view that contains it. Whether a skill fires at the right moment is visible in a transcript and invisible in a single cycle's context — which is why the executor, authoring its own skills, cannot judge them.

Rule 1 forces #1505, which has been open and unbuilt while the corpus it would measure grew to 68 lessons and 32 skills.

## What gets harder

Retrieval must be recorded per cycle, which is instrumentation the loop does not have and which costs writes on a slow machine.

Two components may now propose changes to skills — the executor from inside a cycle, the trainer from across many. That needs a conflict story, and the gate is where it lands.

A fast-tier cloud model gains a route, through the gate, into what the local executor is told it can do. That is a real trust boundary and rule 2 is the whole of the answer; it should be argued with rather than assumed.

## What does not change

`MUTATION_POLICY` and the commit surface. The gate. The executor's ability to author its own skills — this adds a second author, it does not remove the first. `_bound_skills_catalogue` and its budget. The charter. Nothing here touches direction, which is ADR-020's subject.

# Alternatives considered

- **Give the flash-tier agent "skills" of its own.** The operator's first instinct, and it does not map onto the machinery: skills are a catalogue injected into the *executor's* system prompt under a budget, while the reflector and curator have fixed prompts in code and one job each — the problem skills solve is choosing among many. The intuition underneath is right, though: the trainer lacks *instruments*, not prompts. That is rules 2 and 3.

- **Let the trainer commit skill changes directly.** Rejected. It is the wider view, not a wider mandate, and #1188 is the standing precedent for what happens when a component can protect its own edits.

- **Grant authority now and measure later.** Rejected, and this is the alternative most likely to be re-proposed, because measurement is slow and the corpus is visibly untended. It produces a trainer restructuring what it never measured — on a fast-tier model, from a truncated record. Rule 1 exists to refuse it.

- **Let the executor keep sole authorship and improve its prompt.** Rejected. The executor's view is one cycle by construction; no prompt gives it the cross-cycle evidence that says a skill misfires.

- **Cap the corpus and trim automatically by age.** Rejected. That is #1369 — an automatic trim that blanked eight skills' triggers and needed #1595 to undo. Retirement needs evidence of non-use, not a clock.

# Test Contract

- Retrieval is recorded per cycle for skills and lessons; a fixture asserts a cycle that pulled a skill records it, and one that did not records that too — absence must be distinguishable from an unwritten record.
- No trainer-originated skill or lesson change is accepted while the retrieval record is missing or empty; a fixture drives the empty case and asserts refusal.
- A trainer proposal without citations fails validation, and a proposal to retire carries the non-use evidence it rests on.
- No trainer path commits directly; asserted on the call graph, not by convention.
- Every reflection records the fraction of the transcript it saw, and a proposal built on a truncated reflection is marked as such; a fixture drives an overrun record.
- Retirement is bounded per run, and a run that hits the bound reports it rather than continuing.

# References

- #1007 — the per-cycle transcript reflector; the observing half that already exists.
- #1314 — transcripts drop whole turns oldest-first on overrun; the basis of rule 5.
- #1505 — the citation store has never been written; rule 1's prerequisite.
- #1563 — the skills catalogue budget.
- #1369 / #1595 — an automatic trim blanked eight skills' triggers, restored by hand.
- #1188 — the loop wrote the test protecting its own additions; why rule 2 holds.
- ADR-018 — the harness judges and the instance draws; the same boundary, one layer over.
- ADR-019 — a claim of improvement needs a named baseline; rule 4 is its sibling.
- ADR-020 — the same asymmetry applied to direction.
