# Initial Validator Roadmap

Last updated: 2026-03-28 UTC

## Purpose

This document defines the staged plan for turning the current governance specs
into executable validators.

The goal is not to validate everything at once.
The goal is to protect the highest-value invariants first.

## Initial Scope

The first validator wave should focus on:

- cycle records,
- promotion candidates,
- release artifacts,
- deployment fingerprints,
- evidence references,
- drift classifications,
- deploy decisions.

## Phase 0 - Rule Inventory

- map governance statements to machine-checkable rules,
- identify hard failures vs warnings,
- align each validator to a schema and a source policy document.

## Phase 1 - Core Identity And Provenance Validators

Implement the highest-value checks first:

- stable ID presence,
- candidate -> origin cycle linkage,
- artifact -> source commit linkage,
- deployment fingerprint -> artifact linkage,
- evidence reference completeness,
- target repo/branch validity.

Acceptance:

- malformed provenance or ownership records are rejected.

## Phase 2 - Promotion Gate Validators

Add checks for:

- evidence existence,
- replayability indicators,
- rollback or rejection plan presence,
- ownership boundary preservation,
- no forbidden canonical target.

Acceptance:

- no promotion can pass without evidence and valid target metadata.

## Phase 3 - Build And Deploy Validators

Add checks for:

- artifact metadata completeness,
- reproducible build input declaration,
- no unapproved overlays in release artifacts,
- post-deploy startup and control-path health,
- weak-host fit checks where measurable.

Acceptance:

- invalid artifacts or clearly unsafe deployments are blocked.

## Phase 4 - Runtime Export Validators

Add checks for:

- cycle manifest completeness,
- evidence bundle integrity,
- changed-path summaries,
- capability snapshot freshness,
- promotion metadata presence when emitted.

Acceptance:

- runtime exports become structurally auditable rather than free-form.

## Phase 5 - Reconciliation Validators

Add checks for:

- drift classification presence,
- stale vs unsafe drift distinction,
- repair vs rebuild decision support,
- quarantine or rollback trigger records.

Acceptance:

- reconciliation decisions are explainable and linked to evidence.

## Phase 6 - Hardening And Automation

- turn selected warnings into hard failures,
- integrate validators into CI and deploy flows,
- add regression fixtures,
- extend coverage to more schema types.

## Recommended First Checks

The minimum viable validator set should start with:

1. missing or duplicate stable IDs,
2. broken provenance links,
3. invalid target repo/branch ownership,
4. missing evidence refs for promotion candidates,
5. missing rollback plan,
6. artifact without canonical source commit,
7. deployment fingerprint without artifact ID,
8. drift record without classification.

## Governance Ownership

Validators should be owned by the policy/control-plane side, not by mutable host
state.

The policy docs define what should be true.
Validators are the enforcement path for those truths.

## Practical Rule

Start with the checks that prevent loss of provenance, ownership confusion, and
unsafe promotion.
Only after those are solid should the validator layer expand into richer runtime
quality checks.

## Harness Invocation Contract

Once a validator lands under `scripts/(check|validate|audit|analyze|verify)_*.py`
it is picked up automatically by `nanobot.runtime.validator_harness` and run on a
rotation. That harness invokes every script the same way, so a script that wants
to be exercised (and to have its findings become real demand) must fit this
contract:

- **No arguments.** The harness runs `<python> <script>` (or `<script> --json`
  when the script's own source declares `--json`). A script whose `argparse`
  requires a flag will exit non-zero on every single run; offer a no-argument
  default or a `--test` mode.
- **A per-script timeout and a total budget.** Each run gets 60s; the whole
  rotation gets 240s. A script that cannot finish in that window on the
  target hardware produces no verdict at all, and the harness surfaces that
  as its own demand item ("cannot finish within the harness's per-script
  time budget"). Make the work incremental, or decay-declare the script.
  It is deliberately not dropped from the rotation automatically.
- **What a non-zero exit means.** The harness treats a non-zero exit as a
  finding worth surfacing to the loop as demand — write real errors to
  stderr/stdout, since that text becomes the evidence attached to the item.
- **Decay self-declaration excludes a script.** A script whose source
  contains both `is deprecated and marked as archived` (or `is deprecated and
  scheduled for removal`) and its own `scripts/<name>.py` path is treated as
  having declared itself decayed, and is never selected again. The two do not
  have to be on the same line, because real declarations are not written that
  way. **So if you are writing a validator that audits decay declarations,
  do not put your own path in the same file as the phrase** — refer to the
  scripts you check by variable, not by quoting your own name next to the
  marker you search for. A pattern cannot tell "quotes the phrase" from
  "declares the phrase", and guessing wrong in the exclude direction is what
  silenced 14 validators before #934.
- **Declare `--json`, don't just mention it.** The harness appends `--json`
  only when your source declares the option (an `add_argument("--json")`
  call, or a `"--json" in sys.argv` test). Naming the string in a docstring
  or a pattern list does not count — which means a script that searches
  other files for `--json` will not be handed a flag it cannot parse.
