# Implementation proposal: self-maintained SYSTEM_MAP (#749)

- **Issue:** #749
- **story_id:** `docs/specs/self-evolving-runtime/spec.md`,
  `docs/specs/subagent-bridge/spec.md` (LLM proposer, #707)
- **Depends on:** #748 (evidence-based done-detection). **Related:** #750
  (FTS existence index), #751 (value-link).
- **Status:** implemented in this change.

## Problem

The #707 LLM proposer's entire awareness of the system is: filtered
`goal_text` + the last 15 terminal ledger rows + the last 10 proposed
titles — roughly 2.5 hours of memory at the current ~6-cycles/hour cadence
(`nanobot/runtime/llm_proposer.py`'s `build_context`). Nothing in the loop
shows what already exists in the instance repo's `scripts/` directory (~75
scripts and growing). Confirmed consequence (night of 2026-07-14):
`monitor_memory.py` was created at 05:18Z as a "success", four hours after
`track_memory.py` (01:02Z) had already shipped the same capability under a
different name — the earlier success had scrolled out of the 15-row ledger
window, and the title-level dedup check (word-overlap on the proposed title
text) didn't catch the rename.

## Proposal (openwiki-inspired)

The instance repo carries a deterministic, **non-LLM-generated**
`docs/SYSTEM_MAP.md` that the loop keeps fresh itself, and the proposer's
context is extended to include it.

### `nanobot/runtime/system_map.py` (new module)

- `generate_system_map(selfevo_repo) -> str` — walks `scripts/*.py` (and
  `surfaces/*.py` if present), extracts each file's one-line description
  (AST module docstring first line → first `#` comment line → `(no
  description)` placeholder), and emits:
  - `## Inventory` — one line per script, sorted by path.
  - `## Near-duplicate candidates` — scripts grouped by basename-token
    overlap. Token sets are formed by splitting the basename on `_` and
    keeping 4+-character tokens; two scripts group when their **overlap
    coefficient** (intersection size over the *smaller* set's size, not
    Jaccard-over-union) is >= 0.5. Deliberate choice over plain Jaccard: the
    confirmed failure case, `track_memory`/`monitor_memory`, has token sets
    `{track, memory}` / `{monitor, memory}` — Jaccard-over-union is only 1/3
    (would miss it), the overlap coefficient is 1/2 (catches it exactly at
    the threshold). Grouping is a deterministic union-find so transitive
    near-duplicates (A~B, B~C) land in one group even when A~C alone
    wouldn't clear the threshold.
  - `## Backlog` / `## Completed` — carried over **verbatim** from the
    previous `docs/SYSTEM_MAP.md` if present, so the machinery never
    destroys operator/loop-curated content; omitted entirely when no prior
    map exists.
- `update_system_map(selfevo_repo, state_dir) -> bool` — watermark + no-op
  gate (openwiki pattern), two independent gates so a HEAD-moved-but-
  content-unchanged cycle still costs nothing:
  1. **HEAD gate**: a watermark (`<state_dir>/system_map/watermark.json`:
     `updated_at_utc`, `git_head`, `content_sha256`) short-circuits to a
     no-op (`False`, zero work) when the instance repo's current
     `git rev-parse HEAD` still matches the stored one.
  2. **Content-hash gate**: only after regenerating, if the new content's
     sha256 still matches the watermark, the file is not rewritten and the
     watermark is not updated — a commit that never touched `scripts/`/
     `surfaces/` (docs, config, etc.) costs one cheap `git rev-parse` and
     nothing else on every subsequent cycle until content actually changes.
  - Does **not** git-commit — the loop's own cycle commits changes; this
    module is a pure file-write, consistent with every other fail-open
    helper in this codebase (`_deterministic_planner_enabled`-style: no new
    side channel).
  - Fully fail-open: a missing repo, a non-git directory, a `git`
    failure/timeout, or a write failure all degrade to `False`, never raise.

### Wiring into `nanobot/runtime/llm_proposer.py`

- `build_context` appends a bounded `## Existing scripts (do not duplicate —
  extend or skip instead)` section after the existing goal/ledger/rejected-
  titles sections: reads `docs/SYSTEM_MAP.md`'s `## Inventory` lines if the
  file exists, else generates the inventory directly via `system_map`
  helpers (still zero LLM calls either way). Capped independently of the
  existing `_MAX_CONTEXT_CHARS` (8000 as of #826): a new `_MAX_INVENTORY_CHARS` (4000)
  bounds the section's own size, and `_MAX_INVENTORY_ENTRIES` (90) caps the
  entry count (falling back to the 90 most-recently-modified scripts by
  `st_mtime`, with a total-count note) — so a large inventory never eats
  into the goal_text budget by truncating the base context.
- `maybe_propose` calls `update_system_map` once at the top, wrapped in its
  own `try`/`except` (defense-in-depth on top of the module's own fail-open
  behavior) — **before** the `should_propose` kill-switch/novelty gate, so
  the map stays fresh every cycle regardless of whether the LLM proposer
  itself is enabled. This is possible because `bridge.py` already calls
  `llm_proposer.maybe_propose` unconditionally every cycle (see the
  existing comments at each of its four call sites) — the cheapest
  once-per-cycle hook available without touching `bridge.py` at all. The
  watermark no-op gate keeps this free in the common case (HEAD unchanged
  since the last update).
- The proposer system prompt gains one sentence: do not propose a script
  that duplicates an existing one under a different name — extend the
  existing file or pick a different task.

## Alternatives considered

- **LLM-summarized map** (have an LLM write the inventory/descriptions):
  rejected — adds cost and non-determinism to something that is a pure
  filesystem fact; docstrings/comments already carry the intent, and a
  deterministic extractor can't drift or hallucinate.
- **Git-commit the map from this module**: rejected — the loop's own cycle
  already owns commits; a second commit path here would double the
  surfaces that can produce a commit and complicate the existing
  materialize→verify→integrate accounting (R33/R34,
  `docs/specs/self-evolving-runtime/spec.md`).
- **Plain Jaccard-over-union for near-duplicate grouping**: rejected — does
  not clear 0.5 for the confirmed two-token failure case (see above); the
  overlap coefficient does, without over-grouping unrelated multi-token
  names (tested in `tests/test_system_map.py`).
- **Wiring the update call into `bridge.py` directly**: rejected per the
  issue's own guidance — `maybe_propose` is already bridge.py's single
  unconditional per-cycle entry point into this module, so no bridge.py
  edit is needed at all.

## Verification

- New `tests/test_system_map.py`: docstring/comment/none description
  extraction, near-duplicate grouping (positive, negative, and 3-way
  transitive cases), Backlog/Completed carry-over, both no-op gates
  (HEAD-unchanged and content-hash-unchanged), fail-open on a missing repo /
  non-git directory / write failure.
- Extended `tests/test_llm_proposer.py`: inventory section present when a
  map file exists, generated directly when it doesn't, absent gracefully
  when there's no repo or no scripts, entry-count cap, char cap; plus
  `maybe_propose` invokes the system-map update unconditionally (even with
  the proposer kill-switch off) and survives an injected `update_system_map`
  failure.
- Full suite: `.venv/bin/python -m pytest tests/ -q` — all green except two
  pre-existing, environment-dependent failures in
  `tests/test_cycle_health_summary.py` (systemd-dependent severity/exit-code
  assertions, unrelated to this change and present on `main` before it).
- `ruff check nanobot/ tests/` — no new findings introduced by this change
  (pre-existing repo-wide findings unrelated to the touched files are
  unchanged).

## Rollout / rollback

No kill-switch: `system_map.update_system_map` is unconditionally called
from `maybe_propose`, but it is itself a no-op whenever the instance repo's
HEAD hasn't moved, and every failure mode degrades to "skip this cycle's
update" rather than raising — so turning this on carries the same
fail-open safety profile as the rest of the proposer's plumbing (R32,
`docs/specs/subagent-bridge/spec.md`). Rollback, if ever needed, is a plain
revert of this change; no state migration, since the only new state is the
watermark file and the generated `docs/SYSTEM_MAP.md`, both disposable.

## Follow-up: ownership deferral (#749 live observation)

**Problem observed live** (deployed release
`20260714T123819Z-canonical-a0e8552`): the autonomous instance's own P13
cycle seeded (via goal_text) `scripts/generate_system_map.py`, a *second*
generator that writes the exact same `docs/SYSTEM_MAP.md` in a richer
thematic format (header: "It is automatically generated by
`scripts/generate_system_map.py`."). Our `update_system_map` only carries
`## Backlog`/`## Completed` verbatim across regeneration — every other
section, including the instance's thematic structure, would be clobbered
the next time this module regenerates after a HEAD change. Two generators,
one file, no coordination.

**Operator decision**: our module defers entirely to a foreign generator
that already claims the file. We do not try to merge formats or race to
write first.

**Fix**:

- New `system_map._map_has_foreign_generator(existing_content)`: true iff
  non-empty content lacks our own `_GENERATED_NOTE` marker line (the exact
  text `generate_system_map` always emits). An absent or empty file is
  explicitly *not* foreign — nothing to defer to yet, so we adopt it as
  before. This detects any foreign writer (hand-edited or
  `generate_system_map.py`-produced) without hardcoding a filename.
- `update_system_map` now runs this check first, before Gate 1's HEAD
  comparison and before any regeneration work: if the on-disk file is
  foreign, return `False` immediately, write nothing, and do not touch the
  watermark. This costs one small file read every cycle even on the
  otherwise-cheapest HEAD-unchanged path — accepted as the price of never
  clobbering a foreign map.
- `llm_proposer._system_map_inventory_section` already fell back to direct
  generation when the map file was absent; it now also falls back whenever
  parsing the on-disk file yields no `## Inventory` lines (foreign format,
  or — rarely — our own format with an empty section), so the proposer's
  inventory context never silently disappears just because the file on
  disk changed shape underneath it.

**Tests**: `tests/test_system_map.py::TestMapHasForeignGenerator` (marker
absent/present cases) and `TestUpdateSystemMap` additions (foreign map left
byte-identical + watermark untouched even after HEAD moves; our-marker map
still regenerates normally; absent file still adopted);
`tests/test_llm_proposer.py::test_foreign_format_map_falls_back_to_direct_generation`
(inventory section still populated, sourced from direct generation, when
the on-disk map is foreign-format).

See `docs/specs/subagent-bridge/spec.md` R34 for the corresponding spec
clause.
