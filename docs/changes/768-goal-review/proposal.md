# Change: goal-review — periodic goal-derived priority generation (#768)

- **change-id:** 768-goal-review
- **issue:** #768
- **capability:** `docs/specs/subagent-bridge` (adds R45; extends the R30
  goal_text channel with an append-only writer) + one goal-review sentence
  block in `docs/specs/self-evolving-runtime/spec.md`.
- **Depends on:** #765 (scorecard gaps — the measurement substrate), #760
  (R30/R39 demand-driven channel), #761 (usage/decay evidence), #751
  (fail-closed serves-validator pattern), #767 (ordered goal vectors), #707
  (LLM provider plumbing).
- **Status:** implemented in this change. Kill-switch default OFF.

## Problem

Priority seeding is manual: the operator hand-writes "Priority N — ..."
entries into goal_text. That contradicts the demand-driven design (#760):
idea-invention was removed from the weak model but kept as a human chore.
Priorities must derive from the product goals already laid down (goal_text
vectors, #767) — the operator's role is to set/adjust *goals*, not to
hand-write *tasks*. The deterministic half already exists (#765's
`goal-gap` demand); what's missing is the bounded layer that turns measured
gaps into concrete seeded priorities.

## Intended change

One new module, `nanobot/runtime/goal_review.py`, plus minimal wiring:

1. **Entry point** `maybe_goal_review(state_dir, selfevo_repo, *, now)`:
   - hard-gated by `SELFEVO_GOAL_REVIEW_ENABLED` (default OFF —
     absent/falsy = no-op returning `None`, nothing written, no LLM call);
   - own daily watermark `<state_dir>/goal_review/last_run.json` — at most
     one review per 24h, decoupled from the 10-min cycle. The watermark is
     advanced at the START of an attempt so a wedged review can never burn
     one LLM call per scorecard recompute.
2. **Bounded inputs** (durable state only): goal_text vectors verbatim; the
   latest scorecard snapshot (`scorecard/latest.json`) including `gaps`
   (the same gap list `demand._goal_gap_items` presents as `goal-gap`
   demand — read from the persisted snapshot, never via `collect_demand`,
   which would recurse into the scorecard recompute this review rides);
   usage/decay evidence (#761 `stale_artifacts`); recent integration
   history via the rotation-aware `scorecard._ledger_rows`. Gap and decay
   lines are presented id-keyed (`E1`, `E2`, ...) so citations are
   machine-checkable.
3. **LLM task** (reuses `llm_proposer.propose` — same LiteLLM gateway,
   reply extraction, fail-open behavior; no new client code): formulate 1-3
   concrete bounded priorities; each MUST cite the goal vector it serves
   (`V1`/`V2` only) and exactly one presented evidence id; each MUST be a
   single-function ≤40-line change in one file (the P15/P16 host-model
   capability lesson — one small bite, never a multi-part task).
4. **Fail-closed validation** (the #751 serves-validator pattern): a
   priority lacking a `V1`/`V2` vector reference, or citing evidence that
   does not appear in the inputs, or with a label the done-detection
   regexes cannot parse, or duplicating an existing entry, is REJECTED with
   a recorded reason. Zero valid priorities → honest no-op. At most 3
   accepted.
5. **Output — the same R30 channel operator seeding uses**: validated
   priorities are APPENDED to goal_text's "Current priority targets"
   section in `<state_dir>/goals/goal_text.json`, formatted exactly
   `(<letter>) Priority N — <label>: <body>` (the shape
   `demand._PRIORITY_PATTERN` / `cycle_planning._priority_label_prefix`
   parse), numbering continued from the highest existing "Priority N"
   anywhere in the text (Completed mentions included — retired numbers are
   never reused). Append-only: operator entries are never overwritten,
   reordered, or removed; the operator veto stays "edit goal_text".
6. **Ledger**: one `phase: "goal_review"` row per review (`inputs_hash`,
   produced titles, rejections with reasons, outcome ∈ `appended` /
   `no_gaps` / `no_goal_text` / `invalid_reply` / `no_valid_priorities` /
   `error`) via the shared `append_event` helper.
7. **Wiring**: invoked from `scorecard.compute_scorecard`'s recompute path
   (the `run_heldout`/`update_explorer` precedent), after `latest.json` is
   written so the review reads the fresh snapshot, wrapped fail-open — a
   review bug never breaks the scorecard or demand collection.

## Acceptance

- Switch OFF (default): behavior identical to today — hard no-op, no
  watermark, no ledger row, no LLM call (manual seeding still works).
- Switch ON + seeded gaps: ≤3 priorities appended, each citing vector +
  evidence, parseable by demand collection (they flow R30 → demand →
  proposal → integration unchanged).
- No gaps: zero priorities, honest no-op, `goal_review` ledger row with
  empty output, watermark advances.
- Validation rejects (and records) a priority lacking an evidence citation
  or vector reference; other valid priorities in the same reply are kept.
- Operator edits are never overwritten (append-only + dedup, pinned in
  `tests/test_goal_review.py`).
- Malformed/exception LLM reply: no goal_text change, honest ledger row.

## Rollout

1. **Deploy OFF** — `SELFEVO_GOAL_REVIEW_ENABLED` unset on eeepc: zero
   live behavior change; manual seeding continues to work.
2. **Observe** — confirm scorecard/demand behavior unchanged post-deploy
   (no `goal_review` ledger rows expected).
3. **Enable** — set `SELFEVO_GOAL_REVIEW_ENABLED=1` in the agent service
   environment once #765's gaps are trusted; verify the first `goal_review`
   ledger row and that any appended priority is well-formed, cites real
   evidence, and flows through demand → proposal → integration. Operator
   veto: remove the entry from goal_text.
4. **Rollback** — unset the switch; appended priorities can be deleted
   from goal_text by hand (append-only design means nothing else changed).
