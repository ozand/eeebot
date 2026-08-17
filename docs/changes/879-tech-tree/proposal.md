# Change: tech-tree of improvement DIRECTIONS (RSI stage 5)

- **change-id:** 879-tech-tree
- **issue:** #879
- **capability:** self-evolving-runtime (scorecard #765/#789/#865, demand
  #760/#815, goal-review #768/#860, hypothesis loop #878, evolution tree
  #877)
- **role / workstream:** RSI (recursive self-improvement) — a soft ranking
  input over capability domains, layered on the EXISTING demand/goal-review
  pipeline

## Strict product simplicity (the issue's own words)

"2GB-simple, NO heavy bandit machinery, NO MAP-Elites, NO new scheduler."
This is a RANKING INPUT to the existing demand/goal-review pipeline —
exactly the same shape as the #815 V1-over-V2 soft vector bias in
`demand.py` — never a scheduler, never a gate. The entire "bandit" is
epsilon-greedy over five product-seeded nodes plus whatever a supported
hypothesis mints; there is no grid, no population, no new daemon or timer.

## Two DIFFERENT trees — do not conflate

- **#877 `evolution_tree.py`** = STATE space: WHERE the code has been (git
  shas, branches, generations). Already built; untouched by this change.
- **THIS (#879) `tech_tree.py`** = DIRECTION space: WHICH capability
  domain the loop invests its next cycles in. A Civ-style tech tree:
  invest in one direction while it keeps yielding measured gains; when a
  direction PLATEAUS (no net improvement over a trailing window), shift
  investment to the best other direction. The instance can MINT a new
  direction from a supported hypothesis (#878) whose domain isn't already
  mapped.

## Design

### 1. `nanobot/runtime/tech_tree.py` (new, harness-owned, stdlib-only, deny-set)

A sidecar at `<state_dir>/tech_tree/portfolio.json`:

```json
{
  "schema_version": "tech-tree-v1",
  "current": "proposer-quality",
  "nodes": {
    "proposer-quality": {
      "lever_metric": "loop.repeat_failure_rate",
      "direction": "lower",
      "gain_history": [0.02, -0.01, 0.0],
      "status": "active",
      "cooldown_until_ts": null,
      "minted_by": "product",
      "created_ts": "2026-08-17T00:00:00Z",
      "last_lever_value": 0.31
    }
  },
  "switches": [
    {"ts": "...", "from": "proposer-quality", "to": "cycle-cost", "reason": "plateau_switch", "floor": 0.0}
  ],
  "last_mint_ts": null
}
```

**Five product-seeded nodes** (`SEED_NODES`), each naming one EXISTING
scorecard metric (dotted `section.metric` path) as its lever, and which
direction of that metric counts as improvement:

| node | lever_metric | direction |
|---|---|---|
| `proposer-quality` | `loop.repeat_failure_rate` | lower |
| `cycle-cost` | `cost.tokens_per_integration` | lower |
| `tool-reuse` | `loop.confirmed_integration_ratio` | higher |
| `heldout-robustness` | `heldout.heldout_gap` | lower |
| `compile-health` | `quality.compile_clean_ratio` | higher |

Public functions (all fail-open: any error degrades to `None`/no-op/empty
— a tech-tree bug must NEVER block a cycle or the scorecard):

- `ensure_seeded(state_dir)` — create the portfolio from `SEED_NODES` if
  absent; add any MISSING seed nodes idempotently to an existing
  portfolio. Never deletes or overwrites an existing entry (product or
  instance-minted alike).
- `record_gains(state_dir, scorecard_result)` — for each node, reads its
  lever's CURRENT value from the harness-computed scorecard result (dotted
  lookup, skip if absent/non-numeric) and appends one signed marginal-gain
  observation vs. the node's own prior value, oriented by `direction`:
  lower-better → `gain = last - current`; higher-better →
  `gain = current - last`. The FIRST observation for a node records no
  gain (nothing to compare yet), only the baseline. Bounded to a trailing
  window of `GAIN_HISTORY_MAX = 8`. **Harness-computed ONLY** — this never
  reads an instance-authored "gain" field, only the scorecard result it is
  handed and the node's own previously-recorded baseline.
- `node_mean_gain(node)` — mean of `gain_history` (`0.0` when empty).
- `is_plateaued(node, floor=PLATEAU_FLOOR)` — `True` once a node has a
  FULL window (8 observations) whose mean is at or below `floor`.
  `PLATEAU_FLOOR = 0.0` — see "The plateau floor choice" below.
- `select_current_direction(state_dir, *, epsilon=0.15, now=None, rng=None)`
  — epsilon-greedy over non-plateaued, non-cooldown nodes: with
  probability `epsilon` pick a random eligible node (explore), else the
  highest-mean-gain node (exploit; ties → fewest attempts, so a
  freshly-seeded/reactivated node isn't starved by an established
  leader). If the PREVIOUSLY current node just plateaued this pass, it is
  retired (`status: "plateaued"`, a 72h cooldown) and — if the pick
  differs — the forced switch is recorded both in the sidecar's own
  bounded `switches` list and as a `{"phase": "tech_tree", "reason":
  "plateau_switch"}` ledger event. A plateaued node re-enters the
  eligible pool once its cooldown elapses, OR immediately if a
  newly-mintable hypothesis maps back onto it.
- `maybe_mint_node(state_dir, supported)` — given
  `hypothesis_backlog.supported_hypotheses` output (harness-verdicted,
  #878, never an instance claim), a simple normalized-token overlap
  between the hypothesis text and each node's name/lever-metric-tail
  tokens decides whether its domain is already mapped. Mapped → no mint
  (a matched-but-plateaued node is instead reactivated). Unmapped → mint
  ONE new node, rate-limited to at most one per 24h and name-deduped. The
  new node's lever defaults to `loop.confirmed_integration_ratio`
  (higher) unless the hypothesis's own evidence names a metric this
  module recognizes — documented crude v1 behavior.
- `portfolio_snapshot(state_dir)` — `{current, nodes: {name: {status,
  mean_gain, attempts, lever_metric}}, switches: <count>}` for
  control-plane visibility.
- `current_direction(state_dir)` / `direction_for_metric(state_dir,
  metric)` / `matches_direction(text, state_dir, name)` — small read-only
  accessors used by the wiring below.

### 2. Wiring — SOFT ranking, no starvation (mirrors #815)

**`scorecard.compute_scorecard`** — after computing `snapshot["gaps"]`
and before persisting `latest.json`/`history.jsonl`, one fail-open block
calls `tech_tree.ensure_seeded`, `tech_tree.record_gains(state_dir,
snapshot)`, `tech_tree.maybe_mint_node(state_dir,
hypothesis_backlog.supported_hypotheses(state_dir))`, then
`tech_tree.select_current_direction(state_dir)`, and overwrites
`snapshot["control_plane"]["tech_tree"]` with the freshly-updated
`portfolio_snapshot` (so a reader of THIS cycle's snapshot sees this
cycle's pick, not the pre-update one `_control_plane_snapshot` captured
when the section was first assembled). Wrapped as its OWN try/except,
separate from the module's outer fail-open wrapper — a tech_tree bug must
never discard the sections already computed above it.

**`goal_review.maybe_goal_review`** — after the LLM reply is parsed into
candidates, `tech_tree.current_direction(state_dir)` is consulted; if set,
the candidate list is stable-sorted so direction-aligned candidates
(token-overlap with the current node's name/lever) are validated FIRST —
nothing is dropped here, only reordered; the pre-existing
`_MAX_PRIORITIES` cap is the only thing that can ever leave a non-aligned
candidate unaccepted, exactly like #815's V1-over-V2 bias. An accepted
priority whose own text matches the current direction is tagged
`direction: "<name>"` in `derived_priorities.json` (attribution only,
additive — omitted entirely for entries that don't match, so an older
entry's exact shape is unaffected).

**`demand._goal_gap_items`** — each goal-gap item is tagged via
`tech_tree.direction_for_metric` (an EXACT string match between the
gap's bare metric name and a node's lever-metric TAIL — the one place
this module has a precise, non-fuzzy correspondence to demand, so it's
the only demand kind wired with a domain tag in this v1). Within the
kind, items whose `direction` equals the CURRENT investment direction are
stable-sorted to lead WITHIN their existing vector class — `_vector_rank`
(#815, V1-before-V2) stays the PRIMARY sort key; the direction boost is a
secondary tiebreak only, so neither invariant fights the other and
nothing is ever dropped. `_make_item` gained an optional `direction=""`
parameter (additive; every existing caller is unaffected).

## The plateau floor choice

`PLATEAU_FLOOR = 0.0` — "no net improvement over the window" — using
`mean_gain <= floor` rather than strict `<`: a node oscillating exactly
around zero net change (a metric that has fully saturated) reads as
plateaued, not perpetually "not yet plateaued" by a hair's-width
technicality. Recorded explicitly per the issue's own request to document
this choice, since a stricter `<` would leave an exactly-flat metric
stuck "active" forever, quietly starving every OTHER direction of a
chance to be tried.

## The minted-node lever mapping (crude v1, documented)

A hypothesis-minted node's lever defaults to
`loop.confirmed_integration_ratio` (higher-better) unless the
hypothesis's own verdict-evidence names a `metric` string that exactly
matches one of the five known seed-node levers. There is no general
metric-discovery here — recognizing an arbitrary new dotted scorecard
path from free text would need actual schema introspection, well past
"2GB-simple". This is flagged as an explicit v1 simplification: worst
case, an organically-minted node measures the wrong (but still real and
harness-computed) lever, which the epsilon-greedy selection will simply
deprioritize once its gains don't materialize — never a correctness or
trust problem, just a possibly-uninformative direction.

## Trust boundary (do not weaken)

`tech_tree/portfolio.json` lives under `state/` and is therefore
**instance-writable**, exactly like every other bridge sidecar
(`demand/completed.json`, `scorecard/latest.json`, `evolution/tree.json`,
...). It is added to `scorecard.FITNESS_SIDECARS` (#789) so tampering is
**detected** (spawn-boundary hash mismatch → an `integrity` ledger row)
the same generic way every other fitness-input sidecar is — nothing new
to build, just tuple membership.

But selecting a direction is a **steering** decision, not a
**verification** one (the same argument `evolution_tree.py` makes for its
own sidecar, #877). A forged portfolio can, at worst, re-order which
domain the loop *prefers* to work on next:

- **No gain number is ever instance-authored.** `record_gains` is the
  ONLY place a `gain_history` entry is ever appended, and it always
  computes that entry from (a) the node's own prior `last_lever_value`
  and (b) the CURRENT scorecard result the harness's own
  `compute_scorecard` hands it — never from any other field.
- Every cycle still passes the full, unweakened gate (smoke tests +
  deny-set/mutation-surface check + held-out pack); promotions are still
  root-verified (#875); priorities still pass
  `goal_review.validate_priority` — a tech-tree bias can shift WHICH
  priority gets minted/accepted first, never bypass validation.
- `nanobot/runtime/tech_tree.py` is added to
  `runtime_deny._RUNTIME_DENY_ALWAYS_FILES` — fitness-adjacent steering,
  same tier as `evolution_tree.py`/`hypothesis_verdict.py`. No
  basename-token match applies ("tech"/"tree" are not in
  `_RUNTIME_DENY_TOKENS`), so the explicit file entry is the only thing
  keeping it denied — called out per this repo's convention of only
  relying on a token match when it already naturally fires.
- New-node minting is rate-limited (one per 24h) and name-deduped, so an
  instance cannot flood the portfolio with junk nodes to dilute
  selection.

## Non-negotiable invariants

- No MAP-Elites grid, no population manager, no new daemon/scheduler —
  every write happens inline on the existing scorecard recompute path.
- `select_current_direction` has exactly ONE selection mechanism
  (epsilon-greedy over mean gain); no second heuristic was added.
- `tech_tree.py` is deny-set (never promotable/loadable via the
  runtime-slice/trust-ladder path).
- The demand/goal-review direction bias is a STABLE REORDER only —
  nothing is ever dropped or starved because of it; every existing
  gap/hypothesis/vector input keeps working exactly as before.
- Every gain number traces back to a harness-computed scorecard value;
  no instance-authored number can ever enter `gain_history`.

## Tests

- `tests/test_tech_tree.py` (new): seed idempotency (missing nodes
  added, existing ones — including instance-minted — never touched);
  `record_gains` marginal-delta sign for both lower-better and
  higher-better nodes, first-observation-records-no-gain, bounded window;
  `is_plateaued` at/under/over the floor and under a full vs. partial
  window; `select_current_direction` exploit (best mean-gain, tie →
  fewest attempts) and explore (patched `rng`) paths, plateau → cooldown
  → forced switch + ledger event, plateaued/cooldown nodes excluded from
  selection, cooldown-expiry reactivation; `maybe_mint_node` mints an
  unmapped hypothesis, dedups (and reactivates a plateaued match for) a
  mapped one, rate-limits a second mint within the window; a forged
  `gain_history`/`last_lever_value` written directly to the sidecar is
  never itself trusted as a "claimed gain" — the next `record_gains` call
  still appends only the value it computes from the real scorecard
  result. Wiring: `scorecard.compute_scorecard`'s `control_plane.tech_tree`
  is present and reflects the seeded portfolio; `demand`'s goal-gap
  direction tag/boost never drops an item; `goal_review`'s direction bias
  lets an aligned candidate win a capped slot without starving unaligned
  ones when the cap isn't hit.
- `tests/test_scorecard.py` (extended): the two existing
  `control_plane` exact-key-set assertions gained `"tech_tree"`; a new
  assertion pins the freshly-seeded `tech_tree` snapshot's shape (5 nodes,
  all active, zero gain/attempts — the exact `current` pick is left
  unasserted since real epsilon-greedy randomness is in play there).

Run explicitly by path (site-packages shadow gotcha, per this repo's
convention): `python -m pytest tests/test_tech_tree.py
tests/test_scorecard.py tests/test_demand.py tests/test_goal_review.py
tests/test_runtime_slice.py -q`, then the full suite with
`--continue-on-collection-errors`. See the PR description for the
verbatim final lines of both runs.
