---
title: The day is held by a diary the loop appends to and never loads
status: accepted
date: 2026-09-20
authors: [ozand]
related: ["ADR-016", "ADR-022", "ADR-023", "ADR-025", "ADR-026", "ADR-027", "#1805"]
tags: [architecture, runtime, context, memory, observability]
---

# Status

**Accepted by the operator, 2026-09-20.** Nothing in the tree satisfies this
record. It is the continuity half of ADR-026: that record made the day the unit
of delivery, and this one says what carries a day across the cycles inside it.

# Context

A day is roughly ninety cycles. Measured 2026-09-19: 787 executor calls at a
median of 9 tool iterations per cycle gives ~87; ADR-016 independently puts it
at 96. Each of those cycles begins with no knowledge of what the previous
eighty-nine were trying to do.

Five stores already exist, and none of them holds that.

| store | writer | holds |
|---|---|---|
| `state/ledger/cycles.jsonl` | harness | events — what happened |
| `memory/` | loop | durable facts, topic-keyed (62 files, 458 KB) |
| `lessons/` | loop | conclusions, topic-keyed (109 files, 412 KB) |
| `concrete_next_action` | loop | a baton, exactly one cycle forward |
| git log, "Recent activity" | harness | what was committed |

The ledger records what happened, not what was meant. Memory and lessons record
what is true regardless of the day. The baton reaches the next cycle and dies
there. **Nothing holds an intention that outlives the cycle that formed it**, so
no cycle can be step three of a five-cycle plan; each is step one of its own.

The output shows it. All eight successful cycles on 2026-09-19 had the same
shape — add one function to an existing script, plus a test — and five of the
eight extended an artifact with no production consumer. A day of work with no
thread running through it produces ninety first steps.

## Why the day does not go in the prompt

The obvious remedy is a block of day state in the system prompt. It is refused
here, on cost and on a known failure mode.

Cost: assembly is ~20,960 characters against a 35,000 ceiling, with 2,008
characters free across the release blocks. A day accumulates ninety entries.

Failure mode: positional truncation is a recurring class in this system —
strategist, executor and curator each trim by key order, line number or byte
offset, and 11,688 characters of `AGENTS.md` were dropped from every executor
prompt for four days before anyone noticed. An appended day block truncates from
the wrong end: it would keep the morning and drop the afternoon, which is the
exact inverse of what the cycle needs.

The second tier already solves this. 202 files, ~1,025 KB — skills, lessons,
memory — sit on disk, reachable by `read_file`, never loaded. The diary is a new
member of that tier, keyed by date rather than by topic. Its cost to the prompt
is one instruction and zero content.

The known weakness of that tier is also on record: reachable is not read. #1805
was filed because a cycle redid work a skill described in full, one `read_file`
away. Rule 5 below exists for that reason and is the load-bearing rule of this
record.

# Decision

## 1. One file per day, in the repository, appended to through a marker

`diary/YYYY-MM-DD.md`, versioned and published like any other file the loop
writes. It is not runtime state and it does not live in `state/`; the operator
reads it without a shell on the host, it diffs, and it survives the host.

The file ends with a single unique marker line. A cycle appends by replacing
that marker with its entry followed by the marker again. `edit_file` is a
fragment replacement with a uniqueness requirement — it refuses when `old_text`
matches more than once — and a marker occurring exactly once satisfies it.
Ninety cycles can therefore append to one file with no possibility of
overwriting each other, enforced by the tool rather than by instruction.

`write_file` against a diary path is forbidden: it drops what it does not
rewrite, and the whole day is what it would drop.

## 2. The entry is written at the start of the cycle, not at the end

The diary holds intent, and intent is known at step one. An entry written at
step one survives everything that kills step nine — and 38% of cycles end
abnormally (17% truncated at the completion ceiling, 17% gateway error, 4% cut
off mid-flight).

Those entries survive because the bridge already carries them. Its auto-commit
safety net (#666, unconditional since #717) commits uncommitted work before the
gate runs, and #1281 extended that explicitly to a subagent that died on its LLM
call. A truncated or transport-dead cycle does not lose its edits.

A closing entry is permitted and is not required. The opening one is required.

## 3. The diary path integrates on its own verdict

An entry that rides the cycle's diff dies when the code in that diff is
rejected — and a rejected cycle is precisely what the next cycle needs to know
about. The diary path is committed and integrated independently of the verdict
on the code.

This is a carve-out from the code verdict only. Every other check still applies:
the secret-shaped-filename and blocked-pattern scan the auto-commit already
performs runs unchanged, because this content is published.

## 4. The diary never enters the prompt

The prompt carries the instruction and not the content, now and after the diary
has run for a year. Any future proposal to paste diary content into an assembled
block is a change to this rule and needs its own record.

## 5. The instruction separates obligation from permission

Two clauses, and the distinction between them is the point.

**Obligation.** Read today's diary as the first action of the cycle.
Unconditional, with no judgement about whether it is relevant.

**Permission.** Read any earlier day, any month, any horizon that is kept —
when the task points there. The loop is free to study what was done at any
depth, and this is stated to it as a freedom.

Writing both clauses as "read it when it would help" kills the first one.
Conditional triggers fail measurably here: that is #1805, and it is why the
operating rules had to say *read the skill before starting, not after a failed
attempt*.

**Measured from day one.** Whether a cycle read the diary is visible in its tool
calls, so the read rate is a number, not a hope. It ships with the instruction,
not after it. Every instruction in this system that is not measured drifts.

## 6. The reflector folds the day at the boundary, and the fold is an index

At the day boundary the reflector reduces the day file to one line in
`diary/YYYY-MM.md`, and the month file is one line per day. A top-level index
carries one line per month. The reflector is live — 36 to 79 calls a day since
2026-09-16 — so this needs no new role.

The index shape is what makes rule 5's permission real. Freedom to read any
horizon is worth nothing if finding the right file costs thirty reads: a cycle's
median is 9 tool iterations of 80, and four blind reads spend half the useful
budget before the work starts. With one line per day and one per month, any
targeted lookup is two hops.

The day file becomes read-only at the boundary, and by construction rather than
by a flag: the day is over, so no cycle's marker append can address it. The
freeze and the fold are the same event.

## 7. A diary entry is not progress

Writing *"today I plan to…"* costs nothing and produces a file. Under ADR-025
and ADR-027 the diary is excluded from the progress ladder — the same class as
the bookkeeping commits the operating rules already forbid.

# What this record deliberately leaves open

**The month file has no reader among the cycles.** Today's diary has a natural
trigger — the start of the cycle. The month file has none, and no honest trigger
suggests itself. Its realistic readers are the operator and the narrator. This
is named here rather than discovered in a month, because this system has already
produced artifact families with zero readers.

**The diary is not narrator input, by default.** ADR-016 rule 1 lets publishing
read the journal freely, on the reasoning that the journal records what happened
and reading it cannot change what happened. A loop-authored record of *intent*
does not have that property: once the loop knows the narrator reads its diary,
writing a dramatic entry becomes available, and ADR-016 rule 4 forbids shaping
work in order to film it. Feeding the diary to narration would be a real gain —
the story currently has what was done and not what was at stake — and it needs
its own decision, not a default.

**The name.** ADR-016's *journal* is the cycle ledger and the narrator's source.
This is the *diary*. The two words are not interchangeable in this tree.

# Consequences

## What gets easier

A cycle can be part of something. A plan formed at 04:00 is legible at 16:00
without having survived ninety batons. A cycle whose code was rejected still
tells the next one what it was attempting, which is the one thing a rejected
cycle currently fails to communicate. The operator reads the day as prose in the
repository.

## What it costs

One instruction in the operating rules. One carve-out in the bridge's
integration step. One fold job on the reflector. Zero characters of prompt, now
and permanently.

## How we would know it failed

The read rate sits near zero — the file is written and not read, and the second
tier has grown by one unread member. Or entries drift from intent into reports
of what was done, at which point the diary is a slower copy of the ledger and
should be retired under ADR-025. Or day files accumulate while no month line is
ever opened, which would confirm the open question above rather than answer it.
