# Persona harness — does a written trait actually work?

The loop's behaviour is meant to be shaped by the release-owned context files:
`IDENTITY.md`, `SOUL.md`, `goals.md`, `USER.md`, `OPERATING.md`. Until this
harness existed, the only evidence any of it worked was that the files
*arrived* — the `phase: "system_prompt"` ledger row records each block's size
and `missing: []`. Arrival is not effect.

Two measurements, both on demand, neither wired into a cycle or a deploy gate.

## Self-consistency regression set

`persona/statements.json` holds first-person statements whose correct answer
follows from the character definition, wrapped in a plausible fiction so that
asking does not knock the model out of role. Answers are constrained to
yes/no and each statement is sampled several times, so sampling noise can be
told apart from disagreement.

A statement the model does not agree with is a **definition gap**: the trait
is written, and it is not working.

Every statement names the file and the trait it came from. A gap that cannot
be traced back to a line in a file is not actionable.

## Controls

Two or three statements are true or false independently of the persona
("two plus two equals four"). They are asked first. If one of them comes back
wrong, the run stops and reports **plumbing broken**, and no character
verdict is published — a run whose plumbing is suspect produces numbers that
look exactly like character findings and mean nothing.

## Ablation

`--ablate SOUL.md` re-assembles the prompt without that block and re-runs the
same set, printing the per-statement delta against baseline. A block whose
removal moves nothing is a block that is not earning its place in every
prompt.

The block is removed by assembling against a shadow release root that lacks
the file, so the builder's own `[missing: X]` degradation path runs. Nothing
patches the assembler, and the real release files are never modified.

## Running it

```bash
export LITELLM_BASE_URL=...      # the operator's gateway
export LITELLM_API_KEY=...       # never read from disk, never printed

# what it would cost, without spending it
python3 scripts/run_persona_harness.py --dry-run

# baseline against the current release files
python3 scripts/run_persona_harness.py --json /tmp/persona-baseline.json

# baseline plus one ablation per block (raise the budget deliberately)
python3 scripts/run_persona_harness.py --ablate-all --max-calls 800
```

The run states its planned call count before the first call and refuses to
start over `--max-calls`. Default: 3 repeats over 34 behavioural statements
and 3 controls — **111 calls** for a baseline, 666 with every block ablated.

`--workspace` points the workspace-owned block at a loop checkout. Left
unset, that block renders as missing, which the output's `sections` line
reports rather than hiding.

## What it does not do

- It does not change the persona files. It says which traits work; the edit is
  a separate decision.
- It does not measure task performance. A character that answers consistently
  may still be bad at the job; that is a different instrument.
- It does not run per cycle and it does not gate a deploy.

## Why the prompt is not assembled here

The harness calls `ContextBuilder.build_system_prompt(loop_profile=True)` —
the same call the bridge makes — and derives the ablatable block list from
`ContextBuilder.BOOTSTRAP_FILES`. A private copy of the block list or the
assembly order would measure a prompt nobody runs, and would drift the moment
ADR-022 changed. `tests/test_persona_harness.py` fails if a block filename is
ever hard-coded in the harness module.
