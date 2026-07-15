# Implementation proposal: demand-driven proposer — evidence-based demand, LLM-free idle default (#760)

- **Issue:** #760
- **story_id:** `docs/specs/subagent-bridge/spec.md` (LLM proposer, R28-R38;
  this change adds R39)
- **Depends on:** #762 (`proposer_reject` rows + `_consecutive_self_dedup_rejects`
  — the saturation signal and the row this change extends with `demand_id`),
  #748 (evidence-based priority done-detection — reused, not reimplemented).
  **Related:** #749 (SYSTEM_MAP watermark pattern), #750 (kill-switch pattern),
  #751 (`serves`/honest no-op — the no-op option is superseded structurally
  by idle).
- **Status:** implemented in this change.

## Problem

The loop is supply-driven: every ~10 minutes the timer asks the LLM to
"invent a task" over a value-poor workspace. Live evidence (2026-07-14/15):
the only source that ever produced valuable integrations is operator-seeded
goal_text priorities (#748: 3/3 integrated in 56 min); autonomous invention
produced only plausible filler, now correctly blocked by the dedup stack
(#750/#757) — so the saturated loop burns 2-3 LLM calls per cycle on
proposals its own self-dedup silently rejects. The model never uses #751's
`no_valuable_task` option (0 skips ever): asked to invent, it invents. An
LLM cannot be prompted out of sycophancy; the loop structure must stop
asking it to invent.

## Change

Invert the loop from supply-driven to demand-driven: the proposer works only
when there is demand; the LLM **selects and refines** from presented demand
items, never invents from a bare inventory; with no demand the cycle makes
**zero** LLM calls and records an idle heartbeat.

### 1. `nanobot/runtime/demand.py` — deterministic, LLM-free, fail-open collector

`collect_demand(state_dir, selfevo_repo) -> list[{kind, id, summary,
evidence, affected_path}]`, stable `id` = hash of kind+summary. Kinds, in
trust order:

- **`priority`** — remaining "Current priority targets" entries from the
  filtered goal_text. Done-detection is delegated verbatim to
  `cycle_planning.filter_completed_priorities_from_goal_text` (#748) — this
  preserves R30: seeding a fresh priority still wakes the loop.
- **`defect`** — real, recent failures from state artifacts: (a) terminal
  ledger `outcome` rows with `failed`/`timeout` in the last 48h (`skipped-*`
  is the dedup stack working, not a defect); (b) failed/blocked subagent
  result files with error text, bounded to the 50 most recently modified
  files (the `existence_index._MAX_LEDGER_RESULTS` bounded-read discipline);
  (c) instance-repo `scripts/`/`surfaces/` files that fail to byte-compile —
  watermark-gated on repo git HEAD exactly like `system_map.update_system_map`
  (own sidecar `<state_dir>/demand/py_compile_watermark.json` caching the
  findings), so the scan costs nothing while HEAD is unchanged.
- **`hypothesis`** — ONLY hypotheses carrying measurement evidence: a
  non-empty `evidence` or `metric` field, or an `acceptance` referencing a
  file that actually exists in the repo. The two chronic boilerplate
  candidates ("Use one bounded subagent-assisted review to verify the
  materialized improvement artifact", "Synthesize one new bounded improvement
  candidate from retired lanes") have none of these and are regression-pinned
  as non-demand by exact title.

### 2. Exhaustion

A demand item whose proposals have been self-dedup-rejected 2+ times —
matched via the `demand_id` now recorded on `proposer_reject` rows (#762
extended) — is marked exhausted in the schema-versioned sidecar
`<state_dir>/demand/exhausted.json` and no longer presented. Exhaustion
expires after 7 days or when repo HEAD moves; expiry leaves a `reset_at`
marker so only rejects newer than the reset can re-exhaust the item
(otherwise the old ledger rows would instantly re-exhaust it).

### 3. `should_propose` + idle heartbeat

Existing gates kept (proposer kill-switch, anti-stacking guard); then, in
demand mode, fire iff `collect_demand` is non-empty. Empty demand — the only
remaining reason not to propose at that point — records ONE
`{phase: "idle", reason: "no_demand"}` ledger row (fail-open, no `cycle_id`,
at most one per bridge cycle via a process-lifetime flag: one bridge cycle ==
one timer-paced process invocation). Recorded from inside `should_propose`,
not `maybe_propose`, because `should_propose` is the single point that knows
the refusal reason is "no demand" rather than e.g. anti-stacking — recording
from `maybe_propose` would force a richer return type or a second demand
collection.

### 4. `build_context` + prompt contract

Demand mode leads the context with a separately-bounded `## Demand` section
(`_MAX_DEMAND_CHARS` = 4000, the `_MAX_INVENTORY_CHARS` precedent): one line
per item with kind, id, summary, quoted evidence, plus the selection
instruction. A new system prompt (`_DEMAND_PROPOSER_SYSTEM_PROMPT`) tells the
model to select exactly ONE demand item, propose a bounded task addressing
it, and set `serves` to `demand <id>` — or reply `no_valuable_task` if no
item is addressable. Vector 1/2 open-ended invention is retired from the
prompt; `validate_sizing` accepts `demand <id>` as the primary form while
tolerating the legacy `priority N`/`vector`/`hypothesis` prefixes for one
release. The inventory/system-map/hypothesis/ledger sections are kept — they
prevent duplicates. `proposed` and `proposer_reject` rows carry `demand_id`.

### 5. Kill-switch

`SELFEVO_DEMAND_DRIVEN_ENABLED` — #750 pattern, default ON; `"0"` restores
the pre-#760 `should_propose`/`build_context`/prompt behavior wholesale (the
old code paths are kept intact in this PR, not deleted).

## Acceptance

- Goal_text drained + no fresh defects + no measurement-backed hypotheses ⇒
  zero LLM calls, one `idle` ledger row per bridge cycle. ✔ tests
- Seeding a fresh goal_text priority wakes the loop (R30 verbatim). ✔ test
- An injected real defect (script failing compile) surfaces as demand and
  yields a proposal whose `serves` references it. ✔ tests
- py_compile scan no-ops on unchanged HEAD; bounded reads; fail-open on
  unreadable state. ✔ tests
- 2 self-dedup rejects with `demand_id` drop the item; expiry on HEAD move
  and after 7 days. ✔ tests
- Kill-switch OFF reproduces pre-#760 behavior (the existing #707-#762 test
  suite runs pinned to OFF as its regression contract). ✔ tests
- `loop_metrics_report.py` tolerates `idle` rows. ✔ test

## Deviations from the issue brief

- Exhaustion counts an item's `self_dedup` rejects cumulatively per
  `demand_id` (2+), rather than literally consuming the global
  `_consecutive_self_dedup_rejects` counter — the global streak cannot
  attribute rejects to one item; the R38 signal remains available for
  cross-item saturation observability.
- The compile scan uses the builtin `compile()` (the identical syntax check
  `py_compile` performs) instead of `py_compile.compile`, so nothing is ever
  written into the instance repo (no `__pycache__`/`.pyc` side effects).
