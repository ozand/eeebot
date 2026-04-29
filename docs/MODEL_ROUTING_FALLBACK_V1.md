# Model Routing Fallback V1

Last updated: 2026-04-29 UTC

## Purpose

Define the smallest task-type routing layer after live host model probes.

## Current Live Telegram Constraint

The live Telegram gateway must use models that are verified against the current
OpenAI-compatible `/chat/completions` endpoint, not just models that appear in
`/v1/models`. On 2026-04-29 the live Telegram key rejected
`qwen3-coder-flash` and `coder-model` at chat-completion time. Do not put those
names back into active routing without a fresh successful `/chat/completions`
probe for the same key.

## Scope

This version only routes the main agent runtime across three task types:

- `general`
- `code`
- `vision`

It keeps a single provider instance and switches only the `model` parameter per turn.

## Routing Rules

### General

Fallback order:

1. `gpt-5.4-mini`
2. `gemini-3-flash`
3. `gpt-5.4`

### Code

Fallback order:

1. `gpt-5.3-codex`

### Vision

Fallback order:

1. `gemini-3.1-flash-image`

## Detection Rules

- `vision` if the inbound message has media attachments
- `code` if the text strongly suggests coding work (`pytest`, `pip`, `npm`, `git`, code fences, file extensions)
- otherwise `general`

## Fallback Rules

Fallback triggers only on model-availability style failures, such as:

- key not allowed
- model not found
- unsupported model
- access denied
- invalid model name

It does not fallback on every error, to avoid hiding prompt/runtime bugs.

## Minimal Planner/Executor Split

V1 also supports a small per-task executor override after the first tool-calling
response:

- `general` executor -> `gpt-5.4-mini`
- `code` executor -> `gpt-5.3-codex`
- `vision` executor -> `gemini-3.1-flash-image`

This keeps one provider instance and only switches the `model` parameter.

## Deferred On Purpose

- no separate planner/executor provider split yet
- no audio/file routing yet (requested `gemini-3.1-flash-lite-preview` is not available on the current host key)
- no dynamic health scoring or latency-based routing
- no cross-provider fallback

## References

- `docs/MODEL_PROBE_RESULTS_2026-03-30.md`
- `todo.md`

## Host Rollout Status

- `modelRouting` is enabled in the live host runtime config.
- Live Telegram gateway default/code executor was repaired to `gpt-5.3-codex` after the `qwen3-coder-flash` chat-completion rejection.
