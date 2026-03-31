# Userstory: Supermind Operator Boost

## User Story

As an operator in Telegram,
I want a `/supermind` command that gives the bot a temporary high-thinking boost for a limited number of requests,
so that I can invoke a stronger reasoning mode only when needed without changing the whole runtime permanently.

## Scope

The first version should stay small: one operator command, a bounded request counter,
and a temporary switch to a stronger model/thinking mode for the main runtime.

## Acceptance Criteria

- `/supermind` enables a temporary boost for a fixed number of turns (currently desired: 20).
- The boosted mode is visible in status/provenance output.
- The boost expires automatically after the configured number of requests.
- The feature does not permanently alter the default runtime model.

## Current V1 Slice

- `/supermind [count|off]`
- `/supermind_status`
- boost model: `gpt-5.4`
- boost reasoning effort: `high`
- bounded remaining-turn counter per session

## References

- `docs/MODEL_ROUTING_FALLBACK_V1.md`
- `docs/MODEL_PROBE_RESULTS_2026-03-30.md`
