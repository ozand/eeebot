# Change: coordinator-lane artifact consumption audit — delete write-only artifacts

- **change-id:** 864-artifact-consumption-audit
- **issue:** #864
- **capability:** self-evolving-runtime (state artifacts written by
  `nanobot/runtime/coordinator.py` and friends)
- **role / workstream:** runtime maintenance / product simplicity

## Problem

The self-evolving coordinator cycle writes a wide set of JSON/JSONL artifacts
into the runtime state tree on every cycle. Some of these are pure write-only
dead weight: no production code (dashboard, CLI, bridge, or another cycle
phase) ever reads them back, they only accumulate on disk and inflate the
live host's file count. Precedent: #747 deleted ~900 LOC of a similarly
dead deterministic-planner lane. Product-simplicity principle: delete dead
weight, never touch alive paths.

## Audit — writer, readers, verdict

An initial candidate list of 9 artifacts was proposed. Each was
re-verified against the actual call graph (grep for real readers — file
opens/`_safe_read_json`/`.exists()` checks that gate behavior — not just the
literal write call), because two of the nine turned out to have real,
non-test production readers the initial pass missed (both patterns dereference
a *path stored inside another alive JSON blob*, e.g. `source_artifact` /
`accepted_record_path`, which a naive grep for the artifact's own directory
name does not surface). The corrected table:

| # | Artifact | Writer | Readers found (production, non-test) | Live count (measured 2026-08-16) | Verdict | Action taken |
|---|---|---|---|---|---|---|
| 1 | `experiments/contracts/{id}.json` | `coordinator.py` (`run_self_evolving_cycle`, was ~L904-908) | none | 8252 | write-only | **Deleted the write.** `contract` dict stays embedded in `experiments/latest.json`/`experiments/{id}.json`/`reports/evolution-*.json` unchanged; the `contract_path`/`revert_path` string fields were REPOINTED at the experiment record `experiments/{id}.json` (Opus review: the old strings named files that no longer exist — the CLI `state.py:1243` renders `contract_path`, so it must point at a real file). |
| 2 | `experiments/reverts/{id}.json` | `coordinator.py` (was ~L909-914) | none | 318 | write-only | **Deleted the write.** Same reasoning as #1 — `revert` dict/`revert_path` string stay embedded in the alive experiment JSON. |
| 3 | `experiments/history.jsonl` | `coordinator.py` (was ~L923-924, append) | none | n/a (jsonl) | write-only | **Deleted the append.** `experiments/latest.json` write (coordinator.py, next line up) is untouched, byte-identical. |
| 4 | `credits/history.jsonl` | `cycle_persist.py:_write_credits_ledger` (was L201-202, append) | none | n/a (jsonl) | write-only | **Deleted the append.** `credits/latest.json` write (line above) is untouched, byte-identical. |
| 5 | `improvements/materialized-*.json` | `cycle_planning.py:_write_materialized_improvement_artifact` (~L1103) | **`cycle_planning.py:1849`** (`_safe_read_json(Path(materialized_improvement_artifact_path))`, reads `task_id` back to route lane transitions) | 1123-ish | **ALIVE — initial audit was wrong** | **Not deleted.** The artifact's own directory is never grepped by name at the read site — the path arrives as a string forwarded through `current_plan`/`task_plan` across cycles and is only read via a variable, so a literal-string grep for `improvements/` at the read site misses it. |
| 6 | `improvements/llm-proposed-*.json` | `llm_proposer.py:write_request` (~L1625) | **`bridge.py:1017-1035` and `bridge.py:1494-1498`** (`Path(source_artifact).read_text()`/`json.loads`, builds the actual subagent task prompt from this file's `next_bounded_candidate`) | — | **ALIVE — initial audit was wrong** | **Not deleted.** Same blind spot as #5: the request payload stores the artifact path under `source_artifact`; the bridge dereferences that field by variable, not by a literal path-prefix grep. |
| 7 | `promotions/accepted/{id}.json` | `promotion.py:review_promotion_candidate` (L279, on `decision == "accept"`) | **`state.py:1291,1309-1314`** (`load_runtime_state_from_root`, read for `accepted_at_utc`/`patch_bundle_path`, feeds `promotion_replay_readiness` in the control-plane summary) | 233 | **ALIVE — initial audit was wrong** | **Not deleted.** `review_promotion_candidate` is still called from the operator CLI (`nanobot/cli/commands.py:1117`); "0 written since Aug 1" reflects that no promotion has been operator-accepted recently, not dead code. |
| 8 | `promotions/readiness_packets/{id}.json` | `promotion.py:complete_promotion_readiness_packet` / `supply_missing_promotion_readiness_inputs` (L71, L168) | **`state.py:1292,1295-1302`** (read every time the control-plane summary/dashboard is built) | — | **ALIVE — initial audit was wrong** | **Not deleted.** Both writers are called from `coordinator.py:610,616` on *every* cycle that lands `not_ready_for_policy_review` — this is a hot, per-cycle production path, not dead code. |
| 9 | `promotions/patches/{id}.json` | `promotion.py:review_promotion_candidate` (L270-271, on accept) | **`state.py:1324-1325`** (`Path(promotion_patch_bundle_path).exists()` gates `promotion_replay_readiness` state between `'ready'` and `'patch_bundle_missing'`) | — | **ALIVE — initial audit was wrong** | **Not deleted.** Existence alone is load-bearing for the control-plane summary's promotion-replay state; deleting the write would silently and permanently flip that state to "blocked: patch_bundle_missing" for every future accepted candidate. |

**Net effect vs. the original 9-item proposal: 4 write-only artifacts deleted
(1-4), 5 reclassified ALIVE and left untouched (5-9).** Re-auditing #864's
premise against the call graph — not just the initial grep pass — is exactly
the kind of verification the issue's own acceptance criteria called for
("VERIFY the enclosing function's remaining callers before deleting").

## Also noted (no writer/reader anywhere)

`confirmation_status.json` has no writer and no reader anywhere in `nanobot/`
— it is an instance-vanity artifact the harness never reads. Left alone (no
writer exists to delete); flagged here for visibility only.

`promotions/{id}.json` carries a large legacy volume (~8130 files) but the
path itself is alive — `bridge.py:2745` (#812 candidates), `promotion.py:32`,
`state.py:1290` all read it — so the volume is retained rather than pruned.

## Bounded-growth fix (independent of the delete/keep audit above)

`reports/evolution-*.json` is alive (read by `cycle_planning.py:839`'s
`_recent_report_streak`, which looks at the 10 newest by mtime, and by
`state.py:917`'s `load_runtime_state_from_root`, which looks at only the
single newest) but was unbounded — every cycle wrote a new report and
nothing ever pruned old ones (8143 files live at audit time). Added
`REPORTS_RETENTION_KEEP = 200` (a module constant in `coordinator.py`, far
above the 10-file reader window) and `_prune_stale_reports()`, called right
after the `report_path.write_text(...)` in `run_self_evolving_cycle`. The
sweep only targets the `evolution-*.json` naming this module writes, is
wrapped fail-open (never raises), and deletes oldest-by-mtime beyond the
keep threshold.

## Acceptance

- [x] `experiments/contracts/`, `experiments/reverts/`, `experiments/history.jsonl`,
      `credits/history.jsonl` writers deleted; `experiments/latest.json` and
      `credits/latest.json` writes byte-identical to before.
- [x] `improvements/materialized-*.json`, `improvements/llm-proposed-*.json`,
      `promotions/accepted/`, `promotions/readiness_packets/`,
      `promotions/patches/` writers left untouched (re-audit found real
      production readers).
- [x] `reports/evolution-*.json` retention sweep added (`REPORTS_RETENTION_KEEP = 200`).
- [x] Tests updated: coordinator BLOCK-path test now asserts
      `experiments/contracts/`, `experiments/reverts/`, `experiments/history.jsonl`
      are never created; discard/revert test asserts the same for the revert
      file; credits test asserts `credits/history.jsonl` is never created;
      new unit test for the retention sweep.
- [x] Full test suite run; no new failures vs. base commit.

## Out of scope

- Re-verifying `promotions/decisions/`, `promotions/latest.json`,
  `promotions/{id}.json`, `experiments/latest.json`, `credits/latest.json`,
  `control_plane/current_summary.json`, `reports/evolution-*.json` itself,
  `research/*`, `hypotheses/*`, `goals/*`, `outbox/*` — all previously
  confirmed alive and unchanged here except the reports retention sweep.
- Deleting the 5 artifacts reclassified ALIVE above. If a future task wants
  to actually remove that functionality, it must be a deliberate behavior
  change to the control-plane summary / bridge task-prompt construction /
  cycle_planning lane routing, reviewed as such — not a "write-only cleanup".
