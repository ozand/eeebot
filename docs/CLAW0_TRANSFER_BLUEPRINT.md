# Claw0 Transfer Blueprint

Last updated: 2026-03-22 UTC

## Purpose

Capture which ideas from `repo_research/claw0` should be ported into the
current eeepc stack:

- `nanobot` as operator-facing agent
- `self_evolving_agent` as controlled self-improvement runtime

The goal is not to replace nanobot with claw0. The goal is to transfer the
useful operational patterns that fit weak hardware and file-based autonomy.

## Core Recommendation

Port patterns, not platform.

Use `claw0` as a source of:
- file-backed state
- delivery queues
- resilience wrappers
- layered prompts
- context compaction
- named execution lanes

Do not import the full gateway/channel architecture.

## Workstreams

### WS1 - Durable Delivery Queue (P0)

Status: implemented (v1)

Port from claw0:
- write-ahead queue
- retry/backoff
- startup recovery
- idempotent delivery state

Target files:
- `Project/servers/eeepc/runtime/self_evolving_agent/bridge/bridge_enqueue_followup.py`
- `Project/servers/eeepc/runtime/self_evolving_agent/bridge/bridge_followup_worker.py`
- `Project/servers/eeepc/runtime/self_evolving_agent/app/store.py`
- `repo_research/nanobot/nanobot/agent/improve_bridge.py`

Acceptance:
- pending follow-ups survive restart
- delivered follow-ups are not re-sent
- transient Telegram/API failures are retried with bounded backoff

### WS2 - LiteLLM Resilience (P0)

Status: implemented (v1) / hardening in progress

Port from claw0:
- failure classification
- bounded retry
- cooldown/backoff
- fallback model chain

Target files:
- `Project/servers/eeepc/runtime/self_evolving_agent/app/litellm_client.py`
- `Project/servers/eeepc/runtime/self_evolving_agent/app/orchestrator.py`
- `Project/servers/eeepc/runtime/self_evolving_agent/app/config.py`
- `repo_research/nanobot/nanobot/providers/litellm_provider.py`

Acceptance:
- transient timeout/429 does not hard-fail on first attempt
- report records retry/fallback behavior explicitly

### WS3 - Anti-Repeat Guard (P1)

Status: implemented

Port concept from claw0:
- context and memory should reduce repetition, not amplify it

Target files:
- `Project/servers/eeepc/runtime/self_evolving_agent/app/orchestrator.py`
- `Project/servers/eeepc/runtime/self_evolving_agent/app/memory.py`
- `Project/servers/eeepc/runtime/self_evolving_agent/app/anti_repeat.py` (new)
- `repo_research/nanobot/nanobot/agent/improve_bridge.py`

Acceptance:
- repeated near-identical improve goals are blocked or redirected
- reports show `repeat_goal` / `repeat_summary` instead of endless similar WARNs

### WS4 - Named Lanes (P1)

Status: implemented (v1 metadata routing)

Port from claw0:
- lane-based serialized execution

Suggested lanes:
- `operator`
- `followup`
- `scheduled`

Target files:
- `Project/servers/eeepc/runtime/self_evolving_agent/app/lanes.py` (new)
- `Project/servers/eeepc/runtime/self_evolving_agent/app/orchestrator.py`
- `repo_research/nanobot/nanobot/agent/operator_mode.py`
- `repo_research/nanobot/nanobot/agent/loop.py`

Acceptance:
- timer/followup work cannot starve direct operator requests
- each lane preserves FIFO ordering

### WS5 - Layered Prompt Assembly (P1)

Status: implemented (basic layering; further refinement possible)

Port from claw0:
- base identity + tools + memory + task layering

Target files:
- `Project/servers/eeepc/runtime/self_evolving_agent/app/prompt_stack.py` (new)
- `Project/servers/eeepc/runtime/self_evolving_agent/app/orchestrator.py`
- `Project/servers/eeepc/runtime/self_evolving_agent/prompts/system.txt`
- `Project/servers/eeepc/runtime/self_evolving_agent/prompts/layers/*` (new)

Acceptance:
- prompt behavior can be tuned by editing prompt layer files only
- total prompt size remains bounded with explicit caps

### WS6 - Minimal Routing Model (P2)

Status: implemented (v1)

Port from claw0:
- mode/binding idea, not full gateway server

Target files:
- `repo_research/nanobot/nanobot/agent/operator_mode.py`
- `repo_research/nanobot/nanobot/agent/loop.py`
- bridge command helpers

Acceptance:
- command families resolve deterministically to operational modes
- responses state which mode/lane handled the request

## What To Avoid

- full `claw0` websocket/json-rpc gateway stack
- heavy hybrid retrieval/memory search pipeline on eeepc
- complex auth-profile rotation
- any change that weakens `DRY_RUN`, approval gate, or mode guards

## Recommended Execution Order

1. WS1 Durable Delivery Queue
2. WS2 LiteLLM Resilience
3. WS3 Anti-Repeat Guard
4. WS4 Named Lanes
5. WS5 Layered Prompt Assembly
6. WS6 Minimal Routing Model

## Why This Order

- WS1/WS2 improve correctness and reliability first.
- WS3 reduces repetitive low-value self-improvement.
- WS4/WS5 organize growth cleanly once the runtime is stable.
- WS6 is useful, but only after the core operational path is predictable.

## Immediate Next Focus

The next implementation target should be:

1. Refine minimal routing model on top of lanes.
2. Keep hardening durable delivery queue retries/backoff with fault-injection tests.
3. Add richer source ranking/relevance into subagent artifact manifests.
