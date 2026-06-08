# eeepc Agent Runtime Instructions

Last updated: 2026-06-08 UTC

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

## Operator health check

Use the compact cycle health command for read-only operator triage after the runtime containing it is deployed:

```bash
eeebot cycle-health \
  --runtime-state-root /var/lib/eeepc-agent/self-evolving-agent/state \
  --runtime-state-source host_control_plane
```

For automation or dashboard ingestion:

```bash
eeebot cycle-health \
  --runtime-state-root /var/lib/eeepc-agent/self-evolving-agent/state \
  --runtime-state-source host_control_plane \
  --json
```

It reports the latest cycle id, report path, subagent telemetry id/path, bridge service status, failed units count, promotion readiness, and the next recommended action.

## Operator expectations

The dashboard should be able to observe:
- current goal
- current task plan
- approval freshness
- latest PASS/BLOCK status
- blocker reason when blocked
- reward / credits surfaces
- subagent/task correlation

## LiteLLM credentials — single source of truth

**One file, one place to change:**

```
/etc/eeepc-agent/litellm.env
```

This file is injected into the systemd service via a drop-in:
`/etc/systemd/system/eeepc-self-evolving-agent.service.d/litellm-env.conf`

Neighbouring files that must NOT be edited directly for key rotation:

| File | Role |
|------|---------|
| `/etc/eeepc-agent/instances/self-evolving-agent.env` | Agent runtime config only (no LiteLLM keys) |
| `/home/opencode/.nanobot-eeepc/config.template.json` | Gateway wrapper — update if template drifts |
| `/etc/eeepc-agent/models.yaml` | Allowed model registry |

### Key rotation steps

```bash
sudo nano /etc/eeepc-agent/litellm.env
# → update LITELLM_API_KEY, LITELLM_BASE_URL, comment suffix/rotated fields

sudo systemctl restart eeepc-self-evolving-agent.service

# Smoke test
curl -s -o /dev/null -w "%{http_code}" \
  "${LITELLM_BASE_URL}/models" \
  -H "Authorization: Bearer ${LITELLM_API_KEY}"
# expect: 200

# Then: eeebot cycle-health ... to confirm PASS
```

## Safety rule

Do not execute vague or unconstrained changes.
If no concrete bounded action exists, emit a blocked-next-step instead of pretending progress.
