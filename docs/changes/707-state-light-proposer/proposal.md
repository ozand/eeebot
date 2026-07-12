# Implementation proposal: the state-light LLM proposer (#707)

- **Issue:** #707 (loop-redesign ticket F — implementation, gated on ticket B)
- **Status:** docs-only proposal; no code in this change.
- **story_id:** docs/specs/self-evolving-runtime/spec.md
- **Amends/consumes:** `docs/changes/702-ledger-loop-architecture-decision/decision.md`
  (C1–C4 + non-goals, below, are treated as acceptance criteria, not
  suggestions) and its `design-constraints.md`.

## Gating status — read this before anything else

#707 is formally gated on ticket B (#706) passing go/no-go, per
`decision.md` §6: *"Nothing in F starts until B passes its success
thresholds."* The current record of that gate is
`docs/changes/711-on-host-shadow-run/results.md` (2026-07-10), whose
explicit verdict is **NO-GO — #707 stays BLOCKED**, for two documented
reasons: (a) the run substituted a frontier model for the real weak host
model (qwen was down), so host-model fidelity is untested, and (b) it
found a genuine **novelty-collapse** failure — the LLM proposer, run
sequentially against the real `goal_text.json`, re-proposed the same
already-done task in 4 of 5 cycles once the hardcoded standing priority in
`goal_text` had been satisfied.

Since that run, three follow-ups closed against its own recommendations:

- **#712** (closed, roll-out) — `filter_completed_priorities_from_goal_text`
  in `nanobot/runtime/cycle_planning.py` now strips already-done priorities
  out of the raw `goal_text` before it reaches any prompt, using the same
  git-log done-detection heuristic the deterministic path already used.
- **#713** (closed, roll-out) — added novelty pressure / done-awareness to
  the proposal step (`_recent_activity_context` in `bridge.py`).
- **#716** (roll-out) — pre-spawn suppression of repeats of failed/rejected
  proposals, layered on top of #713.
- **#714** (still OPEN) — the actual re-run of #711 against the real weak
  host model, which is what would flip the go/no-go. It has not run yet in
  this repo's record.

This proposal is written under an operator instruction that treats the
combination of (a) live evidence that the deterministic planner has run out
of novel work on the host (duplicate-rate saturation / liveness DEGRADED
once the current `goal_text` priorities complete — the host refreshed
`goal_text` for P11/P12 once P9/P10 completed autonomously, per the local
main branch history at 11d7161) and (b) the #712/#713/#716 fixes closing the
specific gaps #711 found, as sufficient live-signal justification to write
this plan now, in parallel with #714 rather than strictly after it. **This
is a live tension worth flagging explicitly: the paper trail in this repo
does not yet contain a #714 result that reverses 711's NO-GO.** The plan
below is written so that shipping it costs nothing if #714 later fails —
the flag defaults OFF, the deterministic planner is untouched, and rollout
is staged behind the same shadow/canary discipline #706/#711 already used.
Recommend closing #714 (or documenting why it is superseded) before
flipping `SELFEVO_LLM_PROPOSER_ENABLED` to ON on the host.

## 1. Evidence-driven case (why now, briefly)

- The deterministic generator's core failure mode is structural, not a bug:
  `next_bounded_candidate` / `_derive_generated_candidates` in
  `nanobot/runtime/cycle_planning.py` enumerate a small, hand-maintained
  `goal_text.json` priority list. Once those priorities are done (P9/P10
  completed autonomously per recent host history), the generator has
  nothing left to propose — the loop idles safely (no unsafe action) but
  produces zero productive spawns until an operator hand-edits `goal_text`
  again. This is exactly the "machinery running, nothing productive"
  pattern named in `decision.md` §5.
- Shadow evidence already in this repo shows the LLM-proposal path *can*
  produce novel, gate-passing, integrable work inside the existing harness:
  #706 (Sonnet-shadow, 5/5 genuinely-new, gate-passing) and #711 cycle C1
  (gemini proposer, real bridge, real gate — integrated cleanly, 0
  mutation-surface violations). The execution shell (bridge spawn → bounded
  gate #686 → integrate-on-green → auto-commit → rollback → lock) is
  proven by the same P9/P10 live integrations and by #711's C3–C5 rejections
  holding correctly.
- What #711 also showed is that novelty is fragile once the harness runs
  sequentially against the *real* `goal_text`: the LLM anchored on a
  hardcoded standing priority and repeated it after satisfying it. #712 and
  #713 target that exact mechanism. This proposal's context-construction
  step (§2) is designed around those same fixes, not a fresh design.

## 2. Minimal design honoring C1–C4

### One new module: `nanobot/runtime/llm_proposer.py` (~150–200 lines)

```
build_proposer_context(state_root, workspace) -> ProposerContext
propose_task(context, *, model, max_retries=1) -> Proposal | None
validate_sizing(proposal) -> tuple[bool, str]           # C2
write_proposer_request(state_root, cycle_id, goal_id, proposal) -> str  # C1
should_invoke_proposer(state_root) -> bool               # invocation policy
```

**C1 — single proposer contract.** `write_proposer_request` is a thin
wrapper that produces *the same* dict shape
`_write_subagent_request_artifact` already writes and drops it in
`state/subagents/requests/request-<cycle_id>.json`:
`{request_id, request_status: "queued", cycle_id, task_title, goal_id,
source_artifact, ...}`. The bridge (`find_pending_request`,
`build_task`), the pre-spawn dedup, the gate, and the ledger writers in
`cycle_ledger.py` are **not touched**. From the bridge's point of view a
proposer-written request file is indistinguishable from a planner-written
one — `source_artifact` simply points at a small proposer-authored JSON
(the "materialized improvement" analogue) instead of one derived from
`goal_text`/backlog parsing.

**C2 — checkable sizing.** `validate_sizing` runs *before* the request file
is written (pre-spawn, same phase the write-ahead cycle marker in
`record_cycle_started` occurs at). It requires the LLM's structured
response to declare:
- `target_surface`: one path/module scope (a single glob under the mutable
  surface — reuses the same allow-list `_validate_mutation_surfaces` in
  `bridge.py` already enforces at gate time, so a proposal that names a
  path outside it is rejected pre-spawn instead of relying on the gate to
  catch it after a subagent already ran);
- `concern`: a one-sentence single behavior/bug statement (rejected if it
  contains multiple "and"-joined asks — a cheap heuristic, not an LLM
  self-report);
- `expected_size_class`: one of `{tiny, small, medium}` (declared, not
  measured — the gate's existing shrink-guard and mutation-surface checks
  remain the actual enforcement; this is the pre-spawn checkable
  declaration C2 requires).

If validation fails, `propose_task` retries the LLM call **once** with the
rejection reason appended to the prompt; a second failure means the
proposer contributes nothing this cycle (fails open to "no request written",
identical to today's idle-safe behavior when the deterministic generator
has nothing to offer).

**C3 — plan-state vs learning-state split.** `build_proposer_context`
composes exactly two read-only inputs, never merged:
1. **Structured goal queue** — the *filtered* `goal_text` produced by the
   existing `filter_completed_priorities_from_goal_text` (#712) — the
   planner-owned, mutable "what's next" artifact, unchanged by this
   proposal.
2. **Append-only lessons/failure digest** — the last N rows of the #720
   `<state_dir>/ledger/cycles.jsonl`, filtered to terminal rows
   (`outcome`, `reason`, `task_title`/`request_id`), read-only. No new
   ledger, no new file format — this reuses `cycle_ledger`'s existing
   `cycles.jsonl` exactly as `scripts/loop_metrics_report.py` already reads
   it (`load_ledger_rows`).

These two pieces are concatenated into the LLM prompt as clearly labeled,
separate sections; the module never writes back into either source (the
ledger is append-only by construction; `goal_text.json` is untouched — only
the in-memory *filtered* copy is used, same as #712's existing call site).

**C4 — tag digest for goal rotation.** Rather than a bespoke new script, the
digest half of C3 is produced by a small (~30-line) helper inside
`llm_proposer.py`, `_tag_recent_outcomes(rows, n=20)`, that maps each
terminal ledger row's `outcome`/`reason` to a short tag (e.g.
`success:<surface>`, `dup:<matched-title>`, `gate-fail:<reason>`) and
returns the last N as a flat list of strings. This satisfies C4's "~50-line
script" scope without introducing a second ledger reader outside this
module — `loop_metrics_report.py`'s existing tag/reason vocabulary
(`_gate_fail_breakdown`, `_dedup_breakdown`) is reused for tag naming so the
two report surfaces stay consistent.

### Invocation policy (kept deliberately dumb)

The proposer is not a parallel lane and does not compete with the
deterministic generator. `should_invoke_proposer(state_root)` is a single
boolean gate checked from the coordinator/bridge call site, right where
`_write_subagent_request_artifact` currently returns `None` for the
"nothing to propose" case:

```
invoke_proposer := (no queued request exists)
                AND ( filtered goal_text has no current-priority entries left
                      OR last K terminal ledger rows are all
                         outcome == "skipped-duplicate" )
```

`K` defaults to 3 (same order of magnitude as #711's observed 4-in-a-row
duplicate collapse — small enough to react within one host cycle, large
enough not to fire on a single coincidental duplicate). On a positive
check, the coordinator calls `propose_task` **once** for the cycle; if it
returns `None` (validation failed twice, or the LLM call errored), the
cycle proceeds exactly as it does today when there is nothing to propose —
idle-safe, no request written, no subagent spawned.

**Kill switch:** `SELFEVO_LLM_PROPOSER_ENABLED` (env, default unset/`0` =
OFF). When OFF, `should_invoke_proposer` always returns `False` and the
module is inert — this is the entire rollout control, no other config
surface. The model used is read from the same `SUBAGENT_BRIDGE_MODEL` /
LiteLLM gateway env the bridge already uses (`BRIDGE_MODEL` in
`bridge.py`) — no second model config, per the repo's single-source-of-
truth rule for LiteLLM env (`/etc/eeepc-agent/litellm.env`).

### Downstream unchanged

Dedup (`_task_already_done`, `_recent_failure_match`, `#721` tag-first
dedup), the mutation-surface gate, the smoke-test shrink guard, auto-commit,
integrate-on-green, rollback, and the cycle-lock all run exactly as they do
today — the proposer's output is just another queued request file entering
`find_pending_request`.

## 3. Rollout

1. **Phase 1 — ship OFF.** Land `llm_proposer.py` + call-site wiring with
   `SELFEVO_LLM_PROPOSER_ENABLED` unset (default OFF). No behavior change
   on the host. Verified by tests (§4) and CI only.
2. **Phase 2 — canary.** Operator flips the env var ON on the host
   (`/etc/eeepc-agent/`-scoped override, not committed to the repo, per the
   "never set LITELLM_* elsewhere" rule — this new var is proposer-specific,
   not a LiteLLM credential, so it is fine to set alongside the existing
   env file). Observe via `scripts/loop_metrics_report.py` over the next N
   cycles.
3. **Phase 3 — keep or revert.** If go/no-go (§4) holds, leave it ON and
   open a follow-up issue to consider retiring the deterministic generator
   (explicitly **not** in this change — see non-goals). If it regresses,
   flip the env var back OFF; no code revert needed, no state to clean up
   (the proposer wrote ordinary request/ledger rows, nothing else).

No deletion of `cycle_planning.py`'s deterministic generator happens in
this change — it keeps running and takes priority whenever it has
something to propose; the LLM proposer only fills the gap when it is
provably empty-handed.

## 4. Go/no-go criteria

Measured by `scripts/loop_metrics_report.py` over a window of the next 20
proposer-invoked cycles (i.e. 20 cycles where the LLM path actually fired,
not 20 wall-clock cycles — most cycles will still be handled by the
deterministic path or be idle):

| Metric | Threshold | Source |
|---|---|---|
| `genuinely_new_proposal_rate` | ≥ 0.60 | `compute_metrics` in `loop_metrics_report.py` |
| `duplicate_rate` | < 0.80 saturation threshold (must not reproduce #711's 4/5) | same |
| integration rate | ≥ 1 successful integration per 10 proposer cycles | `integration_rate.of_spawned` |
| `protected_surface_rejections` | 0 reaching `main` (rejections at gate/precheck are fine and expected; nothing unsafe integrates) | gate ledger rows |
| liveness | `healthy` state sustained per the #705 report-spec watchdog (no `degraded` streak longer than K cycles) | `compute_liveness` |
| cost | wall/token cost per proposer call within the host's existing per-cycle time budget (no new budget carve-out) | telemetry |

Falling short on any threshold is a revert-the-flag event, not a blocking
redesign — the env kill-switch makes this a cheap, reversible experiment
exactly like #706/#711 were.

## 5. Test plan

- **Unit** (`tests/test_llm_proposer.py`, new):
  - `build_proposer_context` returns a bounded structure (goal-queue text +
    last-N tags only; no other state leaks in) even when the ledger file is
    missing/empty (fail-open, mirrors `cycle_ledger`'s own convention).
  - `validate_sizing` accepts a well-formed single-surface/single-concern
    proposal and rejects: multi-surface, multi-concern (via the "and"
    heuristic), missing `expected_size_class`, and a `target_surface`
    outside the existing mutation-surface allow-list.
  - `write_proposer_request`'s output is schema-equal (same keys, same
    `request_status` value) to a fixture request written by
    `_write_subagent_request_artifact`, keeping C1 an enforced invariant
    rather than a convention.
  - `should_invoke_proposer` returns `False` when a queued request already
    exists, `False` when a current-priority entry remains after #712
    filtering, `True` after K consecutive `skipped-duplicate` ledger rows,
    and always `False` when the env kill-switch is unset.
- **Integration** (extends the existing temp-repo bridge harness used by
  `tests/test_bridge_*`): a proposer-written request file is dropped into a
  scratch `state/subagents/requests/`, and the real `find_pending_request`
  → `build_task` → gate path picks it up and runs identically to a
  planner-written request (schema-equality is exercised end-to-end, not
  just at the unit level).
- **Kill-switch test**: with the env var unset, running a full cycle where
  the deterministic generator has nothing to propose leaves the loop
  exactly as idle-safe as it is today (no request written, no regression to
  current behavior) — this is a regression guard, not new functionality.

## 6. Explicit non-goals

- No new frameworks, memory platforms, or context-substrate services (no
  mem0/cognee/OpenViking) — the ledger (#704/#720) stays the only durable
  state, per `design-constraints.md`'s non-goals.
- No multi-proposal ranking/tournaments — exactly one proposal per
  invocation, one retry on validation failure, nothing else.
- No deletion or disabling of the deterministic planner in this change —
  it remains the default path; the proposer only activates on proven
  novelty exhaustion.
- No new state files beyond the existing `cycles.jsonl` ledger — the tag
  digest (C4) is computed on read, not persisted.
- No SQL/Dolt-backed ledger, no crew/swarm/multi-agent lanes — one
  fresh-context subagent per cycle, unchanged.
- No change to the immutable safety shell (#703) — mutation-surface guard,
  green-only integration, suite-shrink guard, rollback, and lock are
  consumed, not modified.

## 7. Size estimate and slicing

| Component | Size | Suggested PR |
|---|---|---|
| `llm_proposer.py` (context builder, sizing validator, request writer, tag digest) | ~150–200 lines | PR 1 |
| Coordinator/bridge call-site wiring (`should_invoke_proposer` check + kill-switch env read) | ~20–30 lines, touches `cycle_planning.py` and/or `bridge.py` at the existing "nothing to propose" branch | PR 1 |
| Unit tests (`test_llm_proposer.py`) | ~150–250 lines | PR 1 |
| Integration test extending the bridge harness | ~50–80 lines | PR 2 (can land with PR 1 if the harness fixture is trivial to extend; split out only if it turns out to need harness changes) |
| Docs: this proposal → archive on merge; `docs/specs/self-evolving-runtime/spec.md` update noting the proposer as an optional, flagged proposal source | small | part of PR 1 |

Total: comfortably fits in **one PR** (PR 1), with the integration test as
the only piece that might justify a second if the existing bridge test
harness needs non-trivial extension to accept a proposer-authored fixture
request.
