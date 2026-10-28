# Planner capability profile (#2036)

This change documents the implementation slice of #2036; it does not track issue status.
The decision and detailed test contract live in Proposed ADR-044.

## Problem

The planner is instructed not to mutate, but currently receives executor file-write/edit and command tools. Tool names alone do not describe file path behavior or the planner's allowed actions.

## Intended change

Use the closed planner profile (`read_file`, `list_dir`; read/list only; existing relative and absolute path behavior) at the SubagentManager registration seam. Generate allowed-actions text from the same profile. Keep the executor compatibility default unchanged and exclude `search_memory` because its current implementation may write the runtime SQLite index.

## Acceptance

See ADR-044 Test Contract. In addition, the planner's existing memory discovery via `memory/index.md`, structured JSON response, and harness-owned diary/ledger output remain intact.

## Out of scope

No filesystem sandbox/path narrowing, runtime-state query tool, command runner, new dependency/framework, host operation, timeout-finalization change, or forced-proposal-pressure change.
