# Implementation proposal: deterministic-planner minting kill-switch (#739)

- **Issue:** #739
- **story_id:** `docs/changes/archive/707-state-light-proposer` (named follow-up)
- **Status:** implemented in this change (bounded first step — disable minting,
  delete nothing).

## Why

#707 closed GO: the LLM proposer is permanently ON and sourced 12 autonomous
integrations in 24h (`docs/changes/archive/707-state-light-proposer/results.md`).
With the proposer live, the deterministic planner (coordinator
feedback-decision lanes) is now pure noise: it re-mints the same duplicate
`subagent-verify-materialized-improvement` request roughly every 10 minutes
(~79 dup-skips/day against a fixed commit, `7dc70ea`), dominating ledger
traffic and burning a dedup pass every bridge run for zero productive spawns.
Planner retirement is the explicitly named follow-up in the #707 results doc;
the planner's chronic fragility (5+ prior fixes across #656/#664/#690/#695/
#697) is already long documented. Per proposer spec R28
(`docs/specs/self-evolving-runtime/spec.md`), the proposer already fires
whenever the queue is empty or every recent row was a duplicate-skip — so it
is already positioned to be the sole request source once the planner's
minting is turned off.

## What

A single kill-switch env var (retired along with the planner itself in #747
— no longer read by any code), default `"1"` (any value other than the exact
literal `"0"` — absent, `"1"`, or garbage — preserves today's behavior
byte-for-byte). Read via a small
helper in `nanobot/runtime/cycle_planning.py`,
`_deterministic_planner_enabled() -> bool`, mirroring the style of
`SELFEVO_LLM_PROPOSER_ENABLED` in `nanobot/runtime/llm_proposer.py` (#707) —
one flag, no other new config surface.

When the flag is `"0"`, exactly two call sites early-return `None` without
any side effect (no directory creation, no file write), logging one INFO
line each:

1. `_write_subagent_request_artifact` (`cycle_planning.py`) — guard placed
   before the function's first side effect (`request_dir.mkdir(...)`).
2. `_ensure_verify_request_for_fresh_materialization` (`cycle_planning.py`,
   called from `coordinator.py`'s #700 decouple guard around line 487) —
   guard placed before it reads `state/improvements/`.

Both call sites in `coordinator.py` (`_write_subagent_request_artifact` at
~line 468, the decouple guard at ~line 487) already tolerate a `None` return
— `subagent_request_path` falls back to the previously recorded plan value or
stays `None`, and the decoupled-path branch is skipped entirely (`if
decoupled_verify_request_path and not subagent_request_path`) — so the rest
of the coordinator cycle (goal handling, reports, learning, HADI bookkeeping)
completes unchanged whether the flag is on or off. No lane code, bridge code,
or proposer code is touched.

## Rollout

1. **Ship default ON (`"1"` / unset).** Lands inert — behavior is
   byte-identical to before this change, verified by the full existing test
   suite passing unchanged plus new kill-switch-specific tests.
2. **Canary (post-merge, operator-gated).** Operator sets the kill-switch env
   var to `0` on the eeepc host (a runtime env var, not a LiteLLM credential,
   so it does not touch `/etc/eeepc-agent/litellm.env`). Observe the ledger:
   planner dup-skips should stop and the proposer becomes the sole request
   source. Watched via
   `scripts/loop_metrics_report.py` the same way #707's canary was.
3. **Keep or revert.** If the proposer alone sustains healthy liveness and
   productive spawns, leave the flag off and open a follow-up issue to
   consider retiring the deterministic-planner lane code itself (explicitly
   out of scope here). If it regresses, flip back to unset/`"1"` — no code
   revert, no state cleanup, since the flag only gates two writes.

## Rollback

Set (or unset) the kill-switch env var back to `"1"` on the
host. No migration, no state repair — the deterministic planner resumes
minting exactly as before.

## Non-goals

- Deleting or restructuring the deterministic-planner lane code
  (`_derive_feedback_decision`, `next_bounded_candidate`,
  `_derive_generated_candidates`, etc.) — it stays intact and simply stops
  being called to mint new requests when the flag is off.
- Any change to the bridge (`nanobot/runtime/bridge.py`) or the LLM proposer
  (`nanobot/runtime/llm_proposer.py`).
- Any change to goal handling, reporting, learning, or HADI bookkeeping in
  the coordinator cycle — these run identically regardless of the flag.
- A live canary run — that is an operator-gated follow-up action after this
  change merges, not part of this PR.
