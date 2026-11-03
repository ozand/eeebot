# Chat-framework source coverage

This exact-tree ledger assigns every admitted chat-framework source path to a contract section in [[chat-agent-framework]]; it is inventory evidence, not a claim of semantic completeness.

## Inventory boundary

Pinned ledger: 69 Python modules and two shell assets outside runtime at the exact baseline SHA.

Baseline is commit `e5c52196cbef9d21498b7f87563b2b1c4049b802`: 69 tracked Python paths plus two tracked shell assets under `nanobot/`, excluding `nanobot/runtime/`. Each path below is linked to its thematic contract; package initializers are grouped as import/export shims, not given invented behavior. Shell assets are documented but never executed. Adjacent Markdown/config manifests are contextual contract evidence and are not part of this 71-path executable ledger.

## Package and agent

Package launch surfaces and agent modules map to framework turn and delegation contracts.

- [[nanobot/__init__.py]], [[nanobot/__main__.py]] — package/import and launch surfaces: [[chat-agent-framework#Configuration, CLI, and package contracts]].
- [[nanobot/agent/__init__.py]], [[nanobot/agent/block_loader.py]], [[nanobot/agent/context.py]], [[nanobot/agent/loop.py]], [[nanobot/agent/memory.py]], [[nanobot/agent/skills.py]], [[nanobot/agent/subagent.py]] — turn, context, memory, skills, delegated work: [[chat-agent-framework#Turn processing and conversation state]] and [[chat-agent-framework#Tools, skills, and delegated work]].

## Agent tools

Tool modules implement the registry contract and its bounded concrete adapters.

- [[nanobot/agent/tools/__init__.py]], [[nanobot/agent/tools/base.py]], [[nanobot/agent/tools/registry.py]], [[nanobot/agent/tools/toolsets.py]] — tool interface, registration, composition: [[chat-agent-framework#Tools, skills, and delegated work]].
- [[nanobot/agent/tools/cron.py]], [[nanobot/agent/tools/external.py]], [[nanobot/agent/tools/filesystem.py]], [[nanobot/agent/tools/mcp.py]], [[nanobot/agent/tools/memory_search.py]], [[nanobot/agent/tools/message.py]], [[nanobot/agent/tools/shell.py]], [[nanobot/agent/tools/spawn.py]], [[nanobot/agent/tools/web.py]] — concrete tool adapters and side-effect limits: [[chat-agent-framework#Tools, skills, and delegated work]].

## Bus and channels

Message types, queueing, and channel adapters form the transport boundary.

- [[nanobot/bus/__init__.py]], [[nanobot/bus/events.py]], [[nanobot/bus/queue.py]] — typed messages and transport: [[chat-agent-framework#Channels and message transport]].
- [[nanobot/channels/__init__.py]], [[nanobot/channels/base.py]], [[nanobot/channels/manager.py]], [[nanobot/channels/registry.py]], [[nanobot/channels/telegram.py]] — channel lifecycle, discovery, and Telegram adapter: [[chat-agent-framework#Channels and message transport]].

## CLI, configuration, and sessions

Operator commands, validated settings, and conversational sessions are inventoried here.

- [[nanobot/cli/__init__.py]], [[nanobot/cli/commands.py]], [[nanobot/cli/eeebot.py]], [[nanobot/cli/model_info.py]], [[nanobot/cli/onboard_wizard.py]] — command and onboarding surfaces: [[chat-agent-framework#Configuration, CLI, and package contracts]]; model discovery: [[chat-agent-framework#Provider and model contracts]].
- [[nanobot/config/__init__.py]], [[nanobot/config/loader.py]], [[nanobot/config/paths.py]], [[nanobot/config/schema.py]] — config schema, loading, migration, paths: [[chat-agent-framework#Configuration, CLI, and package contracts]].
- [[nanobot/session/__init__.py]], [[nanobot/session/manager.py]] — session state boundary: [[chat-agent-framework#Turn processing and conversation state]].

## Providers, safety, and services

Model adapters, network checks, and periodic chat services are grouped by contract.

- [[nanobot/providers/__init__.py]], [[nanobot/providers/azure_openai_provider.py]], [[nanobot/providers/base.py]], [[nanobot/providers/custom_provider.py]], [[nanobot/providers/factory.py]], [[nanobot/providers/litellm_provider.py]], [[nanobot/providers/model_window.py]], [[nanobot/providers/openai_codex_provider.py]], [[nanobot/providers/registry.py]], [[nanobot/providers/transcription.py]] — provider contracts, resolution, and adapters: [[chat-agent-framework#Provider and model contracts]].
- [[nanobot/security/__init__.py]], [[nanobot/security/network.py]] — network trust boundary: [[chat-agent-framework#Scheduling, heartbeat, telemetry, and safety]].
- [[nanobot/cron/__init__.py]], [[nanobot/cron/service.py]], [[nanobot/cron/types.py]], [[nanobot/heartbeat/__init__.py]], [[nanobot/heartbeat/service.py]], [[nanobot/observability/__init__.py]], [[nanobot/observability/llm_telemetry.py]] — scheduled/periodic work and model-call records: [[chat-agent-framework#Scheduling, heartbeat, telemetry, and safety]].

## Supporting resources

Utilities, skill assets, templates, and process attribution have supporting contracts.

- [[nanobot/crash_record.py]], [[nanobot/utils/__init__.py]], [[nanobot/utils/evaluator.py]], [[nanobot/utils/helpers.py]] — process attribution, response evaluation, and shared utilities: [[chat-agent-framework#Supporting contracts]].
- [[nanobot/skills/skill-creator/scripts/init_skill.py]], [[nanobot/skills/skill-creator/scripts/package_skill.py]], [[nanobot/skills/skill-creator/scripts/quick_validate.py]] — skill authoring/packaging helpers, not runtime agents: [[chat-agent-framework#Tools, skills, and delegated work]].
- [[nanobot/skills/tmux/scripts/find-sessions.sh]], [[nanobot/skills/tmux/scripts/wait-for-text.sh]] — bundled shell helpers, not Python APIs; their execution contracts remain the scripts themselves: [[chat-agent-framework#Tools, skills, and delegated work]].
- [[nanobot/templates/__init__.py]], [[nanobot/templates/memory/__init__.py]] — template package markers with no independent service contract: [[chat-agent-framework#Supporting contracts]].

## Verification and gaps

Named checks are representative anchors; this ledger does not claim complete test coverage.

Named tests in [[chat-agent-framework]] are representative existing checks for the documented contracts, not a one-to-one module coverage assertion. Package initializers and launch shims are justified as import/export surfaces; no behavior or test coverage is inferred solely from their path. Tests for individual paths without a relevant named assertion remain unknown. The manifest boundary can be independently reconstructed from the pinned Git tree; no generated per-symbol inventory or source annotations are introduced.
