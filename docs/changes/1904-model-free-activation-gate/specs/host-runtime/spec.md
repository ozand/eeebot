# Host runtime specification delta — #1904

## Deploy activation

Before switching `current`, a release activation gate SHALL run a bounded, deterministic self-check against the extracted candidate release as the bridge service user, using the bridge service environment, sandbox, resource limits, and state permissions. It validates bridge imports and strict configuration, initializes a local tool registry, completes one model-free no-op tool step, and writes verified state evidence. It SHALL NOT call a model or start a real bridge cycle. A self-check failure SHALL use the existing rollback path. After switching `current`, the first real cycle is a separate behavioural observation: `TimeoutStartSec` records an `inconclusive`/not-measured outcome and does not roll back; any other nonzero exit rolls back. Neither bridge timeout nor model budgets are changed.
