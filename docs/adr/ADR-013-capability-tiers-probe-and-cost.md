---
title: A capability has a probe, a tier, and a measured cost on this host
status: proposed
date: 2026-09-14
authors: [eeebot maintainers]
related: ["#1605", "#1606", "#1607", "#1599", "#879", "#893", "#1557", "#1173", "#1208", "#1335", "#1457"]
tags: [self-improvement, capabilities, tech-tree, eeepc]
---

# Status

Proposed — filed with #1605, #1606 and #1607, ahead of implementation. Complements ADR-011 (how the charter reaches demand) and ADR-012 (what the charter may ask for); this record governs how a capability comes into existence and how one capability becomes the foundation for the next.

# Context

The loop's output is 154 `scripts/*.py` across 104 distinct commit scopes. Of those, **22 are reachable from no entry point at all** and 8 are named by a harness, CI or repo-root caller; the rest are reachable only through other scripts mentioning each other. Composition depth is approximately 1: artifacts are leaves. Over 2 288 non-merge commits since 2026-07-01, 197 of 266 recent `feat` subjects begin with "add" and 144 of 266 are detect / check / validate / assert / guard / report.

Nothing in the system rewards, records, or even notices building on top of what was already built. The integration counter treats the 154th leaf exactly like the first foundation.

`nanobot/runtime/tech_tree.py` looks like the place this would live, and partly is. It is a Civ-style tree over improvement *directions*: five seed nodes, each naming a scorecard metric as a lever, with marginal gain paced by integration progress (#893), plateau at `PLATEAU_FLOOR = 0.0` over `GAIN_HISTORY_MAX = 8`, a 72-hour cooldown, and `EPSILON = 0.15` exploration. What it cannot express is **"not yet possible"**. A node does not unlock anything — it says "push metric X while it still moves" — so there are no prerequisite edges, no unlocks, and no frontier. Its levers are also `_TARGETS` entries, all of which are currently satisfied, so nodes plateau, rotate, and nothing new enters.

Two charter clauses are dead in exactly the same shape. Vector 1 asks for optimisation in Rust, C or C++ with proven measurement: **0 native files in 2 288 commits**, and no one has established whether a compiler exists on the host. Vector 2 asks for pixel-art terminal output: **0 artifacts**, and no one has established whether a drawable surface is reachable. Neither clause failed for lack of intent. Both failed because a capability was named without a way to ask whether the instance has it, without a place in an order, and without a cost.

Meanwhile the pattern that would answer all three questions already exists and is running. `scripts/eeebot_dashboard.py` probes camera and bluetooth in a four-state vocabulary — `present` / `absent` / `present_uninitialized` / `probe_unavailable` — and #1557 put it on a daily schedule. `present_uninitialized` is what makes it correct: an adapter that exists but is rfkill-blocked is neither present nor absent, and collapsing it either way would be a lie.

# Decision

**A capability is something the instance can be asked about, is placed in an order, and knows what it costs here.** Four rules.

## 1. A capability has a probe

Every capability answers a probe in the four-state vocabulary already used for hardware. A named capability with no probe is an intention, not a capability, and the charter's two dead clauses are what intentions look like after two months.

A probe returning `probe_unavailable` is a third answer, never folded into `absent`. This extends the #1173 reader contract from state files to capabilities.

## 2. A tier-N artifact is built only from tier-(N−1) artifacts that probe present

This is the only mechanism by which "simple before complex" is an enforced order rather than a hope. It is checkable from the call graph — the same graph #1599 computes for reachability — so it is a property of the artifact, not a claim in its commit message.

Tiers are: **0** external readings (world, own body, screen, toolchain); **1** primitives built on them; **2** composed answers built only from primitives; **3** something delivered to a human.

## 3. Every artifact declares its measured cost on this host

Microseconds and peak RSS, measured on the eeePC, not asserted. An artifact whose cost is unmeasured is not finished.

This is the charter's strictest sentence — every optimisation claim comes with a before/after measurement — made cheap enough to be routine. It is exercised 20 times in 2 288 commits today (0.9%) because there is nothing cheap to measure; a body probe (#1605) makes the numerator free.

## 4. A prerequisite affects preference, never permission

An unmet prerequisite makes a node ineligible for *selection* in the tech tree. It never blocks a cycle, never fails a gate, and never enters fitness. This is the #879 constraint — the tree is a ranking input to the existing demand and goal-review pipeline, never a scheduler and never a gate — and the #1457 boundary, and this record strengthens neither.

Blocked nodes are **reported with the unmet prerequisite named**. That list is the frontier: what is not yet possible and what would open it. A hidden blocked node is indistinguishable from a node nobody thought of.

# Consequences

## What gets easier

"Add another checker" becomes visibly flat. With tiers and a call graph, a 155th leaf reads as a leaf, and the question "is the loop building or tidying" has an answer instead of an impression.

The two dead charter clauses get resolved either way. A toolchain probe that answers `absent` closes the native-code clause honestly — which is a better outcome than leaving it open as decoration. The same applies to the drawable surface.

Cost measurement stops being exceptional. When every artifact carries µs and RSS, the before/after requirement is satisfied by construction rather than by discipline, and cost-per-delivered-thing becomes computable — the physical replacement for `tokens_per_integration`, and the one scale on which a 2008 netbook can be genuinely good.

The frontier is the first artifact this project has that describes what it *cannot* do. Every existing surface describes what happened.

## What gets harder

Rule 3 makes every artifact more expensive to finish, and rule 2 refuses work whose foundation is missing — which will feel like the loop got slower. That is the intended trade: 154 leaves at depth 1 is what the alternative produced.

Prerequisite graphs acquire their own failure modes — cycles, stale probes, a prerequisite whose probe breaks and silently blocks a subtree. The mitigation is rule 1's third state plus reporting: a subtree blocked by `probe_unavailable` must read as unavailable, not as ineligible.

Rule 2 is enforceable only as well as the call graph is accurate. #1599 already records that a name-mention pass is a lower bound and #1208 records the cost of trusting a weak one: a previous dead-script list held 5 files that were live via units, held-out contracts, SKILL.md references and sibling scripts.

## What does not change

`record_gains` and its integration pacing (#893), `PLATEAU_FLOOR`, `GAIN_HISTORY_MAX`, `EPSILON`, `COOLDOWN_HOURS`, `MAX_SWITCHES`, hypothesis-driven minting (#878), and the #879 prohibition on this module becoming a scheduler or a gate. `MUTATION_POLICY` and the commit surface. The gate. Nothing here is read by fitness, targets or gaps.

# Alternatives considered

- **Add the missing capabilities to the charter and let the loop find its way.** Rejected — that is precisely what produced 0 native files and 0 pixel-art artifacts. Both clauses are already in the charter. The missing part was never the instruction.

- **Build a scheduler that sequences tiers directly.** Rejected under #879, which says in the module's own words: 2GB-simple, no heavy bandit machinery, no MAP-Elites, no new scheduler. A ranking input that can also say "ineligible" is the smallest change that expresses order; a scheduler is a different system with a different blast radius.

- **Measure composition by declaration — let each artifact state its tier.** Rejected. A self-declared tier is an agent assertion, and this project's integrity rests on harness-derived facts. The call graph is the harness's own answer.

- **Retire the 22 unreachable scripts first, then start tiering.** Rejected as ordering. #1208 is the precedent for why a dead list needs evidence before deletion, and #1599 produces that evidence. Tiering does not require the cleanup, and the cleanup is a decision this record does not make.

- **Make composition depth a `_TARGETS` entry immediately.** Rejected as premature under ADR-011 rule 3: a rule that creates demand gets replayed against recorded history first. Depth becomes a reported number (#1599) before it becomes a goal.

# Test Contract

- A capability with no probe cannot be registered; a test asserts the registration path rejects it.
- A probe returning `probe_unavailable` makes its node ineligible **and reports as unavailable**; a test asserts it never reads as `absent` and never reads as met.
- A tier-N artifact whose call graph reaches only tier-(N−1)-and-below passes; one that reaches a same-tier or higher artifact fails, with the offending edge named.
- A prerequisite cycle is detected and reported rather than producing an empty eligible set.
- A test asserts no call path from prerequisite evaluation reaches the gate, the terminal demand decision, or fitness.
- `record_gains`, plateau, cooldown, epsilon and mint behaviour are pinned unchanged.
- Every probe path added under this decision is verified against the host before its reader is written — see below.

# The failure mode this decision must not repeat

#1557, four days before this record: mutation proofs for the bluetooth probe passed against a sysfs attribute that does not exist on this host, because a fixture cannot refute sysfs. Probes are the one place in this codebase where a green suite is not evidence. Every path is read on the host first, and what was found there is recorded alongside the reader.

# References

- #1605 — probes for body, screen and toolchain; rule 1 and the cost numerator for rule 3.
- #1606 — probes and prerequisites on tech-tree nodes; rules 2 and 4.
- #1607 — the first vertical slice through the tiers.
- ADR-011 / ADR-012 — how the charter reaches demand, and what it may ask for.
- ADR-014 — the delivered surface as an instrument; the tier-3 obligation.
- #879 — tech tree as a ranking input, never a scheduler or a gate.
- #893 — gain observations paced by integration progress.
- #1557 — the four-state hardware probe, and the fixture-cannot-refute-sysfs lesson.
- #1173 — empty is not unavailable.
- #1208 / #1335 / #1599 — dead-script evidence, enhancement-shaped cycles, and the call-graph measurement.
- #1457 — the boundary none of this crosses.
- `nanobot/runtime/tech_tree.py`, `scripts/eeebot_dashboard.py`.
