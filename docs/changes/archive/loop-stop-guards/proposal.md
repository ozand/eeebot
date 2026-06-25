# Change: cycle stop-guards (no-progress, bounded revisions, stop conditions)

- **change-id:** loop-stop-guards
- **issue:** (linked on open)
- **capability:** `docs/specs/self-evolving-runtime`
- **role / workstream:** role:developer / workstream:runtime

## Problem

The self-evolving runtime can spend long stretches of wall-clock making no
material progress: cycles repeat the same blocker, re-emit a delivery with no
file change, or re-run a verifier whose output is unchanged. This is the
observed "~95% of cycles on bookkeeping" stagnation (the "не маловато за 12
часов" finding). The current spec asserts that a no-file-change cycle is not a
kept improvement (R8) and that an empty backlog must not stall (R6), but it has
**no normative requirement that the loop actually STOP** when it is making no
progress, **no cap on how many times a failed gate may be revised**, and **no
enumerated set of stop conditions**. So a stalled loop is "incorrect" by R8 but
nothing requires it to terminate.

External reference: [`ksimback/looper`](https://github.com/ksimback/looper)
(MIT) is a Claude-Code loop-design tool whose `loop.yaml` makes exactly these
guards normative and machine-checkable: `loop_control.no_progress`
(`max_stalled_iterations` + enumerated stall signals + `action: stop`),
`gates.*.max_revisions` with `verdict_policy: revise_until_clean`, and an
explicit `stop_conditions` list. We are not adopting its code — only the three
invariants we lack.

## Intended change

Add three normative requirements to `self-evolving-runtime/spec.md`:

- **R11 — no-progress STOP guard.** After a bounded number of consecutive
  stalled cycles (default 2), the runtime SHALL stop the current goal/lane and
  record the stall, rather than continue. Stall is defined by enumerated,
  observable signals.
- **R12 — bounded revisions.** A failed gate (e.g. smoke gate) SHALL be retried
  at most a bounded number of times (default 3) before the experiment ends with
  `blocked`; revisions SHALL NOT be unbounded.
- **R13 — enumerated stop conditions.** The cycle/lane SHALL terminate on an
  explicit, enumerated set of stop conditions (gate clean, max iterations,
  no-progress guard tripped, any budget cap exceeded) — not on budget exhaustion
  alone, implicitly.

Each maps to an observable signal in durable state so the stop reason is
answerable per R7.

## Acceptance

- [ ] `self-evolving-runtime/spec.md` contains R11, R12, R13 with SHALL wording
      and a corresponding scenario each.
- [ ] Each new requirement names the durable-state field / signal that makes it
      checkable (stall counter, revision counter, stop-reason).
- [ ] The capability map / references note Looper as an external reference.
- [ ] No code or runtime behavior is changed in this PR (spec-only); the
      implementation lands as a separate Issue once the contract is agreed.

## Out of scope

- Implementing the guards in `coordinator.py` / the bridge (separate Issue).
- The optional Looper-derived items: independent cross-family review gate before
  promotion, and egress redaction on model calls (separate optional Issues).
- Any change to budget fields (R2) or outcome taxonomy (R4) — those already
  exist and are stronger than Looper's.
