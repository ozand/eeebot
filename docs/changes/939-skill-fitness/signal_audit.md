# Signal audit — #939 context (adjacent to #929)

## What this document covers

This note captures the signal-audit surface that became visible when implementing
#939 (skill-fitness sidecar).  It is _informational_ — no new signal type is added
by #939 — and records the outcome of the audit for future reference.

## Harness signals (`HARNESS_SIGNALS`)

Defined in `nanobot/runtime/usage_evidence.py`:

```python
HARNESS_SIGNALS: frozenset[str] = frozenset({"pycache", "output", "benchmark", "reference"})
```

These are the **only** signal values the scorecard trusts when counting confirmed
entries in `demand/completed.json`.  A `confirmed=True` entry whose `signal` is
not in this set is treated as a foreign/forged write and ignored (live reward-hack
2026-07-17 defence, pinned in `test_bridge_fitness_integrity.py`).

## Skill-fitness signal (NOT added)

The `skill_fitness/reads.json` sidecar introduced by #939 uses its own `confirmed`
field with a **different semantics**: it reflects the birth-use guard
(last-edit commit vs cycle base sha), not a harness usage signal.  It is a separate
accounting file and is NOT folded into `HARNESS_SIGNALS`.  The scorecard does not
yet surface skill-fitness counts in the fitness targets or gap analysis — this is
left as a future gap (hypothetically: a `skill_reads_per_integration` metric once
enough data accumulates).

## Spawn-boundary coverage

`skill_fitness/reads.json` is added to `scorecard.FITNESS_SIDECARS` so the
bridge's pre-spawn/pre-gate hash window covers it.  An instance that appends
false skill reads to claim fitness credit would be detected as a
`sidecar_write_during_spawn` integrity incident, recorded in the ledger.

## #929 adjacency

Issue #929 audited the available signal surface; this document confirms that #939
does not widen `HARNESS_SIGNALS` or add a new usage-evidence signal, so the
#929 audit boundary is unchanged.
