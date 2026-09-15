# #1596 — Retain goal-review decision provenance

## Problem

The 2026-09-15 read-only audit established that the `no_gaps` branch was not
recorded in 33 historical goal reviews; neighboring outcomes showed that the
review had other evidence, rather than reaching an empty source set. Yet the
available scorecard history did not retain decision-time tech-tree Direction.
A replay of ADR-011's proposed rule (`gaps: []` plus Direction) therefore has
an **unavailable** input, not a zero emission rate. Projecting today's
Direction backwards would manufacture data.

## Intended change

Each new `phase: "goal_review"` ledger row records:

- `evidence_sources`: the stable names of sources whose citable lines actually
  survived the bounded evidence projection;
- `direction_at_review`: the current tech-tree Direction at the same decision
  point, or JSON `null` if none was available;
- `retention_status`: `"complete"` only when every source reader and
  Direction validation completed; otherwise `"unavailable"` (for example,
  no readable goal channel or an unreadable source).

The change is retention only. It does not add an evidence source, a priority,
a demand kind, a scheduler, a gate, or a fitness input. It does not touch
`demand.py`.

## Compatibility

Existing ledger rows omit the retention fields. The reader contract maps that
absence to `status: "unavailable"`; it must never be treated as `[]` or
`null`, and the writer does not backfill old rows. A new row whose source scan
could not run is also explicitly unavailable. This avoids the #1374 class of accidental
cycle/grouping changes from a new key on legacy records.

## Acceptance

- New rows preserve the source set and decision-time Direction for every
  recorded `goal_review` outcome, including `no_gaps`.
- An evidence source displaced by the existing prompt bound is not recorded as
  available to the model.
- A legacy or malformed row reads as unavailable, not empty.
- Existing cycle grouping remains unchanged: goal-review rows have no
  `cycle_id`, and no reader treats the new provenance keys as a cycle field.
- No change to `_TARGETS`, demand order, `validate_priority`, the #879 reorder,
  scorecard behavior, or fitness.

## Follow-up

#1642 owns the replay. It begins after 14 calendar days of post-rollout
observations and is due within three calendar days after that. The predeclared
threshold for considering a Direction rule a demand creator is at least three
recorded reviews with an empty retained source set and a recorded Direction;
zero and unavailable remain distinct results.
