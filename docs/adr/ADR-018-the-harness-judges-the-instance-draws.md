---
title: The harness judges and the instance draws, and the seam between them is a published file rather than an import
status: proposed
date: 2026-09-15
authors: [eeebot maintainers]
related: ["#1607", "#1618", "#1613", "#1619", "#1599", "#603"]
tags: [architecture, display, honesty, self-improvement, mutation-surface]
---

# Status

Proposed, and decided under delegation from the operator on the open question in PR #1618: which repository owns the avatar's tile primitives. ADR-013 sets the tier ladder, ADR-014 the avatar's obligations, ADR-015 the published artifact's. None of them says where the code lives, and that omission produced a concrete misplacement within two days of their merge.

# Context

PR #1618 implements the ADR-014 tier-1 and tier-2 work — tile atlas, tilemap composer, palette, dirty-tile push, sprite budget with visible overflow, pose table, measured costs — in `nanobot/runtime/avatar/`, in the harness repository.

`MUTATION_POLICY._COMMIT_PATH_PREFIXES` is `surfaces/`, `scripts/`, `memory/`, `lessons/`, `docs/`, `tests/`, `skills/`, **inside `eeebot-self-evolving/`**. So the loop cannot commit to that module: it cannot extend the primitives, cannot optimise them, and cannot build a tier 2 on them that counts as its own work.

That matters because the argument those records rest on is about the instance. Composition depth is about 1 across 154 `scripts/*.py` **in the instance**, and #1599 measures it there. Tools placed in the harness cannot move that number by construction. It is the defect ADR-012 closed for Vector 1 four days ago — the charter named objects outside the commit surface, and every measurement against it became a proxy — reproduced inside the issue set that introduced the ladder.

The counter-argument is real and must be answered rather than dismissed: **the avatar must not lie**, ADR-014 rule 1 is the reason it exists, and the instance is exactly the component whose output the harness is built to constrain. Handing the loop the code that decides what the cat is feeling would put the honesty guarantee inside the thing it guards.

One measured fact settles how to hold both. The instance **cannot import the harness**: `nanobot` is not importable as the agent user, and #603 already relies on this — the held-out checker pack is invisible to the instance by design, which is why demand tells the loop *what* is broken without ever showing it the checker. Any seam between these two halves is therefore a data contract already, not a choice.

# Decision

**The harness decides what is true; the instance decides what it looks like; between them is a file the harness writes and the instance reads.** Four rules.

## 1. The harness owns the verdict

What signals exist, whether one is observed, how a signal resolves to a state, and that an unknown state exists — all of it stays in the harness, operator-owned and outside the loop's commit surface. The recorded-span fixture and the cover test stay with it, because a test the loop can edit is not a constraint on the loop.

## 2. The instance owns the craft

Atlas, tilemap, palette, expansion, dirty-tile push, sprite budget, effects, fonts, timing, layout: the rendering half lives in `scripts/` or `surfaces/` of `eeebot-self-evolving/`, where the loop can extend it, optimise it, and be measured on it. This is Vector 2's own subject matter — the charter names terminal and pixel-art rendering as an operator interface — and Vector 1's before/after clause finally has code the loop owns to exercise it on.

## 3. The seam is a published state file, never an import

The harness writes the resolved state: the pose, the signal it derives from, and its four-state status. The instance reads that file and renders it. No import crosses in either direction, which the runtime already enforces.

This is the part worth stating twice, because it is the reason the split is safer than keeping everything in the harness: **the instance cannot invent a state, because it never receives the signals — only the harness's verdict about them.** The honesty guarantee stops being a code convention and becomes structural. A renderer that has no access to a thermal reading cannot draw a cat that claims to know the temperature.

## 4. Placement follows who must change it tomorrow

The general rule this case instantiates. Code the loop is expected to improve repeatedly belongs in the instance; code that constrains the loop belongs in the harness. When both are true of one module, it is two modules with a file between them.

# Consequences

## What gets easier

The ladder becomes the loop's. Every later cycle that makes a scene cheaper, an effect richer or a font sharper is composing on its own earlier artifact, and #1599's depth measurement can see it. That was the entire premise of ADR-013 and it was one repository away from being false.

The honesty argument gets stronger rather than weaker. Today the avatar could in principle read a signal and mis-render it; after this it cannot reach a signal at all.

The rule generalises past the avatar. Every future capability with a judging half and a doing half now has a default answer, and the seam it implies is one the runtime already enforces.

## What gets harder

PR #1618 needs splitting before it lands: the pose contract, the recorded fixture and the cover test stay; the tile primitives and the cost script move to the instance. That is rework on a good piece of code, and the measured costs it already carries should move with it rather than be re-measured.

Two repositories now hold one feature, and a change to the published state file is a change to a contract with a reader the harness cannot see. The file needs a version and a reader that fails loudly on an unknown one — the four-state discipline applied to the seam itself.

The instance's rendering code is loop-mutable, so it can be made worse as well as better. That is the deal: it is also the only way it can be made better by the loop, and the gate, the tests and the cover test upstream of it are what bound the damage.

## What does not change

ADR-014's five rules and ADR-013's tiers, both of which this serves. `MUTATION_POLICY` itself — no prefix is added or removed; this record decides where code should be written, not what may be written. The #603 invariant. The charter.

# Alternatives considered

- **Keep everything in the harness and label #1607, #1613 and #1619 as operator-executed under ADR-012 rule 2.** Honest, and briefly tempting because it needs no rework. Rejected because it makes the tech ladder the operator's ladder: the loop would consume tooling it cannot extend, composition depth would stay at 1 by construction, and the Factorio framing in `IDENTITY.md` would describe something the loop is structurally unable to do. A charter that asks for composition and a layout that forbids it is the exact pairing ADR-013 was written about.

- **Move everything, including the pose contract, to the instance.** Rejected. The pose table decides what the machine claims to be feeling, the cover test is what falsifies it, and both would become loop-editable. #1188 is the precedent for what follows: the loop wrote the test forbidding removal of its own additions. Never open that path; close it.

- **Merge #1618 as it stands and move the rendering half in a follow-up.** Rejected on timing rather than on principle. Two priorities are already seeded in the operator canon and are the whole demand queue, so the loop will begin writing instance-side rendering within a cycle or two. Landing a harness-side implementation first guarantees two palettes, and "temporary" placement is what permanent placement is called before anyone measures it.

- **Let the instance import the harness module.** Not available: `nanobot` is not importable as the agent user, and #603 depends on that. Even if it were arranged, it would hand the instance the signals rule 3 exists to withhold.

# Test Contract

- No instance artifact imports `nanobot`; asserted rather than assumed, so the seam cannot erode quietly.
- The published state file carries a version, and a reader encountering an unknown version fails loudly instead of rendering a default.
- A missing or unreadable state file renders the unknown pose, never a healthy one — ADR-014 rule 1 at the seam.
- The pose contract, the recorded-span fixture and the cover test live outside the loop's commit surface; a test asserts their paths are not commit-eligible under `MUTATION_POLICY`.
- The rendering artifacts live under a commit-eligible prefix in the instance, and their measured costs are carried over from #1618 rather than re-derived, with the source of each figure named.
- Composition depth across the instance's rendering artifacts is reported once live (#1599), since moving the ladder into reach is the point of the record and the number is how it is checked.

# References

- #1618 — the PR whose placement question this record answers.
- ADR-012 — Vector 1 names only what the loop is permitted to change; the same failure, one layer down.
- ADR-013 — capability tiers and measured cost; the ladder this makes reachable.
- ADR-014 — the avatar's obligations, which rule 1 here strengthens.
- ADR-015 / ADR-016 / ADR-017 — the published artifact, its metrics, and how long work runs.
- #1599 — composition depth, measured on the instance.
- #603 — the pack is invisible to the instance; the seam this record relies on.
- #1188 — the loop wrote the test protecting its own additions; why rule 1 keeps the cover test out of reach.
- `nanobot/runtime/mutation_policy.py` — the commit surface this record reads and does not change.
