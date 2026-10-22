# Compatibility and operator tooling

Compatibility aliases and repository tools serve stable operator-facing contracts; they do not grant runtime or host authority.

## Package compatibility

`eeebot` remains the public compatibility package over the `nanobot` implementation; explicit alias modules preserve shared module identity and both CLI entry points. See [[eeebot/__init__.py]], [[nanobot/cli/commands.py#main]], [[nanobot/cli/eeebot.py#main]], and [[docs/specs/migration/spec.md]].

The compatibility shims under `eeebot/` expose bus, channels, CLI, config, cron, heartbeat, providers, runtime state, security, sessions, and utilities. Alias identity is exercised by [[tests/test_eeebot_imports.py]] and import hygiene by [[tests/test_import_hygiene.py]]. Direct tests for every individual shim are not implied.

## Bounded verification tools

Local CI orchestration is separate from product CI and records whether a run occurred, its target set, and outcome. [[scripts/run_local_ci.py#main]] delegates to [[nanobot/runtime/local_ci.py#run_and_record_local_ci]]; the selected invariant tests are declared by [[nanobot/runtime/local_ci.py#DEFAULT_TEST_TARGETS]]. The task does not execute these tools or make host claims.

Evidence and diagnostics tools summarize existing artifacts; they are not themselves fitness authority. Runtime fitness remains harness-owned as described in [[evidence]] and [[docs/specs/self-evolving-runtime/spec.md]].
