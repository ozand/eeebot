---
title: Planner tool capabilities are explicit and minimal
status: accepted
date: 2026-10-26
authors: [eeebot maintainers]
related: ["#2036", "ADR-001", "ADR-028", "ADR-035"]
tags: [runtime, planner, tools, safety]
---

# ADR-044: Planner tool capabilities are explicit and minimal

## Status

Accepted — delegated technical acceptance under the owner-authorized #2036
capability slice, 2026-10-27. This is not personal operator acceptance. The
exact accepted revision is this ADR status commit `fcca5519836ef4c0593bc310509c54a2d947350a`, following prerequisite test
citations merged in PR #2125 at `9c88962c75a3ff5bd4756471679046a4312ac634`.
Implementation was reviewed and merged in PR #2124 at
`2138f0587da76f7a6d63164d5c55a077522829f1`; main CI run `40446247702` passed
all six jobs for that implementation merge. This record covers only the
planner-capability slice; timeout finalization (#1899/#1925) and forced-proposal
pressure review remain separate #2036 acceptance items. ADR-035 remains
Proposed and #2036 remains open.

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

`SubagentManager` receives one of two immutable, built-in tool capability
profiles: the legacy executor profile (the compatibility default) or the
planner profile. A profile explicitly contains tool names, file-operation
allowances, file path-resolution scope, and its generated allowed-actions
text. Profiles are validated against the known profiles and tool implementations
before registration; unknown, empty, or malformed profiles refuse startup and
never fall back to a broader profile. The same resolved profile configures
registered tool implementations and their exposed definitions, and generates
the role's allowed-actions text. The role file contains no competing tool/path
inventory; it retains only the planner's functional instructions.

The planner profile permits only `read_file` and `list_dir`. It preserves the
existing planner path behavior exactly: workspace-relative paths resolve under
the workspace, but absolute paths are accepted and are not restricted to the
workspace; path resolution follows the existing `Path.resolve()` behavior.
`denied_paths` is not applied by either read/list tool today and this decision
does not claim otherwise. Its operation policy is read contents (existing
bounded read/pagination) and list directory entries; it has no write, edit,
command, web, search, or delegation operation. This is an explicit record of
the current read scope, not a new restriction or data-access expansion.

The role reads `memory/index.md` directly; `search_memory` is excluded because
its existing `MemorySearchTool.execute` calls `existence_index.search_memory`
with `reindex_first=True`, which creates or updates
`<state_dir>/existence_index/index.sqlite`. This record does not permit that
planner-triggered runtime-state write. The bridge passes the planner profile
explicitly and does not pass the legacy `restrict_to_workspace` switch for that
spawn. When a profile is explicit, its path policy is authoritative; the legacy
switch is consulted only for callers using the compatibility default and may
not override a profile's resolved file-tool settings. The executor profile
remains the compatibility default and is unchanged in tool names, workspace
restriction behavior, or operations.

Planner output remains its final JSON response. The harness remains the writer
of diary plans and runtime evidence. Tests must verify that parsing and
harness-owned diary/ledger output still work with the planner profile.

### What this IS NOT

This is an application-level tool boundary, not an OS sandbox, kernel
containment, or restriction on what the provider/model can infer from content
it is allowed to read. Because existing planner file reads permit absolute
paths, this record does not claim those reads are workspace-confined. It does
not narrow that existing path scope, add a command allowlist/runner, add a new
output sidecar, introduce dependencies, change planner semantics, grant the
planner delegation, or change executor tools. No generic policy engine or
framework is introduced. The declared memory entry point `memory/index.md`
remains in the planner role; this change preserves that product obligation
without granting a search tool that mutates runtime index state.

### Success criteria

- The actual planner registry exposes exactly `read_file` and `list_dir`; the
  profile's operation set is respectively read-only/paginated read and list
  entries, and its path scope is the explicitly recorded existing unrestricted
  absolute-path resolution plus workspace-relative resolution. Actual tool
  instances receive these settings from the profile.
- A forbidden invocation is refused as unavailable and cannot execute,
  including `write_file`, `edit_file`, `exec`, `search_memory`, and delegation
  names. The `search_memory` counterexample must not create/update the SQLite
  index.
- Unknown/malformed profile values fail closed before spawn; neither the
  legacy `restrict_to_workspace` flag nor an omitted/invalid explicit profile
  can silently replace or broaden a selected profile.
- Planner bridge wiring explicitly selects the planner profile. The profile-
  generated allowed-actions text fills one role placeholder and agrees with the
  actual registry and path/operation policy; no second role inventory exists.
- The permitted reads/list operations work under the declared path rules; tests
  exercise both accepted and denied cases required by those rules.
- Final JSON parsing and harness-owned diary/ledger writes remain functional.
- Existing executor registration, prompt declarations, path behavior, and
  operations remain unchanged under the compatibility default.

## Consequences

### What gets easier

Planner intent no longer depends on prompt obedience for the prohibited write,
edit, search/index-maintenance, and command capabilities. Registry behavior
and role guidance have one profile source.

### What gets harder

A planner requiring a newly justified tool or path/operation change needs a
reviewed profile change and tests. The planner profile intentionally preserves
its existing broad read scope, so least privilege is improved for tool kinds
but not for filesystem path reach. That limitation is visible rather than
misrepresented as workspace confinement.

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
- **Remove every planner tool:** rejected; the planner needs `read_file` and
  `list_dir` access to consult diary/context and memory files directly.
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

Profile selection is intentionally a small closed set rather than an open
policy engine: executor-default and planner-read-only profiles only. Future
roles require an explicit reviewed profile, not arbitrary runtime composition.

## Test Contract

| Claim | Test | Currently |
|---|---|---|
| The built-in planner profile names exactly `read_file`/`list_dir`, encodes their read/list operations and the existing absolute/workspace-relative path policy, and rejects unknown profiles | `tests/test_planner_tool_profile.py::test_planner_profile_declares_exact_tools_operations_and_existing_paths`, `::test_invalid_profiles_fail_closed` | passing |
| Actual planner manager resolves path policy over legacy flag; actual `ReadFileTool`/`ListDirTool` preserve relative and absolute path behavior | `tests/test_planner_tool_profile.py::test_profile_is_source_for_registration_and_allowed_actions`, `::test_planner_read_and_list_use_preserved_path_policy` | passing |
| Actual planner manager exposes only read/list; forbidden provider tool calls are refused by that same registry without file/index/exec effects | `tests/test_planner_tool_profile.py::test_planner_registry_forbids_mutating_tools_without_side_effects` | passing |
| Planner allowed-actions text comes from its profile placeholder, role file has no duplicate tool/path inventory, and bridge selects the profile explicitly | `tests/test_planner_tool_profile.py::test_profile_is_source_for_registration_and_allowed_actions`, `::test_planner_registry_forbids_mutating_tools_without_side_effects`, `::test_planner_bridge_wiring_selects_explicit_profile` | passing |
| Real planning response parsing and harness-owned diary/ledger output succeed without planner write tools | `tests/test_run_planning_session.py::test_happy_path_writes_the_diary_and_journals_success` | passing; focused test rerun |
| Default executor profile preserves existing registered tools and interactive web compatibility | `tests/test_subagent_manager.py::test_executor_declared_and_registered_tool_names_match`, `::test_interactive_subagent_role_keeps_web_tools_available`, `tests/test_planner_tool_profile.py::test_profile_is_source_for_registration_and_allowed_actions` | passing |

## Rollback

Revert the planner profile selection and role capability-text change to restore
the current manager default. The executor default is preserved throughout, so
rollback does not require executor migration. No persisted schema or runtime
state migration is introduced.

## References

#2036; #1899; #1925; `nanobot/agent/subagent.py`;
`nanobot/agent/tools/toolsets.py`; `nanobot/runtime/bridge.py`;
`nanobot/runtime/day_diary.py`; `roles/planner.md`; ADR-001, ADR-028, ADR-035.
