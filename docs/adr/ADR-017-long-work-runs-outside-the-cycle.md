---
title: Work too long for a cycle runs in its own unit, checkpointed, and meets its deadline by shortening rather than by skipping
status: proposed
date: 2026-09-15
authors: [eeebot maintainers]
related: ["#1605", "#1607", "#906", "#1197"]
tags: [eeepc, runtime, scheduling, publishing]
---

# Status

Proposed — filed with ADR-015 and ADR-016. Those two govern what a published artifact says and what it may cause. This one governs how a job that outlives a cycle is run at all.

# Context

Every unit of work in this system today fits one bounded cycle: a `Type=oneshot` service on a timer, a fresh process each time, everything it produces committed before it exits. That shape is load-bearing — it is why a wedged cycle cannot poison the next one.

Producing a video does not fit it. Measured on the host on 2026-09-15:

```text
compose + palette, no encode        5.59 fps     (179 ms/frame)
full pipeline to h264, real scene   3.85 fps     (260 ms/frame, 81 ms of it the encoder)
5 minutes at 12 fps                 15.5 min wall, 15-30 MB
espeak-ng narration                 27x realtime, 9 MB peak RSS
thermal under 11 min sustained load 65 C against a 62 C idle baseline
```

Three readings shape this record, and one of them contradicts what I expected.

**The job is long against a cycle and short against a day.** Fifteen minutes exceeds the cycle budget outright, and it is about 1% of the interval between publications. The problem is not the total cost; it is that the cost does not fit the container.

**The composition costs more than the encoder.** 179 ms to build a frame against 81 ms to encode it. Work aimed at making this cheaper belongs in the tile path, not in codec flags — which is also where the tile discipline was always pointed.

**Thermally this is a non-event, so far.** Three degrees over idle after eleven minutes of full load. I expected throttling to be the binding constraint and it is not, at this duration. That is measured over minutes, not hours, and the honest form of the rule is a guard that is currently not binding rather than a rule justified by a reading that does not exist.

The real hazard is elsewhere and it is documented: the host has no UPS, its battery was removed after causing hard power-loss reboots, and unexplained unreachability has meant power more than once. A long job that restarts from zero after every power event never finishes on a machine that loses power.

# Decision

**Work whose expected duration exceeds the cycle budget runs in its own unit, yields to the loop, survives a reboot, and meets its deadline by producing less rather than by producing nothing.** Five rules.

## 1. Its own unit, never inside a cycle

A long job is a separate `systemd` unit with its own timer and its own budget. It is never started from inside a cycle, and a cycle never waits on one. The bounded-cycle property is preserved by keeping the long work outside the boundary rather than by stretching it.

## 2. It yields to the loop

`Nice`, `CPUWeight` and idle I/O scheduling, set so that thinking wins every contest against rendering. The loop is the product; the video is an account of the product. A render that slows the loop it is filming has corrupted its own subject, and it would do so in the most embarrassing way available: by inflating the cost-of-thought figure it exists to display.

## 3. It checkpoints per segment and resumes

Progress is written at segment boundaries and a restart continues from the last one. This is required by the host, not by the duration: no UPS, no battery, a history of hard power-loss reboots. A job that cannot survive a power cut on this machine is a job that will not complete on this machine.

## 4. The deadline is met by shortening, never by skipping

At a stated margin before the publication slot, whatever is finished is what ships — a shorter artifact, visibly marked as truncated, with the reason recorded. A skip is a silent degradation and this project prefers a visible one; that is ADR-014 rule 3, and the same argument that rejected skipping a quiet day in ADR-016.

## 5. It declares its own cost, and backs off on a signal it does not yet need

Every run records wall time, CPU-seconds, peak RSS and thermal readings taken during its own load — ADR-013 rule 3 applied to a job rather than to an artifact, and the numerator of ADR-016's cost-per-finished-minute.

The thermal backoff is built and wired to the #1605 probe, and the record states plainly that at the measured duration it does not trigger: three degrees over idle after eleven minutes. It exists because the measurement covers minutes and the intended workload will eventually cover hours, and because a guard that leaves no trace reads the same as one that was never loaded — so it records its own evaluations whether or not it fires.

# Consequences

## What gets easier

Expensive work becomes possible at all. Until now, anything that could not finish inside a cycle simply could not be attempted, which quietly bounded what the loop was able to build to what fits in fifteen minutes.

Rule 5 produces the first honest cost-of-thought numerator on a workload big enough to measure. The charter's before/after requirement has been exercised 20 times in 2 288 commits, mostly because nothing was cheap and large enough to measure well.

## What gets harder

A second execution shape means a second place where things can wedge, and it does not inherit the cycle's guarantees. It needs its own liveness observation — #1197 is the standing warning: a nine-hour crash loop that systemd, the ledger and the gate all reported as normal. A long-running job is exactly the shape that failure hides in, and the duration in state is the thing to watch, not the transitions.

Checkpointing costs code and correctness attention that a straight-through renderer would not need.

## What does not change

The cycle: `Type=oneshot`, its timer chain and preset (#906), the bounded per-cycle budget, the mutation gate, `MUTATION_POLICY`. Nothing here gives the loop a longer cycle or a persistent process; it gives a *different* job a different container.

# Alternatives considered

- **Extend the cycle budget for jobs that need it.** Rejected. The bounded oneshot cycle is why a wedged cycle cannot poison the next, and a conditional bound is not a bound. The exception would be requested again.

- **Run the render inside the cycle and accept a slow cycle.** Rejected on the measurement: fifteen minutes against a fifteen-minute cadence stalls the loop entirely, and by rule 2's reasoning it would also corrupt the numbers being filmed.

- **Render off the box.** Rejected in ADR-015 on measurement and on purpose — the local pipeline fits the day, and optimising it is the stated achievement.

- **Skip the publication when the render does not finish.** Rejected under rule 4 for the reason ADR-016 rejected skipping a quiet day: an absence hides the fact that a shorter artifact would have stated.

- **Leave thermal backoff out until a measurement demands it.** Rejected, narrowly. The reading genuinely does not justify it today, and the temptation is to skip it — but the intended workload grows from minutes to hours, and the cost of the guard is small next to the cost of discovering the need through a throttled loop. It is built, it is recorded, and the record says it does not currently fire, so no one later mistakes an unexercised guard for a working one.

- **Use a long-lived daemon instead of a timer.** Rejected. A resident process is a new failure surface on a 2 GB box, and the checkpointing rule 3 requires for power loss also gives restart-on-timer everything it needs.

# Test Contract

- No cycle code path starts or waits on the long job; asserted on the call graph.
- A run killed mid-segment resumes from the last checkpoint and produces a byte-identical artifact to an uninterrupted run; a test drives the kill.
- At the deadline margin, a fixture with unfinished segments produces a shorter artifact marked truncated with a recorded reason, and never an absent artifact.
- Contention: a fixture asserts the job's scheduling parameters are set such that the loop's own process is preferred, and the assertion names the values rather than trusting a comment.
- Every run records wall time, CPU-seconds, peak RSS and thermal samples taken during its own load; a run that records none fails.
- The thermal backoff records an evaluation on every sample whether or not it fires, so a never-triggering guard is distinguishable from an absent one.
- Liveness: a job stuck in one segment past a stated duration is reported as stuck; the test drives duration in state, not a transition.

# References

- ADR-015 — the channel as an instrument; what the produced artifact may claim.
- ADR-016 — the one-way barrier and the deadline-hit-rate metric this record feeds.
- ADR-014 — visible degradation over silent degradation, which rule 4 reuses.
- ADR-013 — a measured cost per artifact; rule 5 applies it to a job.
- #1605 — the thermal and own-process probes rule 5 reads.
- #1607 — the surface whose frames this job records.
- #906 — the cycle's timer chain and preset, unchanged by this record.
- #1197 — three observers, all green, over a nine-hour crash loop; why rule 2 of the consequences names liveness.
