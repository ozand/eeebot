# Change: trust-ladder widening of the mutation surface (RSI stage 2)

- **change-id:** 876-trust-ladder
- **issue:** #876
- **capability:** self-evolving-runtime (#812 runtime-slice tier, #875
  root-verified auto-promotion, #822 causal micro-benchmark, #865
  control-plane visibility)
- **role / workstream:** RSI (recursive self-improvement) — widening the
  bounded mutation surface without adding a second trust mechanism

## Problem

#875 gave the loop a real (if narrow) path to auto-promotion: propose a
change to an operator-approved `nanobot/runtime/*.py` module, have root
independently re-verify it, and — after 3 clean soak passes — root promotes
it into `PROMOTED_TREE`. But the operator-approved slice
(`SELFEVO_RUNTIME_SLICE`) is still a manually-widened, static allowlist. The
loop has no way to EARN more surface on its own; every widening step still
requires an operator to edit an env var. That is a bottleneck on the whole
RSI plan: the loop's ability to improve its own runtime is capped at
whatever an operator happened to pre-approve, regardless of how much
trust it has actually earned via root-verified promotions.

## Design: derive the ladder, don't build a second state machine

The trust ladder is **not** a new mutable state machine. It is a pure
function of one thing that already exists and is already trustworthy: which
`nanobot/runtime/*.py` modules currently have an **active** root-verified
promotion in the #875 `PROMOTED_TREE/manifest.json`. No new env var, no new
daemon, no separate ladder-state file — progression (and, symmetrically,
demotion) is always just "what does the root-owned manifest say is active
right now".

```
RUNTIME_TRUST_LADDER (nanobot/runtime/runtime_deny.py, ascending blast radius)
  rung 0: existence_index.py   — always unlocked (operator-seeded base rung)
  rung 1: demand.py            — unlocked once rung 0 is ACTIVE in the manifest
  rung 2: llm_proposer.py      — unlocked once rung 0 AND rung 1 are ACTIVE
  rung 3: cycle_planning.py    — unlocked once rungs 0,1,2 are ACTIVE
```

`earned_ladder_slice(active_modules)` / `earned_ladder_level(active_modules)`
walk the ladder from the bottom and stop at the first rung whose module is
NOT in `active_modules` — **consecutive-from-bottom only**. A higher rung
being active (e.g. an operator manually widening `SELFEVO_RUNTIME_SLICE` to
include `cycle_planning.py` directly and getting it promoted) never skips
over an unproven lower rung; the derived level still reflects only the
unbroken run from rung 0. This makes the ladder inherently self-limiting: to
reach rung 3 the loop must first have rungs 0, 1, and 2 all independently
root-verified and currently active — not vetoed, not rolled back.

Demotion is automatic and requires no extra code: #875's verifier already
rolls back (`_rollback`) any active promotion that regresses on held-out or
gets an operator `status: "vetoed"` edit. The moment a rung's manifest entry
stops being `"active"`, the very next pass's `earned_ladder_level`/
`earned_ladder_slice` computation reflects that — every rung above it
re-locks automatically, with zero additional demotion logic.

## Components

### 1. `nanobot/runtime/runtime_deny.py` (pure, stdlib-only, unchanged contract)

Adds `RUNTIME_TRUST_LADDER`, `earned_ladder_slice(active_modules)`,
`earned_ladder_level(active_modules)`. Stays filesystem/env-free — callers
pass in `active_modules`. A module-level assertion pins that no ladder
module is ever `_is_runtime_deny` (the verification kernel stays
constitutionally unmodifiable, #603) — this fails loudly at import time if
a future edit ever lists a deny-set path on the ladder.

### 2. `nanobot/runtime/promoted_overlay.py` (the root-owned-manifest read side)

Adds `active_promoted_modules(promoted_tree=None) -> set[str]` — reuses the
existing `_resolve_promoted_tree` + `_boundary_ok` (root-owned,
non-writable-by-others) boundary check `install_promoted_overlay` already
enforces, so the ladder can NEVER advance from an instance-writable or
misprovisioned tree; a boundary failure returns `set()`, exactly like the
overlay loader refuses to load anything in that case. Fail-closed to
`set()` on any error.

Adds `effective_runtime_slice(env_value, promoted_tree=None) -> set[str]` =
`runtime_deny.runtime_slice_paths(env_value) | earned_ladder_slice(active_promoted_modules(promoted_tree))`
— the ONE function every runtime-slice consumer now calls. Kept here (not in
`runtime_deny`) because computing it requires reading the root-owned
manifest, and this module already owns that read + its boundary check;
`runtime_deny` stays pure.

### 3. Wiring — the three consumers now call the EFFECTIVE slice

- `bridge.py`'s `_runtime_slice_paths()` wrapper (the #812 gate's
  `_classify_mutation_surface` input).
- `llm_proposer.py`'s `_runtime_slice_paths()` (both `validate_sizing`'s
  target-path check and the proposal-prompt's advertised-surface text).
- `host/eeepc/libexec/eeepc_promotion_verifier.py`'s `verify_pass()`
  eligibility check — reads its OWN root-owned `PROMOTED_TREE`, so this is
  trustworthy self-reference, not circular trust.

No import cycle: `promoted_overlay` imports only `runtime_deny` + stdlib;
`bridge.py` and `llm_proposer.py` already imported `promoted_overlay`
(#875) / `runtime_deny` (#812/#875) respectively, so both add a
module-level import with no lazy-import workaround needed.

**Behavior change, by design.** With zero active promotions, the effective
slice is the pre-#876 env-only slice **union rung 0**
(`existence_index.py`) — this is a real (if minimal) widening, not a
byte-identical no-op for every possible operator config: rung 0 is the
"operator-seeded base rung" and is unconditionally unlocked. On the
CURRENT live deployment this is a no-op in practice, because
`SELFEVO_RUNTIME_SLICE` already includes `existence_index.py` (the #822
microbenched seed module). Existing unit tests that pinned an env-only,
rung-0-free slice were updated accordingly (`tests/test_runtime_slice.py`,
`tests/test_llm_proposer.py`) — this is the intended widening the issue
title describes, not a regression.

### 4. Ledger event on level change (`eeepc_promotion_verifier.py`)

At the end of every `verify_pass()`, after the promote/rollback/veto
reconciliation for that pass, the verifier computes
`earned_ladder_level(active_modules_from_manifest)` from its own in-memory
`manifest` dict (the exact bytes this pass either already wrote or is about
to write — never re-read from disk mid-pass, so it can't observe a torn
intermediate state) and compares it against a `ladder_level` value
persisted in the root-owned `verifier_state.json`. A never-persisted
(fresh-install) value normalizes to the implicit baseline of 0 rather than
`None`, so the very first pass on a brand-new `PROMOTED_TREE` does not log
a spurious "unset → 0" event. On a genuine change, one
`{"phase": "trust_ladder", "reason": "ladder_level_changed", "from": <old>,
"to": <new>, "unlocked": [...]}` event is appended to
`PROMOTED_TREE/verifier_ledger.jsonl` (the same root-owned ledger #875's
promote/rollback events already use) and the new level is persisted.

### 5. `scorecard.py` control-plane visibility (#865)

`_control_plane_snapshot()` gains a `runtime_trust_ladder` key:
`{"level": <int>, "unlocked": [<sorted module_path>...], "ladder":
[<RUNTIME_TRUST_LADDER, in order>]}`. scorecard runs as the eeepc-agent uid
and can READ (never write) the root-owned manifest via the same
`active_promoted_modules()` the ladder logic trusts. The whole section is
wrapped and fails open to `{}` on any import/read error — visibility only,
never fed into fitness/targets/gaps, and never allowed to crash the
scorecard.

## Per-rung coverage (first 3 rungs)

| Rung | Module | Auto-promotable today? | Why |
|---|---|---|---|
| 0 | `existence_index.py` | **Yes** | Already has a #822 `MICROBENCHES` spec (`heldout/microbench.py`) — the verifier has a causal before/after measurement to gate on, exactly the #875 auto-promotion path already exercises in production. |
| 1 | `demand.py` | Proposable + held-out-checked, **not yet auto-promotable** | No `MICROBENCHES` entry exists for it yet. The loop can propose a change and it will be smoke-gated + held-out-checked like any runtime-slice candidate once rung 0 unlocks rung 1, but `eeepc_promotion_verifier.py`'s `_measure_candidate` requires a registered spec to compute `improvement_pct` against — without one, a candidate for this module cannot clear the microbench eligibility check, so it can soak but never promote. |
| 2 | `llm_proposer.py` | Proposable + held-out-checked, **not yet auto-promotable** | Same gap as rung 1 — no `MICROBENCHES` entry. Notably this module is ALSO where `_runtime_slice_paths()` itself lives (#876's own wiring) — a future candidate touching it is held-out-checked like any other runtime module, but is not exempt from the deny-set (it is not on the deny-set; only the gate/promotion/coordinator/scorecard/etc. kernel is). |

**Follow-up filed separately, not fixed here:** writing `MICROBENCHES`
specs for `demand.py` and `llm_proposer.py` (mirroring the existing
`existence_index.py` seed spec's synthetic-corpus + correctness-assertion
pattern) is required before either module can ever reach `active` status,
even after it is unlocked. Until that follow-up lands, rungs 1 and 2 behave
as "the loop may work toward it, but a human/product PR is still the only
way it actually ships" — an intentional, honest gap, not an oversight.

## Non-negotiable invariants (unchanged, re-verified here)

- Deny-set modules are NEVER on the ladder and NEVER slice-eligible — pinned
  by a module-level assertion in `runtime_deny.py` plus
  `tests/test_runtime_trust_ladder.py::test_no_ladder_module_is_ever_in_the_deny_set`.
- Ladder progression reads ONLY the root-owned manifest, via the same
  boundary-checked `active_promoted_modules` the #875 overlay loader
  trusts — an instance-writable tree fails the boundary check and returns
  `set()`, so the ladder can never be forged.
- Zero active promotions → effective slice is env-slice ∪ {rung 0} exactly
  (no other behavior change).
- No new env var, no new daemon, no separate mutable ladder-state file.

## Tests

- `tests/test_runtime_trust_ladder.py` (new): `earned_ladder_slice` /
  `earned_ladder_level` pure-function coverage (zero active, consecutive
  unlocks, non-consecutive no-skip, all-active full set, deny-set
  assertion, fail-open on bad input).
- `tests/test_promoted_overlay.py`: `active_promoted_modules` boundary
  fail-closed cases (absent tree, missing manifest, boundary-check
  failure, non-POSIX, malformed manifest) and `effective_runtime_slice`
  (env-only-plus-rung0, env-union-earned-ladder, empty-env-still-rung0).
- `tests/test_runtime_slice.py` / `tests/test_llm_proposer.py`: updated the
  pre-#876 exact-slice assertions to include the always-on rung 0, and
  added a dedicated "rung 0 accepted even with the env slice empty" case.
- `tests/test_promotion_verifier.py`: the ladder-level-change ledger event
  fires exactly once across a full soak-then-promote lifecycle (not once
  per soaking pass, and not a spurious event on a fresh, zero-promotion
  install), and stays silent when the level never changes.
- `tests/test_scorecard.py`: `control_plane.runtime_trust_ladder` reflects
  active promotions and fails open when `PROMOTED_TREE` is absent.
