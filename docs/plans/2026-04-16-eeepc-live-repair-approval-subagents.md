# eeepc Live Repair Plan: Approval Persistence and Subagent Bridge

Last updated: 2026-04-16 UTC

## Goal

Move the live eeepc self-evolving system from:
- manual expiring approval windows
- no real subagent launch path

to:
- repeatable approval persistence for bounded apply
- repeatable durable subagent report generation
- live reports that no longer block on missing approval
- live reports that show subagent rollup enabled with real report counts

## Current Verified Root Causes

### 1. Approval closes because nothing renews it

Verified in host runtime code:
- approval is only the file `STATE_DIR/approvals/apply.ok`
- validity depends on `expires_at_epoch`
- if missing or expired, live result becomes `approval_required` / `promotion_execute_denied`

Current live reality:
- the latest live report is `BLOCK`
- `approval.ok = false`
- `reason = missing`
- `apply.ok` is absent on disk

Conclusion:
- there is no persistent approval/renewal mechanism in the live system today
- prior PASS proofs depended on a manually created short-lived gate

### 2. Subagents stay disabled because the self-evolving runtime does not spawn them

Verified in host runtime code:
- the self-evolving host runtime derives `subagent_policy`
- it reads subagent rollups from `<target_workspace>/.nanobot/subagents`
- but no subagent spawn path exists inside the self-evolving runtime itself

Verified separately in the Nanobot repo on host:
- the interactive Nanobot agent has a real `SubagentManager`
- it writes durable reports with schema `nanobot.subagent.result.v1`
- it supports bounded profiles and budgets
- but that path is part of the Nanobot agent loop, not the self-evolving host control-plane

Conclusion:
- the live self-evolving runtime only consumes subagent reports
- a bridge/launcher is required to produce them automatically

## Repair Strategy

### Slice 0 — privileged readiness preflight

Before installing or activating any host-side change, run a non-mutating readiness check and record what is actually proven.

Ready for privileged rollout requires all of these:
- `sudo` or equivalent privileged execution is available for `/var/lib/eeepc-agent/self-evolving-agent/state`
- the opencode Nanobot venv can be executed through the intended service account or through `sudo env PYTHONPATH=... /home/opencode/.venvs/nanobot/bin/nanobot ...`
- `outbox/report.index.json`, `goals/registry.json`, and the newest report can be read from the same authority root
- the `/opt/eeepc-agent/runtimes/self-evolving-agent/current/.venv/bin/python` path resolves to an executable interpreter without a `.venv -> current/.venv` symlink loop
- side-by-side `nanobot status --runtime-state-source host_control_plane --runtime-state-root /var/lib/eeepc-agent/self-evolving-agent/state` reports concrete status, goal, approval, report, and outbox fields, not `unknown`

If any of those are false, the slice remains a readiness/proof-preservation slice only. Do not claim HADI/follow-through host-emitter parity from readable reports alone; create or keep a privileged follow-up issue with exact blockers.

### Slice 1 — approval keeper

Add a host service/timer that maintains a valid `apply.ok` window intentionally and repeatably.

Properties:
- explicit env/config-controlled enable flag
- bounded TTL
- writes only the canonical approval file expected by the live runtime
- runs as the correct host account for `STATE_DIR`

Expected outcome:
- repeated live cycles no longer fail with `approval.ok=false reason=missing`

### Slice 2 — subagent bridge

Add a host service/timer that:
- reads the current active goal from live state
- derives the preferred subagent profile/budget from existing goal context
- reuses the real Nanobot `SubagentManager`
- writes durable reports into `<target_workspace>/.nanobot/subagents`

Expected outcome:
- the next live self-evolving cycle sees `subagent_rollup.enabled=true`
- live reports show non-zero subagent counts

### Slice 3 — repeatable proof

After both slices are installed:
1. run approval keeper once
2. run subagent bridge once
3. run self-evolving health service
4. confirm:
   - approval valid
   - subagent report files exist
   - live report references enabled subagent rollup
   - live result is no longer blocked by missing approval

## Target Done State

The repair is considered complete when all are true:
- approval gate is maintained automatically by a documented host service/timer
- live cycles no longer block on `approval.missing`
- subagent reports are generated automatically by a documented host service/timer
- live report shows `subagent_rollup.enabled=true`
- the end-to-end result is repeatable by rerunning the services and observing the same behavior
