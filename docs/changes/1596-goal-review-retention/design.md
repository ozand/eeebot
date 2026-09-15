# Design — decision-time retention, not a new demand path

## Writer boundary

`goal_review.maybe_goal_review()` builds the bounded citable evidence set once
per review. Before the existing `no_gaps` branch it reads the current
tech-tree Direction. `_record_review()` receives those facts for every
outcome and writes them into the existing `phase: "goal_review"` row with a
`retention_status`: `complete` only after every source reader and Direction
validation completed, otherwise `unavailable`. The source set is computed from
lines that survive `_MAX_EVIDENCE_LINES`, not
from source functions merely attempted. Thus a later replay observes exactly
what the review model could have cited.

`direction_at_review` is captured once and then reused by the existing #879
candidate ordering and attribution code. This avoids recording a Direction
which changed between the decision and its ledger write.

## Reader boundary and legacy semantics

`goal_review_retention(row)` is the explicit reader for #1642:

| Row shape | status | source set / Direction |
|---|---|---|
| New row, source scan ran, valid fields | `complete` | exact retained values, including `[]` / `null` |
| New row, a source reader or Direction validation was unavailable (`retention_status: unavailable`) | `unavailable` | `None` / `None` |
| Pre-retention row, malformed row, non-review row | `unavailable` | `None` / `None` |

No code writes missing keys into historical ledger entries. `group_by_cycle`,
`action_index`, scorecard, and demand were inspected before the key addition:
they discriminate rows by `phase` and `cycle_id`; goal-review rows continue to
have no cycle id. No reader receives a new key that it could mistake for a
cycle, outcome, demand id, or demand source.

## Observation window and predeclared replay rule

The rollout day is day 0. #1642 must run after 14 calendar days of retained
observations and within three calendar days thereafter. Its proposed future
Direction evidence rule is considered to **create demand** only if at least
three retained `goal_review` rows in that window have:

1. `status == "complete"`;
2. `evidence_sources == ()`; and
3. a non-null `direction`.

This is intentionally a minimum evidence threshold, not authorization to ship
a demand rule. `0` says the retained rule did not occur; `unavailable` says
retention did not permit a claim. A later issue must still decide whether a
measured occurrence warrants a demand source.

## Non-goals

- No `demand.py` changes or future demand item.
- No new scorecard key, target, threshold, fitness input, or tech-tree writer.
- No old-ledger migration.
- No host-state change, cycle trigger, or deployment.
