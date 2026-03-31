# Approval Gate Stabilization Note

Last updated: 2026-03-31 UTC

## Goal

Make approval-window freshness visible enough that the self-evolving runtime stays operable,
without introducing automatic approval renewal.

## Principle

- no auto-renewal
- no silent privilege escalation
- explicit operator-visible status
- heartbeat summaries may surface gate freshness and the next manual step

## Practical Rule

The periodic self-evolving summary should include:

- current approval gate state
- remaining TTL if active
- a small next-step hint if the gate is missing or expired

This keeps the runtime easier to operate without weakening the supervised model.

## Host Rollout Note

- the host workspace summary adapter now surfaces `approval_gate` and a small `Next:` hint
- this was applied without introducing any automatic approval renewal
