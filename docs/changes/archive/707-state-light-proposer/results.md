# #707 state-light LLM proposer — go/no-go results

**Verdict: GO** (2026-07-13, evaluated at 21 ledgered proposer cycles + 1
pre-ledger-window cycle = 22 total). The proposer stays permanently ON in the
live environment (`SELFEVO_LLM_PROPOSER_ENABLED=1`); the deterministic
planner's replacement/retirement is a separate later change, per the
proposal's non-goals.

## Evaluation window

Canary flip-ON 2026-07-13 ~01:20 MSK → evaluation 2026-07-14 ~01:15 MSK
(release chain `776dd5c` → `08c45c7`+diversity `d759b10` → bulk-skip
`ce602a7`/`7b6447f` → dedup-precision `19452ed`). Source of truth:
`<STATE_DIR>/ledger/cycles.jsonl` (#720), joined
`proposed → started → dedup → gate → outcome` per `request_id`/`cycle_id`;
integration evidence cross-checked against the instance repo's git log.

## Thresholds (proposal.md) vs measured

| Criterion | Threshold | Measured | Result |
|---|---|---|---|
| Genuinely-new proposal rate | ≥ 0.60 | **0.73** overall (16/22 distinct themes); **1.0** post-#732 (15/15) | PASS |
| Integration rate | ≥ 1 per 10 proposer cycles | **5.5 per 10** (12 integrations / 22 proposals) | PASS |
| Unsafe changes | 0 | **0** (gate pass 12/12; mutation-surface rejections 0; rollbacks 0) | PASS |
| Liveness | healthy | No wedge; 12 integrations in 24 h, heartbeat current. Oscillates productive↔degraded with the ~1 proposal/hour cadence (see caveats) | PASS (with caveat) |

## Per-proposal outcomes (22 total)

- **12 integrated** (each `proposed → dedup:proceeded → gate:pass →
  outcome:success`, merged to instance main): profile_resources,
  cleanup_artifacts (log rotation), analyze_cycle_duration, check_disk,
  profile_imports, analyze_repo_size, validate_memory_json,
  measure_api_latency, cycle_summary, monitor_battery,
  compile/index lessons, validate_syntax.
- **6 correctly skipped** — semantic clones of the first integrated task,
  all minted before the diversity fix (PR #732); zero clones after it.
- **3 falsely skipped** — novel proposals killed by the whole-log ≥3-keyword
  dedup heuristic (CPU-throttle 11:59Z, check_memory_pressure 18:03Z,
  prune_memory 18:56Z; none of the target files exist). Root-caused and fixed
  as #736 (target_path-aware dedup, PR #737), verified live the same day:
  the next novel proposal (validate_syntax) proceeded and integrated.
- **1 queued** at evaluation time (profile execution time of agent
  components) — pending normally, not stalled.

## Canary findings fixed during the window (all shipped + live-verified)

1. **Proposer never fired** — queue never emptied (planner mints duplicates
   faster than 1-consume/run). Fix: anti-stacking guard + propose-after-skip
   (PR #731).
2. **Novelty collapse** — 6/6 semantic clones of the first integrated task.
   Fix: pre-write self-dedup + rejected-themes digest + verbatim-priorities
   preference (PR #732). Post-fix diversity: 15/15 distinct.
3. **Proposal starvation behind stale queue tail** (~6 h latency). Fix:
   bulk-skip per run + marker-id sanitization root cause (#733, PRs
   #734/#735). Latency now ~1 timer cycle.
4. **Dedup false positives, self-worsening** — whole-log keyword matching
   saturates as the loop integrates. Fix: target_path-aware dedup (#736,
   PR #737).

## Caveats and follow-ups

- The report-level `genuinely_new_proposal_rate` (7.5%) uses an all-cycles
  denominator that includes the deterministic planner's duplicate noise
  (~6 skips/hour of the same stale hypothesis). The per-proposal rate above
  is the go/no-go metric. Follow-up: the planner's noise is now the dominant
  ledger traffic; its retirement is the natural next change (explicitly out
  of #707 scope).
- `loop_metrics_report` liveness flips to `degraded` between hourly
  integrations; with the proposer's ~1/hour cadence this is expected, not a
  wedge. Follow-up option: cadence-aware liveness threshold.
- Executor model during the window: `openai/cl/gemini-3.5-flash-low`
  (operator-approved substitute while `un/qwen3.6-27b-mtp` is down, #714) —
  results to be re-confirmed on qwen when it returns, per #711/#714.
- Cosmetic: the `llm-proposer: queued <title>` journal line prints the oldest
  queue title, not the proposer's own.
