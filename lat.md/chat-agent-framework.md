# Chat-agent framework

The operator-facing framework processes conversational turns through shared agent services, provider adapters, channel transports, and bounded tools; it is distinct from the timer-driven host runtime in [[architecture#Two runtime surfaces#Operator-facing chat framework]].

## Turn processing and conversation state

The agent assembles conversational context, invokes models and tools, and maintains session memory.

[[nanobot/agent/loop.py#AgentLoop]] coordinates a turn: context assembly, model generation, tool execution, and response delivery. [[nanobot/agent/context.py#ContextBuilder]] composes prompt material and [[nanobot/agent/memory.py#MemoryStore]] owns persisted conversational memory; [[nanobot/session/manager.py#SessionManager]] separates sessions. Prompt size and source trust boundaries remain explicit; runtime-specific policy imported by shared context does not make the chat framework a host-cycle authority. Existing verification includes [[tests/test_cli_input.py]], [[tests/test_memory_v2.py]], [[tests/test_session_manager.py]], and [[tests/test_context_prompt_budget.py]].

## Tools, skills, and delegated work

Registered tools provide bounded interfaces for agent side effects, skills, and delegated work.

[[nanobot/agent/tools/registry.py#ToolRegistry]] provides the tool contract; individual adapters cover filesystem, shell, web, messaging, cron, memory search, MCP, and spawn. Tools expose side effects through their own boundaries: filesystem paths are constrained, shell execution is bounded, and web targets are validated. [[nanobot/agent/skills.py#SkillsLoader]] loads skill guidance; bundled skill-creator scripts validate/package authored skills, while tmux scripts are executable helpers rather than Python APIs. Verification includes [[tests/test_tool_validation.py]], [[tests/test_filesystem_tools.py]], [[tests/test_exec_security.py]], [[tests/test_web_fetch_security.py]], [[tests/test_mcp_tool.py]], [[tests/test_subagent_manager.py]], and [[tests/test_skill_creator_scripts.py]].

## Channels and message transport

[[nanobot/bus/queue.py#MessageBus]] transports typed [[nanobot/bus/events.py#InboundMessage]] and [[nanobot/bus/events.py#OutboundMessage]] values between [[nanobot/channels/manager.py#ChannelManager]] and the agent-facing path. [[nanobot/channels/base.py#BaseChannel]] defines lifecycle behavior; channel discovery and Telegram are adapters, not a guarantee that every channel is configured or running. See [[tests/test_base_channel.py]], [[tests/test_channel_plugins.py]], and [[tests/test_telegram_channel.py]].

## Configuration, CLI, and package contracts

Typed configuration and dual CLI entry points define operator-facing package contracts.

[[nanobot/config/schema.py#Config]] is the typed configuration contract and [[nanobot/config/loader.py#load_config]] loads/migrates it; [[nanobot/config/paths.py#get_data_dir]] resolves user data paths. The supported `nanobot` CLI and external `eeebot` compatibility CLI remain separate entry points: [[nanobot/cli/commands.py#main]] and [[nanobot/cli/eeebot.py#main]]. Interactive configuration, model discovery, and gateway commands are operator surfaces, not host deployment controls. See [[tests/test_config_schema.py]], [[tests/test_config_migration.py]], [[tests/test_commands.py]], and [[tests/test_eeebot_cli.py]].

## Provider and model contracts

Provider interfaces and adapters route model requests and expose model metadata.

[[nanobot/providers/base.py#LLMProvider]] defines generation behavior; [[nanobot/providers/registry.py#find_by_model]] resolves provider identities and [[nanobot/providers/factory.py#_make_provider]] constructs configured adapters. LiteLLM, Azure, custom, OpenAI Codex, and transcription paths have distinct wire/credential requirements; optional provider imports remain lazy. [[nanobot/providers/model_window.py#resolve_context_window]] resolves model capacity; [[nanobot/cli/model_info.py#find_model_info]] presents operator-facing model information. Relevant checks include [[tests/test_providers_init.py]], [[tests/test_provider_lazy_imports.py]], [[tests/test_azure_openai_provider.py]], [[tests/test_provider_retry.py]], and [[tests/test_model_window.py]].

## Scheduling, heartbeat, telemetry, and safety

Chat services schedule agent work, record model calls, and apply network safety checks.

[[nanobot/cron/service.py#CronService]] manages scheduled jobs described by [[nanobot/cron/types.py#CronJob]]; [[nanobot/heartbeat/service.py#HeartbeatService]] provides periodic agent work. These are chat-framework services, not the separate host runtime scheduler. [[nanobot/observability/llm_telemetry.py#record_llm_call]] records model-call observations with redaction/retention constraints. [[nanobot/security/network.py#validate_url_target]] and [[nanobot/security/network.py#validate_resolved_url]] enforce network-target checks also relevant to tool adapters. See [[tests/test_cron_service.py]], [[tests/test_heartbeat_service.py]], [[tests/test_llm_telemetry.py]], and [[tests/test_security_network.py]].

## Supporting contracts

Shared helpers, evaluation, process attribution, and package assets support framework services.

[[nanobot/utils/helpers.py#split_message]] and [[nanobot/utils/helpers.py#sync_workspace_templates]] provide shared formatting/workspace utilities; [[nanobot/utils/evaluator.py#evaluate_response]] evaluates responses. [[nanobot/crash_record.py#record_exit]] records process outcomes. Package initializers and [[nanobot/__main__.py]] expose package/launch surfaces; template initializers and skill scripts are distribution assets, not independent services. Check [[tests/test_helpers_tiktoken_fallback.py]], [[tests/test_evaluator.py]], [[tests/test_attribution.py]], and [[tests/test_import_hygiene.py]].

## Contracts, limits, and decision records

This map records implemented contracts without implying unverified coverage or ADR acceptance.

Executable code and configuration describe current behavior; adjacent bundled Markdown/config resources supply operator and skill contracts. No private configuration contents are included here. The framework reuses the package identity and dual CLI contract in [[architecture#Product identity and package layout]]; that section's source-precedence rule applies. This map does not change or imply acceptance of any ADR; statuses remain those in [[docs/adr/README.md]]. Tests named above are existing verification anchors, not proof that every behavior or module is covered.
