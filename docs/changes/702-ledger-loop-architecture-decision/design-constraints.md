# KB-mined design constraints for the #707 implementation

- **Issue:** #722 (docs-only; folds constraints into the #702/#707 direction
  before #707 implementation starts)
- **Amends:** [`decision.md`](decision.md) (#702) — the ratified state-light
  loop direction. This record adds acceptance criteria; it changes no
  decision already made there.
- **story_id:** docs/changes/702-ledger-loop-architecture-decision/decision.md

## Why this record exists

`decision.md` ratifies the *shape* of the replacement loop (observe -> compact
context -> LLM proposes one bounded task -> precheck -> one fresh-context
subagent -> gate -> append ledger) but leaves the interface, sizing rule,
context composition, and rotation mechanism unspecified. Those four gaps are
exactly where the KB mining of `agents_library` (2026-07-12) found the
sharpest cross-project evidence. This record turns that evidence into four
constraints that #707 must satisfy — they become acceptance criteria of the
#707 implementation, not suggestions.

## C1 — Single proposer contract

The deterministic planner today, and the future LLM proposer under #707, must
sit behind **one narrow boundary**: the queued-request JSON the bridge already
consumes — `{request_id, request_status, cycle_id, task_title, goal_id, ...}`
(see #704 ledger schema for the sibling shape, e.g. `cycle_id`/`request_id`
join keys). This request-JSON **is** the interface. #707 swaps *who writes*
the request (planner logic -> LLM proposal); nothing downstream — bridge,
precheck, gate, ledger append — changes shape or contract.

Source: agents_library `a-evolve` boundary-contracts pattern (fixed loop /
harness, pluggable strategy behind a stable interface).

## C2 — Task sizing is checkable

A proposal must declare, and the precheck must be able to verify pre-spawn:

- one surface (a single file/module/path scope, not a cross-cutting sweep),
- one concern (a single behavior or bug, not a bundle),
- a bounded expected diff (the proposer states an expected size class; the
  precheck rejects proposals that don't declare one, or that describe more
  than one surface/concern).

Oversized or multi-concern proposals are rejected before a subagent is
spawned — sizing is enforced as a gate condition, not left to subagent
judgment.

Source: agents_library `synthesis/archetype-failure-modes.md` §3 — bad task
sizing is the *top named failure mode* of fresh-context outer loops (a
proposer with no memory of prior attempts keeps re-proposing work at the
wrong grain, and the whole loop's productivity collapses on it). This is the
single highest-priority constraint from the KB mining because it is the
failure mode most directly caused by the state-light design itself (fresh
context per subagent means the loop has no other place to catch bad sizing).

## C3 — Plan-state vs learning-state split

The proposer's compact context is composed of exactly two artifacts, with
different owners and different mutability:

1. a **structured goal queue** — planner-owned, mutable, describes what's
   next (analogous to a `prd.json`-style plan file); and
2. an **append-only lessons/failure digest** — read-only to the proposer,
   accumulates over cycles, never rewritten in place.

These are never merged into one mutable blob. A single shared, freely-edited
context file re-creates exactly the state-corruption failure mode this
project has already lived through (see planner-fragility history: ~7
sequential control-plane fixes that didn't converge because mutable state and
learned history were tangled together).

Source: agents_library `ralph` agent — the `prd.json` (mutable plan) vs
`progress.txt` (append-only journal) split is the concrete precedent.

## C4 — Tag-digest for goal rotation

A small (~50-line) script distills one-line, tagged lessons from the #720
cycle ledger (`<STATE_DIR>/ledger/cycles.jsonl`) — e.g. tagging each
terminal row's outcome/reason with a short category tag. The **last N tags**
feed the proposer prompt as the read-only digest half of C3's context split.
This mechanism replaces the current manual `goal_text` priority-rotation
process (see #656/#663 history) with something the loop maintains itself from
ledger truth, without introducing a new state machine.

This is an **idea only**, taken from the reflect -> tag -> update shape of
agentic-context-engine's approach to accumulating usable signal from run
history. It is explicitly **not** an adoption of the ACE framework itself —
no new dependency, no generalized playbook/context engine, just a small
script reading the existing ledger.

## Non-goals (KB anti-recommendations)

The KB mining surfaced several patterns the agents_library catalog flags as
recurring anti-patterns for a project at this scale. #707 must not introduce:

- **No memory/context-substrate platforms** — no mem0, cognee, OpenViking, or
  similar generalized memory service. The ledger (#704/#720) is the only
  durable state.
- **No crew/swarm/lane multi-agent orchestration** — the loop stays exactly
  one fresh-context subagent per cycle; no parallel lanes, crews, or
  role-swarms.
- **No SQL/Dolt-backed ledger** — `beads`-style structured issue tracking is
  a useful *idea* (tagged, queryable lessons) but not its engine; the ledger
  stays flat, append-only JSONL, no database.
- **No generation-inherited per-cycle state files** — the `anima` split-brain
  failure mode (state silently forked/inherited across generations of a
  process) is a close structural match to this project's own
  planner-fragility history and must not be reintroduced under a new name.

## Status

These four constraints (C1-C4) and the four non-goals above become
**acceptance criteria of the #707 implementation** (gated, per `decision.md`,
on #706's go/no-go). Grounded in the operator's `agents_library` KB mining
(2026-07-12).
