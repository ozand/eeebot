---
title: One clock — the day is host-local, everywhere
status: accepted
date: 2026-09-21
authors: [ozand]
related: ["ADR-026", "ADR-028", "#1178", "#1207", "#1813", "#1821"]
tags: [architecture, runtime, observability, telemetry]
---

# Status

**Accepted by the operator, 2026-09-21.** The tree does not satisfy this record:
the system currently runs on two clocks at once. ADR-026 declared the day a unit
of delivery and never said which day. This record says which, and corrects
ADR-028 rule 6, which asserted an ordering between two events that are three
hours apart.

# Context

The system keeps day-keyed artifacts — rotated telemetry, a rotated cycle
ledger, an action index, crash records, story artifacts, and now a diary — and
runs boundary jobs that act on them. Measured 2026-09-21, those two populations
disagree about what a day is.

**Host-local (MSK):** every systemd timer, because `OnCalendar` without a `UTC=`
suffix is local — the knowledge curator at `daily`, the action index at
`00:05:00`, the narrator at `03:30:00`. Also `bridge.py`'s `date.today()`, the
memory archiver's retention and archive cutoffs, `cycle_logger.py`, and the
clock string the loop is shown.

**UTC:** the cycle ledger's rotation and retention, `llm_telemetry`'s day
filename and rotation, the action index's own rotation and archive cutoffs, and
crash-record day derivation.

So the boundary *jobs* run on one clock and the *files they act on* roll over on
another. For three hours of every day — 21:00 to 24:00 UTC, which is 00:00 to
03:00 local — the two halves of the system disagree about today's date.

## What that costs, concretely

Verified against the host on 2026-09-21: the ledger's last row was
`20:20:43Z` and the file's mtime was `23:20:43 +0300` — the same instant, so
`ts` is genuine UTC. The file's first row is `00:05:00Z`, which is the first
cycle write after **UTC** midnight; rotation is not scheduled at all, it happens
lazily on the next write after the UTC date changes.

The knowledge curator fires at 00:00 local, which is 21:00 UTC. A day fold
running there would summarise a UTC ledger day with three hours still to go, and
would do so every single day. That is the exact failure ADR-028 warns about in
its own context section — keeping the morning and dropping the evening — rebuilt
by a timezone instead of by a truncation.

ADR-028 rule 6 also states that the action index at `00:05` and the ledger
rotation happen at "the same moment". They are three hours apart. The two
numbers matched because both are written `00:05`, in different timezones. That
clause is wrong and is corrected by this record.

# Decision

## 1. One clock, and it is host-local

Every day-keyed artifact and every boundary job uses the host's local date.
A day begins at local midnight and ends at the next local midnight. There is no
second clock for any purpose that names, rotates, folds, retains, or reports a
day.

## 2. Local rather than UTC, and the reason is not convenience

The day exists in this system because of two things outside the machine: the
operator owes a video every 24 hours, and a viewer's day is a local day. Both
are local. UTC is the implementation default of the writers that happen to use
`datetime.now(timezone.utc)`, never a requirement anything stated.

Instant timestamps are a separate question and are not changed here. A `ts`
field may stay UTC with its `Z`; what must be local is every derivation of a
*day* from an instant — the filename, the rotation key, the retention cutoff,
the fold's window.

## 3. The transition day is declared, never silent

Moving the rotation key from UTC to local makes one day of history the wrong
length — 21 hours or 27, depending on direction. That day is named in the
migration, recorded in the affected stores, and excluded from any rate computed
across it. A day of a different length that is silently averaged into a trend is
a false reading, and this system has produced enough of those.

## 4. The reader census is part of the change, not follow-up work

Seventeen modules outside tests format or parse `%Y-%m-%d`. A rotation change
that lands before every one of them is classified is how #1178 broke
`reflection_context` and was only caught in #1207. Each reader is classified as
unaffected, affected-and-updated, or affected-and-accepted **before** the writer
changes, and the classification ships with the change.

# Consequences

## What gets easier

The day becomes a single thing that can be reasoned about. ADR-028's fold has an
unambiguous window — the local day that just ended, whole. ADR-026's day clock,
the narrator's day, the diary's filename, and the ledger's rotation all name the
same twenty-four hours. An operator reading a dashboard figure for "yesterday"
and a loop reading `diary/<yesterday>.md` see the same rows.

## What it costs

A migration across the day-keyed writers, a reader census across seventeen
modules, and one declared irregular day.

## How we would know it failed

A reader still comparing a local day against a UTC-keyed file, which shows up as
a day that reads empty at one end and doubled at the other. Or the irregular
transition day appearing inside a rate or a trend, which is how a one-off
becomes a conclusion.
