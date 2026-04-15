# Subagents Operator UX + Correlation Plan

> For Hermes: execute with subagent-driven-development using separate fresh subagents for the dashboard repo and the Nanobot repo.

Goal:
1. make `/subagents` a better operator page
2. correlate subagents with cycle/goal context so the page answers not just "what ran" but also "for which goal/cycle did it run"

Architecture:
- keep dashboard work in `/home/ozand/herkoot/Projects/nanobot-ops-dashboard`
- keep Nanobot telemetry enrichment work in `/home/ozand/herkoot/Projects/nanobot`
- enrich durable subagent telemetry on the Nanobot side first where needed
- extend dashboard collector/app/template to ingest and present the new correlation fields

Tech stack:
- dashboard: Python stdlib + Jinja2 + SQLite
- Nanobot: Python runtime telemetry files + existing tests

---

## Track A — /subagents operator UX

### Deliverables
- improve `/subagents` for actual operations use
- add filter controls at minimum for status and possibly source/origin
- add better ordering / latest-first clarity
- improve scannability of rows/cards while keeping all fields visible
- preserve graceful fallback when no telemetry exists

### Likely files
- `src/nanobot_ops_dashboard/templates/subagents.html`
- `src/nanobot_ops_dashboard/app.py`
- `tests/test_app.py`
- docs if behavior changes enough to mention

### Verification
- full dashboard pytest suite
- restart dashboard web service
- verify `/subagents` over HTTP

## Track B — cycle/goal correlation

### Deliverables on Nanobot side
- durable telemetry should include correlation fields when available, for example:
  - `goal_id`
  - `cycle_id`
  - possibly `report_path` or equivalent runtime origin pointer
- preserve current durable telemetry fields
- do not break existing tests

### Deliverables on dashboard side
- collector ingests those correlation fields
- `/subagents` page shows them clearly
- operator can see which goal/cycle a subagent belongs to
- if correlation fields are absent, UI degrades gracefully

### Likely files in Nanobot
- `nanobot/agent/subagent.py`
- focused tests around subagent/task cancellation or telemetry writing

### Likely files in dashboard
- `src/nanobot_ops_dashboard/collector.py`
- `src/nanobot_ops_dashboard/templates/subagents.html`
- `tests/test_collector.py`
- `tests/test_app.py`

### Verification
Nanobot repo:
- focused telemetry tests

Dashboard repo:
- full pytest suite
- one real or seeded telemetry file showing goal/cycle fields on `/subagents`

## Execution order
1. Track A and Track B can proceed in parallel
2. Nanobot correlation fields land first if the dashboard needs them for final rendering
3. dashboard collector/template updated to consume them
4. restart services
5. verify `/subagents` live

## Completion definition
This slice is complete when:
- `/subagents` is more operator-friendly
- durable telemetry includes correlation fields when available
- dashboard shows goal/cycle context for subagents when present
- both repos have committed changes
