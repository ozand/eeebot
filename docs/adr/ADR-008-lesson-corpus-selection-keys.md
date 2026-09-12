---
title: The live lesson corpus is a retrieval surface, and titles and tags are its selection keys
status: proposed
date: 2026-09-12
authors: [eeebot maintainers]
related: ["#1507", "#1505", "#1481", "#1344", "#1070", "#1071", "#1171", "#1511", "#1515", "#1518"]
tags: [runtime, lessons, knowledge, retrieval]
---

# Status

Proposed — filed with #1507, ahead of any implementation.

**Revision note (2026-09-12):** this record replaces the version filed as PR #1508, which is closed in favour of this one. The decision is unchanged. Three claims in the original argument did not survive reading the current code and were corrected: the selector's actual title-backfill mechanism, the corpus's real size, and the grounds for invariants 1 and 3. See each section below for what changed and why; the short version is that legacy rows turn out to be rankable today (the original claim that they can't was wrong), and tags are not currently read by the selector at all (the original claim that they already are a selection key was also wrong) — so both invariants are re-derived from what the code actually does, not deleted, because a real problem remains in each once correctly diagnosed.

# Context

The lesson pipeline has three halves that were built separately and are now in different states.

**Minting works, and keeps improving under direct measurement.** #1070 established that `lessons.yaml` was recording cycle protocol with always-zero metrics rather than lessons. #1071 replaced it with a schema-v2 card — `problem`/`solution`, severity, evidence, recurrence — and #1171 added reflector-side minting with a recurrence rule. #1515, filed against this very review, replaced the reflector mint's title with one derived per-card from the observed condition (`nanobot/runtime/knowledge_curator.py:1464`: *"The title is a selection key: derive it from this card's observation and recommendation, never from the recommendation kind"*) — direct evidence that stating the mechanism precisely, and being wrong about it, is corrected rather than argued with once someone reads the code. A card in the live corpus today reads:

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

**Composition does not.** Counted directly against `ozand/eeebot-self-evolving @ origin/main` (`25e504b0`), read-only, from a detached worktree:

```bash
$ git worktree add --detach /tmp/selfevo-count origin/main
$ python -c "
import yaml
data = yaml.safe_load(open('lessons/lessons.yaml', encoding='utf-8'))
rows = data if isinstance(data, list) else data.get('lessons', data)
print('lessons.yaml:', len(rows))
print('  schema_version==2:', sum(1 for r in rows if isinstance(r, dict) and r.get('schema_version') == 2))
"
lessons.yaml: 145
  schema_version==2: 41
$ python -c "
import yaml
data = yaml.safe_load(open('lessons/errors.yaml', encoding='utf-8'))
rows = data if isinstance(data, list) else data.get('errors', data)
print('errors.yaml:', len(rows))
"
errors.yaml: 44
```

```text
lessons.yaml                      145  (41 schema_version:2, 104 legacy)
errors.yaml                        44
────────────────────────────────────────
retrieval-surface corpus          189

v2 distinct titles                  27 across 41 rows
  "Reusable corrective approach"    15 rows — all pre-#1515 mints; #1515
                                     changed only future mints, by design
                                     (no rewrite of history)
v2 rows tagged reflector            38 of 41
  of those, tags == ["reflector"]   33 — no topic tag at all
legacy rows: distinct titles       104 of 104, after normalization (below)
legacy rows: approach field        104 of 104 are a bare commit tally
                                     ("Committed N commit(s): <path>"),
                                     never a description of what was done
```

**145 is `lessons.yaml` alone — the original record's Context section stated it bare, with no scope, and it was read as the corpus size.** `lessons_context.py`'s selector reads `lessons.yaml` and `errors.yaml` in parallel (`build_lessons_context`, `nanobot/runtime/lessons_context.py:244` for errors, `:274` for lessons); the retrieval-surface population this ADR's invariants act on is the sum, 189, not 145. `errors.yaml` was not omitted because it does not matter — it is scored by the exact same mechanism (`_score_entry`/`_best_card`, secondary field `root_cause` instead of `approach`) and was simply left out of the original count.

Separately: `nanobot.runtime.knowledge_curator.iter_lessons` reads a *third*, much larger and non-overlapping population — `lessons/archive/*.yaml.gz` plus a live `reflections.jsonl` under the runtime `state_dir` — for a different purpose (candidate collection for minting/dedup decisions, not executor retrieval). It is not this ADR's corpus and is not counted above. It is relevant to invariant 1 for one specific reason: #1511 found that `lessons/archive/lessons-2026-08-25.yaml.gz` — the exact directory invariant 1 proposes writing 104 more rows into — holds 414 lessons in a structurally invalid YAML document that `yaml.safe_load` cannot parse at all. Re-run against the same `origin/main` snapshot, today:

```bash
$ python -c "
from nanobot.runtime.knowledge_curator import iter_lessons
diagnostics = []
n = sum(1 for _ in iter_lessons('/tmp/selfevo-count', diagnostics=diagnostics))
print(n, diagnostics)
"
189 [{'path': 'lessons-2026-08-25.yaml.gz', 'status': 'unavailable', 'reason': 'parse'}]
```

189 — archive + live, with the archive contributing zero readable rows and one reported failure. PR #1513 (merged, closing #1511) made that failure *visible*; it did not repair the file. **The 414 archived lessons are still unreadable today.** Invariant 1 below states the constraint this leaves it under.

**Selection is a single card by word overlap, and normalization runs before it — reading it, not the schema, is what "titles are a selection key" actually means.**

```text
lessons_context.py:169-175   _capped_entries -> [_normalize_entry(e) for e in entries]
lessons_context.py:154-159   _normalize_entry: absent title <- quoted span inside
                              `hypothesis` if one exists, else the full
                              `hypothesis` text, capped to 200 chars (#1518)
lessons_context.py:185       _score_entry reads entry["title"] (+ "category")
lessons_context.py:190       score = 2 * len(primary_shared) + len(secondary_shared)
                              — title/category weighted DOUBLE vs. approach/root_cause
```

Normalization (`_capped_entries`, called before any scoring happens) means the selector never sees an empty title. A lesson with no stored `title` is scored on a backfilled one instead — since #1518, the *quoted span* inside its `hypothesis` field when one exists (not a blind 200-character prefix of the raw sentence, which is what an earlier version of this record, and the code before #1518, both did). So "titles are a selection key" is correct, but the failure mode this record originally described — "untitled lessons are invisible" — is not what happens. An untitled lesson participates and ranks on a manufactured key, which is a different, and in the case of this corpus's 104 legacy rows, not even the problem: every one of them carries a `hypothesis` field with a quoted span, so every one of them gets a real, specific, and — checked directly — **fully distinct** backfilled title (104 of 104, zero collisions; contrast the 15-row collision among the 41 *titled* v2 rows above). What every one of those 104 rows lacks instead is a usable `approach`: `_normalize_entry` backfills it from `result`, and every legacy `result` is a bare commit tally (`"Committed N commit(s): <path>"`) — never a description of what was done or why. That field is read by `_score_entry` as the secondary key, and it is the field the executor prompt renders verbatim when a legacy row wins.

**`tags` is not read by the selector at all today.** `grep tags nanobot/runtime/lessons_context.py` returns nothing. `_score_entry` reads `title`, `category`, and the secondary field (`approach`/`root_cause`) — never `tags`. The retrieval surface this record is about does not narrow, rank, or filter on tags in its current form; nothing downstream of `build_lessons_context` sees them. What *does* exist is a mint-time quality gate, unrelated to retrieval: `nanobot/runtime/lesson_v2.py:88-90` (`validate_lesson`) already requires every v2 card's `tags` to be a non-empty list drawn from `CONTROLLED_LESSON_TAGS` (`nanobot/runtime/schemas.py:7-11`) — a controlled vocabulary that has existed since #1071 introduced the v2 schema, not something this record is proposing to create. That vocabulary includes `"reflector"` alongside genuine topic terms (`"architecture"`, `"config"`, `"git"`, `"infra"`, `"lint"`, `"perf"`, `"prompt"`, `"security"`, …) — a provenance label sitting inside a topic vocabulary — and the reflector mint path hardcodes exactly that one entry, unconditionally, for every card it writes: `nanobot/runtime/knowledge_curator.py:1493`, `"tags": ["reflector"]`, no topic tag, ever. 38 of the 41 v2 rows carry `"reflector"`; 33 of those carry nothing else. The vocabulary the gate already enforces is real; what it enforces on the reflector path is a provenance stamp that trivially satisfies a schema check without adding any topical signal, on the majority of the corpus.

Two open issues consume this corpus and were originally said to be downstream of its composition; both claims were checked against the code and neither holds as stated, though one survives for a different reason:

- **#1505 (citation-based usefulness) is not blocked by title composition.** Citations are keyed on lesson `id` (`lesson_v2.py`'s `record_citations`, pattern `\[Lesson\s+([A-Za-z0-9_-]+)\]`), and the id is always present and always unique — a citation naming `LESS-REF-16004180ba43-4e57` cannot be confused with any other row regardless of what its title says. #1505 *is* affected by this corpus, but one hop upstream of citation counting: `_best_card`'s tie-break (`lessons_context.py:212`, a strict `>` that lets the first-encountered, i.e. newest, same-score entry stand) decides which of the 15 identically-titled cards is ever shown to the model at all, and therefore which one has any chance of being cited. The other 14 are structurally unreachable regardless of their own content. A citation counter measures what the tie-break let through, not "the selector's luck" in some looser sense — the mechanism is the tie-break, not the citation store.
- **#1481 does not consume this corpus.** #1481 is scoped to `memory/**/*.md` (`nanobot/agent/memory.py`'s prompt remainder) and does not read `lessons.yaml`, `errors.yaml`, or `lessons/index.md`. It is not downstream of this record and should not wait on it.

The pattern this decision adopts — a corpus that carries its own controlled tag glossary, plus a report of tags used but not defined — is operator prior art from a separate knowledge base of 86 hand-curated `problem`/`solution` cards, where it kept the vocabulary from drifting as the corpus grew.

# Decision

Treat the live lesson corpus as a **retrieval surface**, not an append log, and fix the fields the selector actually keys on — title, verified above; the `approach`/`root_cause` secondary field that stands in for it on legacy rows; and the tag vocabulary, verified above to already exist but to be diluted by a provenance label. Three invariants.

**1. The live corpus holds schema-v2 rows only; legacy rows move to `lessons/archive/`, and the move is contingent on the archive being able to hold them.** Not because legacy rows lack a title — checked directly, they don't; all 104 have one, and it is unique across all 104. The reason is what they lack instead: a `problem`/`solution` pair, `seen_count`/recurrence tracking, and — the concrete, measured defect — a non-boilerplate `approach`. Every one of the 104 legacy rows' `approach` field (backfilled from `result`) is a bare commit tally, never a description of what was done. A row that ranks on a real, specific title and then hands the executor "Committed 1 commit(s): lessons/avoid_state_report_inspection.md" as its corrective approach is worse than an untitled row that simply can't compete: it wins the selection and then delivers nothing. Archived, they remain readable — but only to `knowledge_curator.iter_lessons`'s candidate scan, which already walks `lessons/archive/*.yaml.gz`; the retrieval selector this record is about never reads archives, live or otherwise, so archiving costs it nothing it could use. **Precondition, not yet satisfied:** `lessons/archive/lessons-2026-08-25.yaml.gz` already holds 414 unparseable lessons today (#1511), and the writer that would receive these 104 must not reproduce that shape. This invariant does not ship until the archive writer is verified against a real round-trip read, not merely a successful write.

**2. A title is a selection key and is gated at mint.** It must name the condition that triggers the lesson, not the genre of the card. `lesson_v2` already runs `validate_lesson`, `solution_is_meaningful`, `anecdote_only` and `mint_quality_reason`; the title check joins that gate for callers that still supply one derived from `kind` rather than content. #1515 already fixed this for the reflector path specifically (`title = problem.strip()`, `knowledge_curator.py:1471`) — this invariant is the general gate that keeps that fix from being an isolated patch the next new mint path can bypass. A rejection is recorded with its reason — a mint gate that leaves no trace reads exactly like a gate that is passing everything.

**3. Provenance moves out of `tags`, into its own field; `tags` keeps the controlled vocabulary it already has, minus the entry that isn't a topic.** `CONTROLLED_LESSON_TAGS` and its mint-time enforcement (`validate_lesson`) already exist and are not being created here. What changes: `"reflector"` is removed from the controlled set (it names who minted the card, not what it is about) and the reflector mint path (`knowledge_curator.py:1493`) stops hardcoding it as the card's only tag, gaining its own `source`/`provenance` field instead and being required to supply at least one real topic tag from the (now purely topical) vocabulary. A drift report lists any tag present on a corpus row but absent from the current vocabulary — meaningful today only for legacy rows and generic-KB-sourced records, since v2 mint-time validation already rejects anything else. This invariant does **not** make tags a selection key for the current retrieval mechanism — nothing in `lessons_context.py` reads them, and this record does not change that. It prepares the field for a retrieval mechanism that might read it later (an FTS5 index, following the pattern already used for scripts and ledger titles in `existence_index.py`) without pretending that mechanism exists yet.

This decision is about composition and keys. It does **not** change the retrieval mechanism, add embeddings, alter minting cadence, move dedup thresholds, change rotation, or touch the markdown lessons and their generated index (`lessons/index.md` is regenerated per cycle and is not tracked in `origin/main`, so its count is not re-verified here alongside the YAML corpus above).

# Consequences

## What gets easier

Both downstream issues become interpretable, for the reasons corrected above, not the ones originally stated. #1505's citation count, once it exists, will reflect the tie-break's actual output — a corpus where the 15-way collision is gone means the tie-break stops arbitrarily picking one of 15 near-identical cards, so a citation genuinely reflects which specific lesson got shown. #1481 is unaffected either way; it was never downstream of this.

The operator-facing index becomes readable: a list of conditions rather than fifteen repetitions of one phrase, and legacy rows that DO carry good titles keep being visible in whatever reads the archive for candidate purposes, without also winning executor-prompt selection on the strength of a title attached to a content-free approach.

## What gets harder

Minting can still fail on a card whose `problem`/`solution` pair is sound but whose title names a genre — #1515 narrowed this for the reflector path specifically; invariant 2 is the general backstop for any path that reintroduces the same shape. That is a real loss of cards until each generator is confirmed not to do this, and it is why the rejection reason must be recorded and counted, not dropped — the rejection rate is the signal that says whether the gate is calibrated or is simply a second off-switch.

Invariant 1 is now conditional on the archive writer, not merely on deciding to write. `lessons/archive/` already holds one unparseable 414-entry file; the same rotation/archival code path is presumably what would have written it, and the same path is what would receive these 104 rows. Verifying the write is real work this record adds that the original version did not account for.

A controlled vocabulary has to be maintained even after removing one bad entry; an uncurated glossary degrades into the same undifferentiated state as a single provenance tag, only with more entries, and 33 of 41 v2 rows already show what "one entry, no topic" looks like in practice.

## What does not change

`lessons_context` scoring, `_best_card` single-card selection, the reflector recurrence rule, dedup by `keyword_jaccard`, rotation bounds, the markdown corpus, `lessons/index.md` generation, and every existing card's `problem`, `solution`, `evidence` and `seen_count`. `CONTROLLED_LESSON_TAGS`'s existence and mint-time enforcement — only its membership and what writes into it change.

# Alternatives considered

- **Leave legacy rows in place and filter at read time.** Rejected: every consumer then re-implements the filter, and the ones that forget — including any usefulness counter — silently measure the mixed set. The composition problem would be permanent and invisible.

- **Rewrite the 15 generic titles retroactively with an LLM pass.** Rejected as the primary fix, for one of its two original reasons, not both. "It does nothing for the next fifteen" still holds in the sense that a backfill alone never stops recurrence — but #1515 already stopped the reflector path specifically from generating that title again, so the calculus has shifted since this was first written: a backfill today is a pure cleanup of 15 already-frozen rows, not a patch competing against an active, still-firing defect. The originally-stated reason that rewriting "detaches it from the cycle that produced the card" does not hold at all — `evidence` (the cycle-id list) is the provenance field and a title rewrite does not touch it. The gate (invariant 2) is still what stops a *different* mint path from reintroducing the same generic-title shape; a retroactive rewrite of the 15 remains a legitimate, independent follow-up, not a substitute for it.

- **Drop the title and rank on `problem` text alone.** Rejected: the title is what the generated index renders and what an operator scans. Removing it would fix ranking by making the corpus unreadable, and would also discard the one field (verified above) where legacy rows are strong, not weak.

- **Go straight to embeddings.** Rejected on two counts. The precondition is missing — the gateway exposes no embedding route, and the host cannot run a local embedder — and semantic ranking over a corpus whose composition is still unresolved would rank the same noise more confidently. FTS5 already exists in `existence_index.py` with no new dependency; a clean corpus is the prerequisite for either path, and `tags` is being kept clean now specifically so it is ready if that path is chosen.

- **Do nothing until #1505 produces a usage number.** Rejected: #1505's number, once produced, is only interpretable if the tie-break it is downstream of is not still resolving 15-way collisions arbitrarily. The dependency runs from this record to #1505, not from #1505 to this record — confirmed by mechanism (`_best_card`'s tie-break), not merely asserted.

# Test Contract

- A corpus fixture holding both legacy and v2 rows splits with the v2 count unchanged and the legacy rows byte-comparable to what was removed.
- Invariant 1's archive write is round-trip tested: written, then re-read by the same reader `iter_lessons` uses, asserting the row count and content match — not merely that the write call returned without raising. A fixture reproducing #1511's shape (an orphaned top-level `lessons:` key before the first real mapping) is a required negative case.
- A card with a genre title is rejected at mint and the rejection reason is recorded; a card with a condition-naming title passes. Both directions are driven, including a case exercising the general gate (invariant 2) independently of the reflector-specific fix already in `_reflector_card`.
- A reflector-mint card without a topic tag (today's default, per `knowledge_curator.py:1493`) fails validation once `"reflector"` is removed from `CONTROLLED_LESSON_TAGS`; a card carrying a real topic tag plus a separate `source`/`provenance` field passes.
- The drift report lists a tag present on a row but absent from the current vocabulary, on a fixture containing one (legacy or generic-KB shaped, since v2 mint already gates this).
- Selection over a fixture with duplicate titles is not asserted to improve — this ADR does not change the selector, and a test claiming otherwise would be measuring the fixture.
- A test pins that `lessons_context.py` still reads no `tags` field anywhere — this record does not silently turn on a selection behavior it explicitly says it isn't adding.

# References

- #1507 — the implementing issue.
- #1505 — the citation store; corrected above to depend on the selector's tie-break, not on citation keying.
- #1481 — the FTS5 memory-retrieval pointer; corrected above to not depend on this record at all.
- #1511 — 414 archived lessons in an unparseable file; load-bearing precondition for invariant 1.
- #1515 — reflector mint title derived per-card; the fix invariant 2 generalizes.
- #1518 — title backfill uses the quoted hypothesis span, not a blind prefix; the current mechanism this record describes.
- #1344 — reject tautological and near-duplicate lessons at the minting gate.
- #1070 — `lessons.yaml` recorded cycle protocol with always-zero metrics.
- #1071 — lesson schema v2: `problem`/`solution`, dedup with `seen_count`, citation-based usage signal; also the origin of `CONTROLLED_LESSON_TAGS`.
- #1171 — reflector mint recurrence rule.
- `nanobot/runtime/lesson_v2.py` — mint gate (`validate_lesson`, `mint_quality_reason`), `CONTROLLED_LESSON_TAGS` import, related links.
- `nanobot/runtime/lessons_context.py` — `_normalize_entry`, `_score_entry`, `_best_card`, `build_lessons_context`.
- `nanobot/runtime/knowledge_curator.py` — `iter_lessons`, `_reflector_card`.
- `nanobot/runtime/schemas.py` — `CONTROLLED_LESSON_TAGS`.
- `nanobot/runtime/existence_index.py` — FTS5 with `bm25()`, no new dependency.
