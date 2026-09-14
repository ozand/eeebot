---
title: Work is chosen by the journal and never by the channel, and the admissible metrics are about telling rather than about subject
status: proposed
date: 2026-09-15
authors: [eeebot maintainers]
related: ["#1596", "#1599", "#1455", "#879", "#1457"]
tags: [self-improvement, publishing, metrics, honesty]
---

# Status

Proposed — filed with ADR-015 and ADR-017. ADR-015 says what a published artifact may claim. This record says what a published artifact may **cause**.

# Context

A daily story wants a shape: something at stake, something attempted, something resolved. A day of the loop is 96 cycles of which a handful integrate anything. The gap between those two facts is a pressure, and it points somewhere specific.

The loop is self-improving, it selects its own work, and under the operator's decision it will now be able to observe whether people came back. If it ever notices that videos about struggle earn more returning viewers, and it is capable of arranging struggle, then arranging struggle becomes an available strategy. The dark pattern ADR-014 rule 4 forbids on the surface reappears here in a worse form: not manipulating the viewer, but manipulating the *work* so the viewer has something to watch.

This project already refused this once, in a smaller case. The standing lesson is **never manufacture the event** — a detector is not tested by forcing the failure it detects, because the forced failure poisons the store every later measurement reads. A manufactured setback filmed for a channel is the same move with an audience and a reward attached.

There is a second, duller reason to hold the line. Channel numbers are noisy far beyond the effects anyone would try to read from them. #1455 already recorded that the experiment ledger has no valid design because the baseline drifts 16.11 pp/day against a 5–10 pp effect. A new channel publishing once a day is n=1 per day against an opaque recommendation algorithm. Wired into fitness, that is a random number generator with a hill-climber attached.

None of this is an argument against the operator's actual goal, which is sound: *optimise the process so the videos are substantial rather than empty.* That goal is reachable without a single number from YouTube.

# Decision

**The video reads the journal. The journal never reads the channel.** Four rules.

## 1. The barrier is one-way and total

No channel figure — views, watch time, subscribers, returning viewers, impressions, click-through — enters `_TARGETS`, fitness, `goal_review` evidence, tech-tree levers, demand collection, or any prompt that selects work. There is no path, not a weighted one and not an advisory one.

The publishing side reads the journal freely. That direction is safe: the journal is a record of what happened, and reading it cannot change what happened.

## 2. Channel figures are reported numbers, with their noise floor stated

They are recorded, displayed, and never optimised — the ADR-011 rule 3 shape, which exists precisely for a number worth watching and not worth targeting. Every channel figure is published alongside the variation of its own recent history, and a movement that cannot be distinguished from that variation is reported as indistinguishable rather than as a change.

## 3. The admissible optimisation targets are about telling and about cost, never about subject

Three, all local, all available on day one, none dependent on anyone watching:

- **Citation density** — distinct journal-backed beats per minute of finished video. Three beats in five minutes is padding; the only way to raise it is to have done more that is worth citing. It cannot be gamed by drama, because a beat without a row does not count (ADR-015 rule 2).
- **Cost per finished minute** — CPU-seconds, peak RSS, and degrees, per minute of published video. This is the charter's before/after requirement made routine, and it is the lever the operator actually named: the toolchain getting better is the achievement. The first reading is 15.5 minutes of wall time per 5 minutes of video, 69% of it in composition rather than in the encoder.
- **Deadline hit rate** — the share of scheduled publications met without truncation (ADR-017 rule 4).

## 4. The loop may not choose or shape work in order to film it

Task selection is blind to what would make a good video, and no cycle may be started, prolonged, abandoned, or made to fail for the sake of an artifact. Rule 1 makes the incentive unavailable; this rule makes the intent forbidden, because rule 1 protects against the metric and this one protects against a prompt.

The corollary is the acceptance the operator has to grant in advance: **a quiet day yields a short video.** Not a padded one, not a dramatised one, not a skipped one.

# Consequences

## What gets easier

The loop can be pointed at the channel's craft with full force and no hazard. Better tiles, cheaper composition, denser scenes, a faster encoder, a richer glossary — every one of those raises citation density or lowers cost per minute, and none of them touches what the machine chooses to do. That is the tech ladder the operator described, running on a real product instead of on a metric.

Composition depth gets its first natural pressure. Today it is about 1 across 154 artifacts — everything a leaf. A video assembled from reusable explanation scenes is an artifact that is only cheap if earlier artifacts are reused, so the ladder pays for itself in the medium where it is visible.

## What gets harder

Someone will eventually observe that a particular kind of day performs better, and rule 1 forbids acting on it. That is the cost, it is deliberate, and it should be paid without renegotiation. A record that names the temptation before the data arrives is much cheaper than one written after.

Citation density is a proxy, and like every proxy it can be pushed toward the trivial — many small cited beats instead of one substantial one. It is reported next to the count of distinct cycles cited, so the shape stays visible.

## What does not change

`_TARGETS` and the scorecard. `goal_review` and its evidence sources. The tech tree as a ranking input and never a scheduler or a gate (#879), and the #1457 boundary. `MUTATION_POLICY` and the commit surface: publishing is not a commit, and the publishable artifact enters the repository by the ordinary route.

# Alternatives considered

- **Let subscribers enter fitness with a small weight.** Rejected. A small weight on a noisy signal is still a hill to climb, and #1455 already documents that this system cannot resolve effects well above the noise these numbers carry. It also re-opens rule 4 through the back door.

- **Allow channel metrics as a tie-breaker only.** Rejected. A tie-breaker is a path, and the argument for widening it arrives the first time it is decisive. ADR-011 rule 2's prohibition on moving a threshold to manufacture demand applies to opening one as well.

- **Judge video quality by watch time instead of citation density.** Rejected. Watch time is the metric whose optimisation produces the dark-pattern local maximum ADR-014 rule 4 already forbids, and it is unavailable on day one, unavailable for a small channel, and outside the loop's control.

- **Skip publishing on a quiet day.** Rejected. A skip is a silent degradation, and this project prefers a visible one — the same reasoning as ADR-014 rule 3. A two-minute honest video says something true about the day; an absence says nothing and hides the same fact.

- **Let a human choose what gets filmed.** Rejected as the standing arrangement, though it remains available as an override. Operator curation would break the property that makes the channel interesting — that the machine is accounting for itself — and it moves the honesty burden onto a person who cannot audit 96 cycles a day.

# Test Contract

- A test asserts no channel figure is reachable from `_TARGETS`, fitness, `goal_review` evidence, tech-tree levers or demand collection: the publishing state file has no reader on the work-selection side of the call graph #1599 computes.
- A fixture in which channel figures are absent, stale or zero produces byte-identical work selection to one where they are present and large.
- Citation density and cost per finished minute are computed from the artifact and the journal alone, with no network call; a test drives both offline.
- A channel figure whose movement is inside its own recent variation renders as indistinguishable, not as a change; a fixture drives a flat-with-noise series.
- A fixture day with one journal event produces a short video and a recorded deadline-met result, never a skip and never a padded artifact.

# References

- ADR-015 — what a published artifact may claim.
- ADR-017 — how the work that produces it is run.
- ADR-011 — a reported number is not a target; thresholds are not moved to manufacture demand.
- ADR-014 — attention is invited rather than captured; returns rather than glances.
- #1455 — the noise floor that disqualifies these numbers as targets.
- #1599 — the call graph the barrier test is asserted on, and composition depth.
- #879 / #1457 — the ranking-input constraint and the boundary none of this crosses.
- #1596 — how the charter reaches demand when every target is satisfied; the channel is not a shortcut around it.
