# Change: Model-free bridge activation gate

- **change-id:** 1904-model-free-activation-gate
- **issue:** #1904 — https://github.com/ozand/eeebot/issues/1904
- **capability:** `docs/specs/host-runtime`
- **role / workstream:** deployment activation

## Problem

The deploy activation block restarts the oneshot bridge, which launches a real model-backed cycle and waits up to `TimeoutStartSec`. Queue delay is therefore mistaken for release failure.

## Intended change

Before switching `current`, run a bounded bridge self-check against the extracted candidate release as `eeepc-agent`, with the bridge unit's EnvironmentFiles, resource limits, sandbox, and state write permission. A dedicated unit is configured to use the candidate release as its WorkingDirectory and import path while retaining the bridge unit environment. It strictly validates bridge imports/config, performs one deterministic local no-op tool-registry step, and records a self-check event plus required state-write proof. It never constructs/calls a model provider. A nonzero self-check fails activation and triggers rollback. After the symlink flip, start the first real bridge cycle: timeout records `inconclusive`/not-measured and retains the release; non-timeout failure rolls back.

## Acceptance

- [ ] Broken bridge import fails the self-check and deploy activation rolls back.
- [ ] Invalid config fails strict self-check loading and activation rolls back.
- [ ] A valid release completes the self-check without invoking a model; a test provider that raises on use proves this.
- [ ] The check runs against the extracted release, with `eeepc-agent` identity and bridge EnvironmentFiles/sandbox; it creates bounded state evidence.
- [ ] A post-flip real-cycle timeout is recorded as `inconclusive` and retains the release; other failures roll back.
- [ ] `TimeoutStartSec`, model budgets, and later health-gate behavior remain unchanged.

## Out of scope

No second attempt and no inference from `llm_calls` durations or completions.
