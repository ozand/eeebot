# Live Telegram Probe Protocol

Last updated: 2026-03-30 UTC

## Purpose

This protocol closes the remaining gap between simulator-backed verification and
the real Telegram operator path.

Use the smallest possible probe sequence and classify failures as:

- ingress,
- route,
- outbound delivery.

## Minimal Probe Sequence

Send these messages from the real allowed Telegram account to the live bot,
preferably in one short session with a visible UTC timestamp.

1. `PING <UTC>`
2. `/cap_status`
3. `/workspace experiment tiny-runtime-check`
4. `/sub_run --profile research_only --budget micro ping-telegram-live-<UTC>`

## Success Criteria

### `PING <UTC>`

- proves basic ingress and outbound reply.

### `/cap_status`

- proves command routing into the live loop,
- `/action capability.truth-check` on direct host CLI should already agree with
  `/cap_status`; if Telegram disagrees, treat the remaining issue as channel-path
  parity rather than snapshot generation,
- should show `autonomy: enabled=True`, `dry_run_default=False`, and the
  expected `workspace.*` enabled actions.

### `/workspace ...`

- the Telegram command menu/help must include `/workspace` and route it through
  the same command-forwarding path as `/cap_status`,
- if `/cap_status` works but `/workspace ...` fails on Telegram, classify the
  gap as Telegram command registration/route parity first.

### `/workspace experiment tiny-runtime-check`

- proves the operator-friendly runtime lane works on the live Telegram path,
- success means the reply includes:
  - `action_id: workspace.experiment.tiny_runtime_check`
  - `written: True`
  - `executed: True`
  - `verified: True`

### `/sub_run ...`

- proves the existing bounded subagent operator path still works,
- success can be either:
  - a started bounded subagent/task id,
  - or an explicit policy/gate response from the live runtime.

## Failure Classification

- no reply to `PING <UTC>` -> likely `ingress` or outbound delivery failure
- `PING` works but `/cap_status` fails -> likely command `route` mismatch
- `/cap_status` works but `/workspace ...` fails -> likely runtime-lane route or
  config mismatch
- reply exists but never reaches Telegram chat -> likely outbound delivery issue

## Recording

Record at minimum:

- UTC timestamp,
- exact messages sent,
- exact bot replies,
- classification,
- whether follow-up fix is needed.

Before closing the remaining live Telegram proof issues, validate the filled
markdown evidence with:

```bash
python3 scripts/validate_telegram_live_proof.py /path/to/filled-telegram-live-proof.md
```

After validation, collect a redacted read-only summary with:

```bash
python3 scripts/collect_telegram_live_proof.py /path/to/filled-telegram-live-proof.md
```

The validator is intentionally conservative: it passes only for a filled real
allowlisted Telegram transcript and rejects simulated/local evidence. The
collector is also read-only and emits redacted JSON only.
