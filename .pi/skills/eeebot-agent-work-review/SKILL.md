# Skill: eeebot-agent-work-review

**Trigger**: When the user asks "что сделал агент", "над чем работал агент", "покажи результаты агента", "анализ работы агента", or any similar question about what the self-evolving agent has done over a period of time.

---

## Critical known mistakes to avoid

### 1. UTC vs local time confusion
- Cycle history files use **UTC timestamps** (`recorded_at_utc`)
- The eeepc host runs in **MSK (UTC+3)**
- Git commits show **local time** (+0300)
- `date` on host returns MSK
- **Always compare times in the same timezone.** If it's 01:00 MSK, the last UTC cycle at 21:53 UTC = 00:53 MSK — that's ~7 minutes ago, not 11 hours.
- **Never say "it stopped N hours ago"** without converting UTC→local first.

### 2. Confusing coordinator meta-results with subagent actual work
- `/var/lib/eeepc-agent/self-evolving-agent/state/subagents/results/` contains **coordinator-generated stub files** (`result_status: blocked`) for cycles where executor was unavailable
- **Real subagent work** is tracked in:
  - `git log` on `eeebot-self-evolving` (commits with `Author: eeepc-agent`)
  - `memory/HISTORY.md` (subagent writes one line per action)
  - Bridge service journal: `journalctl -u eeepc-self-evolving-subagent-bridge.service`
  - `.nanobot/subagents/` in the workspace
- `result_status: blocked` in results/ does NOT mean subagent did nothing — it means the coordinator's terminalizer fired before bridge finished. The bridge runs independently.

### 3. "Files changed" in cycle history ≠ real code changes
- `artifact_paths` in cycle JSON = report files in `state/reports/`, not source code
- Real code changes → look at `git log` filtered to `Author: eeepc-agent`
- `files_changed: []` in results/ = subagent didn't write to that JSON file; doesn't mean nothing was done

### 4. History file count vs recent activity
- There are 5000+ history files total — always filter by `recorded_at_utc` for a time window
- Use `st_mtime` for ordering (reliable), `recorded_at_utc` for filtering by date
- `today_count` check against UTC date, not local date

### 5. Lessons DB: occurrences not updating
- `lessons/` dir in runtime is a **symlink** to `eeebot-self-evolving/lessons/`
- Coordinator writes lessons to `workspace/lessons/` — workspace = `WorkingDirectory` from systemd = `/opt/eeepc-agent/runtimes/self-evolving-agent/current`
- If symlink is missing, lessons silently write nowhere
- Check: `sudo ls -la /opt/eeepc-agent/runtimes/self-evolving-agent/current/lessons`

---

## Correct analysis procedure

### Step 1: Orient time
```bash
ssh eeepc "date"   # host local time
date               # workstation local time
# Remember: history files are UTC, git commits are +0300 (MSK)
```

### Step 2: Recent cycle activity (last N hours)
```bash
ssh eeepc 'sudo python3 -c "
import json, pathlib, collections
from datetime import datetime, timezone, timedelta

hist = pathlib.Path(\"/var/lib/eeepc-agent/self-evolving-agent/state/goals/history\")
cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
files = sorted(hist.glob(\"*.json\"), key=lambda p: p.stat().st_mtime, reverse=True)

task_counts = collections.Counter()
statuses = collections.Counter()
recent = []
for f in files:
    try:
        d = json.loads(f.read_text())
        ts_str = d.get(\"recorded_at_utc\", \"\")
        if ts_str:
            ts = datetime.fromisoformat(ts_str.replace(\"Z\",\"+00:00\"))
            if ts < cutoff:
                break
            recent.append((ts_str, d.get(\"current_task_id\"), d.get(\"result_status\"), d.get(\"reward_signal\", {}).get(\"value\")))
            task_counts[d.get(\"current_task_id\", \"?\")] += 1
            statuses[d.get(\"result_status\", \"?\")] += 1
    except: pass

print(\"Cycles in last 6h:\", len(recent))
print(\"Status:\", dict(statuses))
print(\"Tasks:\")
for t, c in task_counts.most_common(): print(f\"  {c}x {t}\")
print(\"Last 5:\")
for r in recent[:5]: print(\" \", r)
"'
```

### Step 3: Real subagent git commits (source of truth for actual work)
```bash
# All subagent commits since date (adjust --since)
ssh eeepc "sudo -u opencode git -c safe.directory=/home/opencode/servers_team/repo_research/eeebot-self-evolving \
  -C /home/opencode/servers_team/repo_research/eeebot-self-evolving \
  log --format='%ai %h %s' --since='YYYY-MM-DD' 2>/dev/null"

# Files touched by subagent
ssh eeepc "sudo -u opencode git -c safe.directory=/home/opencode/servers_team/repo_research/eeebot-self-evolving \
  -C /home/opencode/servers_team/repo_research/eeebot-self-evolving \
  log --since='YYYY-MM-DD' --name-only --pretty=format: 2>/dev/null \
  | grep -v '^$' | sort | uniq -c | sort -rn | head -n 15"

# Commit type breakdown
ssh eeepc "sudo -u opencode git -c safe.directory=/home/opencode/servers_team/repo_research/eeebot-self-evolving \
  -C /home/opencode/servers_team/repo_research/eeebot-self-evolving \
  log --oneline --since='YYYY-MM-DD' 2>/dev/null \
  | grep -oP '^[a-f0-9]+ \K(feat|fix|perf|refactor|docs|chore)' | sort | uniq -c | sort -rn"
```

### Step 4: What the active subagent is doing right now
```bash
# Running bridge subagent
ssh eeepc "sudo journalctl -u eeepc-self-evolving-subagent-bridge.service -n 30 --no-pager --since='1 hour ago'"

# Current subagent result (latest bridge output)
ssh eeepc "sudo ls -lt /var/lib/eeepc-agent/self-evolving-agent/state/subagents/results/ | head -n 5"
```

### Step 5: What subagent wrote in memory/HISTORY.md
```bash
ssh eeepc "sudo tail -n 20 /home/opencode/servers_team/repo_research/eeebot-self-evolving/memory/HISTORY.md"
```

### Step 6: System health snapshot
```bash
ssh eeepc "sudo -u opencode python3 /home/opencode/servers_team/repo_research/eeebot-self-evolving/scripts/eeebot_dashboard.py --tui 2>&1"
```

### Step 7: Lessons DB state
```bash
ssh eeepc 'sudo python3 << "EOF"
import yaml, pathlib
lessons_path = pathlib.Path("/home/opencode/servers_team/repo_research/eeebot-self-evolving/lessons/lessons.yaml")
errors_path  = pathlib.Path("/home/opencode/servers_team/repo_research/eeebot-self-evolving/lessons/errors.yaml")
lessons = yaml.safe_load(lessons_path.read_text()) or []
errors  = yaml.safe_load(errors_path.read_text()) or []
print("lessons:", len(lessons), "entries")
for e in lessons:
    print(" ", e.get("id","?")[:45], "occ=" + str(e.get("occurrences","?")), "last=" + str(e.get("last_seen","?")))
print("errors:", len(errors), "entries")
for e in errors:
    print(" ", e.get("id","?")[:45], "occ=" + str(e.get("occurrences","?")))
EOF
'
```

---

## Summary format to use when reporting

After running all steps, structure the report as:

```
## Активность агента [период]

### Статистика циклов
- Всего за период: N циклов
- PASS: X | BLOCK: Y | Pass rate: Z%
- Задачи: ...

### Реальные изменения кода (git)
- N коммитов в eeebot-self-evolving за период
- Файлы: dashboard.py (N), coordinator.py (N), ...
- Типы: feat(N), perf(N), fix(N), docs(N)

### Что конкретно сделано (топ изменений)
- [commit hash] feat: ...
- [commit hash] perf: ...

### Активный субагент сейчас
- ID: XXXXXXXX, запущен в HH:MM MSK
- Действия: list_dir, read_file, exec ...

### Состояние системы
- Reward: X.XX avg | Queue: N pending
- CPU: X% | Memory: X% | Disk: X%

### Lessons DB
- lessons.yaml: N entries, last updated: DATE
- errors.yaml: N entries
```

---

## Repo paths reference
- Subagent write target: `/home/opencode/servers_team/repo_research/eeebot-self-evolving`
- Runtime (current): `/opt/eeepc-agent/runtimes/self-evolving-agent/current`
- State root: `/var/lib/eeepc-agent/self-evolving-agent/state`
- History files: `$STATE_ROOT/goals/history/cycle-*.json`
- Subagent results: `$STATE_ROOT/subagents/results/`
- Bridge service: `eeepc-self-evolving-subagent-bridge.service`
- Health service: `eeepc-self-evolving-agent-health.service`
