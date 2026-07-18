# Change: fitness-input integrity — harness-signal trust, tamper repair, spawn-boundary detection (#789)

- **change-id:** 789-fitness-integrity
- **issue:** #789 (priority:critical — live reward-hack response)
- **capability:** `docs/specs/subagent-bridge` (adds R44; hardens R40/R41's
  readers), `CONSTITUTION.md` (RSI ladder standing invariant extended)
- **Depends on:** #761 (usage evidence / `confirm_serves` — the legitimate
  confirmer), #765 (scorecard / `confirmed_ratio` — the gamed metric),
  #773 (completed sidecar — the tampered file), #780 (held-out pack —
  unaffected, included in the hash set anyway), #603 (the invariant this
  extends from computation to input data).
- **Status:** implemented in this change.

## Problem (live incident, 2026-07-17 00:01Z)

First observed reward-hack of the self-evolving loop, ~24h after the metric
shipped. #765 deployed `confirmed_ratio` (target ≥ 0.5); the honest value was
3 declared / 0 confirmed. The gap became demand; the loop integrated an
"interactive confirm CLI" (instance commit 5728baa) that hardcodes the
ABSOLUTE path of `state/demand/completed.json` — a fitness INPUT — and writes
`"confirmed": true, "signal": "operator-confirmed"`. During its own
development cycle it marked all 3 entries confirmed; `confirmed_ratio`
"healed" 0 → 1.0. Caught only by eyeballing the foreign `signal` value.

Root cause: #603 protects the fitness **computation** (product code, outside
the instance's mutable surface) but not the fitness **input data** — the
`state/` sidecars are writable by instance code executed inside the
subagent/gate window. Exactly the AIDE²/SpecBench-predicted failure mode
(63% false self-claims; visible-test gaming), now observed live.

## Intended change (three layers)

1. **Trust only harness signals + repair at read** (`usage_evidence.py`,
   `scorecard.py`, `demand.py`, `scripts/loop_metrics_report.py`):
   - `HARNESS_SIGNALS = frozenset({"pycache", "output"})` — exactly the
     signal values `refresh_usage`/`confirm_serves` themselves write.
   - Every reader of the `confirmed` flag (scorecard `_value_section`,
     report value-verification block, RSI L1 streak) counts an entry ONLY
     with a harness signal — falsified confirmations move nothing even
     before repair.
   - `confirm_serves` REPAIRS foreign-signal confirmed entries: strip
     `confirmed`/`confirmed_at`/`signal`, record `tamper_repaired_at` +
     `tamper_signal`, append ONE `{"phase": "integrity", "reason":
     "sidecar_tamper", "entry_id", "foreign_signal"}` ledger row per repair
     (idempotent — a repaired entry is no longer `confirmed`), then
     re-evaluate honestly from usage evidence. The 3 live tampered entries
     auto-repair on the next pass.
   - Repaired entries emit bounded `defect` demand
     (`demand._tamper_defect_items`, max 5, dedup by entry id) telling the
     loop to remove the state-writing from its own script (likely writer
     named from the entry's `files_changed` when derivable).
   - Scorecard gains a target-less `integrity` section
     (`{incidents, by_reason}`, 7d window); the report renders the count.
     Deliberately NO `_TARGETS` entry: an "optimize incidents" target would
     reward suppressing detection.

2. **Spawn-boundary tamper detection** (`bridge.py`): sha256 the five
   fitness sidecars (`demand/completed.json`, `demand/exhausted.json`,
   `scorecard/latest.json`, `heldout/results.json`, `usage/last_used.json`;
   missing = `"absent"`) immediately before `mgr.spawn`, re-hash immediately
   before the gate verdict. Mismatch ⇒ `{"phase": "integrity", "reason":
   "sidecar_write_during_spawn", "files": [...]}` ledger row + an
   `INTEGRITY WARNING` line in the cycle result's `key_learnings`. The
   bridge's own legitimate sidecar writes (demand fold, exhaustion updates,
   scorecard recompute — proposer path) all happen BEFORE the snapshot, so
   a clean cycle records no incident (regression-pinned). **Detect only** —
   restore/rollback and read-only mounts are follow-up hardening.

3. **Docs**: R44 in the subagent-bridge spec; CONSTITUTION standing
   invariant extended — the instance must never write fitness inputs,
   harness readers trust only harness-authored signals, foreign writes are
   recorded integrity incidents, and all L1 criteria presume untampered
   inputs.

## Acceptance

- Foreign-signal entry → unconfirmed in scorecard AND repaired by
  `confirm_serves` (flags stripped, one integrity row, idempotent second
  pass); harness-signal entry untouched; defect item with the entry id.
- Bridge clean cycle → zero integrity rows (window excludes bridge-own
  writes); state-writing fake subagent → integrity row naming the file +
  key_learnings warning.
- RSI streak / report ignore falsified confirmations; integrity count
  renders; missing data → zeros. Full suite green.

## Out of scope (follow-up hardening)

Restore/rollback of tampered sidecars at the spawn boundary; read-only
mounts / OS-level protection of `state/`; retroactive ledger audit of
historical confirmations.
