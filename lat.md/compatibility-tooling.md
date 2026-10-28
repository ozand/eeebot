# Compatibility and operator tooling

The compatibility package keeps the public `eeebot` import and CLI surface on the permanent `nanobot` implementation.

## Package compatibility

Compatibility shims preserve import paths while implementation remains in `nanobot.*`.

The root shim extends package search and aliases selected module objects; local forwarding shims re-export canonical modules. Product identity and both CLI contracts are [[docs/specs/migration/spec.md]]; entrypoints are [[nanobot/cli/commands.py#main]] and [[nanobot/cli/eeebot.py#main]].

| `eeebot/` path | Compatibility role / target | Verified existing identity test |
|---|---|---|
| [[eeebot/__init__.py]] | Root `eeebot` identity, extended search path, selected `sys.modules` aliases to `nanobot.*`. | [[tests/test_eeebot_imports.py#test_import_eeebot_top_level_aliases_nanobot_metadata]] |
| [[eeebot/__main__.py]] | `python -m eeebot` delegates to `eeebot.cli.eeebot.main`. | No direct module-entry identity assertion found. |
| [[eeebot/bus/__init__.py]] | Bus package marker; root search-path alias resolves package target `nanobot.bus`. | [[tests/test_eeebot_imports.py#test_import_eeebot_bus_package_alias]] |
| [[eeebot/bus/events.py]] | Star re-export of `nanobot.bus.events`. | [[tests/test_eeebot_imports.py#test_import_eeebot_bus_events_alias]] |
| [[eeebot/bus/queue.py]] | Star re-export of `nanobot.bus.queue`. | [[tests/test_eeebot_imports.py#test_import_eeebot_bus_queue_alias]] |
| [[eeebot/channels/__init__.py]] | Package shim extends `nanobot.channels` path and exports `BaseChannel`, `ChannelManager`. | [[tests/test_eeebot_imports.py#test_import_eeebot_channels_package_alias]] |
| [[eeebot/channels/base.py]] | Star re-export of `nanobot.channels.base`. | [[tests/test_eeebot_imports.py#test_import_eeebot_channels_base_alias]] |
| [[eeebot/channels/manager.py]] | Star re-export of `nanobot.channels.manager`. | [[tests/test_eeebot_imports.py#test_import_eeebot_channels_manager_alias]] |
| [[eeebot/channels/registry.py]] | Star re-export of `nanobot.channels.registry`. | [[tests/test_eeebot_imports.py#test_import_eeebot_channels_registry_alias]] |
| [[eeebot/cli/__init__.py]] | Star re-export of `nanobot.cli`. | Direct package symbol identity assertion not found. |
| [[eeebot/cli/commands.py]] | Re-exports canonical `nanobot.cli.commands.app`. | [[tests/test_eeebot_imports.py#test_import_eeebot_cli_commands_alias]] |
| [[eeebot/cli/eeebot.py]] | Star re-export of `nanobot.cli.eeebot`; `eeebot` command compatibility entry. | [[tests/test_eeebot_cli.py#test_eeebot_cli_alias_imports]] |
| [[eeebot/cli/onboard_wizard.py]] | Star re-export of `nanobot.cli.onboard_wizard`. | [[tests/test_eeebot_imports.py#test_import_eeebot_cli_onboard_wizard_alias]] |
| [[eeebot/config/__init__.py]] | Star re-export of `nanobot.config`. | [[tests/test_eeebot_imports.py#test_import_eeebot_config_package_alias]] |
| [[eeebot/config/loader.py]] | Star re-export of `nanobot.config.loader`. | [[tests/test_eeebot_imports.py#test_import_eeebot_config_loader_alias]] |
| [[eeebot/config/paths.py]] | Star re-export of `nanobot.config.paths`. | [[tests/test_eeebot_imports.py#test_import_eeebot_config_paths_alias]] |
| [[eeebot/config/schema.py]] | Star re-export of `nanobot.config.schema`. | [[tests/test_eeebot_imports.py#test_import_eeebot_config_schema_alias]] |
| [[eeebot/cron/__init__.py]] | Cron package marker resolved through root alias to `nanobot.cron`. | [[tests/test_eeebot_imports.py#test_import_eeebot_cron_package_alias]] |
| [[eeebot/cron/service.py]] | Star re-export of `nanobot.cron.service`. | [[tests/test_eeebot_imports.py#test_import_eeebot_cron_service_alias]] |
| [[eeebot/cron/types.py]] | Star re-export of `nanobot.cron.types`. | [[tests/test_eeebot_imports.py#test_import_eeebot_cron_types_alias]] |
| [[eeebot/heartbeat/__init__.py]] | Package marker resolved through root alias to `nanobot.heartbeat`. | [[tests/test_eeebot_imports.py#test_import_eeebot_heartbeat_package_alias]] |
| [[eeebot/heartbeat/service.py]] | Star re-export of `nanobot.heartbeat.service`. | [[tests/test_eeebot_imports.py#test_import_eeebot_heartbeat_service_alias]] |
| [[eeebot/providers/__init__.py]] | Provider package marker resolved through root alias to `nanobot.providers`. | [[tests/test_eeebot_imports.py#test_import_eeebot_providers_package_alias]] |
| [[eeebot/providers/azure_openai_provider.py]] | Star re-export of `nanobot.providers.azure_openai_provider`. | No direct alias identity assertion found. |
| [[eeebot/providers/base.py]] | Star re-export of `nanobot.providers.base`. | [[tests/test_eeebot_imports.py#test_import_eeebot_provider_base_alias]] |
| [[eeebot/providers/custom_provider.py]] | Star re-export of `nanobot.providers.custom_provider`. | No direct alias identity assertion found. |
| [[eeebot/providers/litellm_provider.py]] | Star re-export of `nanobot.providers.litellm_provider`. | No direct alias identity assertion found. |
| [[eeebot/providers/openai_codex_provider.py]] | Star re-export of `nanobot.providers.openai_codex_provider`. | No direct alias identity assertion found. |
| [[eeebot/providers/registry.py]] | Star re-export of `nanobot.providers.registry`. | [[tests/test_eeebot_imports.py#test_import_eeebot_provider_registry_alias]] |
| [[eeebot/providers/transcription.py]] | Star re-export of `nanobot.providers.transcription`. | No direct alias identity assertion found. |
| [[eeebot/runtime/__init__.py]] | Runtime package marker; root package search resolves runtime modules from `nanobot`. | No direct package identity assertion found. |
| [[eeebot/runtime/state.py]] | Star re-export of `nanobot.runtime.state`. | [[tests/test_eeebot_imports.py#test_import_eeebot_runtime_state_alias]] |
| [[eeebot/security/__init__.py]] | Security package marker resolved via extended root path. | No direct package identity assertion found. |
| [[eeebot/security/network.py]] | Star re-export of `nanobot.security.network`. | [[tests/test_eeebot_imports.py#test_import_eeebot_security_network_alias]] |
| [[eeebot/session/__init__.py]] | Session package marker resolved via extended root path. | No direct package identity assertion found. |
| [[eeebot/session/manager.py]] | Star re-export of `nanobot.session.manager`. | [[tests/test_eeebot_imports.py#test_import_eeebot_session_manager_alias]] |
| [[eeebot/utils/evaluator.py]] | Star re-export of `nanobot.utils.evaluator`. | [[tests/test_eeebot_imports.py#test_import_eeebot_utils_evaluator_alias]] |

Alias tests verify selected public imports/identities, not every symbol or behavior in the canonical modules. Import hygiene is separately covered by [[tests/test_import_hygiene.py]].

## Bounded verification tools

Local CI orchestration is separate from product CI and records whether a run occurred, its target set, and outcome. [[scripts/run_local_ci.py#main]] delegates to [[nanobot/runtime/local_ci.py#run_and_record_local_ci]]; the selected invariant targets are declared by [[nanobot/runtime/local_ci.py#DEFAULT_TEST_TARGETS]]. This task does not execute these tools or make host claims.

Evidence and diagnostics tools summarize artifacts; they are not fitness authority. Runtime fitness remains harness-owned as described in [[evidence]] and [[docs/specs/self-evolving-runtime/spec.md]].
