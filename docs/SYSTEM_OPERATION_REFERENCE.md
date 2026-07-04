# eeebot — Описание работы системы

_Последнее обновление: 2026-07-05. Источники: `nanobot/runtime/coordinator.py`, `nanobot/runtime/subagent_materializer.py`, `nanobot/runtime/bridge.py`, `host/eeepc/systemd/*.timer`, `docs/ARCHITECTURE.md`, `docs/EEEBOT_SELF_IMPROVING_RUNTIME_OPERATING_CONTRACT.md`. См. также `docs/OBSERVABILITY.md`._

---

## 1. Роли в системе

| Роль | Кто | Что делает |
|---|---|---|
| **Координатор** | `eeepc-self-evolving-agent-health` | Читает состояние, принимает решение о следующей задаче, записывает артефакты |
| **Субагент-мост** | `eeepc-self-evolving-subagent-bridge` | Запускает LLM-субагент для выполнения конкретных задач (git, код, тесты) |
| **Approval keeper** | `eeepc-self-evolving-approval-keeper` | Поддерживает файл-ворота `apply.ok` в актуальном состоянии |
| **Strong reflection** | `eeepc-strong-reflection` | Раз в 6 часов запускает более глубокий цикл рефлексии |

---

## 2. Тайминги (systemd timers)

Все таймеры работают на хосте `eeepc` через systemd.

```
eeepc-self-evolving-agent-health.timer    → каждые 15 мин (OnBootSec=2m)
eeepc-self-evolving-subagent-bridge.timer → каждые 15 мин (OnBootSec=4m)
eeepc-self-evolving-approval-keeper.timer → каждые  5 мин (OnBootSec=1m)
eeepc-strong-reflection.timer             → каждые  6 ч   (OnBootSec=12m)
```

Координатор и мост запускаются с разницей ~2 минуты (boot offset 2m vs 4m),
что даёт координатору время записать запрос субагенту до того, как мост его подхватит.

---

## 3. Цикл координатора

Реализован в `nanobot/runtime/coordinator.py` → `run_self_evolving_cycle()`.

### Шаги цикла

```
1. OBSERVE   — читает текущее состояние из state/
2. REFRAME   — вычисляет feedback_decision (что делать дальше)
3. SPECIFY   — формирует experiment contract и run_budget
4. EXECUTE   — выполняет задачу (через LLM вызов)
5. EVALUATE  — определяет outcome: keep / discard / crash / blocked
6. PERSIST   — записывает артефакты в state/
```

### Что читает на шаге OBSERVE

| Файл/директория | Что в нём |
|---|---|
| `state/goals/registry.json` | реестр целей, активная цель (`active_goal_id`) |
| `state/goals/goal_text.json` | текстовое описание текущей цели |
| `state/current.json` | последний план задач (`task_plan`) |
| `state/history/cycle-*.json` | последние N циклов (для streak-анализа) |
| `state/improvements/materialized-cycle-*.json` | материализованные улучшения |
| `state/subagents/results/` | результаты выполнения субагентов |
| `state/approvals/apply.ok` | ворота применения изменений |

---

## 4. Логика принятия решений (feedback_decision)

Ключевые константы из `coordinator.py`:

```python
GOAL_ROTATION_STREAK_LIMIT = 3          # сколько PASS подряд нужно для перехода к следующей фазе
AMBITION_UNDERUTILIZATION_STREAK_LIMIT = 5  # сколько циклов "без дела" до эскалации
```

### Фазы и переходы

```
synthesize-next-improvement-candidate
        ↓ (3 PASS подряд)
materialize-synthesized-improvement
        ↓ (завершено)
subagent-verify-materialized-improvement
        ↓ (субагент отработал)
record-reward
        ↓
synthesize-next-improvement-candidate  (новый круг)
```

### Эскалация амбиций

Если за последние 5 циклов (`AMBITION_UNDERUTILIZATION_STREAK_LIMIT`):
- субагенты не использовались (`subagents_unused`)
- все циклы завершились `discard` (`recent_window_discard_only`)
- инструментарий недоиспользован (`tool_budget_underused`)

→ координатор переходит в режим `escalate_underutilized_ambition`
→ принудительно переключается на `materialize-synthesized-improvement`

### Бюджеты выполнения

| Профиль | max_requests | max_tool_calls | max_subagents | max_timeout |
|---|---|---|---|---|
| conservative (review/blocked) | 2 | 12 | 2 | 900 сек |
| execution (materialize/subagent) | 5 | 40 | 5 | 1800 сек |
| mixed | 4 | 32 | 5 | 1800 сек |

---

## 5. Артефакты цикла

Все артефакты пишутся в `state/` (реальный путь на хосте: `/var/lib/eeepc-agent/self-evolving-agent/state/`).

### Пишет координатор

| Файл | Когда | Что содержит |
|---|---|---|
| `reports/evolution-<ts>-cycle-<id>.json` | каждый цикл | полный отчёт: goal, task, outcome, reward, budget_used |
| `current.json` | каждый цикл | последний план: task_plan, feedback_decision |
| `history/cycle-<id>.json` | каждый цикл | запись в историю для streak-анализа |
| `improvements/materialized-cycle-<id>.json` | при материализации | улучшение с HADI-метаданными и acceptance_checks |
| `subagents/requests/request-cycle-<id>.json` | при запросе к субагенту | задача, профиль, бюджет, ссылка на source_artifact |
| `outbox/latest.json` | каждый цикл | компактный снапшот для dashboard |
| `credits/ledger-<id>.json` | каждый цикл | баланс и delta кредитов |

### Пишет субагент-мост

| Файл | Когда | Что содержит |
|---|---|---|
| `subagents/results/result-<id>.json` | после выполнения | result_status, key_learnings, executor_result |
| `subagents/<agent-id>.json` | во время работы | телеметрия субагента (tool calls, статус) |
| `.nanobot/subagents/handled_<id>.txt` | после выполнения | маркер "уже обработан" для дедупликации |

### Схема materialized-improvement артефакта

```json
{
  "schema_version": "materialized-improvement-v1",
  "cycle_id": "cycle-<hex>",
  "goal_id": "goal-bootstrap",
  "task_id": "materialize-synthesized-improvement",
  "summary": "...",
  "reward_signal": { "value": 1.0, "source": "result_status" },
  "feedback_decision": { "mode": "...", "selected_task_id": "..." },
  "hadi_cycle": {
    "hypothesis": "...",
    "action": "...",
    "data": { ... },
    "insight": "..."
  },
  "concrete_improvement_statement": "...",
  "recommended_next_action": "...",
  "acceptance_checks": ["..."],
  "next_bounded_candidate": { "task_id": "...", "title": "..." }
}
```

### Схема subagent-request артефакта

```json
{
  "schema_version": "subagent-request-v1",
  "request_id": "subagent-verify-materialized-improvement-cycle-<hex>-<short>",
  "cycle_id": "cycle-<hex>",
  "goal_id": "goal-bootstrap",
  "task_id": "subagent-verify-materialized-improvement",
  "task_title": "...",
  "task": "...",
  "recommended_next_action": "...",
  "request_status": "queued",
  "profile": "research_only",
  "budget": "micro",
  "source_artifact": "/var/lib/eeepc-agent/.../improvements/materialized-cycle-<id>.json"
}
```

---

## 6. Субагент-мост: логика работы

Реализован в `nanobot/runtime/bridge.py` (`scripts/eeepc_self_evolving_subagent_bridge.py`
остаётся тонкой обёрткой для systemd/deploy-контракта — см. #599).

### Алгоритм `main()`

```
1. Читает outbox/report.index.json → получает goal_id и report_source
2. Вызывает find_pending_request():
   a. Сканирует state/subagents/requests/*.json
   b. Исключает request_id, для которых уже есть НАСТОЯЩИЙ результат
      (не blocked-заглушка координатора)
   c. Возвращает самый старый не-обработанный запрос
3. Если запроса нет → выводит 'already_handled', завершается (~3 сек)
4. Cycle-branch isolation: _setup_cycle_branch() делает
   `git checkout -B selfevo/cycle-<id> origin/main` в рабочем репозитории —
   субагент пишет в изолированную ветку, а не прямо в main
5. Читает goal_text.json для формирования промпта
6. Вызывает build_task() → встраивает содержимое source_artifact + addendum
   "Branch discipline (MANDATORY): реализуй + закоммить, без mid-flight push"
7. Запускает SubagentManager.spawn() с обязательной моделью-исполнителем
   un/qwen3.6-27b-mtp; ждёт завершения, таймаут 3000 сек (50 мин)
8. Import-smoke gate: _run_smoke_tests() делает syntax/import-проверку ТОЛЬКО
   изменённых .py-файлов (не весь pytest-прогон) — быстро и без env-ложных
   падений. Нет изменённых .py → gate пропускается
9. Integrate-to-main: при PASS gate _integrate_cycle_to_main() мержит HEAD
   ветки цикла в main и пушит `origin/main`. При FAIL — изменения остаются в
   ветке цикла, в main НЕ попадают (пишется learning-артефакт)
10. _cleanup_cycle_branch() удаляет ветку цикла после интеграции
11. Записывает handled_<id>.txt как маркер завершения
```

**Почему cycle-branch, а не прямой push в main:** каждый цикл изолирован — если
субагент сломал что-то или import-smoke упал, main остаётся чистым; only
проверенные изменения интегрируются. Это даёт наблюдаемую границу
«сделано и проверено» на уровне git.

### Ключевое: различие blocked-заглушки и реального результата

Координатор во время своего цикла **сам пытается** выполнить запрос субагента
через `materialize_subagent_requests()`. Поскольку `NANOBOT_SUBAGENT_EXECUTOR_COMMAND`
**специально не задан** в `agent.service`, создаётся заглушка:

```json
{ "result_status": "blocked", "terminal_reason": "local_executor_unavailable",
  "materialized_from": "queued_request_terminalizer" }
```

Мост фильтрует такие заглушки через `_is_real_result()` и всё равно берёт
запрос в работу для реального LLM-выполнения.

**Важно:** `NANOBOT_SUBAGENT_EXECUTOR_COMMAND` намеренно не прописан в `agent.service`.
Если его добавить — координатор будет выполнять субагента synchronously
через `bounded_subagent_executor` (без LLM) и записывать `completed` раньше,
чем мост успеет взять запрос.

---

## 7. Модели LLM

Гибридная топология: лёгкий координатор-оркестратор + локальный
исполнитель на GPU. **Это разные роли и разные модели — не путать.**

| Роль | Модель | Где исполняется | Назначение |
|---|---|---|---|
| **Координатор** (meta-orchestrator) | `cl/gemini-3-flash` (remote, через LiteLLM proxy) | `eeepc` host, `ALLOW_CODE_EDITS=false` | bookkeeping: оценка цикла, reward, выбор следующего перехода, синтез описания кандидата. Кода НЕ пишет |
| **Субагент-исполнитель** | **`un/qwen3.6-27b-mtp`** (локальный, ОБЯЗАТЕЛЬНЫЙ) | dev-host с GPU, через мост | пишет код, гоняет проверки, коммитит в `eeebot-self-evolving` |

`un/qwen3.6-27b-mtp` — локальная модель на видеокарте оператора; система
спроектирована работать именно на ней. **Не заменять модель-исполнитель.**
В коде/конфиге она может фигурировать под логическим алиасом `gpt-5.3-codex`
(провайдер `hermes_pi_qwen`), который LiteLLM-прокси прозрачно маршрутизирует
на реальный qwen-деплой.

Конфигурация моста: `SUBAGENT_BRIDGE_MODEL` в
`/etc/eeepc-agent/instances/self-evolving-subagent-bridge.env`
Credentials (единственный источник): `/etc/eeepc-agent/litellm.env`
LiteLLM proxy: `100.82.9.44:4001/v1`

---

## 8. Репозитории

| Репозиторий | Путь на хосте | Назначение |
|---|---|---|
| `ozand/eeebot` (canonical) | `/opt/eeepc-agent/runtimes/self-evolving-agent/current` | релизная версия рантайма |
| `ozand/eeebot-self-evolving` | `/var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving` | рабочий репозиторий субагентов: сюда они пишут код и коммитят |

Субагент работает в `eeebot-self-evolving`, пишет там изменения и пушит в `origin/main`.
Оператор периодически переносит изменения из `eeebot-self-evolving` в `eeebot` (canonical) через PR.

---

## 9. Approval gate

Файл `/var/lib/eeepc-agent/self-evolving-agent/state/approvals/apply.ok`:
```json
{ "expires_at_epoch": 1234567890 }
```

- `approval-keeper` обновляет его каждые 5 минут.
- Без актуального `apply.ok` субагент работает в режиме `strict` (только чтение).
- При открытом gate — режим `auto` (разрешено писать файлы).

---

## 10. Диагностика

```bash
# Последние циклы координатора
sudo journalctl -u eeepc-self-evolving-agent-health.service --since "1h ago" --no-pager

# Последние запуски моста
sudo journalctl -u eeepc-self-evolving-subagent-bridge.service --since "1h ago" --no-pager

# Статус запросов субагентов (реальные vs blocked-заглушки)
sudo python3 -c "
import json, glob
state = '/var/lib/eeepc-agent/self-evolving-agent/state'
for f in sorted(glob.glob(state+'/subagents/results/*.json'))[-5:]:
    d = json.load(open(f))
    print(d.get('result_status'), d.get('terminal_reason'), d.get('materialized_from'), '|', f[-60:])
"

# Health check через CLI
eeebot cycle-health \
  --runtime-state-root /var/lib/eeepc-agent/self-evolving-agent/state \
  --runtime-state-source host_control_plane --json
```
