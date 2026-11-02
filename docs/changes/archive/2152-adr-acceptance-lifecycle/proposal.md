# Change: Review-before-effective ADR acceptance

- **change-id:** 2152-adr-acceptance-lifecycle
- **issue:** #2152 / https://github.com/ozand/eeebot/issues/2152
- **capability:** development process — ADR acceptance lifecycle
- **role / workstream:** delegated design executor / policy design

## Problem

The current ADR procedure treats the status-flipping commit as acceptance, while
the delivery gate requires independent PASS on the exact final revision before
acceptance or merge. The newly created status-flip SHA cannot satisfy a review
that must occur before acceptance if that flip takes effect immediately.

## Intended change

After independent design review and any required decision acceptance, clarify
the policy so the status-flipping commit is only a provisional candidate;
effective ADR acceptance occurs only after exact-SHA independent review PASS,
applicable required checks, and authorized merge of that unchanged candidate.
Any changed SHA must receive fresh exact-SHA review and checks. Preserve
operator-reserved authority and prior chronology.

## Acceptance

- [ ] Published ADR and routing policy specify candidate, review/check, and
  effective-acceptance chronology unambiguously.
- [ ] Review PASS, checks, and merge apply to the exact same final SHA; changed
  SHA requires fresh gates.
- [ ] Index/status evidence does not claim acceptance before authorized merge.
- [ ] No prior issue/PR status or operator authority is changed or inferred.
- [ ] Independent reviewer verifies policy consistency against the ADR process,
  routing policy, and adjacent open ADR work.

## Out of scope

No implementation or change to ADR policy before independent review of this
Proposed decision. No acceptance of ADR-049, changes to other ADRs/issues/PRs,
retroactive approval, host activity, push, PR, or merge in this design stage.
