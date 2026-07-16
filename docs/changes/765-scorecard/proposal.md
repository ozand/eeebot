# Change: instance scorecard — deterministic fitness metrics + goal-gap demand (#765)

- **change-id:** 765-scorecard
- **issue:** #765 (scope narrowed by its two binding comments: gap-analysis
  demand kind `goal-gap` into #760's `collect_demand`; targets follow the
  ORDERED goal vectors per #767 — goal-review consumption is #768, the
  before/after gate snapshot and metric-declared proposals are follow-ups)
- **capability:** `docs/specs/subagent-bridge` (adds R41; consumes R38-R40
  state) + one immutability sentence in
  `docs/specs/self-evolving-runtime/spec.md`
- **Depends on:** #760 (demand kinds), #761 (usage/confirmed-serves
  sidecars), #762 (`proposer_reject` rows), #675 (LLM telemetry), #767
  (ordered goal vectors), #603 (fixed-harness invariant).
- **Status:** implemented in this change.

## Problem

The integration gate answers only "are tests green?" — nothing measures
whether the loop creates value, so "value" is undefined and the loop has no
objective to optimize (the root gap behind the filler-proposal era,
analysis 2026-07-15). AIDE² demonstrates the missing mechanism: score-gated
acceptance against a *measured*, externally-owned objective is what turns a
loop into an optimizer.

## Intended change

1. **Scorecard module** — `nanobot/runtime/scorecard.py`, deterministic
   (NO LLM), fail-open. `compute_scorecard(state_dir, selfevo_repo)`
   produces a `scorecard-v1` snapshot over the last 7 days:
   - **loop** (V1): integrations, skips by class, proposer rejects, idle
     share, repeat-failure rate (`recent_duplicate_failure` skips +
     `self_dedup` rejects over proposals). Rotation-aware: current
     `cycles.jsonl` PLUS up to 7 newest `cycles-*.jsonl.gz` archives (the
     #773 rotation-blindness lesson).
   - **cost** (V1): from `state/llm_calls/<date>.jsonl` daily telemetry
     (#675 — NOT the prompts recordings): total calls/tokens, calls and
     tokens per integration (`None`-safe at 0 integrations).
   - **quality** (V1): instance-repo script count, compile-clean count
     (reusing `demand`'s HEAD-watermarked py_compile scan when a git HEAD
     exists, else a bounded own scan), test-file count.
   - **value** (V2, #761 sidecars): completed declared vs confirmed,
     decay-candidate count, usage-tracked artifact count.
   Persisted to `state_dir/scorecard/latest.json` (overwritten) + one line
   appended to `scorecard/history.jsonl` (bounded read). Recompute gated by
   a 30-minute time watermark (`system_map` no-op pattern).
2. **Targets + gap analysis → demand kind `goal-gap`** — `_TARGETS` is a
   declarative table (metric → direction, threshold, vector, rank) derived
   from the ordered goal vectors: `repeat_failure_rate` ≤ 0.3 (V1),
   `compile_clean_ratio` ≥ 0.95 (V1), `tokens_per_integration` trend-only —
   gap when worsening >50% vs the mean of the prior 7-day history window
   (V1; skipped with thin history), `confirmed_ratio` ≥ 0.5 once ≥3
   completed entries (V2). `idle_share` deliberately has NO target (idle is
   the honest no-op working); the goal's FUTURE section maps to NO metric
   and generates nothing (regression-pinned). Gaps are embedded in the
   snapshot and exposed via `goal_gaps()`; `demand.collect_demand` gains
   kind `goal-gap`, ranked priority > defect > **goal-gap** > hypothesis >
   decay, V1 gaps before V2 within the kind, bounded to 5, fail-open.
3. **Reporting** — `scripts/loop_metrics_report.py` gains an "Instance
   scorecard" section: latest snapshot, trend arrows vs the previous
   history entry, open gaps. Read-only (renders persisted state; never
   computes/writes); missing/corrupt scorecard state → absent, no crash.
4. **Immutability (#603)** — the scorecard, its targets, and its sidecars
   live in the product runtime and the harness-owned `state_dir` — outside
   the instance's mutable workspace. The instance must never be able to
   redefine its own fitness (AIDE²'s public/private evaluation split).

## Acceptance

- Snapshot computed from existing state with zero LLM calls; watermark
  makes back-to-back computations no-ops; counts survive ledger rotation.
- Target breaches surface as `goal-gap` demand items ordered between
  `defect` and `hypothesis`, V1 before V2; FUTURE generates nothing;
  idle_share never gaps.
- Report renders the section and tolerates missing state.
- Full test suite green (`tests/test_scorecard.py`, additions to
  `tests/test_demand.py` and `tests/test_loop_metrics_report.py`).
