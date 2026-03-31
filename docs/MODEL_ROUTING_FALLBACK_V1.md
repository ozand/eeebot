# Model Routing Fallback V1

Last updated: 2026-03-30 UTC

## Purpose

Define the smallest task-type routing layer after live host model probes.

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
2. `gpt-oss-120b-medium`
3. `coder-model`
4. `gemini-3-flash`
5. `gpt-5.4`

### Code

Fallback order:

1. `qwen3-coder-plus`
2. `gpt-5.3-codex`
3. `qwen3-coder-flash`

### Vision

Fallback order:

1. `vision-model`
2. `gemini-3.1-flash-image`

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

It does not fallback on every error, to avoid hiding prompt/runtime bugs.

## Minimal Planner/Executor Split

V1 also supports a small per-task executor override after the first tool-calling
response:

- `general` executor -> `gpt-oss-120b-medium`
- `code` executor -> `qwen3-coder-flash`
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
- Host simulator still returns valid answers after the rollout.
