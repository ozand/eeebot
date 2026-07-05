# Chat-Agent Framework — spec

_Status: current. Last updated: 2026-07-05._

## Purpose

The chat-agent framework is the operator-facing surface inherited from upstream
`nanobot`. It exposes one agent processing loop over a set of pluggable chat
channels discovered through a registry/manager, and defines how an operator
talks to the host bot today. Each channel adapts a platform's transport to a
single shared message bus; the agent loop is channel-agnostic.

> Capability surface (2026-07-05, #602): the supported surface is **telegram**
> (chat channel) plus the channel-agnostic **TUI/CLI** (`nanobot agent`) and
> **web/gateway** surfaces (`nanobot gateway`, the HTTP/OpenAI-compatible API,
> webhook receivers). The other built-in chat channels inherited from the
> upstream fork (dingtalk, discord, email, feishu, matrix, mochat, qq, slack,
> wecom, whatsapp) were removed as dead capability surface per operator
> decision recorded on issue #602 — they are not part of this product's
> supported surface. The registry/manager mechanism itself is unchanged and
> still supports external plugins (see `docs/CHANNEL_PLUGIN_GUIDE.md`).

> Implementation note: the framework lives in the `nanobot/` package
> (`nanobot.channels.*`), and `eeebot.channels.*` resolves to the same module
> objects via the compatibility shim. The package name is `nanobot` — final
> state, decided in #619; internal imports use `nanobot.*` (enforced by
> `tests/test_import_hygiene.py`). See the `migration` spec.

## Requirements

### Channel registry and discovery
- R1. The gateway SHALL discover channels from two sources: built-in channels in
  `nanobot/channels/`, and external packages registered under the
  `nanobot.channels` Python entry-point group.
- R2. A channel SHALL be instantiated and started only when its config section
  (`channels.<name>`) has `"enabled": true`.
- R3. A channel plugin SHALL subclass `BaseChannel` and SHALL declare a unique
  `name`; the entry-point key SHALL equal that `name` and SHALL become the config
  section name (`channels.<name>`).

### Channel contract
- R4. A channel SHALL implement `async start()`, `async stop()`, and
  `async send(msg: OutboundMessage)`. `start()` SHALL block until `stop()` is
  called; if `start()` returns, the channel is considered dead.
- R5. On each inbound message a channel SHALL call the base-provided
  `_handle_message(sender_id, chat_id, content, media?, metadata?, session_key?)`,
  which enforces `is_allowed()` and publishes to the bus. A channel SHALL NOT
  bypass `_handle_message` to inject messages directly.
- R6. Access control SHALL be enforced via `config["allowFrom"]`: `"*"` allows all
  senders, `[]` denies all. The check SHALL be applied by `_handle_message` rather
  than reimplemented per channel.
- R7. A channel SHALL override `default_config()` (classmethod) to declare its
  config fields so `nanobot onboard` can auto-populate `config.json`; if not
  overridden the base default SHALL be `{"enabled": false}`.
- R8. `send()` SHALL deliver an `OutboundMessage` (`channel`, `chat_id`, markdown
  `content`, `media` local paths, `metadata`) to the recipient `chat_id` that was
  passed to `_handle_message`, converting markdown to the platform format and
  honoring `metadata` flags (`_progress` for streaming chunks, `message_id` for
  reply threading).

### Host ↔ bot communication
- R9. The framework SHALL provide two practical operator paths to the host bot:
  `nanobot agent` (a new local CLI session over SSH, for direct/debug/one-off use)
  and the long-running `nanobot gateway` service (the persistent deployed host-bot
  runtime that receives channel traffic and runs heartbeat background work).
- R10. `nanobot agent` and the running `nanobot gateway` SHALL be treated as
  distinct: an `agent` CLI session SHALL NOT be assumed to attach to the
  already-running gateway process; the gateway SHALL be treated as the real host
  bot runtime.
- R11. The local simulator path (inject into the simulator inbox → running gateway
  processes it → inspect the simulator outbox) SHALL be the supported bounded,
  terminal-safe way to exercise the live gateway without depending on Telegram.
- R12. There SHALL NOT (today) be a first-class terminal ingress bridge that
  attaches a TTY directly to the running gateway; if one is added later it SHALL
  reuse the existing loop rather than create a second runtime.

## Scenarios

### Scenario: external channel plugin is discovered and enabled
- Given a package registers a `BaseChannel` subclass under the `nanobot.channels`
  entry point with key `webhook`, and `channels.webhook.enabled` is `true`
- When `nanobot gateway` starts
- Then the channel is instantiated, `start()` runs and blocks, and
  `nanobot plugins list` shows it as a `plugin`.

### Scenario: disallowed sender is rejected
- Given a channel with `allowFrom: []`
- When an inbound message arrives and the channel calls `_handle_message`
- Then the message is denied and not published to the bus.

### Scenario: operator exercises the live gateway safely
- Given a running `nanobot gateway` and no Telegram dependency
- When an operator injects a message into the simulator inbox
- Then the gateway processes it and the reply appears in the simulator outbox.

## References

- Reference docs: `docs/CHANNEL_PLUGIN_GUIDE.md` (active developer how-to — see
  recommendation below); `HOST_BOT_COMMUNICATION.md` was folded into this spec
  and removed 2026-07-05 (#613; recoverable from git history).
- Code: `nanobot/channels/base.py` (`BaseChannel`, `_handle_message`,
  `is_allowed`, `default_config`), `nanobot/channels/registry.py`,
  `nanobot/channels/manager.py`, built-in channels in `nanobot/channels/`
  (telegram), `nanobot/bus/events.py`
  (`OutboundMessage`), `nanobot/cli/commands.py` (`gateway`, `agent`, `onboard`,
  `plugins`).
- Related specs: `migration` (package naming), `model-routing`, `host-runtime`.

> Archival note: `HOST_BOT_COMMUNICATION.md` was fully captured here and was
> removed 2026-07-05 (#613). `CHANNEL_PLUGIN_GUIDE.md` is a hands-on
> developer build-a-plugin tutorial (full code skeleton, pyproject entry-point
> wiring, install/verify commands) whose detail is intentionally out of scope for a
> normative spec; recommend keeping it as an active how-to guide rather than
> archiving it.
