# Change: FTS5 existence index for semantic task dedup

- **change-id:** 750-existence-index
- **issue:** https://github.com/ozand/eeebot/issues/750
- **capability:** `docs/specs/self-evolving-runtime/spec.md` (dedup section)
- **depends on:** #748. related: #749 (system map), #751 (value-link)

## Problem

Every pre-spawn dedup check in the self-evolving loop is title-**text**
matching:

- `_task_already_done` / `_task_already_done_for_path` (#736) — greps the
  proposed title against the instance repo's git log.
- `_recent_failure_match` (#716) — a bounded-recency keyword-overlap scan of
  recent bridge results.
- the #712 completed-filter — matches title words against commit lines to
  strip already-done "Current priority target" entries.

All three require the words in a NEW proposal's title to literally overlap
words already seen in a past commit subject or result title. They cannot
catch a **semantic** near-duplicate whose wording differs. Live evidence,
the night of 2026-07-14: the loop shipped `track_memory.py`, then later the
same night shipped `monitor_memory.py` as a separate "success" — same
capability, different words ("track"/"log" vs. "monitor"/"RAM"). The same
pattern repeated for `check_cpu_governor.py` / `monitor_cpu_status.py` and
`disk_monitor.py` / `benchmark_disk_io.py`. Each pair burned a full subagent
spawn, review, and merge cycle on work that already existed.

## Intended change

A new, additive pre-spawn check, `nanobot.runtime.existence_index`: a local
**existence index** built with Python's stdlib `sqlite3` FTS5 extension (no
new dependency — required, since the eeepc host is stdlib-only).

- **Corpus**: script filenames + first docstring line from
  `<selfevo_repo>/scripts/*.py` and `<selfevo_repo>/surfaces/*.py`; past
  attempt titles from `<state_dir>/subagents/results/*.json`
  (`backlog_title`/`task_title`, bounded to the 500 most-recently-modified
  files); hypothesis titles from `<state_dir>/hypotheses/backlog.json` and
  `<state_dir>/research/hypotheses.json`. Content-addressed (`content` table
  keyed by `sha256(text)`) with a soft-delete `active` flag on `documents`,
  so re-indexing is a cheap incremental diff, not a full rebuild, on every
  cycle.
- **Matching**: FTS5/BM25 narrows candidates by an OR-query of the
  proposal's 3+-character words plus the target path's tokens; a
  duplicate-suspect decision is then made in plain Python over those
  candidates — a `script`-kind hit counts as duplicate-suspect iff it shares
  at least 2 of its 4+-character content words (generic words stripped) with
  the proposal. No LLM, no embeddings.
- **Wiring**: one new `elif` branch in the bridge's existing pre-spawn dedup
  sequence (`nanobot/runtime/bridge.py`), placed after the two existing
  exact/keyword checks and before the fall-through "proceed" branch. On a
  duplicate-suspect script hit, the request is skipped exactly like the
  existing `skipped_duplicate` branches (same `handled_marker`, same
  `_write_bridge_completed_result` / `record_dedup_decision` /
  `record_cycle_outcome` / `_tag_cycle_post` bookkeeping), with
  `matched_against = f'existence-index:{path}'` so `#705`-style dedup
  false-positive-rate measurement can distinguish this gate from the others.
- **Kill switch**: `SELFEVO_EXISTENCE_INDEX_ENABLED`, default `"1"`
  (anything but the literal `"0"` keeps it on) — mirrors the deterministic
  planner's kill-switch pattern (#739, itself since retired along with the
  planner in #747).
- **Fail-open**: every public function in the module swallows its own
  exceptions and a missing/corrupt DB file is dropped and rebuilt from
  scratch. `find_duplicate_script` (the bridge's single call site) never
  raises — any internal failure is indistinguishable from "no match".

### Excluded case: same target path

A hit whose `path` is exactly the proposal's own `target_path` is
deliberately **not** flagged by this index — that "does this exact file
already exist" case is already the job of the narrower, git-scoped
`_task_already_done_for_path` check (#736). The existence index's job is
catching a *different* existing artifact that duplicates the same intent
(`track_memory.py` found while proposing `monitor_memory.py`), not
re-litigating the same-file case with a blunter heuristic.

## Acceptance

- [x] `monitor_memory` proposed (title "Create a script to monitor RAM and
      memory usage", target `scripts/monitor_memory.py`) while
      `track_memory.py` exists → `find_duplicate_script` returns
      `scripts/track_memory.py`; wired into the bridge, the cycle's ledger
      `dedup` row records `decision=skipped_duplicate`,
      `matched_against=existence-index:scripts/track_memory.py`.
- [x] A genuinely new theme ("generate a markdown changelog") does not flag
      an unrelated existing script.
- [x] Index update (`reindex`) measured well under 1s on the eeepc — a
      synthetic 100-script tree reindexes in ~13ms cold, ~7ms warm
      (incremental, all-unchanged) on commodity hardware.
- [x] Unit tests cover: schema creation, rebuild-on-corrupt, incremental
      reindex (unchanged-hash skip, changed-content reindex, deleted-file →
      `active=0`), the acceptance positive/negative pair, `ledger_title`/
      `hypothesis` corpora, the kill switch, fail-open on missing
      state/repo dirs, and the same-target-path exclusion. Plus a
      bridge-level integration test (seeded fake request + pre-existing
      near-duplicate script) asserting the `skipped_duplicate` ledger row
      and the `existence-index:` prefix, and a kill-switch bridge test
      proving the branch is inert when disabled.

## Out of scope

- Vector/embedding search (qmd's full recipe has a documented path if BM25
  ever proves insufficient — not needed at this corpus size).
- Injecting "closest existing artifacts" into the subagent's proposer
  context (the issue's "Proposer context" idea) — this change only wires
  the pre-spawn dedup **gate**, not proposer-side evidence. Left for a
  follow-up if the gate alone doesn't move the false-positive rate enough.
- Any change to the two existing text-matching gates
  (`_task_already_done*`, `_recent_failure_match`) — this is a pure
  addition alongside them.
- Touching `nanobot/runtime/llm_proposer.py` (owned by concurrent work on
  #750's dependency chain during implementation).
