---
title: The channel is an instrument — the cat may feel anything, and claim nothing the journal does not carry
status: proposed
date: 2026-09-15
authors: [eeebot maintainers]
related: ["#1607", "#1605", "#1505", "#1197", "#1173", "#1455"]
tags: [display, honesty, publishing, eeepc, operator-interface]
---

# Status

Proposed — filed ahead of implementation, with ADR-016 and ADR-017. ADR-013 governs how a capability is built; ADR-014 governs what the resident surface may say to the person in the room. This record governs what that same surface may say to strangers, in a register chosen for people rather than for engineers.

# Context

The loop has no external signal. From the September demand mix, 92% of cycles take their task from a corpus the loop itself wrote, and every metric it optimises is a row of its own authorship. ADR-014 opened the first crack in that: a resident surface makes *returns* — a person coming back unprompted — observable rather than asserted. It is one person, in one room.

The operator has decided to extend the same surface to a public channel: a video most days, produced entirely on the eeePC, narrated by the pixel-art cat in plain non-technical English with burned-in subtitles, autopublished, with subscribers and returning viewers as the success signal. The constraint is the subject: a 2008 netbook that draws, speaks, encodes and publishes without leaving the box is not an imitation of anything that already exists.

Two properties of that decision matter architecturally.

**The video is a recording of the instrument, not a second production.** The surface #1607 builds already draws the cat, the state, and the cost of its own render. A demoscene is that surface running. Composition cost beyond ADR-014's is therefore zero, and everything ADR-014 forbids the surface to say, it forbids the video to say — automatically, because it is the same picture.

**The register is narrative and emotional, by decision.** The cat tells a story in words a non-engineer follows, even about hard work. This is the correct product call and it is also where lying happens, because a story wants a shape that a day of 96 cycles rarely has.

Measured on the host, 2026-09-15, before this record:

```text
compose + palette, no encode      5.59 fps      ffmpeg 5.1.9 + libx264   installed (i386)
full pipeline to h264, real scene 3.85 fps      espeak-ng 1.51           installed
  -> 5 min at 12 fps              15.5 min, 15-30 MB
espeak-ng narration               27x realtime, 9 MB peak RSS, en + ru voices present
thermal, 11 min sustained load    65 C against a 62 C idle baseline
PNG per frame                   2318 ms   <- why frames reach the encoder raw, never through a file
```

Two of those redirect work. **x264 is cheaper than the composition feeding it** — encoding adds about 81 ms per frame against 179 ms to build one, so 69% of the cost is the palette expansion that the tile discipline exists to avoid. And a five-minute video costs about 1% of the day, which is what makes a fully local pipeline the reasonable choice rather than an ideological one. ADR-017 covers running it; this record covers what it is allowed to say.

# Decision

**The channel is an instrument pointed outward. Feeling is free; claiming is not.** Five rules.

## 1. The cat may feel anything; it may not claim anything the journal does not carry

A feeling is not a claim. It is unfalsifiable, it promises the viewer nothing, and it is the whole reason a character works where a table does not. A **claim** — what happened, what improved, what was learned, what is now possible — cites a ledger row, a commit, or a measurement.

Simplification may drop detail. It may never change the sign.

```text
allowed   "I spent all night trying to draw a picture and it kept coming out wrong"
allowed   "I was sick of it by morning"
refused   "I got it working!"        when the cycle failed
refused   "it got faster"            when no before/after measurement exists
```

"The palette expansion cost 259 ms per frame and blew the budget" and "the picture came out so slowly I could not keep up" are the same claim at different resolutions. "But I learned something" is a different claim and needs its own row.

## 2. The covered-story test

Cover the narration and show the cited rows. A narrative beat with no row is invention, and the difference is the defect. This is ADR-014's cover test applied to language instead of posture, and it is mechanised the same way: every published artifact carries its **citation set** — beat to row — and the set is checkable without watching the video.

The check is on provenance, not on vocabulary. A video containing no technical word at all passes; a video whose every sentence is precise and uncited fails.

## 3. The channel is egress-only, and its ingress is numeric

The host holds exactly two authorisations: `youtube.upload` and `yt-analytics.readonly`. Comment, community and subscription scopes are **never requested**. The Analytics API returns numbers, so no third-party text ever enters the machine that writes its own prompts.

This is injection closed by absent capability rather than by policy — the ADR-013 discipline applied to permissions. A rule saying "do not read comments" is a rule a future cycle can reinterpret; a credential that cannot address the endpoint is not.

Work needing a broader scope — the channel card, caption tracks — runs under a separate operator-triggered credential that does not live on the host.

## 4. Synthetic origin is disclosed in every artifact

The channel card and every description say what made them. The loop authors that text and may not omit the disclosure. A machine arguing for honest instruments does not get to be coy about what it is.

## 5. The persona is a register, not a source

The cat's voice is stable across videos and carries no state the surface does not show. ADR-014 rule 1 applies verbatim to narration: a signal that is missing or `probe_unavailable` becomes "I don't know what happened there", never a confident line and never a cheerful one. An avatar with no unknown state is a fabricated zero wearing fur; a narrator with no unknown state is the same thing with better pacing.

# Consequences

## What gets easier

The loop acquires an external, third-party-attested signal for the first time. Nothing about a returning viewer is under the loop's control, which is exactly what makes it worth having.

The register is the one a small local model is best at. Plain, warm narration is easier for it than precise technical formulation, and precision does not disappear — it moves into the citation set, where a machine checks it instead of a listener. The constraint became the form again.

Rule 2 forces #1505. The citation store has never been written, so lesson usefulness is unmeasured today; a channel that cannot publish without citations makes that machinery load-bearing rather than optional.

## What gets harder

Every video needs its citation set assembled before it can ship, and a day with little in the journal produces a short, honest video. That has to be acceptable, out loud, in advance — because the alternative is the failure ADR-016 exists to prevent.

Rule 5 bounds the character by what the machine measures. A more expressive cat needs more probes, which is the correct pressure and more work than writing a better script.

## What does not change

The charter. `MUTATION_POLICY` and the commit surface — a publish is not a commit, and ADR-016 holds that line. The gate, fitness, `_TARGETS`. ADR-014's five rules, which this record extends rather than relaxes. ADR-013's tiers: the channel is a tier-3 delivery and its prerequisites are probed like any other.

# Alternatives considered

- **Narrate freely and check facts afterwards.** Rejected. It puts a public exception to the project's honesty discipline on its most visible surface, and a correction reaches a fraction of the people the claim did.

- **Drop the persona and publish plain telemetry.** Rejected for the reason ADR-014 already rejected a table instead of an avatar: glanceability and interest are the properties being built. Rule 2 is what makes the persona safe, so the persona does not have to be sacrificed.

- **Require technical precision in the narration.** Rejected. It is the operator's product call, it makes the video worse for its audience, and it points the local model at the thing it is least reliable at. Precision belongs in the citation set.

- **Read comments as a feedback signal.** Rejected twice over. Comments are third-party text flowing into a loop that composes its own prompts — an injection channel with a public edit button — and the returns signal rule 4 of ADR-014 already selected is both quieter and harder to game.

- **Render or encode off the box.** Rejected on measurement, not on principle. The pipeline fits a 24-hour budget locally, and the operator's stated goal is that optimising the local toolchain *is* the achievement. An off-box render would remove the only interesting engineering in the product.

# Test Contract

- Every published artifact carries a citation set mapping each narrative beat to a ledger row, commit or measurement; a beat with no citation fails the build rather than the review.
- A fixture in which a probe reports `probe_unavailable` produces an explicit unknown line in the narration; a test asserts no confident or positive phrasing is reachable from that state.
- The host credential cannot address a comment, community or subscription endpoint: asserted on the granted scope list, and by a test that no such call site exists in the publishing client.
- The disclosure string is present in the channel card and in every generated description; a test asserts it and asserts it cannot be templated away.
- Narration state strings come from one table shared with the surface's posture table, so a video cannot describe a state the instrument cannot display.
- A fixture day with a single journal event produces a short video, not a padded one; a test asserts the artifact's length tracks its citation count.

# References

- ADR-014 — the resident surface as an instrument; this record is its outward-facing case.
- ADR-013 — capability tiers, probes, and declared cost; the channel is a tier-3 delivery.
- ADR-016 — the one-way barrier between the journal and the channel, and which metrics are admissible.
- ADR-017 — how work too long for a cycle is run.
- #1607 — the tile-rendered surface this records.
- #1605 — the probes whose unknown states rule 5 depends on.
- #1505 — the citation store, unwritten today, that rule 2 makes load-bearing.
- #1197 — three observers, all green, over a nine-hour crash loop.
- #1173 — empty is not unavailable.
- #1455 — the experiment ledger's noise floor; why an external metric is not automatically a good one.
