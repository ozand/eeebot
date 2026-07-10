# Shadow experiment protocol — LLM-proposal arm vs old planner (#706)

- **Issue:** #706
- **story_id:** `docs/specs/self-evolving-runtime/spec.md`
- **Status note:** shadow/offline measurement; no live-loop replacement; no
  integration; local stacked commit. Report scripts live under #710; ledger
  write-points are designed in #707.

## Hypothesis under test

From #702/#706: an LLM proposing bounded tasks from a compact context (goal
vectors + done-ledger proxy + failure/state digest) sustains novelty and
productive gate-passing work, versus the old deterministic planner, which
collapsed to already-done titles and produced 0 productive spawns.

## Method actually used

5 independent, fresh-context, isolated Sonnet 5 (`claude-sonnet-5`) shadow
cycles, run in parallel, each in its own throwaway git worktree. Each cycle:

1. Build a proposal from the SAME compact context.
2. Conceptual precheck — P1 mutable-surface / P2 duplicate-vs-done-proxy / P3
   tree-sane, per #703.
3. Implement in the worktree.
4. Bounded gate — import smoke (`python -c "import nanobot"`) + pytest on the
   affected test file only, per #686 S8.
5. Classify `general` / `host_local`.

Nothing was integrated; worktrees are throwaway.

## Fixed harness (held constant across cycles)

- Mutable surface: `tests/`, `scripts/`, `docs/`, `memory/`, `lessons/` (S2).
- No-secret constraint (S3).
- One bounded change per cycle (S6).
- Bounded gate (S8).
- Only the proposal source varied (LLM vs old planner).

## Compact context fed to each cycle

- The two eeebot goal vectors: V1 self-optimization on a constrained host; V2
  owner utility.
- A done-ledger proxy: ~22 recent completed-work titles.
- A failure/state digest: the live planner is stuck at 0 productive spawns due
  to novelty collapse.

## Fidelity caveats (prominent — this is the crux of the go/no-go)

1. **MODEL FIDELITY:** cycles ran on Sonnet 5, NOT the weak host model (qwen
   on i386). #706's central risk — "LLM quality on the host model
   insufficient" — is therefore NOT tested here. This run validates the
   ARCHITECTURE, not the host model.
2. **TARGET-REPO FIDELITY:** ran against the eeebot *product* repo as a
   stand-in, not the live self-evolving instance repo or its real ledgers
   (those live on the host, out of reach from this dev session).
3. **PROPOSAL-DRAW FIDELITY:** cycles were independent and parallel (each saw
   the same done-proxy), not sequential cycles that accumulate prior shadow
   proposals into the done-ledger. This tests cross-draw novelty; the live
   loop would additionally suppress repeats of prior shadow cycles.
4. **SAFETY-PATH COVERAGE:** no cycle proposed outside the mutable surface, so
   the precheck P1/P2 REJECTION paths were not negatively exercised this run.
5. **BASELINE:** the old-planner arm is documented/observed evidence (0
   productive spawns over 16+ ticks; ~7 non-converging planner fixes
   #656/#664/#690/#695/#697/#700), NOT a re-run side-by-side execution.
