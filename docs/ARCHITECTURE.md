# eeebot Architecture Map

Last updated: 2026-06-24

## Overview

`eeebot` operates as a bounded self-improving engineering runtime designed for the `eeepc` weak host. Instead of acting as an open-ended conversational bot, it strictly follows a bounded cycle of observing state, selecting a constrained task, running an experiment, and persistently writing proof to a canonical state root before promoting changes.

## System Flow

The system runs via a strictly delineated control path:

```text
CLI / systemd timer
      ↓
cycle runner (Coordinator — bookkeeping only, ALLOW_CODE_EDITS=false)
      ↓
state root (Evidence / Control Plane)   ──→ subagent request artifact
      ↓                                            ↓
health summary / promotion packet         subagent bridge (executor qwen)
      ↓                                            ↓
export repo / human review              cycle-branch isolation → import-smoke
      ↓                                            ↓ (PASS)
canonical repo  ←──────────────────────  integrate-to-main (eeebot-self-evolving)
      ↓
pinned runtime deploy (Side-by-side verification)
```

Two distinct write paths meet at the canonical repo: the **coordinator** never
edits code (it only writes state evidence and queues a bounded subagent request),
while the **subagent bridge** runs the executor model, writes code in an isolated
per-cycle git branch (`selfevo/cycle-<id>`), gates it with an import-smoke check of
the changed files, and integrates to `main` only on PASS. A failed gate keeps the
work on the cycle branch and writes a learning artifact — `main` stays clean.

## Minimal Cycle Contract

A cycle runs under a strict budget (hourly or fixed limits). Every execution cycle MUST produce durable state evidence of its activity. The foundational artifacts are:

### Write Path
1. **`state/reports/<cycle_id>.json`**
   The single definitive log of what the runtime executed, why, and the outcome (`PASS` | `BLOCK` | `CRASH`).
2. **`state/current.json`**
   The latest snapshot pointing to the active goal, active cycle, and active tasks.
3. **`state/promotions/`**
   If a change needs to graduate to canonical source, a promotion candidate is generated here for human or policy review.

### Contract Format (Conceptual)

```json
{
  "cycle_id": "<unique-id>",
  "started_at": "...",
  "ended_at": "...",
  "goal": "<active-goal>",
  "task": "<bounded-task>",
  "status": "PASS|BLOCK|CRASH",
  "outcome": "keep|discard|blocked|crash",
  "changed_files": ["..."],
  "promotion": {
    "readiness": "absent|ready_for_review|blocked"
  },
  "risk": "low|medium|high",
  "next_action": "..."
}
```

Everything else outside this contract is considered *extended evidence*.

## Core Components

- **eeebot Core Runtime**: The minimal self-evolution loop decoupled from any specific chat provider. Handles the `Observe -> Reframe -> Specify -> Execute -> Evaluate -> Persist` operating model (see `EEEBOT_SELF_IMPROVING_RUNTIME_OPERATING_CONTRACT.md`). The Insight→next-Hypothesis arc that makes this a *learning* loop (HADI) is described in `EEEBOT_INSIGHT_HYPOTHESIS_LOOP_CLOSURE.md`.
- **State Reader/Aggregator**: Reads runtime state arrays to compute cycle health, material progress, operator utility, and subagent telemetry.
- **Export & Ops Adapters**: Tools like `eeebot cycle-health` and github export runners (`scripts/export_selfevo_repo.py`) that do not mutate live product state but safely export or expose it for operators.
