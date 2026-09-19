---
title: An artifact is finished when something depends on its working — readiness by artifact kind, and how it reaches task selection
status: accepted
date: 2026-09-19
authors: [eeebot maintainers]
related: ["#1769", "#1766", "ADR-022", "ADR-023", "ADR-024", "ADR-012", "ADR-019"]
tags: [architecture, runtime, scorecard, demand, observability]
---

# Status

**Accepted, 2026-09-19.** Nothing in the tree satisfies this record yet. #1769 (the artifact dependency graph) is its implementation and is in progress; this record exists because #1769 must state the rung definition once, in the graph module, and a definition invented during implementation would be an accident rather than a decision.

This record **extends** ADR-024 and does not supersede it. ADR-024 answers *what is an edge*; this answers *what is finished, and how does that reach the choice of the next task*. Nothing in ADR-024 is corrected: its three edge kinds stand, and its `used_by` already admits a systemd unit or a run-instruction as production use. Per `docs/adr/README.md` an accepted ADR is immutable, so the extension is a new record.

# Context

ADR-024 defined the rung as *"an artifact is a component when at least one `used_by` edge points at it"*. Correct, and on this corpus it is not yet usable. Three problems surfaced when it met the data.

## The corpus is not a library. It is 176 entrypoints.

Measured on `ozand/eeebot-self-evolving@main`, 2026-09-19:

| | count |
|---|---|
| scripts | 180 |
| …with a `__main__` block | **176** |
| …library-style, no `__main__` | 4 |
| artifacts with a real production consumer | **8** |
| leaves | 114 |
| …of the leaves, covered by a test and run by nothing | **90** |
| …of the leaves, carrying `__main__` | **113 of 114** |

Asking "who imports you?" of a corpus that is 98% entrypoints is a category error at scale, not an edge case. An entrypoint is *designed* to have no importer. Its readiness question is not who imports it but **who invokes it, and when**.

This reframes the finding entirely. 114 leaves is not 114 half-built components. It is **114 finished tools that were never plugged in** — they have a `__main__`, they have tests, and nothing has ever run them. The system does not have a construction problem. It has a wiring problem.

## `tested_by` does not discriminate

90 of the 114 leaves are tested. A signal present in 79% of the failing population carries almost no information about readiness. A tested leaf and an untested leaf are equally unused.

## Reference resolution is far worse than "some prose mentions"

362 distinct `scripts/*.py` paths are named somewhere in the repository, and **186 of them name a file that does not exist** — more than half. (Counted with a plain path grep over `scripts/`, `tests/`, `skills/`, `surfaces/` and `docs/`, so it includes prose; that is exactly the point. Documents name scripts that were proposed, renamed, retired, or never built.) A resolver that silently drops these loses 186 references; one that counts them loses nothing and invents 186 edges. Neither is acceptable, which is why ADR-024 clause 5 exists — this is the size of what it governs.

## The ladder's top rung has no in-repo consumer at all

The charter's ladder is raw → components → products → audience. Its last rung is a terminal artifact: a frame on the screen, a published video. By construction nothing inside the repository consumes it, so "leaf = dead" classifies the charter's goal as dead work.

# Decision

## 1. Readiness is defined by artifact kind, and kind is a first-class attribute

The rung, stated once, in one sentence that every kind specialises:

> **An artifact is finished when something else depends on its working.**

Not when it works. Not when its tests pass. When something breaks if it stops.

| kind | depends-on relation | `used_by` evidence |
|---|---|---|
| library module | another artifact imports it | an import on a path production runs |
| **entrypoint** | something invokes it | a unit or timer naming it; a surface or skill invoking it; another artifact executing it; a documented operator procedure that runs it |
| terminal artifact | a person outside the system received it | an **observation**, see clause 4 |

The graph records the kind it resolved and why. An artifact whose kind cannot be determined is reported as such and is not silently treated as a library.

## 2. Readiness and testedness are two axes, not one ladder

`tested_by` is **not** a step toward readiness. A tested leaf and an untested leaf occupy the same rung. They differ in *disposition*, which is a separate and useful thing:

| | untested | tested |
|---|---|---|
| **unconnected** | cheapest to retire — nothing pins its behaviour and nothing needs it | **cheapest to connect** — behaviour is pinned, only a trigger is missing. *90 artifacts today.* |
| **connected** | highest risk in the system — something depends on it and nothing pins it | healthy |

Readiness answers *does anything need this*. Tests answer *does it work*. A thing that works perfectly and nothing needs is not partway to being needed. The graph publishes all four cell counts; today only two are measured (114 unconnected, of which 90 tested).

## 3. The graph is a source of proposals, not a filter on them

This is the clause that makes the definition an **input to task selection** rather than a postmortem.

The reason 114 unconnected entrypoints accumulated one at a time is not that the loop chose badly. It is that nothing in its world could express the task *"this tool exists, is tested, and nothing runs it — give it a trigger"*. That task is invisible, so it is never proposed, while "add a function to a script" is always available.

- The graph publishes the unconnected list with each artifact's kind, test status and age, and **that list is a demand source.** Supply the missing task type; do not suppress the available one.
- At proposal time a candidate names a target. The graph answers one question about it: *does this target already have something depending on it?* Three outcomes — extends a component, grows a leaf, or creates a new artifact. The answer is recorded on the proposal.
- **A proposal that creates a new artifact must name the consumer that will depend on it.** That claim is checked at integration against the graph: did the promised edge appear? A proposal that cannot name a consumer is not refused — it is recorded as creating a leaf deliberately, and the count of those is published.

No gate, no quota. ADR-024 clause "an input, not a gate" holds. The failure mode being fixed is *absence of a visible alternative*, and a suppressor does not supply one.

## 4. A terminal artifact has three states, and absence of evidence is not death

For an artifact whose purpose is to leave the system — a rendered frame, a published video — the depends-on relation lives outside the repository and cannot be resolved statically. Its evidence is an **observation**: a recorded event that the artifact reached its audience.

| state | meaning |
|---|---|
| `unconnected` | nothing produces it; it is not on any path to the screen |
| `unverified` | it is produced, and no observation of it reaching anyone exists |
| `observed` | an observation exists, with its timestamp and source |

`unverified` is never reported as a leaf and never as used. An observation is never inferred — not from the fact that the code ran, not from a render succeeding, not from a publish call returning 200. Only a recorded event that a person received it counts, consistent with ADR-014 and ADR-015: the instrument may not imply a state it does not report.

**The consequence is uncomfortable and belongs in the record.** The system has no observation channel for terminal artifacts today. Every one of them is therefore `unverified`, which means **the charter's top rung is currently unmeasurable**. That is the honest reading, and it names a missing capability precisely rather than scoring the ladder's goal as dead work.

## 5. Where these figures may be read

Per ADR-023, every input to this graph except the systemd-unit class lies inside the loop's writable surface, so **no figure derived from it may be presented to the loop as harness-owned fact.** It reaches task selection through the demand pipeline and the dashboard — both harness-side — and reaches the executor's prompt only labelled as self-reported, if at all.

# Consequences

**The headline number changes shape, not just value.** "8 components of 180" is arithmetically right and useless as a direction. The actionable framing is: 8 connected, 114 finished-but-unplugged, of which 90 can be connected cheaply. The first is a verdict; the second is a backlog.

**The loop gains 114 concrete tasks it cannot currently see**, each smaller than the "add a function" work it does today and each raising a rung instead of adding a leaf. Whether it takes them is a ranking question, not a gating one.

**Two `used_by` classes must be resolvable before the rung means anything.** Today `surfaces/` names zero script paths and `skills/` names 15. If unit files and run-instructions are not resolved, the entrypoint kind has almost no admissible evidence and the count of 8 will barely move for the wrong reason.

**"Done" acquires a check the loop can fail.** A proposal that promised a consumer and did not produce the edge is visible after the fact. That is a measurement, not a punishment, and it is the first definition of done in this system that is not "tests pass".

**Retirement becomes decidable.** Unconnected and untested, old, is a retirement candidate with an argument behind it. Unconnected and tested is a connection candidate. Previously both were the same undifferentiated 122.

**186 dangling references need a home.** They are neither edges nor nothing: they are evidence that documents outlive the artifacts they name. Published as their own count, they are also a lead on documentation rot that nobody is currently chasing.

# Alternatives considered

**Keep ADR-024's single rule and accept 8 of 180.** Rejected not because the number is wrong but because it is silent: it tells the loop that 96% of its output failed and nothing about what to do next. A definition of done that produces only a verdict is half a definition.

**Count an entrypoint as ready when it has a `__main__`.** Tempting — it is what "ready to run" means for a script, and it would score 176 of 180. Rejected: it measures intent, not dependence. 113 of the 114 leaves already pass it, so it would declare the exact population that has never run to be finished.

**Count `tested_by` as a half-step and rank artifacts by a combined score.** Rejected. It puts coverage and demand on one scale, and 90 of 114 leaves being tested means the combined score would be dominated by the axis that does not discriminate. Two axes reported separately carry more information than any weighting of them.

**Infer an observation from a successful render or publish call.** Rejected outright. That is the instrument implying a state it does not report, which ADR-014 and ADR-015 forbid, and it would convert the one honest `unverified` into a fabricated `observed` at exactly the rung where the charter's goal is judged.

**Gate proposals that grow leaves.** Rejected, and this is the load-bearing rejection. The operator's position on preventive quotas is that the agent gets the full context and is trusted to choose; the doc-only budget's measured record supports it — it reported "exceeded" all day while deferring nothing, and it pushed the monoculture from documents into eight near-identical script additions a day. A gate here would do the same thing again. The defect is that the better task is invisible, and the fix for an invisible task is to publish it.

# References

- #1769 — the artifact dependency graph (implementation); this record supplies the rung definition its clause requires.
- ADR-024 — edge taxonomy; extended here, not superseded.
- ADR-023 — provenance rule; why these figures cannot be shown to the loop as fact.
- ADR-022 — context ontology; the one-question-one-owner principle applied to the word "done".
- ADR-014, ADR-015 — the instrument may not imply a state it does not report; the basis for clause 4's refusal to infer an observation.
- ADR-012 — Vector 1 names only what the loop is permitted to change; the artifact kinds here are that surface.
- Measurements, `ozand/eeebot-self-evolving@main`, 2026-09-19: 180 scripts, 176 with `__main__`; 8 with a production consumer; 114 leaves, 90 of them tested, 113 of them with `__main__`; 362 distinct `scripts/*.py` paths named repository-wide, 186 naming no existing file; `surfaces/` naming 0 script paths, `skills/` naming 15.
