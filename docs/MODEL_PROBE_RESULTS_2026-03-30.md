# Model Probe Results — 2026-03-30

## Purpose

Record the live host endpoint checks before implementing any model fallback or
task-type routing changes.

## Endpoint Context

- runtime provider: `custom`
- endpoint style: OpenAI-compatible gateway behind the current host config
- probing method: tiny chat-completion calls with very small outputs

## Important Finding

The current endpoint key accepts the provider's raw model names, not the
`openai/...`-prefixed aliases used in planning notes.

Examples:

- works: `gpt-5.4-mini`
- does not work with the current key/endpoint: `openai/gpt-5.4-mini`

## Probe Summary

### General work — verified `ok`

- `gpt-5.4-mini`
- `gpt-oss-120b-medium`
- `coder-model`
- `gpt-5.4`
- `gemini-3-flash`

### Code work — verified `ok`

- `qwen3-coder-plus`
- `gpt-5.3-codex`
- `qwen3-coder-flash`

### Vision — verified `ok`

- `vision-model`
- `gemini-3.1-flash-image`

### Requested but not available with the current key/endpoint

- `gemini-3.1-pro-preview`
- `gemini-3.1-pro-high`
- `gemini-3.1-flash-lite-preview`

## Implication for the Next Step

- Do not implement fallback routing against the `openai/...` aliases first.
- Use the raw model names that the current endpoint actually accepts.
- Treat the unavailable requested models as access/key issues, not just prompt issues.
