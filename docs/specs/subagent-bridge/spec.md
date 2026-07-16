# Subagent Bridge — spec

_Status: current. Last updated: 2026-07-16 (#773: R39 gained ledger-chain
done-truth — a `proposed` row carrying `demand_id` plus a same-cycle terminal
`outcome: success` row is folded into an append-only completed sidecar
(`<state_dir>/demand/completed.json`, rotation-proof by construction);
completed ids are dropped from all demand kinds before the exhausted filter,
and `filter_completed_priorities_from_goal_text` consumes the same sidecar
when given a `state_dir` — fixing the live P14 case where a refined-title
demand integration carried no text evidence and the retired priority was
re-proposed daily). Previous entry: 2026-07-16 (#771: R39's exhaustion gained
reset-on-success-integration and reset-on-release-change semantics, the
expiry shortened from 7 days to 24h, and a missing sidecar entry now behaves
like a reset (honest manual clear) — fixing the live 2026-07-15 exhaustion
deadlock where the only demand item stayed frozen because HEAD-move expiry
was circular and ledger recomputation silently undid operator clears).
Previous entry: 2026-07-15 (#760: added R39 — the
demand-driven proposer inversion: behind `SELFEVO_DEMAND_DRIVEN_ENABLED`
(default ON), `should_propose` fires only on non-empty deterministic demand
(`nanobot/runtime/demand.py`: remaining goal_text priorities, recent real
defects, measurement-backed hypotheses), with an `idle` heartbeat ledger
phase (zero LLM calls) when demand is empty, a `demand <id>`-referencing
`serves` contract, and per-item exhaustion from repeated self-dedup rejects
— see `docs/changes/760-demand-driven-proposer/proposal.md`). Previous
entry: 2026-07-15 (#762: added R38 — a
`proposer_reject` ledger phase makes `maybe_propose`'s four formerly-silent
rejection exits (`empty_context`/`sizing_rejected`/`self_dedup`/`error`)
observable, with `matched_against` on self-dedup rejects, a
reject-by-reason breakdown in `scripts/loop_metrics_report.py`, and the
`_consecutive_self_dedup_rejects` saturation signal for #760). Previous
entry: 2026-07-14 (#757: dedup precision — R35
gained the kind-aware tests-for-X rule (a `tests/`-target proposal is never
a duplicate of the script it tests) and added R37: the recent-failure gate
keys on derived (action-class, target) intent and records the matched
HISTORICAL title as `matched_against`). Previous
entry: 2026-07-14 (#749 follow-up: R34 gained an
ownership-deferral clause — `update_system_map` now defers entirely to a
foreign generator that already claims `docs/SYSTEM_MAP.md` (detected by the
absence of our own generated-note marker), never clobbering it, and the
proposer's inventory context falls back to direct generation whenever the
on-disk map is in a foreign format — see the "Follow-up: ownership
deferral" section of `docs/changes/749-system-map/proposal.md`). Previous
entry: 2026-07-14 (#751: added R36, the 'serves'
goal-alignment field, the honest `no_valuable_task` no-op reply, and the
hypothesis-backlog reader/lifecycle — see
`docs/changes/751-value-link/proposal.md`). Previous entry: 2026-07-14 (#749:
added R34, the deterministic SYSTEM_MAP inventory fed into the LLM proposer's
context so it stops shipping near-duplicate scripts under new names — see
`docs/changes/749-system-map/proposal.md`; #750: added R35, the FTS5
existence-index semantic dedup gate — see
`docs/changes/750-existence-index/proposal.md`). Previous entry: 2026-07-08 (#703:
added the "Immutable safety shell (loop-independent)" section below, freezing
the invariants already implemented by #653/#666/#678/#680/#686 as a fixed
contract that the loop-redesign set (#702, #704-#708) may consume but MUST
NOT relax — loop-redesign ticket D. No behavior changed; this only names,
cites, and freezes existing enforcement points. See also
`docs/changes/703-safety-shell-invariants/precheck-contract.md` and
`.../test-coverage-map.md`, and the architecture decision at
`docs/changes/702-ledger-loop-architecture-decision/decision.md`). Previous
entry: 2026-07-07 (#686: the smoke gate (R10/R11) is
now BOUNDED — import-smoke of changed files + tests they affect + a small
fixed core smoke set, computed by `_select_gate_tests`, instead of the full
`pytest tests/` suite. Rationale: the subagent's mutation surface is already
bounded (core `nanobot/` is hard-blocked, #678), so full-suite validation of
core belongs to product CI + re-seed-time verification, not a 300s per-cycle
gate that the full suite (601s measured) can no longer fit. Every #678
fail-safe/hard-block property is preserved against the smaller selection).
Previous entry: 2026-07-07, #680: defense-in-depth follow-up to
#678 — an exclusive non-blocking `flock` on `bridge.lock` guards against
concurrent bridge runs (R26), and a HEAD-on-main precondition re-asserts the
shared checkout is clean and on `main` before any bookkeeping runs, aborting
with a `blocked` result if it cannot be repaired (R27)). Earlier entry:
2026-07-07, #678: integration-gate hardening — mutation-surface/blocked-pattern
violations are now a hard block (R12a), the smoke gate fails safe on
missing/empty suites and harness exceptions instead of passing (R11 rewritten),
a suite-shrink guard closes the repair-loop-weakening path (R11b), and every
bookkeeping push that runs outside the gated integration path is
scope-constrained to its intended file(s) (R16a). Earlier entry: 2026-07-06,
#666: R11a auto-commit safety net for uncommitted subagent work added; #653:
R8-R15 cycle-branch isolation implemented in code; R10/R11 corrected to
describe the full-pytest gate that was already running)._

## Purpose

The subagent bridge is the LLM-execution arm of the self-evolving runtime. The
coordinator (a lightweight bookkeeper that does not write code) queues bounded
subagent requests under the state root; the bridge picks the oldest queued
request, builds a concrete prompt from the source artifact, and runs the
mandatory local executor model to actually implement the change. Each cycle is
isolated on its own git branch off `origin/main`, gated by a bounded smoke
selection (import-smoke of changed files + tests they affect + a small fixed
core smoke set — R10, #686), and integrated into `main` only when that gate
passes — so a broken or unverified cycle never reaches `main`.

> This is **product** runtime behavior. Explanatory detail and host operations
> are in `docs/SYSTEM_OPERATION_REFERENCE.md` §6–§7 (`EEEPC_AGENT_RUNTIME_INSTRUCTIONS.md`
> was folded there and removed 2026-07-05, #613; recoverable from git history).
> Where docs and the running
> code disagree, `nanobot/runtime/bridge.py` is authoritative (moved from
> `scripts/eeepc_self_evolving_subagent_bridge.py` in #599; that path is now a
> thin wrapper kept for the unchanged systemd/deploy contract).

## Requirements

### Request selection
- R1. The bridge SHALL select the oldest queued/pending request under
  `state/subagents/requests/` whose status is `queued` or `pending` and that has
  not already been handled by a real executor run.
- R2. The bridge SHALL treat coordinator-produced blocked stubs as NOT handled
  (`_is_real_result`): a result with `result_status`/`status` = `blocked`,
  `terminal_reason` = `local_executor_unavailable`, `materialized_from` =
  `queued_request_terminalizer`, or `blocker.reason` = `local_executor_unavailable`
  SHALL NOT mark its request as handled, leaving it eligible for the bridge.
- R3. The bridge SHALL record its own completion with a `handled_<request_id>.txt`
  marker and SHALL skip a request whose marker already exists (idempotent — no
  duplicate execution of the same request).

### LLM proposer (#707, GO 2026-07-13)

When the deterministic backlog is exhausted, the bridge itself sources the
next bounded task from an LLM instead of idling. Design + go/no-go evidence:
`docs/changes/archive/707-state-light-proposer/` (proposal.md, results.md).

- R28. Behind the `SELFEVO_LLM_PROPOSER_ENABLED` kill-switch (default OFF;
  ON in the live env since the 2026-07-13 GO decision), the bridge SHALL
  invoke `nanobot/runtime/llm_proposer.py` to propose exactly ONE bounded
  task when no genuinely-new queued request remains — i.e. after a run whose
  pending requests were all skipped as duplicates, or when the queue is
  empty — and SHALL NOT stack proposals (no new proposal while a
  proposer-authored request is still queued). **#760:** in demand-driven
  mode (R39, default ON) the "when to fire" half of this requirement is
  superseded by the demand gate — the enabled and anti-stacking clauses are
  unchanged, but the queue-empty / dup-streak firing conditions apply only
  with `SELFEVO_DEMAND_DRIVEN_ENABLED=0`.
- R29. A proposal SHALL be written as a request JSON identical in shape to a
  planner-produced request (companion `llm-proposed-<id>.json` artifact in
  the `next_bounded_candidate` shape), so the entire downstream path —
  dedup, branch isolation, spawn, gate, integration — is unchanged and
  shared. The proposal SHALL name exactly one `target_path` under the
  allowed mutation surfaces, and sizing SHALL be validated before write
  (retry once with feedback, bounded total LLM calls per run).
- R30. The proposer SHALL self-dedup before writing: against the recent git
  log, against its own recently-proposed titles, and via a
  rejected-themes digest in the prompt context; when the goal text still
  lists numbered priorities it SHALL prefer them verbatim over invention.
  **#773:** done-truth for demand-era priorities is the ledger chain, not
  text: in demand mode the model refines proposal titles, so integration
  commits carry no verbatim `Priority N —` label and text-based done
  evidence (#748/#769 label/basename heuristics) structurally cannot retire
  a completed goal_text priority. `filter_completed_priorities_from_goal_text`
  therefore accepts an optional `state_dir` and checks the R39 completed
  sidecar FIRST (the priority's derived demand id — the same kind+summary
  hash `demand._priority_items` computes); the git-log heuristics remain for
  pre-demand-era priorities and for callers without a `state_dir`
  (fail-open, unchanged behavior).
- R31. Every proposal SHALL be recorded as a `proposed` row in the cycle
  ledger (`<STATE_DIR>/ledger/cycles.jsonl`, #720), making
  proposal→integration traceable per `request_id`.
- R32. Pre-spawn dedup SHALL be target_path-aware (#736): if the request
  names a target path that does not exist in the instance repo, the
  keyword-overlap heuristic SHALL be bypassed (the task cannot be already
  done); if the target exists, the heuristic SHALL be scoped to commits
  touching that path. Requests without a target path keep the whole-log
  heuristic. All proposer plumbing is fail-open: a proposer or dedup error
  degrades to the pre-#707 behavior, never blocks the run.
- R33. A single bridge run SHALL bulk-skip consecutive duplicate requests
  (bounded by `SUBAGENT_BRIDGE_MAX_SKIPS_PER_RUN`, default 10) so a stale
  queue tail cannot starve a fresh proposal for hours (#733).
- R34 (issue #749). The proposer's context (`build_context`) SHALL be
  extended with a bounded, deterministically-generated (no LLM call)
  inventory of the instance repo's existing scripts/surfaces, so the
  proposer stops proposing near-duplicate work under a new name — the
  confirmed failure: `monitor_memory.py` shipped four hours after
  `track_memory.py`, after the earlier success had scrolled out of the
  15-row ledger digest window (R31). `nanobot/runtime/system_map.py`
  provides two pieces:
  - `generate_system_map`/`update_system_map` maintain a self-evolving-
    instance-repo artifact, `docs/SYSTEM_MAP.md` — an `## Inventory`
    section (one line per script, description from its docstring/leading
    comment), a `## Near-duplicate candidates` section (scripts grouped by
    basename-token overlap coefficient >= 0.5), and any `## Backlog` /
    `## Completed` sections carried over verbatim from the previous map so
    the machinery never destroys hand-curated content. `update_system_map`
    is watermark-gated (instance-repo git HEAD + a content sha256) so an
    unchanged HEAD, or a HEAD change that regenerates byte-identical
    content, costs no write. This module does not commit — the loop's own
    cycle commits changes.
  - `llm_proposer.maybe_propose` calls `update_system_map` unconditionally
    on every invocation (bridge.py already calls `maybe_propose`
    unconditionally every cycle regardless of R28's kill-switch), so the
    map stays fresh independent of whether the proposer itself is enabled;
    `build_context` appends the map's inventory (or, if no map file exists
    yet, an inventory generated directly the same deterministic way) as a
    separately-bounded section so a large inventory cannot truncate the
    goal_text/ledger sections R29-R31 rely on. Fail-open throughout: any
    error yields an empty/omitted section, never blocks a proposal.
  - **Ownership deferral (#749 follow-up).** The instance repo MAY ship its
    own generator for `docs/SYSTEM_MAP.md` (observed live: a
    `scripts/generate_system_map.py` seeded via goal_text, writing a richer
    thematic format). `update_system_map` SHALL defer entirely to any
    foreign generator that already claims the file: before any regeneration
    work, it reads the existing file and, if non-empty content lacks this
    module's own generated-note marker line, returns `False` immediately —
    no write, no watermark update — regardless of whether HEAD has moved. An
    absent or empty file is NOT foreign (nothing to defer to yet) and is
    still adopted as before. This costs one small file read per cycle even
    on the cheapest HEAD-unchanged path, accepted as the price of never
    clobbering a foreign map. Correspondingly, `build_context`'s inventory
    section SHALL fall back to direct generation (the same
    `system_map.inventory_lines`, no LLM call) whenever the on-disk map's
    `## Inventory` section cannot be parsed (foreign format, or a rare
    empty section) — the proposer's inventory context must never silently
    go empty just because a foreign generator changed the file's shape.
- R35 (issue #750). In addition to R32/R33's exact-title/keyword checks, the
  pre-spawn dedup sequence SHALL also consult a local FTS5 **existence
  index** (`nanobot/runtime/existence_index.py`, stdlib `sqlite3` only, no
  new dependency) for SEMANTIC near-duplicates whose wording does not
  literally overlap a past commit or result title (e.g. a proposed
  "monitor RAM and memory usage" script while `track_memory.py` already
  exists). The index incrementally reindexes on every cycle from: script
  filenames + first docstring line under the instance repo's `scripts/`,
  `surfaces/` and `tests/` (`tests/` added in #757); past attempt titles
  from `<state_dir>/subagents/results/*.json`; and hypothesis titles from
  `<state_dir>/hypotheses/backlog.json` and
  `<state_dir>/research/hypotheses.json`. A `script`-kind FTS candidate is
  flagged duplicate-suspect only if it shares >= 2 of its 4+-character
  content words with the proposal (generic words stripped) AND its path is
  not the proposal's own `target_path` (that same-file case stays R32's
  job). **Kind-aware tests-for-X rule (#757):** matching SHALL key on the
  proposal's derived intent (`derive_intent`: action-class + target). A
  proposal whose intent is `test-for(<subject>)` — target under `tests/`,
  or a "test suite for X"/"unit tests for X" title — SHALL NEVER be flagged
  against a `scripts/`/`surfaces/` hit (a test-suite title must name the
  script it tests, so that word overlap is guaranteed; writing tests for
  existing code is new work, not a duplicate). It MAY only be flagged
  against another test artifact (a hit whose path is under `tests/`) or a
  prior attempt title that is itself test-for the same subject.
  Symmetrically, a non-test proposal is never flagged against a `tests/`
  hit. A duplicate-suspect hit is recorded exactly like R32/R33's
  `skipped_duplicate` decisions, with
  `matched_against = "existence-index:<path>"` distinguishing it in the
  cycle ledger. Behind the `SELFEVO_EXISTENCE_INDEX_ENABLED` kill-switch
  (default ON); fail-open on any internal error (missing/corrupt index,
  missing source directories) — degrades to R32/R33-only behavior, never
  blocks a proposal it failed to evaluate.
- R36 (issue #751). Every proposal SHALL name what goal it serves, and the
  proposer MAY honestly decline to propose when nothing serves a goal, so
  goal-alignment is queryable and a saturated theme space produces a
  recorded skip instead of invented filler work:
  - **`serves` field.** The proposal schema (`_PROPOSER_SYSTEM_PROMPT`)
    SHALL require a fourth key, `serves`, naming what the task serves:
    `"priority <N>"` (a numbered `goal_text.json` priority), `"vector 1"` /
    `"vector 2"` (Vector 1 = self-optimization, Vector 2 = owner utility;
    optionally suffixed with a 3-8 word justification after a colon), or
    `"hypothesis <id-or-short-title>"` naming an entry surfaced by the
    hypothesis-backlog section below. `validate_sizing` SHALL reject a
    missing/empty `serves`, one over 160 characters, or one not starting
    (case-insensitively) with one of those four prefixes — the same
    reject/retry-once/fail-closed path as the other schema checks (R29).
    Every `proposed` ledger row (R31) SHALL carry the accepted `serves`
    value (recorded in the ledger event only, not in the written request
    payload, to preserve the R29 request-schema-equality invariant);
    pre-#751 rows without `serves` read as class `"missing"`, never a
    crash. `scripts/loop_metrics_report.py` reports a goal-alignment
    section: a count of `proposed` rows per `serves`-class
    (`priority`/`vector 1`/`vector 2`/`hypothesis`/`missing`/`other`) over
    the report window, plus the count of honest no-op skips (below).
  - **Honest no-op.** The proposer prompt SHALL allow the LLM to reply
    `{"no_valuable_task": true, "reason": "<short>"}` instead of a proposal
    when nothing it could propose creates real value toward the goals
    (everything worthwhile is done, queued, or already listed). Accepting
    this reply SHALL append a distinct `proposer_skip` ledger event
    (`reason`, no `cycle_id` — no cycle/subagent request exists for a
    skipped cycle) and return with NO subagent request minted — deliberately
    a different phase than `proposed`, so it never pollutes R30's
    title-based dedup or the goal-alignment counts above. To bound this
    against a lazy model idling the loop indefinitely, the reply is only
    honored while `_consecutive_noop_streak` (counted from trailing
    `proposed`/`proposer_skip` ledger rows, not in-memory, so it survives a
    process restart) is under `_MAX_CONSECUTIVE_NOOP_SKIPS` (3); the next
    call is forced into normal proposal mode (the built context carries an
    explicit "you must propose" note), and even a model that still replies
    `no_valuable_task` in that state has the reply ignored and is treated as
    an ordinary schema violation (missing `task_title`), following the same
    reject/retry/fail-closed path as any other invalid proposal. `bridge.py`
    already invokes `maybe_propose` at most once per bridge cycle
    (timer-paced, ~10 min per R28's surrounding cadence) — sufficient
    pacing on its own; no additional rate limit was needed for this path.
  - **Hypothesis-backlog reader.** `nanobot/runtime/hypothesis_backlog.py`
    gives the proposer context a bounded `## Hypothesis backlog (candidate
    value sources)` section (top 5, one `- [<key>] <title>` line each) read
    from `<state_dir>/hypotheses/backlog.json` (primary) and
    `<state_dir>/research/hypotheses.json` (secondary) — both written every
    self-evolving cycle but, with the deterministic planner retired (#739),
    read by nothing else on the live path until this change. Candidates
    have a small lifecycle — `active` -> `answered` (evidenced by the
    resolving `cycle_id`) once a `serves: hypothesis <ref>` proposal's cycle
    reaches a `success` outcome, or `active` -> `stale` (dropped from the
    context) once untouched by any such proposal for 50 reconciliation
    passes or 14 days, whichever comes first — persisted in a sidecar
    `<state_dir>/hypotheses/lifecycle.json` this module owns exclusively
    (additive-only; never drops unknown keys), rather than inside
    `backlog.json` itself, which is fully regenerated every cycle by the
    coordinator and so cannot hold cross-cycle status without a
    read-modify-write change to that writer (out of this change's scope).
    Reconciliation is lazy — it runs as a side effect of every
    `build_context` call (once per proposer cycle) rather than at a
    dedicated cycle-outcome hook, since no such hook exists without
    invasive coordinator changes. Fail-open throughout: a missing/corrupt
    file degrades to an omitted section, never blocks a proposal.
- R37 (issue #757). The recent-failure suppression gate
  (`_recent_failure_match`, #716) SHALL key on structured intent before
  word overlap: when BOTH the proposal (title + `target_path`) and a
  historical failed title derive an (action-class, target) via
  `derive_intent`, differing targets are NOT a match (one skipped "Create
  test suite for X script" must not cascade over every later "Create unit
  tests for Y script" sharing the create/unit/tests/script word bag) and
  the same target IS a match (a reworded retry of the same work stays
  suppressed). If derivation fails on either side, the pre-#757
  keyword-overlap behavior applies unchanged (fail-open). The gate SHALL
  return the matched HISTORICAL title, and the `skipped_recent_failure`
  ledger row's `matched_against` SHALL record that historical title — not
  an echo of the proposal's own title.
- R38 (issue #762). None of `maybe_propose`'s rejection exits may be silent:
  each formerly-silent `return None` SHALL append a distinct
  `proposer_reject` ledger event (a sixth cycle-ledger phase alongside
  `proposed`/`started`/`dedup`/`outcome`/`proposer_skip`; like
  `proposer_skip` it carries no `cycle_id` and never pollutes R30's
  title-based dedup or the R36 goal-alignment counts) with `reason` ∈
  `empty_context` (context builder returned nothing), `sizing_rejected`
  (double `validate_sizing` failure, carrying the rejected `task_title`/
  `target_path` and the rejection detail), `self_dedup` (double
  `_is_duplicate_proposal` rejection — the live-saturation case where every
  cycle burned 2-3 LLM calls with zero ledger trace — carrying
  `task_title`/`target_path` and, per R37's discipline, `matched_against` =
  the git-log/ledger line the heuristic actually matched, not an echo of
  the proposal's own title), or `error` (the final catch-all, recorded
  inside the except block). Recording is fail-open — it can never raise or
  block a cycle, including from within the catch-all itself.
  `scripts/loop_metrics_report.py` reports a `proposer_reject`-by-reason
  breakdown in the goal-alignment section (legacy ledgers with no such rows
  read as zeros, never a crash). A saturation signal,
  `_consecutive_self_dedup_rejects` (trailing `self_dedup` rejects among
  the proposer's own decision rows, same ledger-backed construction as
  R36's `_consecutive_noop_streak`), is exported for #760's
  demand-exhaustion escalation to consume.
- R39 (issue #760). The proposer SHALL be demand-driven, not supply-driven:
  behind the `SELFEVO_DEMAND_DRIVEN_ENABLED` kill-switch (#750 pattern —
  default ON; the literal `"0"` restores the pre-#760 R28 firing conditions
  and prompt wholesale, whose code paths remain intact), the proposer works
  only when there is demand, and with no demand a bridge cycle makes ZERO
  LLM calls:
  - **Demand collection.** `nanobot/runtime/demand.py`'s `collect_demand`
    is deterministic (no LLM call) and fail-open, yielding structured items
    `{kind, id, summary, evidence, affected_path}` with a stable id (hash of
    kind+summary), in trust order: `priority` — remaining (non-completed)
    goal_text "Current priority targets" entries, done-filtering delegated
    verbatim to `cycle_planning.filter_completed_priorities_from_goal_text`
    (#748; preserves R30's operator-seeding wake-up); `defect` — real,
    recent failures: terminal ledger `outcome` rows with `failed`/`timeout`
    outcomes in the last 48h (`skipped-*` never counts), failed/blocked
    subagent result files with error text (bounded to the 50 most recently
    modified files), and instance-repo scripts that fail to byte-compile —
    the compile scan watermark-gated on the repo git HEAD exactly like R34's
    `update_system_map` (own sidecar, `<state_dir>/demand/
    py_compile_watermark.json`), so it costs nothing while HEAD is
    unchanged; `hypothesis` — ONLY hypotheses carrying measurement evidence
    (a non-empty `evidence` or `metric` field, or an `acceptance` naming a
    file that exists in the repo); the chronic boilerplate candidates
    ("Use one bounded subagent-assisted review...", "Synthesize one new
    bounded improvement candidate from retired lanes") never qualify
    (regression-pinned).
  - **Gate + idle heartbeat.** `should_propose` keeps the R28 enabled and
    anti-stacking gates, then requires `collect_demand` non-empty. When the
    only reason not to propose is empty demand, it appends ONE `idle`
    ledger row (`reason: no_demand`; a seventh cycle-ledger phase alongside
    `proposed`/`started`/`dedup`/`outcome`/`proposer_skip`/`proposer_reject`;
    no `cycle_id`; at most one per bridge cycle, fail-open write) — an idle
    cycle is thereby structurally distinguishable from a crash and from an
    LLM-declined `proposer_skip`. `scripts/loop_metrics_report.py` tolerates
    `idle` rows (no phantom cycles, no goal-alignment pollution).
  - **Select-and-refine contract.** In demand mode `build_context` leads
    with a separately-bounded `## Demand` section (kind, id, summary,
    quoted evidence per item; existing inventory/system-map/hypothesis
    sections are kept as duplicate-prevention context) and the system
    prompt instructs the model to select exactly ONE demand item, propose a
    bounded task addressing it, and set `serves` to `demand <id>` — or
    reply `no_valuable_task` if no item is addressable. Inventing work no
    demand item calls for is no longer offered (Vector 1/2 invention is
    retired from the prompt); `validate_sizing` accepts `demand <id>` as
    the primary `serves` form while tolerating the R36 legacy prefixes for
    one release. `proposed` and `proposer_reject` ledger rows carry the
    referenced `demand_id`.
  - **Exhaustion.** Once a demand item's proposals have been self-dedup-
    rejected 2+ times (matched via `demand_id` on R38's `self_dedup` reject
    rows), the item is marked exhausted in the schema-versioned sidecar
    `<state_dir>/demand/exhausted.json` and no longer presented. An expired
    entry keeps a `reset_at` marker so only rejects newer than the reset can
    re-exhaust the item. **Reset semantics (#771, live deadlock 2026-07-15
    21:33–22:31Z):** an exhausted entry SHALL reset on ANY of: (a) a
    terminal ledger `outcome: success` row NEWER than the entry's
    `exhausted_at` (any successful integration; `reset_at` is the success
    timestamp) — this closes the circularity where HEAD-move expiry never
    fired because the only demand item being exhausted meant nothing ever
    integrated; (b) a runtime release change — each entry records the
    running release id (the `/releases/<id>/` path segment of the resolved
    module path on the host, product version as dev fallback; unknown ids
    never trigger a reset), so rejects produced by since-fixed runtime bugs
    stop counting after the next deploy; (c) a repo HEAD move; (d) 24h
    elapsing (was 7 days — far too long as the sole deadlock escape).
    **Honest manual clear (#771):** a MISSING sidecar entry SHALL behave
    like a reset — when recomputing rejects for an item with no entry, only
    rejects newer than the newest of (last success outcome, 24h ago) count,
    so an operator deleting `entries` is not silently undone within one
    cycle by stale bug-era ledger rows. All fail-open: any error presents
    the item rather than hiding it.
  - **Completed (ledger-chain done-truth, #773, live P14 evidence
    2026-07-15/16).** The authoritative done-signal for a demand item is
    the ledger chain: a `proposed` row carrying its `demand_id` followed by
    a terminal `outcome: success` row for the same `cycle_id`. On every
    `collect_demand` run, new pairs from the CURRENT `ledger/cycles.jsonl`
    are folded into the schema-versioned sidecar
    `<state_dir>/demand/completed.json` (`demand-completed-v1`; entries map
    `demand_id → {cycle_id, ts, files_changed}`), append-only — an existing
    entry is never overwritten. This makes done-truth **rotation-proof by
    construction**: the midnight ledger rotation that blinds every
    single-file ledger reader (the #771/#772 success-reset blind spot)
    cannot un-complete a folded entry. Completed ids are dropped from ALL
    demand kinds BEFORE the exhausted filter — a completed item needs no
    exhaustion bookkeeping at all, and is never presented again regardless
    of what text-based git-log evidence says (in demand mode the model
    refines proposal titles, so #748/#769 label/basename evidence
    structurally never fires for these integrations).
    `cycle_planning.filter_completed_priorities_from_goal_text` consumes
    the same sidecar when given a `state_dir` (see R30 note). All
    fail-open: an unreadable sidecar or ledger degrades to prior behavior.

### Executor model
- R4. The bridge SHALL run the bounded subagent on the mandatory local executor
  model `un/qwen3.6-27b-mtp` (logical alias `gpt-5.3-codex`), configured
  through `SUBAGENT_BRIDGE_MODEL` / `config.tools.subagent.model`, calling the
  LiteLLM proxy directly. The executor model SHALL NOT be swapped for a
  remote/coordinator model.
  - History: through #637 the model was routed through an external `pi`
    binary profile (provider name `local_pi_cli`, historical alias
    `hermes_pi_qwen`) with `--no-tools`, which is functionally a single
    LiteLLM call. #641 removed that external-binary profile entirely — the
    runtime now has exactly one built-in executor path (this bridge /
    `queued_request_terminalizer`), with no dependency on `/usr/local/bin/pi`
    or any subprocess shell-out. Historical state artifacts that still carry
    the old provider names are never rewritten (migration spec R7).
- R5. `NANOBOT_SUBAGENT_EXECUTOR_COMMAND` SHALL NOT be set in `agent.service`.
  If set, the coordinator's in-process materializer runs a deterministic,
  no-LLM `bounded_subagent_executor` and writes a `completed` result before the
  bridge can claim the request — defeating real LLM execution.

### Executor autonomy contract

The local executor's system/developer instructions (formerly kept in two
standalone `docs/HERMES_AUTONOMY_*.md` files, folded here and removed
2026-07-05, #637) encode a short completion-discipline contract so a bounded
executor run does not stop early or hand off instead of acting:

- Every progress/status reply names the current time (from a tool), what is
  being done now, and — if work was delegated — what was delegated.
- The executor does not end a turn on a summary or handoff sentence
  (`"next I will"`, `"if you want"`, etc.) while an open bounded issue with no
  blocker remains; it moves to the next open issue in the same run instead.
- Every claimed action must have actually been performed in the same
  response/session — no reporting hypothetical future work as done.
- On a failed bounded attempt, the executor repairs or rolls back to a green
  baseline rather than leaving half-broken state while claiming progress.
- GitHub Issues remain the source of task truth; lifecycle state and
  rollout/proof links are updated on the issue when work advances.

### Prompt construction
- R6. `build_task` SHALL inline the content of the request's `source_artifact`
  (the materialized-improvement JSON) directly into the subagent prompt
  (truncated to ~4000 chars), so the subagent has concrete data and does not
  hunt the workspace for context.
- R7. When the cycle is isolated on a branch, the prompt SHALL include a
  mandatory branch-discipline addendum instructing the subagent to commit on the
  current branch and to NOT run `git checkout`/`switch`/`branch` or `git push`.

### Cycle isolation
- R8. Before spawning, the bridge SHALL isolate the cycle on a fresh branch
  `selfevo/cycle-<id>` created with `git checkout -B <branch> origin/main`
  (clean base + rollback) in the working repo `eeebot-self-evolving`.
- R9. If branch setup fails, the bridge SHALL fall back to the current branch and
  continue — branch-setup failure SHALL NOT break the loop.

### Smoke gate
- R10. After the subagent commits, the bridge SHALL gate the cycle with
  `_run_smoke_tests`, which runs a **BOUNDED** selection via the runtime's own
  interpreter (`sys.executable`, 300s timeout) inside the isolated checkout,
  computed by `_select_gate_tests(repo_root, changed_files)` from the cycle's
  changed-file set (`_changed_files_and_violations`, `pre_spawn_sha..HEAD`):
  1. **Import-smoke**: `sys.executable -m py_compile <changed .py files>` —
     a syntax/compile error fails the gate immediately, before pytest
     collection even starts. Only a compile check, deliberately not an actual
     import of arbitrary changed modules (which may have side effects);
     import-time errors are caught by phase 2's affected tests instead.
  2. **Targeted pytest**: `sys.executable -m pytest <test paths> -q --tb=native
     -p no:cacheprovider`, where `<test paths>` is the union of (a) each
     changed file's corresponding test module(s) — `scripts/foo.py` →
     `tests/test_foo.py`; any changed file's stem also fuzzy-matches
     `tests/test_*<stem>*.py`; a changed `tests/test_*.py` file selects
     itself — and (b) a small fixed **core smoke set**
     (`_CORE_SMOKE_TESTS`: `tests/test_import_hygiene.py`,
     `tests/test_config_schema.py`, `tests/test_config_paths.py`) that always
     runs regardless of what changed, for cross-cutting breakage.
  - **Rationale (#686):** the subagent's mutation surface is bounded to
    `scripts/`, `docs/`, `memory/`, `lessons/`, `tests/` — core `nanobot/` is
    hard-blocked (R12a) — so the bulk of the full suite (core `nanobot/`
    tests) cannot have been broken by a cycle; re-running it every cycle
    against a 300s gate timeout was pure waste (measured 601s for the full
    product suite on the host — #672's re-seed showstopper). Full-suite
    validation of core is **product CI** (every product PR) plus **re-seed-time
    verification**, not this per-cycle gate. This changes only WHICH tests the
    gate runs; the gate DECISION shape in `main()`
    (blocked → mutation → smoke → integrate) and every #678 property below are
    unchanged.
  The pytest subprocess (both phases) SHALL run with a sanitized environment
  (`_sanitized_smoke_env`), stripping every key starting with `STATE_DIR`,
  `NANOBOT_`, `SUBAGENT_`, `EEEBOT_`, `TARGET_WORKSPACE`, `LITELLM_`, `GOAL_`,
  `SOURCE_`, or `SELFEVO_` from `os.environ` before the call, rather than
  inheriting the bridge systemd unit's environment wholesale. (#668
  env-pollution finding: without sanitization, the subprocess inherits the
  bridge unit's `STATE_DIR` and friends, and target-repo tests that read
  process env to locate state observe LIVE production state instead of a
  hermetic fixture — deterministically reproduced via
  `tests/test_active_lane_continue.py` passing in a clean env and failing with
  the bridge env sourced, on identical code. The gate must evaluate the repo
  hermetically, not against live runtime state.)
- R11. If no commits landed on the cycle branch, the smoke gate SHALL be
  skipped entirely (nothing to test) and the cycle branch SHALL be discarded
  without touching `main`. A missing/empty `tests/` directory, an empty
  selection (`_select_gate_tests` returns no test paths at all — e.g. no
  changed file maps to an existing test AND none of `_CORE_SMOKE_TESTS`
  exists in this tree), a suite that collects zero tests, a pytest timeout,
  or any other harness exception (subprocess crash, `FileNotFoundError` for a
  missing `pytest`, an import-smoke `py_compile` failure, ...) SHALL **fail
  the gate** (`_run_smoke_tests` returns `(False, reason)`) — this runtime
  always has tests, so their absence, emptiness, or an unexplained harness
  failure is suspicious, never a benign skip. A changed file with no matching
  test is fine (the core smoke set still runs) — an empty selection is only
  reached when there is truly nothing to check, and that is a hard fail, not
  an auto-pass.
  - History: an earlier draft of this requirement (through #653) described an
    import-only syntax check of only the changed `.py` files. That was never
    implemented; `_run_smoke_tests` ran the full suite from #653 through
    #686. #653 corrected the requirement text to match the running code
    (CLAUDE.md "executable truth wins") rather than implementing the cheaper
    import-only check, since the full suite was then a strictly stronger
    (if increasingly unaffordable) gate.
  - **Reversal (#678, finding 2/4):** through 2026-07-06 this requirement said
    the opposite — missing tests, an empty suite, a timeout-adjacent harness
    exception, and a missing `pytest` binary all `return True` ("skip" =
    pass). A two-lens adversarial security review confirmed this let a cycle
    that `rm -rf tests/` (or otherwise emptied the suite) turn a failing
    change green, and a bare `except Exception: return True` meant a pytest
    subprocess crash (OOM/OSError/disk-full) integrated untested code. Both
    now fail closed; only a genuine `_sp.TimeoutExpired` was already `False`
    and is unchanged.
  - **Narrowed (#686):** the full-suite gate described immediately above
    (2026-07-06 through 2026-07-07) is replaced by the bounded selection in
    R10. Every fail-safe property in this requirement is preserved against
    the new, smaller selection — an emptied selection is treated exactly like
    the emptied-suite case it replaces.
- R11b. Before running pytest, the gate SHALL compare a hermetic proxy for
  suite size — the count of `def test_` occurrences across `tests/**/*.py`
  (`_count_tests`) — in the cycle's working tree against the same count
  captured at `origin/main` before the cycle branch was created
  (`_count_tests_at_ref`, read via `git show <ref>:<path>`, no checkout
  needed). If the cycle's count is lower, the gate SHALL fail immediately
  with a `suite-shrink guard` reason, without needing to run pytest at all.
  This check SHALL be re-applied on every gate evaluation, including each
  closed-loop repair retry (R12), not only the first — otherwise a repair
  turn could iteratively delete or weaken tests across revisions until the
  (now-smaller) suite happens to pass. A baseline of `0` (unreadable ref, no
  `tests/` at `origin/main`) never blocks — there is nothing to compare
  against.
- R11a. If no commits landed on the cycle branch but the working tree is dirty
  (`git status --porcelain` non-empty), the bridge SHALL commit those changes
  itself (`_auto_commit_uncommitted_work`) before applying R11 — excluding any
  file matching the same `_BLOCKED_FILE_PATTERNS` used by
  `_validate_mutation_surfaces` (logged, never staged) — then recount commits
  and proceed through the normal smoke gate / R12-R15 flow unchanged. Found
  live during #656 verification (2026-07-06): a subagent implemented real
  changes via `edit_file` but ended its turn without running `git commit`;
  because `cycle_commit_count` stayed `0`, the gate was skipped and the
  `finally`-block restore-to-main discarded the work outright, so every
  following cycle re-did (and re-lost) the same task. This is a bridge-level
  safety net, not a prompt-only fix — R7's branch-discipline prompt also
  reinforces "commit is the final step" so the gap is rarer, but the bridge
  no longer relies on the subagent remembering to commit (#666).

### Integration to main
- R12. The bridge SHALL integrate the cycle branch into `main` (merge `--no-ff`
  the subagent's HEAD onto a fresh `origin/main`, then push) ONLY when the smoke
  gate passes **and** R12a's mutation-surface/blocked-pattern check is clean.
- R12a. The bridge SHALL apply `_validate_mutation_surfaces` to every file
  changed across ALL of the cycle's commits (the subagent's own `git commit`s,
  not only the R11a auto-commit fallback) and treat any violation as a **hard
  block on integration** — `_integrate_cycle_to_main` SHALL NOT be called, the
  cycle branch SHALL be kept for forensics exactly like a failed smoke gate
  (R13), and `main` SHALL be left untouched. Two violation classes are
  distinguished for the resulting rollback reason: a filename matching
  `_BLOCKED_FILE_PATTERNS` (secret-shaped names, lockfiles, `.git` internals,
  ...) anywhere in the changed files → `blocked_file_present`; a file outside
  `_ALLOWED_PATH_PREFIXES` (`surfaces/`, `scripts/`, `memory/`, `lessons/`,
  `docs/`, `tests/`) → `mutation_surface_violation`.
  - **Reversal (#678, findings 1 and 3):** through 2026-07-06,
    `_validate_mutation_surfaces` violations were only printed —
    integration was decided solely by the smoke gate, so a cycle editing core
    `nanobot/`, `.github/workflows/`, `pyproject.toml`, or `bridge.py` itself
    would integrate as long as pytest happened to pass. Separately, the
    `_BLOCKED_FILE_PATTERNS` secret-filename filter ran only inside the R11a
    auto-commit fallback (i.e. only when the subagent forgot to commit); a
    subagent that committed normally had no secret-pattern screening at all.
    Both gaps are closed by the same enforcement point, since
    `_validate_mutation_surfaces` already checked blocked patterns across all
    changed files — the fix was making its output authoritative rather than
    advisory.
- R13. When the smoke gate fails, the bridge SHALL leave the commits on the cycle
  branch, SHALL keep `main` clean (no merge/push), and SHALL record a learning
  artifact; the cycle branch SHALL be kept for inspection.
- R14. A merge conflict or push failure during integration SHALL abort cleanly,
  keep the cycle branch, and SHALL NOT corrupt `main`.
- R15. The bridge SHALL delete the cycle branch only after it has been integrated
  into `main`; on non-integration it SHALL return to `main` and keep the branch.

### Result evidence
- R16. After each run the bridge SHALL write a real `bridge_llm_execution` result
  to `state/subagents/results/` (with `commits_pushed`, `files_changed`,
  `backlog_title`, `result_status`) so the coordinator can observe that a real
  subagent ran rather than only a blocked stub. The result SHALL also carry a
  `rollback` record — `{"integrated": bool, "cycle_branch": str,
  "main_sha_before": str, "main_sha_after": str, "reason": str | None,
  "auto_committed": bool}` — so integration/non-integration is git-verifiable
  from the artifact alone: `main_sha_before == main_sha_after` whenever
  `integrated` is `false`. `auto_committed` is `true` when R11a fired for this
  cycle (#666). `commits_pushed` counts only commits that reached
  `origin/main` (i.e. it is `0` whenever `integrated` is `false`, even if the
  subagent committed on the cycle branch) — this is the one semantic change
  from the pre-#653 field, which counted any subagent commit regardless of
  whether it survived the gate.
- R16a. Several bridge code paths commit and `git push origin main` directly,
  with **no smoke gate at all**: the already-done bookkeeping mark (runs before
  `_setup_cycle_branch`, on most cycles), the post-integration backlog-done
  safety net, the memory archiver (which **executes** `scripts/memory_archiver.py`
  from the target repo), and the structured-lesson recorder. Each such push
  SHALL be preceded by `_diff_against_remote_touches_only(repo, "origin/main",
  allowed)`, comparing the diff about to be published against an explicit
  allow-set for that path (`{"memory/MEMORY.md"}` for the already-done and
  backlog-done paths, the archiver's own declared `files_changed` output for
  the archiver, `{"lessons/lessons.yaml"}` for the lesson recorder). The push
  SHALL be skipped (logged, not raised) whenever the diff is empty or touches
  anything outside the allow-set — defense-in-depth, since none of these paths
  otherwise has a gate standing between a commit and `origin/main`.
  - **Added (#678, findings 5 and 6):** previously each of these four sites was
    a bare, unconstrained `git push origin main` — a bug in the bookkeeping
    logic (or an archiver script mutating something unexpected) had a direct,
    ungated path to `main`. This requirement does not change the intended
    (successful) behavior of any of the four bookkeeping flows, only adds a
    refusal on an out-of-scope diff.

### Tool harness — phase 1 (read-only tools, #643)
- R17. `nanobot.runtime.subagent_materializer.materialize_subagent_requests`
  SHALL run the in-process phase-1 tool harness
  (`nanobot.runtime.tool_harness`) only when a request's `profile` field is
  exactly `tool_harness`; every other profile SHALL take the pre-existing
  path (configured external executor, or the blocked-stub
  `queued_request_terminalizer` path) completely unaffected.
- R18. The phase-1 tool set SHALL be exactly `read`, `grep`, `ls` —
  read-only, no mutation, no command execution. `edit`/`write` (phase 2) and
  command execution (phase 3) remain gated per
  `docs/changes/643-subagent-tool-harness/design.md`.
- R19. Every tool call SHALL resolve its path argument
  (`Path.resolve()`, following symlinks) and verify the resolved path is a
  descendant of the workspace root *before* any I/O. An escape (`..`,
  absolute path outside root, symlink pointing outside) SHALL be vetoed —
  the model SHALL see a tool-result string explaining the veto, and the loop
  SHALL continue; the harness SHALL NOT crash or raise on an escape attempt.
- R20. `read` and `grep` output SHALL be truncated by one shared,
  deterministic head-tail truncation function (2000 lines / 50KB defaults)
  whose `{truncated, total_lines, total_bytes}` metadata is surfaced to the
  model in the tool result, never silently dropped.
- R21. No tool call SHALL raise an exception into the turn loop. Bad paths,
  invalid regexes, missing files, and vetoes SHALL all become normal
  tool-result text the model sees on its next turn.
- R22. Exactly one veto hook (`before_tool_call`) SHALL sit between "model
  requested a tool call" and "tool call executes", checking (a) the harness's
  own tool-call budget and (b) path confinement. Tools themselves SHALL stay
  policy-free.
- R23. The harness loop SHALL NOT invent a second budget/stop-reason system:
  it SHALL record one of the stop reasons already enumerated by
  `nanobot.runtime.stop_guards` (`gate_clean` when the model stops calling
  tools on its own, `max_iterations`, or `budget_tool_calls`) using the caps
  `SubagentToolConfig.harness_max_iterations` (default 8) and
  `harness_max_tool_calls` (default 24). Exception: an LLM-call failure is not
  a cycle-stall concern `stop_guards` models, so it is the one harness-local
  stop reason, `llm_error` (`nanobot.runtime.tool_harness.STOP_REASON_LLM_ERROR`)
  — set when `chat_with_retry` exhausts retries and returns
  `finish_reason="error"` instead of raising. The loop SHALL break
  immediately in that case (`run_tool_harness_request` returns `ok=False`,
  and `subagent_materializer._run_tool_harness` maps it to
  `failure_reason="tool_harness_llm_error"`, distinct from
  `tool_harness_incomplete`) rather than reporting the failed run as
  `gate_clean`/`completed` (found live during #643 phase-1 verification: an
  `un/qwen` model-group outage was silently recorded as a completed run).
- R24. Every tool call (request, allow/veto decision, result byte size,
  truncation flag) SHALL be appended to a per-request JSONL sidecar at
  `state/subagents/tool_calls/<request_id>.jsonl`. The result JSON SHALL
  carry only the bounded summary fields `tool_calls_count`,
  `tool_call_journal` (path to the sidecar), and `stop_reason` — full detail
  lives in the sidecar, not inlined into the result artifact.
- R25. The harness workspace root SHALL be the cycle's existing isolated
  checkout (`state_root.parent / "eeebot-self-evolving"`, the same
  convention `nanobot/runtime/bridge.py` already uses), overridable
  per-request via a `workspace_root` field for tests. The harness SHALL NOT
  touch `_setup_cycle_branch`, the smoke gate, or `_integrate_cycle_to_main`
  — those remain the bridge's exclusive authority. This convention applies to
  both `SubagentManager` spawns `nanobot/runtime/bridge.py` makes: the main
  implementation-turn `mgr = SubagentManager(...)` and the repair-turn
  `_repair_mgr = _SM2(...)`. Both pass `workspace=_selfevo_repo` (#718) —
  never `TARGET_WORKSPACE` (the deployed release tree, which the bridge
  never syncs from `_selfevo_repo`, so anything a subagent authored there
  under the old `workspace=TARGET_WORKSPACE` was committed, gated, and
  integrated nowhere). `TARGET_WORKSPACE` remains in use only for bridge
  bookkeeping unrelated to subagent writes (the `goal_text.json` fallback
  read, the `.nanobot/subagents` directory, and a diagnostic path print).

### Concurrency and checkout-state defense-in-depth (#680)
- R26. `main()` SHALL hold an exclusive, non-blocking `flock` on
  `<STATE_DIR>/bridge.lock` for the duration of the cycle, acquired before any
  repo work (before `find_pending_request`). Systemd's `Type=oneshot`
  single-unit semantics are the primary defense against two bridge processes
  racing through `_setup_cycle_branch`/`_git_cmd` on the same shared
  checkout; the lock is defense-in-depth for an out-of-band invocation (e.g.
  a manual `python -m nanobot.runtime.bridge`) overlapping a timer-triggered
  run, which can take up to ~3000s plus repair turns. If the lock is already
  held, `main()` SHALL log one line and exit cleanly (`0`, not an error — a
  concurrent run is expected, not a fault) without touching `STATE_DIR` or
  the git checkout. On a platform without `fcntl` (non-POSIX; the eeepc host
  is always Linux), locking SHALL degrade to a no-op with a logged warning
  rather than hard-failing the cycle.
- R27. Before `find_pending_request`/`_task_already_done` run, the bridge
  SHALL assert the shared `eeebot-self-evolving` checkout is on `main` with a
  clean tree, re-running `_restore_to_main` defensively if not. This guards
  against a prior cycle whose `_restore_to_main` failed twice (R13/R15's
  `finally` block only `WARN`s, it does not abort — see
  `nanobot/runtime/bridge.py` around the cycle-branch `finally`): without
  this precondition, the next invocation would proceed with the checkout
  still on a stray `selfevo/cycle-<id>` branch, and the `_task_already_done`
  bookkeeping commit would land on that branch and be silently discarded the
  moment `_setup_cycle_branch`'s own `checkout -B ... origin/main` runs. If
  the defensive restore still fails, the bridge SHALL NOT proceed with the
  cycle — it SHALL write a `blocked` result (`rollback.reason =
  "head_on_main_precondition_failed"`) and return without spawning a
  subagent. A checkout that does not exist yet (not yet cloned) is not a
  stray-branch condition and SHALL be left to `_setup_cycle_branch`'s
  existing `repo_missing` handling.

> **Journald timestamp gotcha (#620):** under systemd, stdout/stderr are a pipe
> to the journal, and Python fully-buffers a piped stream by default. During a
> 2026-07-04 token-rotation incident, a stale `auto-push` print line was
> journaled minutes after the event it described, which sent the investigation
> down a wrong path. The bridge now calls
> `sys.stdout.reconfigure(line_buffering=True)` / same for stderr at process
> start (`cli_main`), so journal timestamps are trustworthy going forward.

## Immutable safety shell (loop-independent)

_Added 2026-07-08, #703 (loop-redesign ticket D, stacked on the #702
architecture decision). This section does not introduce new behavior — every
invariant below is already implemented and enforced by the R-requirements
above; it exists to name the subset that is **frozen** and **loop-independent**:
it applies identically to the current control-plane loop, the ledger-based
shadow experiment (#706), and any eventual replacement core loop (#707), and
none of them — nor #704's ledger design, nor #705's metrics, nor #708's
optional analytics migration — may relax, bypass, or reinterpret it. A loop
redesign may **consume** this shell (spawn subagents, read its results) but
has no authority to weaken it; any change to these invariants requires its own
issue and an explicit amendment of this section, not an incidental side effect
of loop work. Per the #702 decision record §4, this is the canonical detailed
freeze that record points to._

- **S1. Green-only integration.** `origin/main` advances only when the bounded
  smoke gate passes; it never advances on a red gate, a harness error, or a
  timeout (R10-R13; the gate fails safe, R11/R11b). Implemented by
  `_run_smoke_tests`, `_run_smoke_tests_with_shrink_guard`, and the
  gate-decision shape in `main()` (blocked → mutation → smoke → integrate,
  R10-R15); `_integrate_cycle_to_main` is the only path that can move `main`
  and is called only after both gates are clean.
- **S2. Protected paths / mutation surface.** A subagent cannot land an edit to
  core `nanobot/`, CI (`.github/`), `pyproject.toml`, or the gate/bridge
  itself (`nanobot/runtime/bridge.py`) — any file outside
  `_ALLOWED_PATH_PREFIXES` (`surfaces/`, `scripts/`, `memory/`, `lessons/`,
  `docs/`, `tests/`) is a hard block on integration (R12a), enforced by
  `_validate_mutation_surfaces` and checked across every commit on the cycle
  branch, including repair-turn commits, not only the first.
- **S3. No-secret checks.** A filename matching `_BLOCKED_FILE_PATTERNS`
  (secret-shaped names, credential/token patterns, lockfiles, `.git`
  internals, ...) anywhere in the changed-file set is a hard block on
  integration (R12a), enforced by the same `_validate_mutation_surfaces` call
  as S2, and additionally screened out of the R11a auto-commit path by
  `_auto_commit_uncommitted_work` before it ever reaches a commit.
- **S4. Suite-shrink guard.** A cycle cannot weaken the very suite it is
  judged by: `_run_smoke_tests_with_shrink_guard` compares `_count_tests`
  against the `origin/main` baseline (`_count_tests_at_ref`) before every gate
  evaluation, including each repair retry (R11b), and fails the gate
  immediately — without running pytest — if the count dropped.
- **S5. Git-verifiable rollback record.** Every cycle's result carries a
  `rollback` record — `{"integrated", "cycle_branch", "main_sha_before",
  "main_sha_after", "reason", "auto_committed"}` — written by
  `_write_bridge_completed_result`, such that `main_sha_before == main_sha_after`
  whenever `integrated` is false (R16). `_cleanup_cycle_branch` deletes the
  cycle branch only after a successful integration (R15); otherwise the branch
  is retained for forensics.
- **S6. Concurrency lock + exactly one bounded subagent per cycle.**
  `_acquire_bridge_lock` holds an exclusive, non-blocking `flock` on
  `bridge.lock` for the duration of the cycle (R26); a contended lock exits
  cleanly without touching the checkout. `main()` spawns exactly one subagent
  per cycle onto one fresh `selfevo/cycle-<id>` branch (`_setup_cycle_branch`,
  R8), and the HEAD-on-main precondition (`_restore_to_main`, R27) guards
  against a stray branch left by a prior failed restore before the next cycle
  starts.
- **S7. Stop-guard time/iteration budgets.** The subagent's own turn loop is
  bounded by `config.agents.defaults.max_tool_iterations` (both the initial
  spawn and each repair turn); the bounded gate's repair loop is capped by
  `SUBAGENT_BRIDGE_MAX_REVISIONS` / `stop_guards.REVISION_CAP_DEFAULT` via
  `stop_guards.revision_outcome` (recorded in the result's `revisions` field).
  These are the `nanobot.runtime.stop_guards` budgets already enumerated for
  the tool-harness path (R23) applied to the bridge's own executor and repair
  loop.
- **S8. Bounded gate sized to the host per-cycle budget.** The smoke gate runs
  a bounded selection (`_select_gate_tests`: import-smoke of changed files +
  their affected tests + the fixed `_CORE_SMOKE_TESTS` set) inside a 300s
  timeout, not the full `pytest tests/` suite (R10; #686 rationale: the
  mutation surface is already bounded by S2, so full-suite validation of core
  belongs to product CI and re-seed-time verification, not a per-cycle gate
  the full suite no longer fits).

## Scenarios

### Scenario: blocked stub does not suppress a real run
- Given a queued request whose only result is a coordinator blocked stub
  (`terminal_reason = local_executor_unavailable`)
- When the bridge selects a pending request
- Then `_is_real_result` rejects the stub and the bridge picks up that request
  for real LLM execution.

### Scenario: passing smoke gate integrates to main
- Given a subagent committed on `selfevo/cycle-<id>`
- When the full `pytest tests/` gate passes
- Then the bridge merges the cycle HEAD into `main`, pushes `origin/main`, and
  deletes the cycle branch.

### Scenario: failing smoke gate keeps main clean
- Given a subagent commit that breaks the test suite
- When the smoke gate fails (after repair attempts are exhausted)
- Then the commits remain on the cycle branch, `main` is not modified (verified
  by `origin/main`'s SHA before/after the cycle), a `result_status="blocked"`
  artifact records `rollback.integrated=false`, and the cycle branch is
  retained for inspection.

### Scenario: mutation-surface violation blocks integration
- Given a subagent commit on `selfevo/cycle-<id>` touches a file outside
  `_ALLOWED_PATH_PREFIXES` (e.g. `nanobot/foo.py`) or matching
  `_BLOCKED_FILE_PATTERNS` (e.g. `id_rsa`)
- When the bridge evaluates the cycle for integration, regardless of whether
  the smoke gate itself passed
- Then `_integrate_cycle_to_main` is never called, `main` is left untouched,
  and the cycle branch is kept for forensics with rollback reason
  `mutation_surface_violation` or `blocked_file_present` respectively.

### Scenario: uncommitted subagent work is auto-committed before the gate
- Given a subagent edited files on `selfevo/cycle-<id>` via `edit_file`/`write_file`
  but ended its turn without running `git commit` (dirty tree, `cycle_commit_count == 0`)
- When the bridge checks for new commits after the subagent run
- Then `_auto_commit_uncommitted_work` commits the dirty changes (excluding any
  `_BLOCKED_FILE_PATTERNS` match) as `selfevo: auto-commit uncommitted subagent
  work — <title>`, the commit count is re-derived, and the normal smoke
  gate/integration flow (R10-R15) proceeds exactly as if the subagent had
  committed itself.

### Scenario: idempotent re-run
- Given a request already has a `handled_<id>.txt` marker
- When the bridge runs again
- Then it prints `already_handled` and does not re-spawn the subagent.

## References

- Reference docs: `docs/SYSTEM_OPERATION_REFERENCE.md` §6 (subagent bridge) and
  §7 (models/topology); `EEEPC_AGENT_RUNTIME_INSTRUCTIONS.md`
  ("Subagent bridge — architecture and troubleshooting") was folded there and
  removed 2026-07-05 (#613; recoverable from git history). The executor
  autonomy contract above was folded from `docs/HERMES_AUTONOMY_CHECKLIST.md`
  and `docs/HERMES_AUTONOMY_INSTRUCTION_SNIPPET.md`, removed 2026-07-05
  (#637; recoverable from git history).
- Code (authoritative): `nanobot/runtime/bridge.py`
  (`main`, `find_pending_request`, `_is_real_result`, `build_task`,
  `_setup_cycle_branch`, `_run_smoke_tests`, `_run_smoke_tests_with_shrink_guard`,
  `_count_tests`, `_count_tests_at_ref`, `_validate_mutation_surfaces`,
  `_diff_against_remote_touches_only`, `_integrate_cycle_to_main`,
  `_cleanup_cycle_branch`, `_auto_commit_uncommitted_work`).
  `scripts/eeepc_self_evolving_subagent_bridge.py`
  is a thin wrapper that calls `nanobot.runtime.bridge.cli_main`.
- Related specs: `docs/specs/self-evolving-runtime/spec.md`,
  `docs/specs/host-runtime/spec.md`, `promotion-and-release`, `model-routing`.
- Loop-redesign set (#702-#708): `docs/changes/702-ledger-loop-architecture-
  decision/decision.md` (direction + deprecation of control-plane patching);
  `docs/changes/703-safety-shell-invariants/precheck-contract.md` (per-cycle
  precheck rules that run before a subagent spawn) and
  `.../test-coverage-map.md` (invariant → existing test mapping) — both freeze
  loop-independent constraints alongside the "Immutable safety shell" section
  above.
