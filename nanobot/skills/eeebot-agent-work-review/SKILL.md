---
name: eeebot-agent-work-review
description: Trigger: Creating a report of the self-evolving agent's work.
version: "1.1.0"
---

# Skill: eeebot-agent-work-review

**Trigger**: When the user asks "что сделал агент", "над чем работал агент", "покажи результаты агента", "анализ работы агента", or any similar question about what the self-evolving agent has done over a period of time. Use this skill to produce a time-bounded, evidence-first report of actual agent work.

---

## FIRST: Clarify the time window before doing anything

**Never choose a default time window yourself.** The user's intent is ambiguous:
- "за последнее время" could mean 30 minutes, 2 hours, or a day
- "с последней проверки" — you must know when that was; check the conversation history
- "много часов" — does not specify how many

**Do this first:**
1. Check the conversation history for the most recent previous agent analysis
2. If you can determine the last analysis time from context → state it and confirm:
   > "Последний анализ был в HH:MM. Показать изменения с тех пор?"
3. If you cannot determine it → ask explicitly:
   > "За какой период показать? (например: последние 2 часа, с HH:MM, за сегодня)"
4. Only proceed with analysis after the period is confirmed

**Known bad pattern to avoid:**
- User says "прошло уже много часов" → you pick 24h arbitrarily → report covers the wrong window
- User says "за это время" → you pick "6h" from skill template → same problem

---

## Critical known mistakes to avoid

### 1. UTC vs local time confusion
- Cycle history files use **UTC timestamps** (`recorded_at_utc`)
- The eeepc host runs in **MSK (UTC+3)**
- Git commits show **local time** (+0300)
- **Always compare times in the same timezone.** UTC 21:53 = MSK 00:53 — 7 minutes ago, not 11 hours.
- **Never say "it stopped N hours ago"** without converting UTC→local first.

### 2. Confusing coordinator meta-results with subagent actual work
- `state/subagents/results/` contains **coordinator-generated stub files** (`result_status: blocked`)
- **Real subagent work** is in:
  - `git log` on `eeebot-self-evolving` (Author: eeepc-agent) ← primary source
  - `memory/HISTORY.md` ← subagent narrative
  - `journalctl -u eeepc-self-evolving-subagent-bridge.service` ← real-time tool calls
- `result_status: blocked` ≠ "agent did nothing"
- `files_changed: []` in results/ ≠ "no files changed"

### 3. "Files changed" in cycle history ≠ real code changes
- `artifact_paths` in cycle JSON = report files in `state/reports/`, not source code
- Real code changes → `git log` filtered to `Author: eeepc-agent`

### 4. Default time windows are wrong
- Do not use `timedelta(hours=6)` or `--since='YYYY-MM-DD'` without user confirmation
- The correct window comes from the user or from the last analysis timestamp in chat history

### 5. Lessons DB: occurrences not updating
- `lessons/` in runtime is a **symlink** to `eeebot-self-evolving/lessons/`
- If symlink is missing, lessons silently write nowhere
- Check: `sudo ls -la /opt/eeepc-agent/runtimes/self-evolving-agent/current/lessons`

---

## Correct analysis procedure

### Step 0: Determine time window (MANDATORY — do not skip)
```
1. Scan this conversation for previous agent analysis outputs
2. If found: state the timestamp, ask for confirmation
3. If not found: ask the user for the period
4. Do NOT proceed to Step 1 until window is confirmed
```

### Step 1: Orient time (run before any timestamp work)
```bash
ssh eeepc "date"   # host local time (MSK)
date               # workstation local time
# Rule: recorded_at_utc is UTC; git commits are +0300 (MSK = UTC+3)
```

### Step 2: Recent cycle activity (use CONFIRMED window, not a default)
```bash
# Replace $HOURS with the confirmed window in hours
ssh eeepc 'sudo python3 -c "
import json, pathlib, collections
from datetime import datetime, timezone, timedelta

hist = pathlib.Path(\"/var/lib/eeepc-agent/self-evolving-agent/state/goals/history\")
cutoff = datetime.now(timezone.utc) - timedelta(hours=$HOURS)
files = sorted(hist.glob(\"*.json\"), key=lambda p: p.stat().st_mtime, reverse=True)

task_counts = collections.Counter()
statuses = collections.Counter()
recent = []
for f in files:
    try:
        d = json.loads(f.read_text())
        ts_str = d.get(\"recorded_at_utc\", \"\")
        if not ts_str: continue
        ts = datetime.fromisoformat(ts_str.replace(\"Z\",\"+00:00\"))
        if ts < cutoff: break
        recent.append((ts_str[:16], d.get(\"current_task_id\"), d.get(\"result_status\"), d.get(\"reward_signal\", {}).get(\"value\")))
        task_counts[d.get(\"current_task_id\", \"?\")] += 1
        statuses[d.get(\"result_status\", \"?\")] += 1
    except: pass

total = sum(statuses.values())
pass_count = statuses.get(\"PASS\", 0)
print(\"Window:\", $HOURS, \"hours | Cycles:\", len(recent))
print(\"PASS:\", pass_count, \"| BLOCK:\", statuses.get(\"BLOCK\", 0), \"| Pass rate:\", round(pass_count/total*100 if total else 0, 1), \"%\")
print(\"Tasks:\")
for t, c in task_counts.most_common(): print(\"  \" + str(c) + \"x  \" + t)
print(\"Last 5:\")
for r in recent[:5]: print(\" \", r)
"'
```

### Step 3: Real subagent git commits (source of truth — use CONFIRMED window)
```bash
# Replace $SINCE with the confirmed start time e.g. "2026-06-15 00:30" (MSK)
ssh eeepc "sudo -u opencode git -c safe.directory=/home/opencode/servers_team/repo_research/eeebot-self-evolving \
  -C /home/opencode/servers_team/repo_research/eeebot-self-evolving \
  log --format='%ai | %h | %s' --since='$SINCE' 2>/dev/null"

# File touch counts
ssh eeepc "sudo -u opencode git -c safe.directory=/home/opencode/servers_team/repo_research/eeebot-self-evolving \
  -C /home/opencode/servers_team/repo_research/eeebot-self-evolving \
  log --since='$SINCE' --name-only --pretty=format: 2>/dev/null \
  | grep -v '^$' | sort | uniq -c | sort -rn | head -n 15"
```

### Step 4: Active subagent right now
```bash
ssh eeepc "sudo journalctl -u eeepc-self-evolving-subagent-bridge.service -n 20 --no-pager --since='1 hour ago'"
```

### Step 5: What subagent wrote in HISTORY.md
```bash
ssh eeepc "sudo tail -n 15 /home/opencode/servers_team/repo_research/eeebot-self-evolving/memory/HISTORY.md"
```

### Step 6: System health snapshot (optional, on request)
```bash
ssh eeepc "sudo -u opencode python3 /home/opencode/servers_team/repo_research/eeebot-self-evolving/scripts/eeebot_dashboard.py --tui 2>&1"
```

---

## Summary format

State the confirmed window explicitly at the top:

```
## Активность агента с [HH:MM MSK] по [HH:MM MSK] (N часов)

### Циклы за период
- Всего: N | PASS: X | BLOCK: Y | Pass rate: Z%
- Задачи: exploit(N), verify(N), ...

### Реальные изменения кода (git)
- N коммитов (Author: eeepc-agent) за период
- Файлы: dashboard.py(N), coordinator.py(N), ...
- [hash] feat: ...
- [hash] perf: ...

### Активный субагент прямо сейчас
- ID: XXXXXXXX, запущен в HH:MM MSK, выполняет: ...

### Состояние системы
- Reward: X.XX | Queue: N pending | CPU: X% | Mem: X%
```

**Before sending:** run the Outcome validation criteria (checks 1–6) and the
Handoff inspection rules (1–6) against the draft. Only send when every check
passes; otherwise fix the backing command and re-run.

---

## Outcome validation criteria (run before reporting)

Every claim in the report must pass the matching check. A claim that fails a
check is either corrected or dropped — never reported as confirmed.

| # | Claim type | Validation check | Pass condition |
|---|---|---|---|
| 1 | Cycle count / pass rate | Recompute from `state/goals/history/cycle-*.json` inside the confirmed window | Numbers match the raw JSON; window stated in the report header |
| 2 | "N commits by the agent" | `git log --since=$SINCE --author=eeepc-agent` (or the configured author) | Count equals the commit list you actually printed |
| 3 | "File X changed N times" | `git log --since=$SINCE --name-only` + `uniq -c` | Count matches the per-file tally, not a guess |
| 4 | Timestamp / "N hours ago" | Convert `recorded_at_utc` (UTC) to MSK (UTC+3) before comparing | Same timezone on both sides; no UTC-vs-local mix |
| 5 | "Agent is active now" | `journalctl -u eeepc-self-evolving-subagent-bridge.service` last line | A live tool call or cycle start is visible, not a stale stub |
| 6 | "No work happened" | Cross-check `git log` AND `HISTORY.md` tail | Both sources agree; a `blocked` stub alone never proves inactivity |

**Hard rules:**
- If a check cannot be run (missing file, permission error), say so explicitly
  in the report instead of omitting the claim.
- Never report a number you did not print from a command in this session.
- A `result_status: blocked` stub in `state/subagents/results/` is not evidence
  of inactivity — validate against git and HISTORY.md (check 6).

---

## Handoff inspection rules (when the report is handed to the operator)

Before the report leaves the session, inspect the handoff for these failure
modes:

1. **Window mismatch.** The header window must equal the window used in every
   command. If the user confirmed "2 hours" but a command used a 24h default,
   the report is wrong — re-run with the confirmed window.
2. **Stale data.** If the newest `cycle-*.json` mtime is older than the
   confirmed window start, the window contains no cycles — say "no cycles in
   window" rather than reporting an empty-looking table.
3. **Author filter drift.** Commits from other authors (root, opencode, the
   harness) must not be counted as agent work. Re-check the `--author` filter
   if the commit count looks high.
4. **Timezone leak.** Scan the report for any "N hours ago" phrasing; each must
   be backed by a UTC→MSK conversion shown in the command output.
5. **Unverified "active now".** If the journalctl tail shows only old entries,
   the agent is not active — report "no active subagent" instead of guessing.
6. **Missing evidence.** Every number in the summary must trace to a command
   run in this session. If a number has no backing command, remove it.

**Handoff gate:** the report is ready only when all six inspections pass. If
any fails, fix the underlying command and re-run before reporting.

---

## Repo paths reference
- Subagent write target: `/home/opencode/servers_team/repo_research/eeebot-self-evolving`
- Runtime (current): `/opt/eeepc-agent/runtimes/self-evolving-agent/current`
- State root: `/var/lib/eeepc-agent/self-evolving-agent/state`
- History files: `$STATE_ROOT/goals/history/cycle-*.json`
- Bridge service: `eeepc-self-evolving-subagent-bridge.service`
- Health service: `eeepc-self-evolving-agent-health.service`
