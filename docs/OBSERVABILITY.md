# Observability — как увидеть, что делает система

_Последнее обновление: 2026-06-24._

Принцип проекта: **простота и прозрачность**. Каждое утверждение о поведении
системы должно сводиться к durable-артефакту, который можно прочитать, или к одной
команде, которая отвечает на вопрос оператора. Этот документ — карта таких сигналов.

Сопутствующее: `SYSTEM_OPERATION_REFERENCE.md` (полная механика),
`EEEBOT_SELF_IMPROVING_RUNTIME_OPERATING_CONTRACT.md` (что цикл обязан доказать).

State root на хосте: `/var/lib/eeepc-agent/self-evolving-agent/state` (далее `state/`).

---

## 1. Семь вопросов оператора → где ответ

Операционный контракт требует, чтобы система всегда могла ответить на эти вопросы
из durable-состояния. Если не может — она работает неправильно.

| Вопрос | Артефакт / команда |
|---|---|
| Какая активная цель? | `state/goals/registry.json` (`active_goal_id`) + `state/goals/goal_text.json` |
| Какой текущий блокер? | последний `state/reports/evolution-*.json` → `blocker` / `result_status=BLOCK` |
| Какие гипотезы в бэклоге? | `state/current.json` → `task_plan`; HADI-метаданные в `improvements/materialized-*.json` |
| Почему выбрана эта задача? | `state/current.json` → `feedback_decision` (mode, selected_task_id) |
| Что делают субагенты сейчас? | `state/subagents/<agent-id>.json` (телеметрия), `state/subagents/results/result-*.json` |
| Какой измеримый результат? | последний report → `reward_signal`, `changed_files` |
| Keep / discard / blocked / crash? | report → `experiment.outcome` и top-level `result_status` |

Одной командой:

```bash
eeebot cycle-health \
  --runtime-state-root /var/lib/eeepc-agent/self-evolving-agent/state \
  --runtime-state-source host_control_plane --json
```

Возвращает: id последнего цикла, путь отчёта, телеметрию субагента, статус моста,
число упавших unit'ов, promotion readiness и рекомендованное следующее действие.

---

## 2. Карта сигналов: «уровень → что смотреть»

### Уровень цикла (координатор)

```bash
# последние циклы координатора
sudo journalctl -u eeepc-self-evolving-agent-health.service --since "1h ago" --no-pager

# что записал последний цикл
ls -t state/reports/evolution-*.json | head -1 | xargs cat
```

Ключевые поля отчёта: `result_status` (PASS|BLOCK|CRASH), `experiment.outcome`
(keep|discard|blocked|crash), `changed_files`, `reward_signal`, `feedback_decision`,
`budget_used`, `promotion.readiness`.

### Уровень субагента (исполнитель qwen)

```bash
# последние запуски моста
sudo journalctl -u eeepc-self-evolving-subagent-bridge.service --since "1h ago" --no-pager

# реальные результаты vs blocked-заглушки
sudo python3 -c "
import json, glob
s='/var/lib/eeepc-agent/self-evolving-agent/state'
for f in sorted(glob.glob(s+'/subagents/results/*.json'))[-5:]:
    d=json.load(open(f)); print(d.get('result_status'), d.get('terminal_reason'), '|', f[-50:])
"
```

Здоровый прогон моста: 30–60 сек, несколько tool-call'ов (`read_file`, `exec`,
`write_file`), затем `completed successfully`. Сессия без единого edit — это
**FAILURE**, а не успех (см. контракт исполнителя в `EEEPC_AGENT_RUNTIME_INSTRUCTIONS.md`).

### Уровень материального прогресса (доходит ли код до main)

Это главный сигнал «система реально саморазвивается», а не крутит bookkeeping:

```bash
# в рабочем репозитории eeebot-self-evolving
git -C /var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving \
  log --oneline origin/main -15
```

Ищем `feat:`-коммиты от циклов и строки `integrate self-evolution cycle
selfevo/cycle-<id>`. Если `origin/main` не двигается несколько часов при идущих
циклах — материального прогресса нет, смотри раздел 3.

---

## 3. Анти-паттерны наблюдаемости (известные режимы застоя)

| Симптом | Что это значит | Где подтвердить |
|---|---|---|
| `origin/main` не двигается, но циклы идут | код пишется, но не интегрируется (упал import-smoke gate или цикл застрял в bookkeeping-лейнах) | journal моста: `smoke: FAIL` / отсутствие `integrate:` |
| Лейны только `record-reward` / `synthesize` / `verify`, нет `materialize` | петля тратит циклы на бухгалтерию, не реализует | `feedback_decision.mode` в серии reports |
| Одна и та же цель повторяется много раз | skip-done не сработал (ничего не интегрировалось → цель не помечена done) | `git log --grep="<goal title>"` в рабочем репо |
| `result_status=blocked`, `terminal_reason=local_executor_unavailable` | это заглушка координатора, НЕ реальный результат | мост должен подхватить запрос для реального LLM-выполнения |

Эти режимы — причина, по которой материальный прогресс измеряется по движению
`origin/main`, а не по числу циклов.

---

## 4. Принцип при добавлении нового поведения

Любое новое поведение рантайма должно оставлять **durable-след**, который можно
прочитать без доступа к процессу: запись в `state/reports/`, поле в артефакте, или
строку в journal. Если изменение нельзя наблюдать постфактум из состояния — оно
нарушает принцип прозрачности и должно сначала получить наблюдаемость.
