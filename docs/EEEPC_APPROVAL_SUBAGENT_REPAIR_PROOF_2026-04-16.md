# eeepc Approval Persistence and Subagent Bridge Repair Proof

Last updated: 2026-04-16 UTC

## Goal

Repair the live eeepc self-evolving system so that:
- approval does not disappear between cycles
- the live system can produce real subagent reports instead of always showing `subagent_rollup.enabled=false`
- the result is repeatable, not a one-off manual pass

## Root Causes Confirmed

### Approval root cause

The live host runtime only checks:
- `STATE_DIR/approvals/apply.ok`
- field: `expires_at_epoch`

There was no renewal or persistence mechanism, so once the manually created file expired or was removed, live cycles returned:
- `approval.ok = false`
- `reason = missing`
- overall result `BLOCK`

### Subagent root cause

The live self-evolving runtime:
- derives `subagent_policy`
- reads subagent reports from `<target_workspace>/.nanobot/subagents`

But it does not spawn subagents itself.

The actual subagent runner existed separately in the Nanobot repo (`SubagentManager`), so a bridge/launcher was required.

## Implemented Host-Side Repair

### 1. Approval keeper

Installed on host:
- script: `/usr/local/libexec/eeepc-self-evolving-approval-keeper.py`
- env: `/etc/eeepc-agent/instances/self-evolving-approval-keeper.env`
- service: `/etc/systemd/system/eeepc-self-evolving-approval-keeper.service`
- timer: `/etc/systemd/system/eeepc-self-evolving-approval-keeper.timer`

Behavior:
- refreshes `apply.ok`
- writes bounded TTL-based approval with metadata

### 2. Subagent bridge

Installed on host:
- script: `/usr/local/libexec/eeepc-self-evolving-subagent-bridge.py`
- env: `/etc/eeepc-agent/instances/self-evolving-subagent-bridge.env`
- service: `/etc/systemd/system/eeepc-self-evolving-subagent-bridge.service`
- timer: `/etc/systemd/system/eeepc-self-evolving-subagent-bridge.timer`

Behavior:
- reads the current live active goal/report from the host authority root
- reuses the real Nanobot `SubagentManager`
- writes durable reports into:
  - `/home/opencode/servers_team/repo_research/nanobot/.nanobot/subagents`
- uses explicit valid bridge model:
  - `gpt-5.4-mini`
- uses extended budget to avoid premature tool-call exhaustion

## Enabled Timers

Enabled and started:
- `eeepc-self-evolving-approval-keeper.timer`
- `eeepc-self-evolving-subagent-bridge.timer`
- existing live cycle timer remained:
  - `eeepc-self-evolving-agent-health.timer`

## First Verified Working Result

After running:
1. approval keeper
2. subagent bridge
3. health service

Observed live result:
- `PASS`
- report: `/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-20260416T080736Z.json`

Observed live proof fields:
- `approval.ok = true`
- `approval.reason = valid`
- `follow_through.status = artifact`
- `artifact_paths = ["prompts/diagnostics.md"]`
- `subagent_rollup.enabled = true`
- `subagent_rollup.count_total = 3`
- `subagent_rollup.count_done = 2`
- `subagent_rollup.count_blocked = 1`
- `subagent_rollup.latest.status = done`

## Verified Subagent Result

Latest durable subagent report showed:
- `status = done`
- `safety_mode = workspace_write`
- `approved_by_gate = true`
- `budget.class = extended`
- a concrete summary indicating a small prompt clarification in `prompts/diagnostics.md`

## Repeatability Proof

The sequence was run again and produced a second successful live result:
- report: `/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-20260416T080840Z.json`
- result: `PASS`
- `approval.ok = true`
- `artifact_paths = ["prompts/diagnostics.md"]`
- `subagent_rollup.enabled = true`
- `latest_subagent_status = done`

This confirms the repair is repeatable, not dependent on a one-time manual gate file.

## Operational Conclusion

The live eeepc self-evolving system now has:
- persistent approval refresh
- real durable subagent launch reports
- live reports that no longer block on `approval.missing`
- live reports that show enabled subagent rollup with non-zero counts
- repeatable PASS cycles under the repaired host setup
