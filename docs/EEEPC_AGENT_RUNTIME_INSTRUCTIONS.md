# eeepc Agent Runtime Instructions

Last updated: 2026-04-21 UTC

## Purpose

This file describes the intended operator-visible runtime behavior for the bounded self-evolving Nanobot runtime on `eeepc`.

## Canonical live authority root

- `/var/lib/eeepc-agent/self-evolving-agent/state`

## Core behavior

1. Read the current goal and latest bounded plan from the live authority root.
2. Respect the bounded apply approval gate before executing work.
3. Prefer one concrete file-level action or one explicit blocked next step per bounded cycle.
4. Emit durable reports, outbox summaries, and promotion surfaces.
5. Surface reward, credits, and subagent telemetry durably when produced.

## Approval gate

Bounded apply requires a valid approval file:
- `/var/lib/eeepc-agent/self-evolving-agent/state/approvals/apply.ok`

Expected schema:
- JSON with `expires_at_epoch`

## Operator expectations

The dashboard should be able to observe:
- current goal
- current task plan
- approval freshness
- latest PASS/BLOCK status
- blocker reason when blocked
- reward / credits surfaces
- subagent/task correlation

## Safety rule

Do not execute vague or unconstrained changes.
If no concrete bounded action exists, emit a blocked-next-step instead of pretending progress.
