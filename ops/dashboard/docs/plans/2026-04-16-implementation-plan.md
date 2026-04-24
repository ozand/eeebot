# Nanobot Ops Dashboard Implementation Plan

> For Hermes: execute this plan end-to-end without pausing for handoff unless a real blocker appears.

Goal:
Build a local dashboard project on this host that can show both current and historical Nanobot activity by combining:
- live eeepc authority state over SSH
- local repo-side bounded runtime state
- normalized snapshots stored in SQLite
- a local web UI suitable for demonstration

Architecture:
- separate project under `Projects/nanobot-ops-dashboard`
- Python package under `src/`
- SQLite for history retention
- SSH-based collector for eeepc host state
- file-based collector for local repo-side Nanobot state
- WSGI/stdlib web app with Jinja2 templates for zero-heavy-dependency local serving

Execution phases:
1. scaffold project files and package layout
2. implement config and SQLite schema
3. implement collectors/parsers for live eeepc and local repo-side state
4. implement snapshot retention/history tables
5. implement HTML UI pages
6. add tests
7. run a local demo instance and verify with curl/manual checks
8. write show-and-run docs

Must-have v1 pages:
- overview
- cycles
- promotions
- approvals
- deployments
- subagents

Notes:
- if no durable subagent telemetry exists, the UI must say so clearly instead of pretending
- history is mandatory: hourly task changes must remain visible across past snapshots
