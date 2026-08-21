# eeepc Apply Gate Operator Runbook

Last updated: 2026-04-15 UTC

## Purpose

Use this runbook to open or manage the bounded-apply approval window for the
live `eeepc` self-evolving control-plane, verify that the gate is valid, and
confirm that the next host cycle produces real evidence.

This runbook does not introduce auto-renewal.

## Approval policy (decided 2026-07-05, #624)

The deployed gate is a manually seeded **standing approval expiring 2036-06-04**
(`expires_at_epoch: 2096153798`), written by hand per Step 1 of this runbook.
**This is deliberate operator policy, not an accident:** the runtime is meant to
operate autonomously without per-window operator confirmation. Safety does not
rest on the approval window's length — bounded autonomy is enforced by the
cycle stop-guards (R11–R13), promotion gating (human review before canonical
promotion), and budget limits, which apply on every cycle regardless of the
gate.

Consequences of this policy:
- No refresher process exists or is wanted. The old
  `eeepc-self-evolving-approval-keeper` unit (a 2h auto-renew window) was never
  enabled and was removed as dead code (#614).
- **To revoke autonomy**, delete or expire the gate file
  (`sudo rm .../state/approvals/apply.ok` or write a past
  `expires_at_epoch`) — the next cycle flips to `BLOCK`. This is the single
  emergency stop for bounded apply.
- The short-lived-window procedure below remains valid for hosts or periods
  where the operator *wants* per-window supervision; it is simply not the
  current mode of the eeepc host.

## Canonical Live Gate Surface

Host state root:
- `/var/lib/eeepc-agent/self-evolving-agent/state`

Approval gate file:
- `/var/lib/eeepc-agent/self-evolving-agent/state/approvals/apply.ok`

Required JSON field:
- `expires_at_epoch`

## When To Use

Use this runbook when:
- the live self-evolving cycle is stuck in `BLOCK`
- reports show approval is missing or expired
- bounded apply is denied even though the operator intends to allow one supervised apply window

Creating a long-lived standing approval is allowed **only** as the deliberate
policy described in "Approval policy" above — not as a shortcut when a
short supervised window was intended.

## Expected Before-State

A blocked report typically shows one or more of:
- `capability_gate.approval.ok = false`
- `capability_gate.approval.reason = "missing"` or `"expired"`
- `capability_gate.capabilities.bounded_apply.allowed = false`
- `capability_gate.capabilities.promotion_execute.allowed = false`
- reflection or summary text mentioning `approval_required` or `promotion_execute_denied`

## Step 1. Write a Short-Lived Approval Gate

Recommended TTL:
- 3600 seconds (60 minutes)

Reference command:

```bash
python3 -c "import json,time,pathlib; p=pathlib.Path('/var/lib/eeepc-agent/self-evolving-agent/state/approvals/apply.ok'); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(json.dumps({'expires_at_epoch': int(time.time())+3600}, indent=2))"
```

If root privileges are required on the host, run the same command under `sudo`.

## Step 2. Verify the Gate File Exists and Looks Valid

```bash
cat /var/lib/eeepc-agent/self-evolving-agent/state/approvals/apply.ok
```

Expected shape:

```json
{
  "expires_at_epoch": 1776297593
}
```

The exact epoch value will differ.

## Step 3. Trigger the Self-Evolving Subagent Bridge

_(Note: the former coordinator/health service (`eeepc-self-evolving-agent-health.service`)
was decommissioned in #900/#910 — it never made an LLM call in production.
The bridge below is the live driver of the self-evolving loop.)_

```bash
systemctl start eeepc-self-evolving-subagent-bridge.service
```

If the service runs under a privileged context, use `sudo systemctl start ...`.

Timer surface:
- `eeepc-self-evolving-subagent-bridge.timer`

Service surface:
- `eeepc-self-evolving-subagent-bridge.service`

## Step 4. Check Journal Output

```bash
journalctl -u eeepc-self-evolving-subagent-bridge.service -n 50 --no-pager
```

Good signals:
- `Self-evolving cycle finished with PASS`
- a fresh report path under `state/reports/`

Blocked signals:
- `Self-evolving cycle finished with BLOCK`
- messages still indicating `approval_required`

## Step 5. Read the Fresh Report

Example verified PASS report from the live host:
- `/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-20260415T230020Z.json`

Read the newest report and confirm:
- `capability_gate.approval.ok = true`
- `capability_gate.approval.reason = "valid"`
- `capability_gate.capabilities.bounded_apply.allowed = true`
- `capability_gate.capabilities.promotion_execute.allowed = true`
- `process_reflection.status = "PASS"`
- `follow_through.status = "artifact"` or another concrete evidence-bearing result

## Expected PASS Evidence

A valid supervised apply window should result in evidence like:
- a fresh report under `state/reports/`
- a concrete artifact path in `follow_through.artifact_paths`
- a backup or rollback artifact under `state/backups/`
- updated goal/result state consistent with that same cycle

Verified real example from `eeepc`:
- report: `/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-20260415T230020Z.json`
- artifact path: `prompts/diagnostics.md`
- backup path under `state/backups/`
- cycle result: `PASS`

## Safety Rules

- Do not auto-renew `apply.ok`
- The TTL is a policy choice: short for supervised windows, long-lived on eeepc
  by deliberate decision (#624, "Approval policy" above) — either way it must be
  intentional and documented
- Bounded apply is constrained by stop-guards, promotion gating, and budgets on
  every cycle — the gate governs *whether* the loop may apply, not *how much*
- Missing, unreadable, malformed, or expired approval must fail closed; never infer approval from dashboard health or from the existence of older PASS reports
- A non-sudo readiness check may document that the gate is protected, but it must not claim the gate is valid unless the current `apply.ok` payload was read and its `expires_at_epoch` is in the future
- If a cycle produces unexpected changes, remove or let the gate expire before rerunning

## Remove Or Let Expire

To close the window immediately:

```bash
rm -f /var/lib/eeepc-agent/self-evolving-agent/state/approvals/apply.ok
```

Otherwise, let the TTL expire naturally.

## Troubleshooting

### Gate exists but cycle still blocks

Check for:
- malformed JSON
- expired epoch timestamp
- wrong path
- host code reading a different state root than expected

### PASS does not appear even with a valid gate

Check:
- latest report contents
- journal output for a different blocker
- whether the cycle is selecting a stale or unrelated goal
- whether sidecar/tool-profile constraints are blocking something else downstream

### Gateway is healthy but self-evolving truth still looks wrong

Remember:
- gateway health is not the same as host control-plane convergence
- current live self-evolving authority on `eeepc` is `/var/lib/eeepc-agent/self-evolving-agent/state`

## Bottom Line

The `apply.ok` file is the live bounded-apply approval gate for the `eeepc` host control-plane. When the file is valid, the next self-evolving cycle can move from `BLOCK` to `PASS` and emit durable host evidence.