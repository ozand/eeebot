# Change: git-native evolutionary tree (RSI stage 3)

- **change-id:** 877-evolution-tree
- **issue:** #877
- **capability:** self-evolving-runtime (bridge.py cycle-branch isolation
  #653/#828, population archive #529/#844, cycle ledger #720, scorecard
  #789/#865)
- **role / workstream:** RSI (recursive self-improvement) — moving along
  an existing evolutionary structure, not building a new one
- **status (2026-09-04, #1225):** the line-switch **trigger is retired**.
  `CycleArchive.stalled()` read `goals/cycle_archive.json`, whose only
  writer was the coordinator deleted in #916/#923; the file froze on
  2026-08-21T23:00:58Z with 200 entries all at reward 1.0 against the
  `< 0.8` rule, `tree.json` records 0 switches ever and no `evo/node-*`
  keeper branch was ever created — a lever that never worked, not one that
  stopped. `should_switch`, the `_setup_cycle_branch` switch block and
  `CycleArchive` are gone; `record_node` / `node_score` /
  `select_switch_target` / `mark_switch_blocked` remain as a record, and the
  integrate-side base-surface gate stays as defence in depth. The sections
  below describe the design as built; read them as history.

## Operator framing

Memory is already files + git. Population = branches, generation =
commit, fitness = one ledger entry per node. Moving along the tree,
rolling back to an ancestor, returning to a stronger line — all of it is
native `git checkout`. Evaluation stays sequential (one live line at a
time); parallelism lives in STRUCTURE (dormant branches), never in
execution.

**Strict product simplicity, by design constraint of this change:** no
MAP-Elites grid, no population manager, no new daemon. This is a thin
bookkeeping layer over the git machinery `bridge.py` already runs every
cycle.

## Problem

`bridge.py` already does everything a population needs structurally: each
cycle branches off `origin/main` (`_setup_cycle_branch`), is gated by the
full smoke/deny-set/held-out pack, and — only on green — merged back
(`_integrate_cycle_to_main`, the sole writer of `origin/main`). The
`CycleArchive` (#529/#844) already tracks reward-per-cycle and detects
`stalled()` (last 5 cycles all reward < 0.8). What's missing is the
connective tissue: when the archive says the current line has stalled,
there is no way for the loop to say "branch the next cycle off a
DIFFERENT, stronger line instead of the same stalled tip" — every cycle
always continues from wherever `origin/main` currently sits, regardless
of whether that line is regressing. The tree of everything the loop has
ever integrated is implicit in git's own commit graph, but nothing reads
it as a population to select from.

## Design

### 1. `nanobot/runtime/evolution_tree.py` (new, harness-owned, stdlib-only)

A sidecar at `state/evolution/tree.json`:

```json
{
  "schema_version": "evolution-tree-v1",
  "current_sha": "<sha or null>",
  "nodes": {
    "<sha>": {
      "parent_sha": "<sha or null>",
      "branch": "<selfevo/cycle-... or the original integrating branch>",
      "cycle_id": "<cycle id>",
      "ts": "<iso8601>",
      "fitness": {
        "reward": null,
        "integrations": null,
        "confirmed_integrations": null,
        "repeat_failure_rate": null
      }
    }
  },
  "switches": [
    {"ts": "...", "from_sha": "...", "to_sha": "...", "reason": "stalled"}
  ]
}
```

Nodes are capped at 100 (`MAX_NODES`) — beyond that, the lowest
`node_score()`, oldest-among-ties entry is evicted, but **never**
`current_sha` itself nor its last 5 ancestors (`_KEEP_ANCESTOR_HOPS`), so
the live line's own recent history always survives a trim. `switches` is
a small bounded (`MAX_SWITCHES=20`) audit trail alongside the durable
cycle-ledger event described below.

Public functions (all fail-open: any error degrades to `None`/no-op/empty
collection — a tree bug must never block a cycle):

- `record_node(state_dir, *, sha, parent_sha, branch, cycle_id,
  reward=None)` — called once per successfully **integrated** cycle.
  Sets `current_sha = sha`. Fitness fields are pulled best-effort from
  `state_dir/scorecard/latest.json` (read directly as JSON — this module
  never imports `scorecard.py`, keeping it a leaf dependency) plus the
  caller-supplied `reward` (a real reward can be backfilled by a later
  cycle once one exists; kept simple for v1). Appends one
  `{"phase": "evolution_tree", "reason": "node_recorded", ...}` cycle-ledger
  event.
- `node_score(node)` — `reward + 0.1*confirmed_integrations -
  0.2*repeat_failure_rate`, missing fields default to 0. **Deliberately
  crude v1** — documented explicitly as NOT a trust/verification input
  anywhere else in the codebase (see the Trust section below); it only
  ever ranks dormant lines against each other for `select_switch_target`.
- `select_switch_target(state_dir, current_sha)` — the best OTHER node by
  `node_score` (ties broken by newest `ts`); `None` when the tree has
  fewer than 2 nodes.
- `should_switch(state_dir, archive_stalled, current_sha)` — the single
  trigger. Returns `select_switch_target(...)` when `archive_stalled` is
  True, else `None`. No second heuristic is added: `CycleArchive.
  stalled()` (#844) already covers "regression → low rewards → stalled".
- `tree_indexed_shas(state_dir)` — the set of all node shas, so the
  bridge's branch pruning never deletes a branch the tree still points
  at.
- `current_sha(state_dir)` / `record_switch(...)` — small accessors used
  by the wiring below.

### 2. Bridge wiring (`nanobot/runtime/bridge.py`)

**`_setup_cycle_branch(repo_root, cycle_id, state_dir=None)`** — gained a
third, optional `state_dir` parameter (defaults to the module-level
`STATE_DIR`, so every pre-#877 call site/test is unaffected). After
resolving the real `origin/main` sha, it now asks: is
`CycleArchive(state_dir).stalled()` true, and does
`evolution_tree.should_switch(...)` have a target? If both yes, and
`git cat-file -e <target>^{commit}` confirms the sha actually exists in
this repo, the base for THIS cycle's branch becomes the target sha
instead of `origin/main`. Before switching, a keeper ref
`evo/node-<sha[:12]>` is created at the abandoned tip — **losers stay
reachable as branches, never deleted** — and both a durable cycle-ledger
event (`phase: evolution_tree, reason: line_switch`) and a bounded
`tree.json` switch record are written. The function now returns two sha
fields: `main_sha` (the base actually used — may be the switched
ancestor) and `origin_main_sha` (always the real, unswitched
`origin/main` tip observed at setup time). Whenever the archive isn't
stalled, the tree is empty, or the target sha doesn't resolve, `base`
never changes — **byte-identical to pre-#877 behaviour**, and the whole
decision is wrapped so any error in it degrades silently to "use
origin/main", never to a blocked cycle.

**`_integrate_cycle_to_main(repo_root, cycle_branch, main_sha_before,
expected_origin_main=None)`** — gained a fourth, optional parameter. The
push now ALWAYS uses `git push --force-with-lease=main:<lease> origin
main` instead of a plain push. See "The push/force-with-lease reasoning"
below for why, and why this is safe.

**Caller (bridge's cycle-flow, inside the main loop):**
- `_origin_main_observed = _cycle_setup.get('origin_main_sha') or
  main_sha_before` — computed once, right after `_setup_cycle_branch`.
- `_detect_out_of_band_main(_selfevo_repo, _origin_main_observed)` (was
  `main_sha_before`) — see the #846 interaction note below for why this
  had to change.
- `_integrate_cycle_to_main(..., expected_origin_main=_origin_main_observed)`.
- On a successful integration:
  `evolution_tree.record_node(STATE_DIR, sha=main_sha_after,
  parent_sha=main_sha_before, branch=cycle_branch, cycle_id=_cycle_id,
  reward=None)` — wrapped in try/except for defense-in-depth even though
  the function is already internally fail-open (matching this file's
  existing convention for calls into sibling steering modules, e.g.
  `archive.record_stepping_stone`).

**`_prune_stale_cycle_branches(repo_root, keep=..., state_dir=None)`** —
gained a `state_dir` parameter. Before deleting anything, it reads
`evolution_tree.tree_indexed_shas(state_dir)` (fail-open to `set()`) and
never deletes a `selfevo/cycle-*` branch whose tip sha is still indexed
by the tree. It also now calls the new `_prune_evo_node_refs` helper,
which bounds the `evo/node-*` keeper refs created by line switches to 20
(`_EVO_NODE_REF_KEEP`), deleting the oldest-by-commit-date beyond that cap
— never the currently checked-out branch, nor one whose tip equals the
tree's live `current_sha`.

### 3. Trust boundaries (explicit, do not weaken)

`tree.json` lives under `state/` and is therefore **instance-writable**,
exactly like every other bridge sidecar (`demand/completed.json`,
`scorecard/latest.json`, ...). It is added to `scorecard.FITNESS_SIDECARS`
(#789) so tampering with it is **detected** (spawn-boundary hash
mismatch) the same way tampering with any other fitness input is
detected.

But selecting a base sha is a **steering** decision (which line to
continue), not a **verification** decision. A forged tree can, at worst,
point the next cycle's branch at a different commit inside the
instance's OWN repo — every cycle branched from that base still runs
through the full, unweakened gate (smoke tests + deny-set/mutation-surface
check + held-out pack), and runtime-slice promotion still requires the
independent root verifier (#875). `node_score()` is deliberately crude
for exactly this reason — it must never be read as a trust signal
anywhere else, only as "which dormant line looks least bad to try next".

`nanobot/runtime/evolution_tree.py` is added to
`runtime_deny._RUNTIME_DENY_ALWAYS_FILES` — it steers base selection
(fitness-adjacent), so an instance must never be able to make its own
steering module promotable. No basename-token match applies to
"evolution_tree.py" against `_RUNTIME_DENY_TOKENS` (deliberately —
"evolution" was not added to that tuple), so the explicit file entry is
the only thing keeping it denied; this is called out explicitly per the
design constraint that a token match should only be relied on when it
already naturally fires.

The switch target's sha is verified to exist in the repo
(`git cat-file -e <sha>^{commit}`) before it is ever used as a checkout
base — a missing/forged sha simply falls back to `origin/main`. `main` is
never force-pushed except via `_integrate_cycle_to_main`'s own
compare-and-swap (see below), which is bounded to a single, explicit,
lease-checked rewrite.

### 4. The push / force-with-lease reasoning

Before #877, `_integrate_cycle_to_main` always built the cycle branch off
`origin/main`, so the merge commit's first parent was always exactly
`origin/main`'s current tip — a plain `git push origin main` was
therefore always a fast-forward (or correctly rejected as
non-fast-forward on genuine out-of-band drift, #846).

A line switch breaks that invariant on purpose: the cycle branches off an
ANCESTOR (`main_sha_before` become the switched-to sha), so the resulting
merge commit's history does NOT descend from the CURRENT `origin/main`
tip. A plain push would always be rejected as a non-fast-forward, even
though this rewrite is exactly what the operator asked for ("return to a
stronger line").

The fix: the push always uses
`git push --force-with-lease=main:<expected_origin_main> origin main`.
`expected_origin_main` is the REAL `origin/main` sha observed at setup
time (`origin_main_sha`), not `main_sha_before` (which differs from it
only on a switched line). This is an atomic compare-and-swap: git accepts
the push if and only if `origin/main` currently equals exactly that
expected value, regardless of whether the new history is a
fast-forward — allowing exactly the one intentional rewrite the switch
performs, while still rejecting ANY other concurrent `origin/main`
movement (the same out-of-band race #846 already guards against). When
`expected_origin_main` is omitted, it defaults to `main_sha_before` — the
non-switched case, where a matching lease produces an identical result to
the old plain push (a fast-forward IS what a matching-lease
force-with-lease push always produces). **No behaviour change on the
non-switch path**; this was pinned as a test
(`TestIntegratePushAfterLineSwitch` / the pre-existing
`test_stale_base_is_rejected_and_origin_main_untouched` case, which now
exercises the same rejection through the lease mechanism instead of a
bare non-fast-forward).

### 5. The #846 (`_detect_out_of_band_main`) interaction

`_detect_out_of_band_main` is positive-only: it fetches `origin/main` and
reports drift if it differs from what the caller expected. Before #877,
the caller always passed `main_sha_before`, which was always exactly the
real `origin/main` observed at setup — so this was correct.

After #877, `main_sha_before` can be the SWITCHED-TO ancestor, which is
never equal to `origin/main` (by construction — that's the whole point of
switching). Passing it unchanged would make `_detect_out_of_band_main`
fire a FALSE positive `out_of_band_main_push` incident on every single
switched cycle, even when nothing moved out of band at all. The fix:
bridge.py now passes `_origin_main_observed` (the real pre-switch
`origin/main` sha, tracked separately) to `_detect_out_of_band_main`
instead of `main_sha_before` — restoring the "did anything ELSE touch
`origin/main` during this cycle" semantics the check is meant to have,
independent of whether this cycle itself switched lines. The
rollback/restore path (`_restore_to_main`) is untouched and unaffected —
it always resets to whatever `main_sha_before` the caller already tracked,
which is exactly the base this cycle actually built from, switch or not.

### 6. `scorecard.py` control-plane visibility

`_control_plane_snapshot()` gained an `evolution_tree` key:
`{"nodes": <count>, "current_sha": <short 12-char sha or null>,
"switches": <count>}`. Lazy import of `evolution_tree.read_tree`, wrapped
fail-open to `{}` — visibility only, never fed into fitness/targets/gaps,
same treatment as `runtime_promotions`/`runtime_trust_ladder` (#875/#876).

## Non-negotiable invariants

- No MAP-Elites grid, no population manager class, no new daemon/timer —
  every write happens inline inside the existing bridge cycle flow.
- Every cycle branched from a switched base still passes through the
  IDENTICAL, unweakened smoke/deny-set/held-out gate as any other cycle.
- `origin/main` only ever advances through `_integrate_cycle_to_main`'s
  compare-and-swap — never a raw/unconditional force push.
- `should_switch` has exactly ONE trigger (`CycleArchive.stalled()`); no
  second heuristic was added.
- `evolution_tree.py` is deny-set (never promotable/loadable via the
  runtime-slice/trust-ladder path).
- Losing lines are never deleted outright — they persist as `evo/node-*`
  keeper branches, bounded like every other retained artifact in this
  codebase (cycle branches #830, cycle tags #721).

## Tests

- `tests/test_evolution_tree.py` (new): `read_tree` defaults/fail-open,
  `record_node` round-trip + ledger event + cap eviction (lowest-score,
  never current/its ancestors within the hop window), `node_score`
  formula + fail-open, `select_switch_target` (best-by-score, tie-newest,
  <2 nodes → `None`, fail-open), `should_switch` (only when stalled),
  `tree_indexed_shas`/`current_sha` fail-open, `record_switch` bounded
  append.
- `tests/test_bridge_cycle_branch.py` (extended): base override on
  stalled+target (`TestEvolutionTreeLineSwitch`), byte-identical when the
  tree is empty or the archive isn't stalled, a bogus/nonexistent target
  sha safely falls back to `origin/main`, the `evo/node-*` keeper ref is
  created before the switch, the switched integration actually advances
  `origin/main` via force-with-lease
  (`TestIntegratePushAfterLineSwitch`), a stale/mismatched lease is still
  rejected (out-of-band race safety), and pruning exempts both
  tree-indexed shas and bounds `evo/node-*` refs
  (`TestPruneExemptsEvolutionTreeRefs`).
- `tests/test_scorecard.py`: `control_plane.evolution_tree` present with
  the expected default shape; the two existing exact-key-set assertions
  updated to include the new key.

Run explicitly by path (site-packages shadow gotcha, per this repo's
convention): `python -m pytest tests/test_evolution_tree.py
tests/test_scorecard.py tests/test_bridge_cycle_branch.py -q`, then the
full suite with `--continue-on-collection-errors`. See the PR description
for the verbatim final lines of both runs.
