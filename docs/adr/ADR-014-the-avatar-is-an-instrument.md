---
title: A displayed avatar is an instrument, and attention is invited rather than captured
status: proposed
date: 2026-09-14
authors: [eeebot maintainers]
related: ["#1607", "#1605", "#1277", "#1197", "#1173", "#1312", "#1482"]
tags: [display, honesty, eeepc, operator-interface]
---

# Status

Proposed — filed with #1607, ahead of implementation. ADR-013 governs how a capability is built; this record governs what the tier-3 surface is allowed to say and how it is allowed to ask for attention.

# Context

The loop's only output channel today is a git commit, which nobody reads in real time. The machine sits in a corner and shows nothing.

`goals.md` Vector 2 already asks for the opposite — terminal-based rendering as the most efficient medium for this host, "including pixel-art style output such as `images/eeebot.png` in the repo". That image contains the intended design: the machine's avatar is a cat on the screen, surrounded by a status HUD. The clause has produced **0 artifacts in 2 288 commits**, for the reason ADR-013 names.

The moment a surface exists, two problems appear that no existing record covers.

**An avatar is trusted faster than a number.** A posture — asleep, alert, hunched, gone — is read instantly and without scepticism. That is exactly what makes it valuable as an ambient display, and exactly what makes it dangerous. #1197 recorded a nine-hour crash loop that was invisible to systemd, the ledger and the gate simultaneously: three observers, all green. A contented cat would have been the fourth, and the one a human would have believed.

**Attention is a hazardous objective.** The reason to build a surface is that something should be useful to a person, and usefulness needs an external signal the agent cannot assert — a person returning is such a signal. But the local maximum of "attract attention" is brightness, sound and motion, which is the mechanism behind every dark pattern on the internet. On a machine with a camera and a microphone in a room where someone lives, this is not an abstract concern, and a self-improving loop pointed at that objective will find the local maximum.

# Decision

**The surface is an instrument. It reports observed state, it degrades visibly, and it invites attention rather than taking it.** Five rules.

## 1. Every avatar state derives from a harness-observed signal, and an unknown state exists

A posture maps from a signal the harness recorded, not from an inference, a default, or the agent's own account of how the cycle went. A signal that is missing or `probe_unavailable` resolves to the **unknown** posture — never to a healthy one. An avatar with no unknown state is a fabricated zero wearing fur, and #1173's contract applies to a picture exactly as it applies to a count.

## 2. The cover test

Cover the avatar and show only the numbers beneath it. If the avatar communicates anything the numbers do not, the avatar is inventing, and the difference is the defect. This is the falsifiability rule for a non-numeric display, and it is testable: every posture names the signal it derives from, and the enumeration is asserted.

## 3. Degradation is visible, never silent

When the render exceeds its budget, the surface drops content in a deterministic and noticeable way rather than taking longer. This is the project's own standing preference — a visible degradation over an invisible one — and it happens to be how console hardware solved the same problem: a sprite limit produced flicker and dropout, not lag. A panel that silently misses frames is a panel lying about the load.

## 4. Attention is invited, not captured

No sound. No raising or stealing focus. No brightness change to draw the eye. The surface earns a glance by being worth glancing at, and the metric — when one exists — is **returns, not glances**: a person coming back unprompted. Optimising for dwell, salience or interruption is out of bounds, and the prohibition belongs in the record rather than in an intention, because it constrains a self-improving loop that will otherwise discover the shortcut.

## 5. The surface reports its own cost

What the render cost to produce is one of the things displayed. This is what separates the aesthetic from a costume: the same visual language on unconstrained hardware is decoration, and here it is the honest consequence of a budget. It also closes ADR-013 rule 3 — an artifact that declares its cost, on the one surface where a human can see it.

# Consequences

## What gets easier

The instrument-honesty discipline this project already applies to pages and counters extends to the one surface a human will actually look at. #1277 showed what an honest display buys: semantic status exposed two zero-writer artifact families on the first page load. A cat with an unknown posture does the same job for the operator standing in the room.

Rule 4 settles the attention question before the metric exists, which is the only time it can be settled cheaply. Once a number exists, arguing against optimising it is much harder.

Rule 5 turns the constraint into the identity. A resident, offline, slow instrument that shows what thinking cost it is not something a phone can imitate, because the phone has nothing to confess.

## What gets harder

Every posture now needs a signal behind it, so the expressive range of the avatar is bounded by what the machine actually measures. A richer cat requires more probes — which is the correct pressure, and also more work than drawing one.

Rule 3 means the panel will sometimes visibly lose content, and that will look like a bug to someone who does not know the rule. It should be legible as deliberate.

Rule 4 forgoes the easiest engagement mechanisms permanently. If nobody looks at the panel, the answer is that it is not worth looking at, not that it should be louder.

## What does not change

The charter is not amended by this record. The pixel-art clause already exists; adding a cat to it would produce a third decorative clause, and ADR-013 explains why. The avatar enters as a tier of the ladder with a probe beneath it. `MUTATION_POLICY`, the gate, and the sibling dashboard's separate role (#1482) are untouched.

# Alternatives considered

- **Show numbers only; skip the avatar.** Rejected. Glanceability is the property being built, and a table does not have it. The objection this alternative raises is real, and rule 2 is the answer: the avatar may not carry information the numbers lack, so it is a rendering of the numbers rather than a substitute for them.

- **Let the avatar express a summarised mood — a single healthy/unhealthy judgement.** Rejected. A summary over readings that include an unavailable one is exactly the fabricated aggregate #1173 and #1312 exist to prevent, and it removes the unknown state that rule 1 requires.

- **Measure attention as dwell time or gaze.** Rejected on two counts. Camera-based gaze on this hardware is unreliable, and dwell is the metric whose optimisation produces the dark-pattern local maximum rule 4 forbids. Returns are cheaper to measure and cannot be manufactured by the surface.

- **Render smoothly with a conventional framebuffer path and drop the tile discipline.** Rejected. A full 1024×600×24bpp surface is 1 843 200 bytes per update against 19 200 for a tilemap — about 96× — while the model is using the CPU. The tile discipline is what makes the surface affordable; the look is its consequence, and rule 5 makes that visible rather than merely true.

- **Put the cat in `goals.md`.** Rejected. Two charter clauses naming capabilities with no probe and no tier have produced nothing in 2 288 commits. A third would not behave differently.

# Test Contract

- Every posture is enumerated and each names the harness signal it derives from; a posture with no named signal fails the test.
- A signal in `probe_unavailable` or missing resolves to the unknown posture; a fixture drives each case and asserts no healthy posture is reachable from an unobserved signal.
- The cover test is mechanised: the set of distinctions the avatar can express is a subset of the distinctions present in the underlying readings.
- Budget overflow produces a deterministic, asserted dropout, and the frame time does not grow.
- The surface makes no sound call, no focus call, and no brightness call; asserted by absence, on the render path.
- The displayed render cost is the measured cost of that render, not a stored constant.

# References

- #1607 — the implementing issue.
- ADR-013 — capability tiers, probes, and declared cost; this record is its tier-3 obligation.
- #1605 — the screen and body probes this surface depends on.
- #1277 — an honest display is an instrument; what semantic status exposed on first load.
- #1197 — three observers, all green, over a nine-hour crash loop.
- #1173 — empty is not unavailable; the contract rule 1 extends to a picture.
- #1312 — a fabricated aggregate over unreadable inputs.
- #1482 — the sibling dashboard's separate role; this surface is not that.
- `goals.md` Vector 2 — the existing pixel-art clause.
- `images/eeebot.png` — the intended design, already drawn.
