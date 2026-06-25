# Host Bot Communication

Last updated: 2026-03-29 UTC

## Purpose

This document explains how to communicate with the host bot today, what the
current paths are good for, and what is not yet available.

It exists to reduce confusion between:

- direct SSH terminal use,
- `nanobot agent` CLI sessions,
- the long-running `nanobot gateway` service,
- and a future minimal terminal ingress path.

## Current Communication Paths

There are currently two practical ways to talk to the host bot.

### 1. SSH + `nanobot agent`

This is the simplest direct operator path.

Examples:

```bash
nanobot agent
```

```bash
nanobot agent -m "Run a quick status check"
```

Use this when you want:

- direct manual control,
- quick debugging,
- one-off checks,
- exploratory local interaction without involving channels.

#### Pros

- simple,
- transparent,
- easy to debug,
- no channel ingress required,
- useful for isolated checks.

#### Cons

- requires SSH access,
- tied to the terminal session,
- not the same as the always-on host bot,
- not ideal for durable background workflows.

### 2. Running `nanobot gateway`

This is the persistent host-bot runtime.

It is the long-running process that:

- stays online,
- receives messages from configured channels,
- runs heartbeat-style background work,
- and represents the actual deployed bot surface.

#### Pros

- persistent,
- appropriate for live host operation,
- supports real channel traffic,
- matches production-like behavior.

#### Cons

- harder to debug than direct CLI use,
- more moving parts,
- channel and runtime problems are mixed together,
- terminal interaction is indirect.

## Important Distinction

`nanobot agent` and the running `nanobot gateway` are not the same thing.

`nanobot agent`:

- starts a new local CLI session,
- is good for direct human use,
- is not attached to the already-running gateway process.

`nanobot gateway`:

- is the persistent service,
- handles the deployed bot behavior,
- is what should be treated as the real host bot runtime.

## Practical Recommendation Today

Use this rule:

- use SSH + `nanobot agent` for direct debugging and quick checks,
- use the running gateway for real host-bot validation,
- use the local simulator when you want a bounded, low-blast-radius ingress path
  into the gateway.

## Local Simulator Path

Today, the safest way to exercise the running gateway without relying on Telegram
is the existing local simulator path.

That means:

- inject a message into the simulator inbox,
- let the running gateway process it,
- inspect the simulator outbox.

This is currently the closest thing to a terminal-safe host ingress path for the
live bot.

## What Is Not Yet Available

The project does not yet provide a first-class terminal ingress bridge directly
into the running gateway service.

That means:

- no “attach to the live bot over TTY” mode,
- no dedicated terminal channel for the gateway,
- no persistent terminal ingress transport beyond the simulator and normal chat
  channels.

## Future Minimal Direction

If we add terminal ingress later, it should stay small and boring.

Good target shape:

- one terminal-local ingress surface,
- one shared message path into the existing loop,
- no second bot system,
- no overbuilt transport layer,
- no generic shell or unrelated execution expansion.

## Summary

Today:

- `nanobot agent` over SSH is the simplest direct operator path,
- `nanobot gateway` is the real persistent host bot,
- the simulator is the safest bounded way to inject messages into the live host
  bot without relying on Telegram.

Future:

- a minimal terminal ingress path may be added,
- but only if it remains aligned with product simplicity and reuses the existing
  loop rather than creating a second runtime.
