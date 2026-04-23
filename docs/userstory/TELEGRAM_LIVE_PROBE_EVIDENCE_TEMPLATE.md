# Telegram Live Probe Evidence Template

Status: canonical evidence template for the final real Telegram proof step.

Use this artifact when running the final allowed-account Telegram probe for:
- `#7` real Telegram model-selection proof
- `#3` remaining live Telegram proof gap

## Probe metadata

- Probe date (UTC):
- Operator/account used:
- Chat/thread identifier:
- Telegram bot identity observed:
- Host/runtime source being validated:

## Required command sequence

1. `PING <UTC>`
2. `/cap_status`
3. `/workspace experiment tiny-runtime-check`
4. `/sub_run --profile research_only --budget micro ping-telegram-live-<UTC>`

## Evidence capture checklist

For each step, record:
- sent timestamp (UTC)
- exact outbound command text
- exact inbound reply text
- whether reply arrived in the same Telegram chat/thread
- whether reply content matches the expected runtime truth

### Step 1: PING
- Sent at:
- Reply received at:
- Reply text:
- Classification:
  - success
  - ingress failure
  - outbound delivery failure

### Step 2: /cap_status
Expected proof:
- command routing works on the live Telegram path
- runtime/model-selection truth is visible
- output agrees with direct host truth sources

- Sent at:
- Reply received at:
- Reply text:
- Contains runtime/model truth? (yes/no)
- Matches direct host truth? (yes/no)
- Classification:
  - success
  - route mismatch
  - parity mismatch

### Step 3: /workspace experiment tiny-runtime-check
Expected proof:
- Telegram command registration and forwarding for `/workspace`
- same command-forwarding lane as the live runtime path

- Sent at:
- Reply received at:
- Reply text:
- Classification:
  - success
  - command registration mismatch
  - forwarding/runtime mismatch

### Step 4: /sub_run --profile research_only --budget micro ...
Expected proof:
- Telegram command registration and forwarding for `/sub_run`
- bounded delegated execution path can be invoked from Telegram

- Sent at:
- Reply received at:
- Reply text:
- Classification:
  - success
  - command registration mismatch
  - forwarding/runtime mismatch
  - bounded execution mismatch

## Final outcome summary

- PING status:
- /cap_status status:
- /workspace status:
- /sub_run status:
- Final classification:
  - live proof complete
  - ingress blocked
  - route parity gap
  - outbound delivery gap
  - runtime path mismatch

## Closure rule

The remaining Telegram issues can be closed only when this template is filled with a real transcript from an allowlisted account and the captured replies match the expected runtime truth.
