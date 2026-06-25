# .legacy — archived documentation

This folder holds documentation that is **no longer current guidance** but is kept
for historical traceability. Nothing here describes how the system works *now*.

## What lands here

- **Dated proof / verification artifacts** — `*_PROOF_*`, `*_VERIFICATION_*`,
  `TRANCHE*`, one-time convergence/host proofs. These recorded that a past event
  happened; they are not living contracts.
- **Completed plans** — dated `plans/*` master plans and convergence plans whose
  work has shipped.
- **Finished milestones** — the `NANOBOT_*` completion contract/criteria/summary
  from the upstream-completion milestone.
- **Superseded notes** — point-in-time fix notes and evaluations replaced by a
  current doc.

## Structure

Paths mirror their original location, so internal cross-links between archived
files still resolve:

```
.legacy/docs/...            # was docs/...
.legacy/docs/plans/...      # was docs/plans/...
.legacy/docs/userstory/...  # was docs/userstory/...
```

## Rule

Do not cite a `.legacy/` file as authority for current behavior. If something here
is still true and load-bearing, promote that fact into a current doc under `docs/`
and link it — don't reactivate the archived file in place.

See `docs/README.md` for the current documentation index.
