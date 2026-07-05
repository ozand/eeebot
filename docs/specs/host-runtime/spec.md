# Host Runtime — spec

_Status: current. Last updated: 2026-06-29._

## Purpose

The host runtime is the live execution environment for the self-evolving runtime
on the constrained `eeepc` host (Intel Celeron M, ~2GB RAM, i386). It defines the
single durable state authority, the operator-supervised approval gate that guards
bounded apply, the hybrid model topology (lightweight local coordinator,
GPU-backed local executor), the deploy/verify/rollback discipline, the GitHub
sync separation, workspace artifact triage, the from-scratch bootstrap path, and
the portable base configuration. It exists to let the runtime improve itself on
weak hardware without unbounded or non-recoverable behavior.

> Explanatory walkthroughs and detailed runbooks live in the legacy source docs
> (see References). This spec is the contract; those docs are the explanation.

## Requirements

### State authority
- R1. The live host control-plane authority root SHALL be
  `/var/lib/eeepc-agent/self-evolving-agent/state`. All live-host claims (status,
  PASS/BLOCK, approval, reports, artifacts) SHALL come from this root.
- R2. Authority surfaces SHALL be queried explicitly; tools that read runtime
  truth (`nanobot status`, `eeebot cycle-health`) SHALL accept
  `--runtime-state-source` (`workspace_state` | `host_control_plane`) and
  `--runtime-state-root`, and SHALL report which source/root they read from.
- R3. Repo-side `workspace_state` SHALL NOT be conflated with live
  `host_control_plane` truth; an operator SHALL NOT claim live proof unless the
  status output and the underlying report come from the same chosen authority root.

### Approval gate (apply.ok)
- R4. Bounded apply and promotion-execute SHALL require a valid approval file at
  `<state-root>/approvals/apply.ok` containing JSON with `expires_at_epoch`.
- R5. A missing, unreadable, malformed, or expired approval SHALL fail closed; the
  runtime SHALL NOT infer approval from dashboard health or older PASS reports.
- R6. The approval gate SHALL be operator-supervised and short-lived (recommended
  TTL 3600s); it SHALL NOT be auto-renewed or made a permanent standing grant.

### Model topology
- R7. The coordinator SHALL run the lightweight remote model `cl/gemini-3-flash`
  (via the LiteLLM proxy over Tailscale) for bookkeeping/evaluation/synthesis
  only, with `ALLOW_CODE_EDITS=false`; it SHALL NOT make code edits.
- R8. Code-editing subagents SHALL run the mandatory local executor model
  `un/qwen3.6-27b-mtp` (referenced in config via the logical alias
  `gpt-5.3-codex`, routed by LiteLLM) on GPU-capable hardware; local inference on
  the eeepc host itself is impossible and SHALL NOT be attempted.
- R9. All LiteLLM credentials/routing for the host runtime SHALL live only in
  `/etc/eeepc-agent/litellm.env` (injected via the service drop-in). They SHALL
  NOT be set in `instances/*.env`, the gateway config template, or `models.yaml`.
  Models SHALL carry a `cl/`, `an/`, or `un/` gateway prefix.

### Loop driver topology
- R23. The continuous self-evolving loop SHALL be driven by exactly two systemd
  timers: `eeepc-self-evolving-agent-health.timer` (runs one coordinator cycle)
  and `eeepc-self-evolving-subagent-bridge.timer` (runs the code-editing
  executor). These are the authoritative drivers of ongoing autonomy.
- R24. `eeepc-self-evolving-agent.service` is a single-shot runner used only as a
  post-deploy kick (it runs one cycle on restart, then deactivates). It SHALL
  remain `disabled` with no timer and SHALL NOT be enabled as a way to "turn on"
  autonomy — doing so duplicates the health-timer driver and creates two
  competing coordinators. Autonomy is already on whenever the two R23 timers are
  enabled; productivity depends on the loop feeding the executor fresh tasks, not
  on this unit.

### Capability policy
- R10. Every host-facing capability SHALL be classified as one of `available`,
  `blocked_by_policy`, `unavailable`, `unverified`, or `degraded`, and free-form
  operator answers SHALL use the same classification logic.
- R11. Capabilities SHALL be gated by class: read-only introspection (A) is
  generally allowed; bounded workspace mutation (B) is allowed only in
  allowlisted surfaces; device/sensor (D), system-level (E), and canonical source
  promotion (F) SHALL be review-gated and proposal-first.
- R12. The runtime SHALL NOT silently change OS policy, security posture,
  credentials, canonical repo ownership, high-risk services, or files outside
  explicit mutation bounds. Meaningful self-improvement SHALL leave evidence
  (why, what changed, expected benefit, observed result).
- R13. If a capability is ambiguous, expensive, privacy-sensitive, or hard to
  recover from, the runtime SHALL default to `unverified`/`blocked_by_policy` and
  wait for review rather than expanding scope.

### Deploy / verify / rollback
- R14. Releases SHALL be unpacked side-by-side under
  `/opt/eeepc-agent/runtimes/self-evolving-agent/releases/<release-id>` and
  verified via `PYTHONPATH` against live host truth BEFORE the active `current`
  symlink is switched. Proof SHALL precede activation. The `current` symlink is
  the SINGLE runtime code authority — the legacy separate
  `runtime/pinned/current` path is retired (#601).
- R15. Activation SHALL switch `current` only when activation is actually
  required; rollback SHALL restore `current` to the last known-good release and
  restart the affected service FIRST, then debug. A failed release directory
  SHALL be preserved for forensics, not deleted.
- R16. When the operator context lacks `sudo`/`opencode`/protected-index access,
  it SHALL record exact fail-closed blockers and SHALL NOT claim host-emitter
  parity or authoritative live proof.

### GitHub sync
- R17. Host-evolution outputs (evidence exports, workspace snapshots, promotion
  candidates, autonomous project repos) SHALL push only to the separate namespace
  `mrsmileystoke92`; canonical product source SHALL live only under `ozand`. No
  canonical repo SHALL live under `mrsmileystoke92` and vice versa.
- R18. Sync SHALL never push secrets, env files, or the full volatile state tree,
  and SHALL never push host-born changes directly into canonical repos; promotion
  into canonical repos SHALL remain reviewable.

### Workspace artifact triage
- R19. `workspace/` SHALL be treated as runtime state, not canonical source; it
  SHALL remain `.gitignore`-ignored, and cycle reports, promotions, outbox files,
  approvals, caches, and local releases SHALL NOT be committed. Durable lessons
  SHALL be moved into tracked `docs/` rather than committing raw artifacts.
- R20. Host-created workspace artifacts SHALL NOT be treated as a second backlog,
  task registry, or planning/release system; only artifacts backing one bounded
  experiment, runtime check, or evidence-backed report SHALL be kept.

### Bootstrap and base configuration
- R21. From a fresh/reset/untrusted host, bring-up SHALL proceed in stages
  (host qualification → canonical source + baseline → minimal trusted runtime →
  introspection → bounded self-change → evidence/sync/promotion → broader
  autonomy), verifying each stage before unlocking the next.
- R22. The portable base configuration SHALL keep canonical source, runtime state,
  and exported evidence separate; SHALL document runtime paths/state layout and
  resource ceilings; SHALL keep bootstrap replayable and rollback possible at
  every stage; and SHALL NOT embed secrets or one-machine-only assumptions.

## Scenarios

### Scenario: blocked cycle unblocked by a supervised apply window
- Given a live cycle reporting `BLOCK` with `capability_gate.approval.ok = false`
- When the operator writes a short-lived `approvals/apply.ok` with a future
  `expires_at_epoch` and triggers `eeepc-self-evolving-agent-health.service`
- Then the next report shows `approval.ok = true`, `bounded_apply.allowed = true`,
  `process_reflection.status = "PASS"`, and a concrete `follow_through` artifact.

### Scenario: live truth is read from the authority root
- Given a fresh cycle report under the host control-plane root
- When `nanobot status --runtime-state-source host_control_plane
  --runtime-state-root /var/lib/eeepc-agent/self-evolving-agent/state` runs
- Then it reports source `host_control_plane`, that exact root, and surfaces the
  active goal, approval state, and report path from the same tree.

### Scenario: verify before activate
- Given a new release unpacked side-by-side under `releases/<release-id>`
- When it is verified read-only via `PYTHONPATH` against live host truth and the
  output is coherent
- Then `current` may be switched; if startup later fails, `current` is restored to
  the last known-good release and the service restarted before any debugging.

### Scenario: host-evolution output stays out of canonical source
- Given a cycle produces an evidence export and a workspace snapshot
- When the sync adapter pushes them
- Then they go to `mrsmileystoke92` repos (evidence / workspace / projects-index),
  contain no secrets or full state tree, and no canonical `ozand` repo is touched.

## References

- Legacy source docs (consolidated here; moving to `.legacy/docs/...`):
  `EEEPC_AGENT_RUNTIME_INSTRUCTIONS.md`, `EEEPC_RUNTIME_STATE_AUTHORITY_USAGE.md`,
  `EEEPC_DEPLOY_VERIFY_ROLLBACK_RUNBOOK.md`, `EEEPC_APPLY_OK_OPERATOR_RUNBOOK.md`,
  `HOST_CAPABILITY_POLICY.md`, `HOST_GITHUB_SYNC_ARCHITECTURE.md`,
  `HOST_WORKSPACE_ARTIFACT_TRIAGE.md`, `SAFE_BOOTSTRAP_FROM_SCRATCH.md`,
  `BASE_CONFIGURATION_PROFILE.md`.
- Related specs: `docs/specs/self-evolving-runtime/spec.md`,
  `docs/specs/subagent-bridge/spec.md`, `docs/specs/promotion-and-release/spec.md`,
  `docs/specs/model-routing/spec.md`.
- Code / paths: `nanobot/runtime/coordinator.py`, `nanobot/runtime/state*.py`,
  `nanobot/runtime/health.py`, `nanobot/cli/commands.py` (`status`,
  `cycle-health`), `nanobot/runtime/bridge.py`,
  `app/main.py`. Config: `/etc/eeepc-agent/litellm.env`,
  `/etc/eeepc-agent/instances/*.env`, `/etc/eeepc-agent/models.yaml`. State root:
  `/var/lib/eeepc-agent/self-evolving-agent/state`.
