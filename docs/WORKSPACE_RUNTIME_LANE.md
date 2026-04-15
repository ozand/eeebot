# Workspace Runtime Lane

Last updated: 2026-04-15 UTC

## Purpose

This document defines the agreed runtime lane for local self-development inside
the project workspace.

It is intentionally:

- workspace-only,
- no-root,
- Python-first,
- truthfully probed,
- aligned with the existing action registry and executor architecture.

The goal is to let the bot move from read-only inspection toward real local
experimentation without opening generic shell freedom too early.

## Current `eeepc` Boundary Note

This document describes the repo-side workspace runtime lane.
It does not by itself prove that the live `eeepc` host self-evolving loop is using this lane as its execution authority.

As of 2026-04-15, the live host self-evolving authority verified on `eeepc` is:
- `/var/lib/eeepc-agent/self-evolving-agent/state`

Therefore:
- workspace-lane artifacts under a deployed gateway workspace should be treated as implementation-slice evidence
- live `BLOCK`/`PASS`, approval truth, and bounded apply evidence on `eeepc` must be verified against the host control-plane state tree unless and until convergence is explicitly completed

## Core Principles

### Workspace-only

- reads and writes stay inside the workspace root,
- no cross-repo mutation by default,
- no hidden writes outside allowlisted targets.

### No-root

- no elevated privileges,
- no global package installs,
- prefer local Python environments over system mutation.

### Python-first

- use a workspace-local Python environment first,
- prefer Python-native tooling for early experimentation,
- add broader language/runtime support only after probing proves it exists.

### Truthful capability probing

- capabilities must be probed, not assumed,
- responses should distinguish `available`, `missing`, `blocked`, `unverified`,
  or `degraded`,
- every claim should have evidence.

## Architecture Alignment

The workspace runtime lane extends the current autonomy foundation rather than
creating a second system.

It reuses:

- `ActionSpec` and `ActionRegistry`,
- the diagnostics-first `ActionExecutor`,
- governance records for request/diagnostic/decision/result,
- the operator-facing `/action ...` entry path.

## Lane Phases

### Phase 1 - Probe

Action:

- `workspace.runtime.probe`

Goal:

- determine what is actually available on the host and in the workspace.

### Phase 2 - Ensure local environment

Action:

- `workspace.venv.ensure`

Goal:

- create or verify a local Python environment without touching the system.

### Phase 3 - Run local Python

Action:

- `workspace.python.run`

Goal:

- run bounded Python code inside the workspace-local environment.

### Phase 4 - Install local dependencies

Action:

- `workspace.pip.install_local`

Goal:

- install project-local dependencies into the workspace-local environment only.

## First Four Actions

### `workspace.runtime.probe`

Purpose:

- truthfully inspect local runtime capabilities.

Should probe at minimum:

- `python`,
- `git`,
- `pytest`,
- `ruff`,
- `uv`,
- `node`, `npm`,
- `aparser`,
- autonomy and executor feature flags.

### `workspace.venv.ensure`

Purpose:

- create or verify `.venv` inside the workspace.

### `workspace.python.run`

Purpose:

- run bounded Python scripts or modules with timeouts and evidence.

### `workspace.pip.install_local`

Purpose:

- perform local-only dependency installation into the workspace environment.

Guardrails:

- only install from a local path inside the workspace,
- use the workspace-local `.venv` only,
- no package names, URL targets, or custom index flags,
- prefer `--no-deps` so this stays a narrow local-artifact lane,
- report the exact command, timeout, stdout/stderr, and install result truthfully.

## Non-goals

- no root,
- no global package installs,
- no generic execute-anything shell lane,
- no broad system-level mutation,
- no device or network expansion before truthful probe coverage exists.

## Success Criteria

The workspace runtime lane is successful when the bot can:

1. probe its local runtime truthfully,
2. create or verify a local workspace environment,
3. run bounded Python code inside it,
4. optionally install local dependencies into it,
5. produce evidence-backed results for each step.

## First Bounded Self-Development Flow

Initial flow:

- `workspace.experiment.tiny_runtime_check`

Purpose:

- prove the runtime lane can diagnose, prepare, write, run, and report one tiny
  local experiment without generic shell expansion.

Expected sequence:

1. `workspace.runtime.probe`
2. `workspace.venv.ensure`
3. write `state/experiments/<id>/tiny_runtime_check.py`
4. `workspace.python.run`
5. report `written`, `executed`, `verified` truthfully

Experiment contract v1:

- emit a `hypothesis` describing what the tiny runtime check is trying to prove,
- emit explicit `success_checks` for probe, environment, write, execute, and
  verify stages,
- emit a final `decision` such as `keep`, `discard`, or `needs_more_evidence`.

Operator/agent decision rule:

- if the request is about self-check, self-improvement readiness, or a first tiny
  local experiment, start with `workspace.experiment.tiny_runtime_check` before
  proposing broader mutation or generic coding work.
