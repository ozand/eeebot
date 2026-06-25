# Block-Cycle Signal Note

Last updated: 2026-03-31 UTC

## Goal

Surface when the current active goal is repeatedly ending in `BLOCK`, without adding a new monitoring subsystem.

## Applied Slice

- `/improve_status` now emits a compact `BLOCK-cycle risk` line
- the signal uses only the latest readable canonical reports in `state/reports`
- the signal is read-only and does not change execution behavior

## Example

`BLOCK-cycle risk: HIGH (goal="goal-44e50921129bf475", blocked_last_n=3/3, latest_status=BLOCK)`

## Purpose

This gives the operator and the runtime a canonical warning that the current goal is repeating a blocked path and may need gate refresh, reprioritization, or deeper intervention.
