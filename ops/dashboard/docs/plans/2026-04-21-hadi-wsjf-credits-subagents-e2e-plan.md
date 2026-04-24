# HADI + WSJF + Credits + Subagent Correlation E2E Plan

Goal:
Close the remaining operator gaps for the Nanobot eeepc self-improvement loop by making the following explicit and durable end-to-end:
1. HADI backlog model for hypotheses
2. explicit WSJF surface
3. credits ledger
4. durable subagent/task correlation visibility

Scope:
- producer side in `/home/ozand/herkoot/Projects/nanobot`
- dashboard side in `/home/ozand/herkoot/Projects/nanobot-ops-dashboard`
- tests in both repos
- proof/runbook docs in dashboard repo

Phase 1 — Producer-side model changes
1. Upgrade `state/hypotheses/backlog.json` snapshot to HADI:
   - hypothesis
   - action
   - data
   - insights
   - execution_spec
   - selected state
   - explicit wsjf block
2. Add credits ledger under `state/credits/`:
   - `latest.json`
   - `history.jsonl`
   - current balance, last delta, source cycle, reason
3. Enrich durable subagent telemetry with task-plan context when available:
   - current_task_id
   - selected_task_title
   - reward_signal
   - feedback decision mode/source

Phase 2 — Producer-side state reader changes
4. Extend `nanobot.runtime.state` to read:
   - HADI hypothesis backlog fields
   - explicit WSJF values
   - credits ledger summary
   - richer subagent latest summary/correlation
5. Add/extend Nanobot tests proving:
   - HADI backlog written
   - WSJF fields written
   - credits ledger written and loaded
   - subagent correlation payload contains cycle/goal/task context

Phase 3 — Dashboard collector + UI changes
6. Extend dashboard collector to normalize:
   - HADI hypothesis entries
   - WSJF text and ranking fields
   - credits ledger summary/history
   - subagent task correlation fields
7. Extend dashboard pages/APIs:
   - `/hypotheses` and `/api/hypotheses`
   - overview cards
   - `/experiments` or dedicated credits section
   - `/subagents`
8. Add dashboard tests proving live/operator surfaces render these fields coherently.

Phase 4 — E2E proof and docs
9. Write proof note describing:
   - HADI backlog lifecycle
   - WSJF surface
   - credits ledger
   - subagent/task correlation visibility
10. Run full tests in both repos and manually verify the dashboard.
