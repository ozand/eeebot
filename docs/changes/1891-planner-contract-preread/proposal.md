# Planner contract pre-read

## Problem

The production planner ignored the task-writing path, spent its 600-second wall clock on other discovery, and context compaction removed the instruction. Model-issued `read_file` is therefore not a reliable trust boundary.

## Change

Before spawning the planner, bridge reads the immutable release skill with a 16 KiB limit and UTF-8 validation. It supplies the contract only in that planner run's task message and records source, bytes and SHA-256 in the existing planning-session ledger row.

## Acceptance

A missing or invalid carrier prevents planner spawn and degrades to the ranked queue. Timeout remains `timed_out`, distinct from pre-read refusal. Live rollout must show `task_writing_read=true`, `task_writing_source=harness_release_preread`, and no integrity incident.

## Non-goals

Do not place the contract in executor prompts, alter #1859 semantics, or implement #1854 propagation.
