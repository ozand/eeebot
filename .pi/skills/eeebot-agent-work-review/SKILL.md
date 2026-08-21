# Skill: eeebot-agent-work-review

**Trigger**: When the user asks "что сделал агент", "над чем работал агент", "покажи результаты агента", "анализ работы агента", or any similar question about what the self-evolving agent has done over a period of time.

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

---

## Repo paths reference
- Subagent write target: `/home/opencode/servers_team/repo_research/eeebot-self-evolving`
- Runtime (current): `/opt/eeepc-agent/runtimes/self-evolving-agent/current`
- State root: `/var/lib/eeepc-agent/self-evolving-agent/state`
- History files: `$STATE_ROOT/goals/history/cycle-*.json`
- Bridge service: `eeepc-self-evolving-subagent-bridge.service` (sole live loop; the
  former coordinator/health unit `eeepc-self-evolving-agent-health.service` was
  decommissioned in #900/#910)
