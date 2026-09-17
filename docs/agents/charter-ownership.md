# Charter ownership: where `goals.md` lives and who may change it

Facts about the operator charter that used to sit in the file's own preamble
and Validity rules (#1724). They are for the operator and the harness
developer; the executor cannot act on them, so they were removed from the
prompt-injected text and recorded here. The ADR-022 record (#1720) points to
this file.

## Location and sandbox

- `goals.md` ships in the product release tree and is read from the release
  root: `/opt/eeepc-agent/runtimes/self-evolving-agent/current/goals.md`.
- The bridge unit runs under `ProtectSystem=strict` (#880, #944); the release
  tree is root-owned and read-only to the agent. The agent cannot edit the
  charter even if it tried.
- `goal_review.read_charter_text(RELEASE_ROOT)` is the single reader. The
  bridge prepends `# Immutable operator charter` and hands the block to the
  executor as `system_context`; the file itself carries no level-1 heading,
  so the injected block has exactly one (`tests/test_charter_file_shape.py`).

## Immutability with respect to the loop

- Proposals targeting `goals.md` are rejected by the proposer's sizing check
  (`llm_proposer.validate_sizing`) and by the gate's immutable-file check
  (`mutation_policy`; see `tests/test_mutation_surfaces.py`).
- Changing the charter is an operator edit: a product PR against
  `ozand/eeebot`, deployed with the next release (ADR-012).

## Mutable priorities

- Derived, self-generated priorities live in
  `state/goals/derived_priorities.json` under the state root
  (`/var/lib/eeepc-agent/self-evolving-agent/state`) and are owned by
  `goal_review`. They are merged into the priority view at read time
  (`goal_review.merged_goal_text`) and never modify the charter.

## Commit surface

- The loop commits only inside `eeebot-self-evolving/`; the `state/` directory
  is not git-tracked. This rule was stated in `goals.md` as an IMPORTANT
  paragraph and is now owned by the operating instructions (`OPERATING.md`,
  ADR-022 / #1720) — it is no longer repeated in the charter.
