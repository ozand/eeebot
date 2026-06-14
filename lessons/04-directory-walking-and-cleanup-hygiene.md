# Lesson: Directory Walking, Stale File Aggregation & Cleanup Hygiene

## Context
The Eee PC agent tracks system performance and state via file logs in `/var/lib/eeepc-agent/self-evolving-agent/state/subagents/`.
A dashboard utility (`scripts/eeebot_dashboard.py`) parses these directories to compute queue depth, stale requests, and system health status.

## Problem
The dashboard reported a critical warning `queue: CRIT` with `Queue: 443 pending / 25 stale`, despite executing a cleanup script. The total health of the host degraded to `[CRIT]`, trigger warnings for queue pressure.

## Root Cause
1. **Scope mismatch in cleanup vs. dashboard**:
   The initial cleanup scripts (`archive_subagent_requests.py` and first-generation `cleanup_subagent_queue.py`) only cleaned up files in `subagents/requests/`.
   However, the dashboard parsed `subagents/` recursively (`rglob("*")`), counting files in `subagents/results/` and temporary workspace files as queue pressure. Since the results directory was never pruned, it grew to hundreds of files, leading to a permanent `CRIT` queue warning.
2. **Missing ISO Timestamps**:
   Early requests did not contain a `"timestamp"` key in their JSON payload. Archiving scripts relying on JSON metadata checks skipped these files, leaving them in the queue.

## Resolution
1. **Fallback to filesystem mtime**:
   Update cleanup utilities to determine age using file modification time (`st_mtime`) if the JSON `"timestamp"` key is missing:
   ```python
   file_mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
   ```
2. **Multi-directory pruning**:
   Extend the cleanup script to target all subagent artifacts (requests, results, and root directory logs):
   ```python
   targets = []
   targets.extend((state_root / "subagents" / "requests").glob("*.json"))
   targets.extend((state_root / "subagents" / "results").glob("*.json"))
   targets.extend((state_root / "subagents").glob("*.json"))  # files in root
   ```
3. **Automate on schedule**:
   Create a hourly systemd timer (`eeebot-archive-subagent-requests.timer`) pointing to the cleanup utility with explicit `--state-root` configuration.

## Key Takeaway
Monitoring metrics (like queue depth) and administrative cleaning routines (like archiving) must share the same scope assumptions. If the dashboard monitors a directory tree recursively, the cleanup daemon must prune it recursively to prevent memory and diagnostic leakages.
