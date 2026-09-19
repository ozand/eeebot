---
title: A figure shown to the loop as fact is one the loop cannot reach — provenance, not file ownership
status: accepted
date: 2026-09-19
authors: [eeebot maintainers]
related: ["#1766", "#1769", "#603", "ADR-022", "ADR-024"]
tags: [prompt, architecture, ontology, runtime, safety]
---

# Status

**Accepted, 2026-09-19.** Nothing in the tree satisfies this record yet. #1766 (the harness-owned state block in the executor prompt) is its first implementation and is open; #1769 (the artifact dependency graph) is affected by it and is open.

This record **extends** ADR-022 and does not supersede any part of it. ADR-022 decision 2 ("code assembles, code does not author") governs the *text* of the prompt; it is silent on the *numbers* a future block would carry, which is the gap #1766 walked into. Per `docs/adr/README.md` an accepted ADR is immutable and superseding requires a new record, so the extension is filed here rather than appended to ADR-022 as an eleventh decision.

# Context

#1766 asks for a state block in the executor's prompt so the loop can see its own results, and states the safety property as *"figures from a file the loop cannot write"*. Measured before building it, that file does not exist.

## There is no uid boundary

Host `eeepc`, 2026-09-19, `systemctl show eeepc-self-evolving-subagent-bridge.service`:

```
User=eeepc-agent
Group=eeepc-agent
ReadWritePaths=/var/lib/eeepc-agent /var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving
```

One unit, one uid, and both trees — the state tree and the instance repository — declared writable by it. The scorecard writer *is* the bridge. Every file under `state/` is owned by `eeepc-agent`. The `0600` on `state/goals/goal_text.json` restricts other system users; it does not separate the harness from the loop, because at the operating-system level they are the same user.

## The conflation is already written down as an invariant

`nanobot/runtime/scorecard.py:1730-1732`, in the docstring of `compute_scorecard` itself:

> The computation and its sidecars live in the product runtime and `state_dir` — outside the instance's mutable workspace, so the instance can never redefine its own fitness (#603 invariant).

The first half is true: the code and the output file do live outside the instance's workspace. The conclusion does not follow, because the function's own signature takes the instance repository as an input —

```python
def compute_scorecard(state_dir: Path, selfevo_repo: Path | None, ...)
```

— and `_retention_cost(repo)` reads `repo/scripts/`, which the loop commits to every day. A number can be written by the harness, into a harness-owned file, and still be a number the loop chose.

## The partition is clean at the level of inputs

Measured 2026-09-19 over `compute_scorecard`:

| | count |
|---|---|
| inputs to the computation | 10 |
| …outside the loop's writable surface | 8 |
| …inside it (`repo/scripts/`, `repo/surfaces/`) | 2 |
| published fields | 139 |
| …safe (no input inside the surface) | 130 |
| …unsafe | 9 |

Of the five figures #1766 wants in the state block, exactly one is unsafe: `zero_static_consumers`.

So the boundary that #1766 needs is real and almost entirely intact. It is simply not the boundary the codebase names.

# Decision

**A figure may be presented to the loop as harness-owned fact only if every input in its derivation lies outside the loop's writable surface.**

Ownership of the file is not the test. Provenance of the number is.

Five clauses follow from it:

1. **Classification is per figure, not per file.** A safe figure and an unsafe one live side by side in `latest.json` today. "This file is harness-owned" is not an argument about any field in it.

2. **Provenance is a property of the producing code and is recorded there.** The safe/unsafe classification is carried next to the field by whatever computes it, machine-readable, so a consumer never re-derives it and a reviewer never holds it in their head.

3. **Unknown provenance is unsafe.** A field whose derivation has not been established defaults to unsafe. A new field added without a classification is unsafe by that default, not safe by omission — the failure mode this rule exists to prevent is a number quietly acquiring authority it was never granted.

4. **Unsafe does not mean forbidden; it means not presentable as fact.** An unsafe figure may still be shown to the loop when it is labelled as self-reported, or replaced by a derivation whose inputs are all outside the surface, or kept operator-side (dashboard, demand ranking) and out of the prompt. What it may not be is stated to the loop as something the loop did not choose.

5. **Only a real boundary makes ownership a valid test.** If a future deployment runs the harness computation under a different uid from the loop's write path, file ownership becomes a sound test and this record should be revisited. Until then it is not one, and no comment in the tree may claim otherwise.

# Consequences

**For #1766.** Four of its five figures pass the test and can enter the state block as fact. `zero_static_consumers` does not, and must take clause 4: labelled, replaced, or left out. The choice belongs in that PR and must be stated there.

**For #1769, and this is the harder one.** The artifact dependency graph (ADR-024) describes the loop's own artifacts, and resolves its edges from `scripts/`, `tests/`, `docs/`, `surfaces/` and `skills/` — all inside the loop's writable surface. Only the systemd-unit class sits outside it. By this rule **the graph cannot produce a figure that is presentable to the loop as fact**, however carefully it is built. That is not a defect in the graph; it is what it means for an agent to describe its own output. The graph's numbers belong on the dashboard and in demand ranking, and reach the prompt only under clause 4's labelling, if at all.

**What gets easier.** A builder asking "may this number go in the prompt?" has one question to answer and it is answerable by reading code, not by asking who owns a directory. The 130 safe fields become usable without a case-by-case argument.

**What gets harder.** Every new scorecard field costs a provenance classification. A derivation that mixes one inside-surface input into nine outside ones is unsafe as a whole — there is no partial credit, and a builder who wants the field safe must re-derive it without that input.

**What this does not claim.** It is not a claim that the loop is adversarial. The loop edits `scripts/` because that is its job. The rule exists because a number the loop can move, presented to the loop as ground truth, closes a feedback loop onto itself — and that misreads the same way whether the cause is intent or ordinary work.

# Alternatives considered

**Keep the file boundary and fix the docstring.** Cheapest, and wrong: the docstring is accurate about the file and the claim it draws is the one #1766 relied on. Correcting the prose without correcting the test leaves the next reader to make the same inference.

**Separate uids — run the scorecard computation as a user the loop's write path cannot reach.** This is the only change that would make file ownership a sound safety test, and it is not rejected on merit: it costs a second system user, a cross-user write path, and unit changes on a host with 2 GB of RAM and no spare operational capacity. Recorded as the condition under which clause 5 reopens this record.

**Sign or hash the inputs.** Does not address the problem. The loop's commits to `scripts/` are legitimate and would be signed too; integrity of transport is not independence of derivation.

**Drop the state block idea.** Rejected. The measurement that produced #1766 stands: the loop cannot see its own results, and 62% of recent cycles end with the model declaring itself done. The fix is to be precise about which figures carry authority, not to withhold all of them.

# References

- #1766 — the harness-owned state block in the executor prompt (first implementation).
- #1769 / ADR-024 — the artifact dependency graph, the source of the one unsafe figure.
- ADR-022 — context ontology; this record extends decision 2 from text to numbers.
- #603 — the invariant whose statement in `nanobot/runtime/scorecard.py:1730-1732` this record corrects.
- `nanobot/runtime/scorecard.py:946-996` — `_retention_cost(repo)`, the inside-surface input.
- Host `eeepc`, 2026-09-19 — `systemctl show eeepc-self-evolving-subagent-bridge.service`: `User=eeepc-agent`, `ReadWritePaths` covering both trees.
