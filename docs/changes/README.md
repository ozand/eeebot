# Changes

A **change** is one durable unit of design for a substantial modification to the
product. It is the engineering artifact that travels with a PR — *not* a task
tracker (tasks/status live in GitHub Issues + Project) and *not* a permanent
top-level doc (it gets archived on merge).

This two-tier split — stable `docs/specs/` (current truth) vs `docs/changes/`
(in-flight) — is what keeps the documentation small and honest over time. See
[`CONSTITUTION.md`](../../CONSTITUTION.md) principle 3.

## When do I create a change folder?

| Situation | What to do |
|---|---|
| Bugfix, doc fix, small scoped edit | Just a branch + PR. No change folder. |
| New or changed **capability** (behavior, contract, interface) | Create a change folder. |
| Anything you'd otherwise want to write a `*_PROOF`/`*_NOTE` doc for | Create a change folder — the archived change *is* the proof. |

When in doubt, prefer a PR. The change folder is for work whose *design* needs to
be reviewed before/with the code.

## Layout

```
docs/changes/
  <change-id>/
    proposal.md      # required: problem, intended change, acceptance
    design.md        # optional: how — architecture, trade-offs, sequence
    specs/           # optional: spec delta(s) for affected capabilities
      <capability>/spec.md
  archive/
    <change-id>/...  # merged changes land here (mirrors the active layout)
```

`<change-id>` is short and kebab-case, ideally matching the Issue/PR (e.g.
`approval-truth-normalization` or `gh-123-approval-truth`).

## Lifecycle

1. **Propose** — copy `TEMPLATE/` to `docs/changes/<id>/`, fill `proposal.md`.
   Open/link the GitHub Issue (`story_id` → this change id).
2. **Design** (if non-obvious) — fill `design.md` and any `specs/<capability>/spec.md`
   delta showing how the current spec will change.
3. **Implement** — branch + PR that references the Issue and this change folder.
   CI validates.
4. **Land** — on merge:
   - move `docs/changes/<id>/` → `docs/changes/archive/<id>/`;
   - apply the spec delta into `docs/specs/<capability>/spec.md` (the new current truth);
   - set Project `Status=Done`.

A change is never left active after its PR merges, and never deleted — it is
archived, so the history of *why* a capability changed stays reconstructable.

## Relationship to other layers

- **GitHub Issue** = coordination + status + acceptance (ephemeral). Links here via `story_id`.
- **This change folder** = durable design + spec delta (versioned, archived).
- **`docs/specs/<capability>/spec.md`** = the current truth this change updates.

They never duplicate: status is only in the Issue; design is only here; current
truth is only in the spec.
