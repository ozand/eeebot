# eeepc Apply Gate Operator Runbook

Last updated: 2026-04-15 UTC

## Purpose

Use this runbook to open a short-lived bounded-apply approval window for the live `eeepc` self-evolving control-plane, verify that the gate is valid, and confirm that the next host cycle produces real evidence.

This runbook does not introduce auto-renewal.

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

Do not use this runbook to create a permanent standing approval.

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

## Step 3. Trigger the Host Self-Evolving Health Service

```bash
systemctl start eeepc-self-evolving-agent-health.service
```

If the service runs under a privileged context, use `sudo systemctl start ...`.

Timer surface:
- `eeepc-self-evolving-agent-health.timer`

Service surface:
- `eeepc-self-evolving-agent-health.service`

## Step 4. Check Journal Output

```bash
journalctl -u eeepc-self-evolving-agent-health.service -n 50 --no-pager
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
- Keep TTL short and intentional
- Treat this as operator-supervised bounded apply, not a permanent capability grant
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