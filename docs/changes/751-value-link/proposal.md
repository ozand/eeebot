# Implementation proposal: goal-alignment 'serves' field, honest no-op, hypotheses reader (#751)

- **Issue:** #751
- **story_id:** `docs/specs/subagent-bridge/spec.md` (LLM proposer, R28-R33; this
  change adds R36)
- **Depends on:** #748 (evidence-based done-detection — the ground the value
  judgment stands on). **Related:** #749 (SYSTEM_MAP inventory in proposer
  context), #750 (FTS5 existence index).
- **Status:** implemented in this change.

## Problem

Nothing in the #707 proposal schema (`task_title`, `rationale`, `target_path`)
ties a proposed task to the project's actual goals. `rationale` is free text
the LLM writes about its own proposal — it is not checked against anything.
Confirmed consequence (night of 2026-07-14): 16 integrations landed overnight
with declining value per cycle as the proposer circled a saturated theme
space (memory/CPU monitoring scripts under successive new names), because
nothing forced it to justify *why* a task mattered or let it admit that
nothing did. Meanwhile `state/hypotheses/backlog.json`
(`cycle_persist._build_hypothesis_backlog_snapshot`, written every cycle by
the coordinator) and `state/research/hypotheses.json`
(`cycle_planning._write_research_feed`, an append-only cycle-snapshot log)
are both written every self-evolving cycle but read by **nothing** on the live
path — the deterministic planner that used to consume them was retired in
#739, and no replacement reader was ever wired up. The "hypothesis ->
priority" chain exists only on paper.

## Proposal

Three additions to `nanobot/runtime/llm_proposer.py`, all schema-validated or
fail-open, none introducing a new kill-switch (the existing
`SELFEVO_LLM_PROPOSER_ENABLED` gate covers all of this — `serves` is simply
part of the proposal contract from now on).

### 1. `serves` field — schema-enforced goal alignment

`_PROPOSER_SYSTEM_PROMPT` now requires a fourth JSON key, `serves`, naming
what the proposed task serves:

- `"priority <N>"` — a numbered `goal_text.json` "Current priority targets"
  entry (e.g. `"priority 5"`);
- `"vector 1"` / `"vector 2"` — goal_text's Vector 1 (self-optimization) /
  Vector 2 (owner utility), optionally with a 3-8 word justification suffix
  after a colon (e.g. `"vector 1: reduces cycle disk writes"`);
- `"hypothesis <id-or-short-title>"` — an entry from the new Hypothesis
  backlog context section (part 3, below), e.g. `"hypothesis h3"`.

`validate_sizing` (extended, not renamed — see "Deviations" below) rejects a
proposal whose `serves` is missing/empty, over 160 characters, or does not
start (case-insensitively) with one of those four prefixes. This follows the
exact same reject → retry-once-with-feedback → fail-closed path the other
schema checks already use (R29); no proposal is written this cycle if
`serves` still fails validation after the retry.

`write_request`'s `append_event` call for the `proposed` ledger row (R31) now
also carries the accepted `serves` string. It is **not** added to the written
request JSON payload itself — that would break the R29
request-schema-equality invariant with
`cycle_planning._write_subagent_request_artifact` (verified unchanged by
`TestWriteRequestSchemaEquality.test_same_keys_and_queued_status`). Rows
written before this change (no `serves` key) read as class `"missing"` in
the report below — never a crash.

`scripts/loop_metrics_report.py` gains a **goal alignment** section:
`_goal_alignment_breakdown` counts `proposed`-phase ledger rows per
`serves`-class (`priority` / `vector 1` / `vector 2` / `hypothesis` /
`missing` / `other`) over the report window, plus the count of honest no-op
skips (part 2). Rendered as a new table section in `render_table` and a new
`goal_alignment` key in the JSON report.

### 2. Honest no-op

The prompt now also allows: *"If nothing you could propose creates real
value toward the goals — everything worthwhile is done, queued, or listed as
existing — reply with exactly `{"no_valuable_task": true, "reason": "..."}`
instead of inventing filler work."*

When `maybe_propose` receives this reply (and the reply is currently
"allowed" — see the cap below), it records a **`proposer_skip`** ledger event
(`phase: "proposer_skip"`, `reason`, deliberately no `cycle_id` — no
cycle/subagent request exists for a skipped cycle) and returns `None`
immediately: no subagent request is minted this cycle. `proposer_skip` is a
distinct phase from `proposed` — never a `proposed` row with a placeholder
title — so it can never pollute R30's title-based dedup
(`_recent_proposed_titles` only reads `phase == "proposed"`) or the
goal-alignment counts above.

**Kill-switch reuse + idle-loop bound.** No new enable flag: `serves` and the
no-op reply are both part of the existing proposer's contract, gated by the
same `SELFEVO_LLM_PROPOSER_ENABLED`. To stop a lazy model from replying
`no_valuable_task` forever, `_consecutive_noop_streak` counts trailing
`proposer_skip` rows among the ledger's own `proposed`/`proposer_skip`
history (stopping at the most recent `proposed` row) — tracked via the
ledger, not in-memory, so the cap survives a process restart. Once the streak
reaches `_MAX_CONSECUTIVE_NOOP_SKIPS` (3), the next call is forced into
normal mode: `build_context(..., force_proposal=True)` appends an explicit
"you must propose a concrete task this cycle" note, and even if the model
still replies `no_valuable_task` anyway, `maybe_propose` ignores it (does not
treat it as an honored skip) and falls through to `validate_sizing`, which
rejects it for a missing `task_title` — the same reject/retry/fail-closed
path as any other invalid proposal (belt-and-suspenders: correct regardless
of whether the model actually respects the forced-mode instruction).

**Pacing.** `bridge.py` calls `maybe_propose` at most once per bridge cycle
(the timer-paced ~10-minute cadence the surrounding R28 invocation policy
already relies on) — this alone is sufficient to keep a run of honest skips
from tight-looping; no additional rate limit was needed.

### 3. Hypotheses reader (гипотеза -> приоритет chain)

New module `nanobot/runtime/hypothesis_backlog.py`:

- `_backlog_candidates` / `_research_candidates` read the two existing
  cycle-writers' files (`hypotheses/backlog.json` primary,
  `research/hypotheses.json` secondary) into a common
  `{key, title, source}` shape. `key` prefers the backlog entry's own
  `hypothesis_id`; otherwise a slug of the title (`research/hypotheses.json`
  entries have no id).
- `top_candidates` / `context_section` surface the top 5 still-`active`
  candidates as a new, separately-bounded `## Hypothesis backlog (candidate
  value sources)` section in `build_context` — one
  `- [<key>] <title>` line each, omitted entirely when there is nothing to
  show (same fail-open convention as the R34 inventory section).
- **Lifecycle** (`active` -> `answered` / `stale`): `reconcile` scans recent
  ledger rows for a `proposed` row whose `serves` names a hypothesis
  (`hypothesis <ref>`) followed by a same-`cycle_id` `outcome` row with
  `outcome == "success"` — that candidate is marked `answered` (with the
  resolving `cycle_id` as evidence) and stops appearing in the context.
  A candidate not referenced by any `serves: hypothesis ...` proposal for
  `STALE_AFTER_UNTOUCHED_CYCLES` (50) reconciliation passes, or older than
  `STALE_AFTER_DAYS` (14) since first observed, is demoted to `stale` and
  also drops out of the context.

## Deviations from the issue brief (and why)

1. **Lifecycle status is NOT stored inside `backlog.json`'s own entries.**
   The brief's most literal reading ("persist status IN the backlog.json
   entries, additive keys, never drop unknown fields") assumes
   read-modify-write semantics. In the actual code,
   `cycle_persist._build_hypothesis_backlog_snapshot` writes a **fully
   regenerated** snapshot every self-evolving cycle
   (`hypothesis_backlog_path.write_text(json.dumps(hypothesis_backlog, ...))`
   in `coordinator.py`, not a merge) — any status key this module added to an
   entry would be silently wiped by the very next cycle's snapshot.
   Persisting lifecycle status there would require invasive changes to the
   coordinator's cycle-persistence path, well outside this change's scope.
   Instead, `hypothesis_backlog.py` owns a small sidecar file,
   `<state_dir>/hypotheses/lifecycle.json`, keyed by the stable candidate
   key, read/written exclusively by this module — additive-only, never
   dropping unknown fields (verified by
   `test_unknown_fields_preserved_after_rewrite`), satisfying the same
   intent (state survives across cycles) without fighting the existing
   writer's overwrite semantics.
2. **No dedicated cycle-outcome hook for answered-marking; lazy
   reconciliation instead.** The brief allows this as a fallback ("if no
   clean hook exists without invasive changes, implement the marking as part
   of the NEXT `build_context` call"). Grepping for where cycle outcomes are
   recorded (`cycle_ledger.record_cycle_outcome`, called from `bridge.py` at
   the point a cycle's result is known) shows no existing seam that already
   has both the `serves` value and the resolving cycle_id available together
   without new plumbing through the bridge's cycle-result path. Reconciling
   lazily inside `top_candidates`/`context_section` (called once per proposer
   cycle via `build_context`) is simple and fail-open: a hypothesis's
   `answered` status becomes visible in the context within one additional
   proposer cycle of its resolving outcome landing, matching the loop's own
   coarse (~10-minute) cadence — sufficient for this purpose.
3. **`_MAX_CONSECUTIVE_NOOP_SKIPS` enforcement is a static text note in the
   context plus a code-level ignore, not a variable system prompt.** Rather
   than parameterizing `propose()`'s system-prompt argument per call (which
   would require changing its call signature — and every existing test in
   `tests/test_llm_proposer.py` monkeypatches `llm_proposer.propose` with a
   fixed `(context, *, rejection_reason=None, timeout=120.0)` signature that
   would break under a new required/optional kwarg mismatch with certain
   mocks), the forced-mode signal is carried entirely in the **context**
   string (`build_context(..., force_proposal=True)`) while `propose()`'s own
   signature and the constant `_PROPOSER_SYSTEM_PROMPT` are both left
   unchanged. This keeps 100% of the pre-#751 `propose()` call sites and
   mocks working unmodified.

## Test evidence

- `tests/test_llm_proposer.py`: extended `TestValidateSizing`'s `_good()`
  fixture with a `serves` default; new `TestValidateServes` (accepted forms:
  `priority 11`, `vector 1: ...`, `vector 2`, `hypothesis h3`, case
  variance; rejected: missing, empty, wrong prefix, over 160 chars, list
  type); new `TestHonestNoOp` (skip records a `proposer_skip` row and mints
  no request, default reason placeholder, the 3-consecutive cap forces
  normal mode on the 4th call with the forced-note present in context, the
  streak resets after a real proposal); new `TestBuildContextHypotheses`
  (section present/bounded from `backlog.json`, corrupt file omitted,
  absent when no hypothesis files exist). All previously-existing fake
  `propose()` return dicts across the file were updated to include a valid
  `serves` field so the suite stays green under the new mandatory field.
- `tests/test_hypothesis_backlog.py` (new): primary/secondary source
  reading, dedup-by-key, corrupt-file fail-open, answered-marking on a
  success outcome with `serves: hypothesis ...`, staying active on a
  non-success outcome, stale demotion by age and by untouched-cycle count,
  unknown-field preservation across a lifecycle rewrite, fail-open on a
  wholly missing state dir.
- `scripts/loop_metrics_report.py`'s `--test` self-check: extended the fixture
  ledger with 5 `proposed` rows (one per serves-class, one legacy row with no
  `serves` at all) and 2 `proposer_skip` rows; asserts the new
  `goal_alignment` breakdown counts each class correctly and that the
  pre-existing metrics/liveness assertions are unaffected.

## Acceptance (from the issue)

- Every ledger `proposed` row carries a non-empty `serves` (schema-enforced,
  fail-closed on violation) — the report shows the distribution.
- A live cycle demonstrably picks a hypothesis from the backlog and marks it
  Answered with commit evidence — verified logically here via
  `test_answered_marking_on_success_outcome_with_serves_hypothesis`; live
  verification on the eeepc host follows after deploy (this PR does not
  close #751 — see PR body).
- A saturated-theme scenario ends in `no_valuable_task` (ledger-visible)
  rather than a near-duplicate integration — covered by `TestHonestNoOp`;
  live confirmation follows after deploy.
- Fail-closed on schema violation (reject + one retry, then no proposal this
  cycle) — unchanged path, now also covering `serves`.
