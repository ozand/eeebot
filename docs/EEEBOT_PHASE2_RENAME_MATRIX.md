# eeebot Phase 2 Internal Rename Migration Matrix

Generated from `docs/EEEBOT_INTERNAL_RENAME_INVENTORY.json`.

Goal: group remaining internal `nanobot` naming surfaces by domain and migration risk so future renames are staged safely.

## High-level counts by repo/category/risk

- nanobot | bridge | high | 3
- nanobot | docs | high | 4
- nanobot | docs | low | 25
- nanobot | other | high | 2
- nanobot | other | low | 4
- nanobot | python_package | high | 91
- nanobot | root | low | 6
- nanobot | runtime_state | high | 30
- nanobot | tests | medium | 53
- nanobot-ops-dashboard | dashboard_src | high | 21
- nanobot-ops-dashboard | docs | low | 18
- nanobot-ops-dashboard | other | low | 1
- nanobot-ops-dashboard | root | low | 2
- nanobot-ops-dashboard | runtime_control | high | 37
- nanobot-ops-dashboard | scripts | high | 21
- nanobot-ops-dashboard | systemd | high | 2
- nanobot-ops-dashboard | tests | medium | 11

## Phase A — Runtime/package compatibility surfaces (do last)

Recommended approach:
- keep current `nanobot` names as compatibility surfaces
- add aliases first
- rename only after proving dual-name support and rollback

Representative files:
- `nanobot/nanobot/cli/commands.py` [high] — import/package/runtime path compatibility
- `nanobot/nanobot/agent/loop.py` [high] — import/package/runtime path compatibility
- `nanobot/nanobot/agent/subagent.py` [high] — import/package/runtime path compatibility
- `nanobot/nanobot/cli/onboard_wizard.py` [high] — import/package/runtime path compatibility
- `nanobot/nanobot/channels/dingtalk.py` [high] — import/package/runtime path compatibility
- `nanobot/nanobot/channels/telegram.py` [high] — import/package/runtime path compatibility

## Phase B — Systemd/service names (do after dual-name runtime support)

Recommended approach:
- keep current `nanobot` names as compatibility surfaces
- add aliases first
- rename only after proving dual-name support and rollback

Representative files:
- `nanobot-ops-dashboard/systemd/nanobot-ops-dashboard-web.service` [high] — import/package/runtime path compatibility
- `nanobot-ops-dashboard/systemd/nanobot-ops-dashboard-collector.service` [high] — import/package/runtime path compatibility

## Phase C — Scripts/wrappers (safe only after service/runtime aliases exist)

Recommended approach:
- keep current `nanobot` names as compatibility surfaces
- add aliases first
- rename only after proving dual-name support and rollback

Representative files:
- `nanobot-ops-dashboard/scripts/install_user_units.sh` [high] — service/script compatibility
- `nanobot-ops-dashboard/scripts/analyze_project_autonomy.py` [high] — service/script compatibility
- `nanobot-ops-dashboard/scripts/run_collector.sh` [high] — service/script compatibility
- `nanobot-ops-dashboard/scripts/run_web.sh` [high] — service/script compatibility
- `nanobot-ops-dashboard/scripts/eeepc_reachability_watchdog.py` [high] — service/script compatibility
- `nanobot-ops-dashboard/scripts/enqueue_active_remediation.py` [high] — service/script compatibility

## Phase D — Dashboard code/public strings (mostly medium/low risk)

Recommended approach:
- rename incrementally where public-facing
- avoid touching compatibility-critical strings in one tranche

Representative files:
- `nanobot-ops-dashboard/src/nanobot_ops_dashboard/config.py` [high] — import/package/runtime path compatibility
- `nanobot-ops-dashboard/src/nanobot_ops_dashboard/app.py` [high] — import/package/runtime path compatibility
- `nanobot-ops-dashboard/src/nanobot_ops_dashboard/collector.py` [high] — import/package/runtime path compatibility
- `nanobot-ops-dashboard/src/nanobot_ops_dashboard/cli.py` [high] — import/package/runtime path compatibility
- `nanobot-ops-dashboard/src/nanobot_ops_dashboard/storage.py` [high] — import/package/runtime path compatibility
- `nanobot-ops-dashboard/src/nanobot_ops_dashboard/__main__.py` [high] — import/package/runtime path compatibility

## Phase E — Docs/historical references (low risk but noisy)

Recommended approach:
- rename incrementally where public-facing
- avoid touching compatibility-critical strings in one tranche

Representative files:
- `nanobot/docs/CHANNEL_PLUGIN_GUIDE.md` [low] — docs/public naming
- `nanobot-ops-dashboard/docs/SHOWING_THE_DASHBOARD.md` [low] — docs/public naming
- `nanobot/docs/plans/2026-04-15-eeepc-live-authority-convergence.md` [low] — docs/public naming
- `nanobot/docs/EEEPC_DEPLOY_VERIFY_ROLLBACK_RUNBOOK.md` [low] — docs/public naming
- `nanobot-ops-dashboard/docs/EEEPC_PRIVILEGED_LIVE_ACTIVATION_HANDOFF.md` [low] — docs/public naming
- `nanobot-ops-dashboard/docs/plans/2026-04-16-subagents-operator-ux-and-correlation-plan.md` [low] — docs/public naming

## Phase F — Runtime control artifacts (do not bulk-rename)

Recommended approach:
- do not bulk rename historical/runtime artifacts
- use explicit migration tooling if ever renamed
- preserve reader backward compatibility

Representative files:
- `nanobot-ops-dashboard/control/status_feed.jsonl` [high] — durable state/control artifact compatibility
- `nanobot-ops-dashboard/control/execution_assignment.json` [high] — durable state/control artifact compatibility
- `nanobot-ops-dashboard/control/execution_assignments/20260417T024647743230Z-stagnating_on_quality_blocker-goal-44e50921129bf475-var-lib-eeepc-agent-self-evolving-agent-state-reports-evolution-20260416T121151Z.json-no_concrete_change-planner_hardening.json` [high] — durable state/control artifact compatibility
- `nanobot-ops-dashboard/control/no_live_executor_incident.json` [high] — durable state/control artifact compatibility
- `nanobot-ops-dashboard/control/no_live_executor_incidents/20260417T163211689902Z-project-nanobot-eeepc-owner-loop-project-hermes-autonomy-self-fix.json` [high] — import/package/runtime path compatibility
- `nanobot-ops-dashboard/control/execution_queue.json` [high] — durable state/control artifact compatibility

## Phase G — Runtime state paths (do not rename without migration layer)

Recommended approach:
- do not bulk rename historical/runtime artifacts
- use explicit migration tooling if ever renamed
- preserve reader backward compatibility

Representative files:
- `nanobot/workspace/state/goals/history/cycle-cycle-e3673a8bbf02.json` [high] — durable state/control artifact compatibility
- `nanobot/workspace/state/outbox/latest.json` [high] — durable state/control artifact compatibility
- `nanobot/workspace/state/outbox/report.index.json` [high] — durable state/control artifact compatibility
- `nanobot/workspace/state/reports/evolution-20260421T171730Z-cycle-e3673a8bbf02.json` [high] — durable state/control artifact compatibility
- `nanobot/workspace/state/experiments/latest.json` [high] — durable state/control artifact compatibility
- `nanobot/workspace/state/goals/current.json` [high] — durable state/control artifact compatibility

## Safe next implementation slices

1. Add more public-facing `eeebot` wording in docs and user-visible help only.
2. Add optional `EEEBOT_*` environment variable aliases while keeping `NANOBOT_*` as canonical fallback.
3. Add service-name aliases only after confirming wrapper compatibility.
4. Do not rename `workspace/state` and control artifact paths until explicit migration tooling exists.

## Recommended non-goals for now

- no package directory rename from `nanobot/` to `eeebot/` yet
- no bulk doc/history search-replace across archived proofs
- no runtime state root rename on eeepc
- no systemd unit rename without alias shims
