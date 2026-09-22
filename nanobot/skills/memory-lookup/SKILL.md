---
name: memory-lookup
description: Trigger: Querying the memory index.
version: "1.1.0"
tags: [memory, lookup, context]
---

# memory-lookup

Retrieve a specific fact or history entry from memory/ using a single bounded
read instead of loading the full files. Use this when you need one fact, not
the entire memory context.

## Memory layout (what lives where)

| Path | Kind | How to read |
|---|---|---|
| `memory/index.md` | Catalog of every fact and discipline file | Full read (small, bounded) |
| `memory/facts/<name>.md` | Reusable lookup facts (identity, key paths, rules) | Full read (small) |
| `memory/<topic>.md` | Discipline & protocol cards (one per topic) | Full read (small) |
| `memory/HISTORY.md` | Append-only cycle log, newest at bottom | Bounded tail read or grep |
| `memory/MEMORY.md` | Active working-memory notes | Full read (small) |
| `memory/MEMORY_ARCHIVE.md` | Archived weekly summaries | Bounded read |

## Structured query procedures

Pick the procedure that matches the question type; each is one or two
bounded calls, never a full-file dump.

### Q1: "What is the fact about X?" (known or unknown fact name)

1. Read the catalog once:
   ```
   read_file("memory/index.md")
   ```
2. Match the question to an index line (each line is
   `[Title](relative/path) - one-line summary`). Read exactly that file:
   ```
   read_file("memory/facts/<fact-name>.md")
   ```
3. If no index line matches, the fact does not exist yet. Do not grep the
   whole tree; note the gap and (if the fact is reusable) create it under
   `memory/facts/` and add its line to `memory/index.md` in the same cycle.

### Q2: "What happened recently?" (recent activity)

HISTORY.md is append-only; newest entries are at the bottom. Read the tail
with a bounded window:

```
read_file("memory/HISTORY.md", offset=<total_lines - 50>, limit=50)
```

Get `total_lines` cheaply first (`wc -l memory/HISTORY.md`) if you do not
know it. Do not read the file from line 1.

### Q3: "When did X happen / which cycle touched path P?" (keyword search)

Grep for the keyword, then read only the hit window:

```bash
grep -n "<keyword>" memory/HISTORY.md | tail -20
```

Each entry starts with `[YYYY-MM-DD HH:MM]` or `- YYYY-MM-DD: [cycle-...]`,
so the line number from `grep -n` is the `offset` for a small
`read_file(..., offset=<hit>, limit=10)` window if you need the full entry.

### Q4: "Is this task already done?" (redundancy check)

Combine Q3 with the "Recent activity" list in the task prompt:

```bash
grep -n "<target-path-or-task-title>" memory/HISTORY.md | tail -5
```

If a recent entry already covers the target, stop and report
`outcome: skipped` — do not fan out into auxiliary confirmation scans.

### Q5: "What is the current priority / working state?"

```
read_file("memory/MEMORY.md")
```

For older state, read the tail of `memory/MEMORY_ARCHIVE.md` with
`offset`/`limit` instead of the whole archive.

## Index navigation guidelines

- `memory/index.md` is the single entry point: read it before any fact
  lookup, and only once per cycle.
- Index lines are `[Title](path) - summary`. The summary is enough to decide
  whether the file is relevant; read the file only when the summary matches
  the question.
- Facts live in `memory/facts/`; discipline cards live directly in
  `memory/`. The index section headings tell you which is which.
- When you add a new fact or card, add its index line in the same commit —
  an unindexed file is invisible to future lookups.
- If the index and the directory disagree (listed file missing, or file
  present but unlisted), trust the directory listing
  (`list_dir("memory/facts")`) and repair the index in the same cycle.

## Invocation workflow (one lookup, end to end)

1. Classify the question (Q1–Q5 above).
2. Issue the single bounded call for that procedure.
3. Stop. Do not re-read the index, do not grep again for the same fact,
   and do not read companion files "just in case" — each extra call is a
   full iteration of the turn budget.

## Constraints

- Always use `offset`+`limit` on large files (HISTORY.md can be long)
- HISTORY.md is append-only; newest entries are at the bottom
- Never write to HISTORY.md directly; the harness appends entries
- Fact files in memory/facts/ are the right place for reusable lookup targets
- One lookup = at most two tool calls (index + fact, or grep + window)

## When to use

- Need one specific fact (host capability, a prior decision, a path)
- Checking recent cycle history before proposing overlapping work
- Confirming a prior lesson before attempting a known-risky operation
