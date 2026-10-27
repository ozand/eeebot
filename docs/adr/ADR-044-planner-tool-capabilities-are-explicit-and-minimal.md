---
title: Planner tool capabilities are explicit and minimal
status: proposed
date: 2026-10-26
authors: [eeebot maintainers]
related: ["#2036", "ADR-001", "ADR-028", "ADR-035"]
tags: [runtime, planner, tools, safety]
---

# ADR-044: Planner tool capabilities are explicit and minimal

## Status

Proposed under #2036. This record authorizes no implementation or ADR acceptance.
It covers only the planner-capability slice of #2036; planner timeout
finalization (#1899/#1925) and forced-proposal pressure review remain separate
acceptance items. ADR-035 remains Proposed.

## Context

At `0aaa8e2a4505eaea6c0a3f6a35909f63017b0b42`,
`bridge._run_planning_session` constructs `SubagentManager` with the standard
executor tools and `roles/planner.md`. The role text says not to edit or run git,
but the manager still registers `write_file`, `edit_file`, and `exec`. Its
fitness-sidecar checks and checkout cleanup are detection/recovery, not a
capability boundary; `exec` is not fenced by `denied_paths`.

The planner reads the diary, skills, memory, and lessons and returns structured
final-response text. The bridge parses that response and writes plan entries to
the diary itself (`_write_diary_plan_block`). No planner-authored filesystem
write is required. The executor separately needs its existing mutation tools.
These facts are source-verified; no host state was inspected.

## Decision

### What this IS

`SubagentManager` receives an explicit immutable tool capability profile. A
profile is validated against known tool names before registration; an unknown,
empty, or invalid profile refuses startup and never falls back to a broader
profile. Tool registration and exposed tool definitions use that same validated
profile. The role's allowed-actions description is derived from the profile,
not maintained as a conflicting second list.

The planner profile allows only `read_file` and `list_dir`, the workspace
capabilities required to consult its declared diary, skills, memory, and lesson
paths. The role reads `memory/index.md` directly; `search_memory` is excluded
because its existing `MemorySearchTool.execute` calls
`existence_index.search_memory` with `reindex_first=True`, which creates or
updates `<state_dir>/existence_index/index.sqlite`. This record does not permit
that planner-triggered runtime-state write. The profile also excludes
`write_file`, `edit_file`, `exec`, web tools, `search_memory`, `spawn`, and any
dynamically added or aliased mutation/delegation capability. The bridge passes
the profile explicitly at the planner construction site. The existing executor
profile remains the compatibility default for current non-planner callers and
is unchanged in names and behavior.

Planner output remains its final JSON response. The harness remains the writer
of diary plans and runtime evidence. Tests must verify that parsing and
harness-owned diary/ledger output still work with the planner profile.

### What this IS NOT

This is an application-level tool boundary, not an OS sandbox or a restriction
on what the provider/model can infer from content it is allowed to read. It
does not narrow workspace read access beyond existing tool path rules, add a
command allowlist/runner, add a new output sidecar, introduce dependencies,
change planner semantics, grant the planner delegation, or change executor
tools. No generic capability framework beyond the explicit profile is
introduced. The declared memory entry point `memory/index.md` remains in the
planner role; this change preserves that product obligation without granting a
search tool that mutates runtime index state.

### Success criteria

- The actual planner registry exposes only `read_file` and `list_dir`; a
  forbidden invocation is refused as unavailable and cannot execute. The
  `search_memory` counterexample is covered explicitly by the negative test.
- Invalid profile values fail closed before spawn; no default-profile fallback
  can grant planner write, edit, exec, or delegation tools.
- Planner bridge wiring explicitly passes its profile; the role's capability
  text agrees with the actual registry.
- Final JSON parsing and harness-owned diary/ledger writes remain functional.
- Existing executor registration and prompt tool declarations are unchanged.

## Consequences

### What gets easier

Planner intent no longer depends on prompt obedience for the prohibited write,
edit, search/index-maintenance, and command capabilities. Registry behavior
and role guidance have one profile source.

### What gets harder

A planner requiring a newly justified tool needs a reviewed profile change and
tests. `read_file` and `list_dir` retain existing workspace-scoped application
checks; they are not an OS sandbox or protection from a compromised process.
This decision makes no stronger filesystem-isolation claim.

### What does not change

The executor keeps its existing read/write/edit/list/exec/memory-search set,
workspace constraints, and guarded cycle lifecycle. Planner final response,
plan parsing, harness diary writing, and no-plan recovery contracts remain
unchanged.

## Alternatives Considered

- **Keep current tools and rely on role instructions plus cleanup:** rejected;
  it leaves `exec` and filesystem mutation callable and makes safety depend on
  model compliance.
- **Keep exec but add a read-only command runner/allowlist:** rejected for this
  slice; planner has no required command output, while a runner creates a new
  parser/argument-security surface.
- **Remove every planner tool:** rejected; the planner needs bounded `read_file`
  and `list_dir` access to consult diary/context and memory files directly.
- **Retain `search_memory` with `reindex_first=False`:** rejected for this slice;
  the existing tool still opens/creates and configures the SQLite database, and
  using it would require changing the shared tool's semantics or defining a
  separate query-only access contract. Direct reads preserve the planner's
  existing memory discovery obligation without that state write.
- **Do nothing:** leaves a shared-repository planner with unnecessary
  mutation/command capabilities and no downstream planner gate.

The manager uses its existing error response for unknown tool invocation; the
negative registry test must prove that calling `search_memory` (as well as
write/edit/exec and delegation names) cannot execute or trigger index creation.

## Test Contract

| Claim | Test | Currently |
|---|---|---|
| Actual planner manager registers exactly `read_file` and `list_dir`; write/edit/exec/search_memory/delegation names are unavailable | `tests/test_subagent_manager.py::test_planner_profile_registers_only_read_tools` and `::test_planner_profile_rejects_forbidden_tool_invocation` | not yet written |
| Invalid/unknown profiles fail before spawn and never fall back to executor capabilities | `tests/test_subagent_manager.py::test_invalid_tool_profile_fails_closed` | not yet written |
| Planner profile is passed at bridge construction and role text agrees with the exact two registered tools | `tests/test_agent_chooses.py::test_planner_bridge_passes_read_only_tool_profile` | not yet written |
| Real planning response parsing and harness-owned diary/ledger output succeed without planner write tools | `tests/test_agent_chooses.py::test_planner_profile_preserves_harness_plan_output` | not yet written |
| Default executor registered tools and declarations remain unchanged, including `search_memory` | `tests/test_subagent_manager.py::test_executor_default_profile_unchanged` | not yet written |

## Rollback

Revert the planner profile selection and role capability-text change to restore
the current manager default. The executor default is preserved throughout, so
rollback does not require executor migration. No persisted schema or runtime
state migration is introduced.

## References

#2036; #1899; #1925; `nanobot/agent/subagent.py`;
`nanobot/agent/tools/toolsets.py`; `nanobot/runtime/bridge.py`;
`nanobot/runtime/day_diary.py`; `roles/planner.md`; ADR-001, ADR-028, ADR-035.
