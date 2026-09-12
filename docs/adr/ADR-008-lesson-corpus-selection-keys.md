---
title: The live lesson corpus is a retrieval surface, and titles and tags are its selection keys
status: proposed
date: 2026-09-12
authors: [eeebot maintainers]
related: ["#1507", "#1505", "#1481", "#1344", "#1070", "#1071", "#1171"]
tags: [runtime, lessons, knowledge, retrieval]
---

# Status

Proposed — filed with #1507, ahead of any implementation.

# Context

The lesson pipeline has three halves that were built separately and are now in different states.

**Minting works.** #1070 established that `lessons.yaml` was recording cycle protocol with always-zero metrics rather than lessons. #1071 replaced it with a schema-v2 card — `problem`/`solution`, severity, evidence, recurrence — and #1171 added reflector-side minting with a recurrence rule. Those landed. A card in the live corpus today reads:

```yaml
- schema_version: 2
  id: LESS-REF-16004180ba43-4e57
  problem: "`git stash` failed with 'No stash entries found' because the file
            had not yet been tracked by git."
  solution: "Use `git stash push -u <file>` when stashing newly created
             untracked files to avoid pathspec mismatch errors."
  seen_count: 2
  distinct_days: 2
  evidence: [cycle-16004180ba43, cycle-f2874ef0bbc1]
```

**Composition does not.** Parsed from `ozand/eeebot-self-evolving` @ `origin/main`, `lessons/lessons.yaml`:

```text
total entries                       145
  schema_version: 2                  41
  legacy (no schema_version)        104     all 104 with an empty title
v2 distinct titles                   27 across 41 rows
  "Reusable corrective approach"     15 rows
v2 rows tagged `reflector`           38 of 41
v2 rows with seen_count >= 2         28 of 41
```

**Selection is a single card by word overlap.** `lessons_context._score_entry` scores `2 * len(primary_shared) + len(secondary_shared)`; `_best_card` returns exactly one row. The primary field it keys on is identical across 37% of eligible rows, and absent on 104 of 145 rows in the file.

Two open issues consume this corpus and are both downstream of its composition. #1505 found the citation store has never been written, so lesson usefulness is unmeasured; a usage counter over this corpus would measure the selector's luck rather than the lessons. #1481 replaces the bounded prompt remainder with an FTS5 search pointer; lexical ranking over a corpus whose most frequent title carries no topic returns noise with confidence.

The pattern this decision adopts — a corpus that carries its own controlled tag glossary, plus a report of tags used but not defined — is operator prior art from a separate knowledge base of 86 hand-curated `problem`/`solution` cards, where it kept the vocabulary from drifting as the corpus grew.

# Decision

Treat the live lesson corpus as a **retrieval surface**, not an append log, and fix the two fields any selector must key on. Three invariants.

**1. The live corpus holds schema-v2 rows only.** Legacy rows move to `lessons/archive/`, the path rotation already writes to. They remain readable; they leave the selection set. A row without `problem`, `solution` or a title cannot be ranked by any mechanism, and its presence taxes every consumer that must learn to skip it.

**2. A title is a selection key and is gated at mint.** It must name the condition that triggers the lesson, not the genre of the card. `lesson_v2` already runs `validate_lesson`, `solution_is_meaningful`, `anecdote_only` and `mint_quality_reason`; the title check joins that gate. A rejection is recorded with its reason — a mint gate that leaves no trace reads exactly like a gate that is passing everything.

**3. `tags` is a controlled topic vocabulary; provenance moves to its own field.** `reflector` on 38 of 41 rows is the writer's name, so tag-based narrowing is unavailable today. The glossary travels with the corpus, and a drift report lists tags used but not defined.

This decision is about composition and keys. It does **not** change the retrieval mechanism, add embeddings, alter minting cadence, move dedup thresholds, change rotation, or touch the 76 markdown lessons and their generated index.

# Consequences

## What gets easier

Both downstream issues become interpretable. A citation count (#1505) over a corpus of titled, topically tagged v2 rows measures the lessons; over today's corpus it measures whether one bag-of-words comparison happened to land. A lexical index (#1481) gains a title field that discriminates and a tag field that narrows, which is the difference between ranking and guessing.

The operator-facing index becomes readable: a list of conditions rather than fifteen repetitions of one phrase.

## What gets harder

Minting can now fail on a card whose `problem`/`solution` pair is sound but whose title is generic. That is a real loss of cards until the generator improves, and it is why the rejection reason must be recorded and counted rather than dropped — the rejection rate is the signal that says whether the gate is calibrated or is simply a second off-switch.

A controlled vocabulary has to be maintained. An uncurated glossary degrades into the same undifferentiated state as a single provenance tag, only with more entries.

## What does not change

`lessons_context` scoring, `_best_card` single-card selection, the reflector recurrence rule, dedup by `keyword_jaccard`, rotation bounds, the markdown corpus, `lessons/index.md` generation, and every existing card's `problem`, `solution`, `evidence` and `seen_count`.

# Alternatives considered

- **Leave legacy rows in place and filter at read time.** Rejected: every consumer then re-implements the filter, and the ones that forget — including any usefulness counter — silently measure the mixed set. The composition problem would be permanent and invisible.

- **Rewrite the 15 generic titles retroactively with an LLM pass.** Rejected as the primary fix: rewriting a title detaches it from the cycle that produced the card, and it does nothing about the next fifteen. The gate at mint is what stops recurrence; a backfill is at most a follow-up once the gate is calibrated.

- **Drop the title and rank on `problem` text alone.** Rejected: the title is what the generated index renders and what an operator scans. Removing it would fix ranking by making the corpus unreadable.

- **Go straight to embeddings.** Rejected on two counts. The precondition is missing — the gateway exposes no embedding route, and the host cannot run a local embedder — and semantic ranking over a corpus that is 72% untitled would rank the same noise more confidently. FTS5 already exists in `existence_index.py` with no new dependency; a clean corpus is the prerequisite for either path.

- **Do nothing until #1505 produces a usage number.** Rejected: #1505 cannot produce an interpretable number from this corpus. The dependency runs the other way.

# Test Contract

- A corpus fixture holding both legacy and v2 rows splits with the v2 count unchanged and the legacy rows byte-comparable to what was removed.
- A card with a genre title is rejected at mint and the rejection reason is recorded; a card with a condition-naming title passes. Both directions are driven.
- A card whose only tag is the writer's name fails the vocabulary check; a card carrying at least one glossary term passes.
- The drift report lists a tag used but not defined, on a fixture that contains one.
- Selection over a fixture with duplicate titles is not asserted to improve — this ADR does not change the selector, and a test claiming otherwise would be measuring the fixture.

# References

- #1507 — the implementing issue.
- #1505 — the citation store has never been written; lesson usefulness unmeasured.
- #1481 — replace the bounded prompt remainder with an FTS5 search pointer.
- #1344 — reject tautological and near-duplicate lessons at the minting gate.
- #1070 — `lessons.yaml` recorded cycle protocol with always-zero metrics.
- #1071 — lesson schema v2: `problem`/`solution`, dedup with `seen_count`, citation-based usage signal.
- #1171 — reflector mint recurrence rule.
- `nanobot/runtime/lesson_v2.py` — mint gate, dedup, related links.
- `nanobot/runtime/lessons_context.py` — `_score_entry`, `_best_card`.
- `nanobot/runtime/existence_index.py` — FTS5 with `bm25()`, no new dependency.
