# Scout Report: Issue #1775 — Iteration Budget Awareness in Executor Loop

**Date:** 2026-09-23  
**Target:** Issue #1775 ("исполнителю один раз сообщают бюджет итераций и никогда не говорят, где он в нём находится")  
**Branch:** `scout/1775-iteration-budget-awareness`  

---

## 1. Где исполнитель получает бюджет итераций и как он упоминается в промпте

### Резолвинг бюджета
Бюджет вычисляется централизованно через `nanobot.runtime.model_registry.resolve_max_tool_iterations(config_fallback)`:
- Переменная окружения `SELFEVO_MAX_TOOL_ITERATIONS` (если задана валидным положительным int) переопределяет значение из конфигурации `config.agents.defaults.max_tool_iterations` (#578, #906).
- В `nanobot/runtime/bridge.py` (строка ~3612):
  ```python
  resolved_iterations = resolve_max_tool_iterations(config.agents.defaults.max_tool_iterations)
  ```
- Значение `resolved_iterations` передается:
  1. В `build_task(..., max_iterations=resolved_iterations)` для формирования текста задачи (`task`).
  2. В `SubagentManager(..., max_iterations=resolved_iterations)`.

### Упоминание в промпте
Исполнитель видит бюджет в двух местах:
1. **В теле задачи (`user` message)** через `build_task()` (`nanobot/runtime/bridge.py:2388`):
   ```python
   f'Iteration budget this cycle: {max_iterations} tool iterations.'
   ```
   Это ровно одна строка в конце секции инструкций задачи.
2. **В блоке `## Position` системного промпта (`system` message)** через `ContextBuilder.build_system_prompt()` (#1793 / ADR-026):
   ```markdown
   ## Position
   Step 1 of 30.
   Cycle: <cycle_id>.
   Day: <day>.
   ```
   В начале цикла итерация инициализируется как `Step 1 of <max_iterations>.`.

---

## 2. Цикл исполнителя и место инкремента итерации

Цикл исполнителя живет в `SubagentManager._run_subagent()` (`nanobot/agent/subagent.py:460-520`):
```python
max_iterations = self.max_iterations
iteration = 0
...
while iteration < max_iterations:
    ...
    iteration += 1
```

### Точка для сообщения о позиции
Точка уже существует и используется!
В строках 480-489 `nanobot/agent/subagent.py`:
```python
# #1793 (ADR-026): patch the step line of the already-built
# system prompt in place for this turn -- cheap (a single
# regex substitution, no re-read of any file) and reads the
# SAME `iteration`/`max_iterations` this loop's own
# condition tests, so the two can never diverge.
if messages and messages[0].get("role") == "system":
    from nanobot.agent.context import ContextBuilder as _CtxBuilder

    messages[0]["content"] = _CtxBuilder.update_step_position(
        str(messages[0].get("content") or ""), iteration, max_iterations,
    )
```

Каждый ход перед вызовом LLM `messages[0]["content"]` перезаписывается: строка `Step X of Y.` заменяется регулярным выражением на текущие `iteration` и `max_iterations`.

---

## 3. Анализ #1850 и #1793: что уже есть, а чего нет

### Что уже сделано:
1. **#1850 (`bd3760a1`)**:
   - Пишет `iterations_used`, `iterations_limit`, `iteration_fraction` в `ledger/cycles.jsonl` **после** завершения цикла.
   - Это исключительно пост-фактум телеметрия для внешнего анализа и отчетов. Исполнитель во время работы эти поля не видит.
2. **#1793 / ADR-026 (`9581ac1e`, PR #1808)**:
   - Внедрил блок `## Position` в `system_prompt`.
   - Внедрил метод `ContextBuilder.update_step_position(system_prompt, iteration, max_iterations)`.
   - Встроил динамическое обновление строки `Step {iteration} of {max_iterations}.` в `messages[0]` на каждом шаге цикла `_run_subagent`.

### Чего НЕТ из постановки #1775:
1. **В пользовательских сообщениях / tool_result**:
   - Позиция обновляется **только в `messages[0]` (system prompt)**.
   - Многие LLM обращают гораздо больше внимания на последние сообщения в контексте (recent user/tool turns), чем на изменение одной цифры в длинном system prompt (где 15 000+ символов онтологии и инструкций).
   - В #1775 ставился вопрос: «исполнителю один раз сообщают бюджет итераций и никогда не говорят, где он в нём находится». На самом деле в system prompt позиция обновляется, но модель не получает явного напоминания в потоке диалога (например, `[Step 18/20: 2 steps remaining]`).
2. **Предупреждения о приближении к лимиту (critical threshold / deadline warning)**:
   - Для planner (#1893) на последнем шаге добавляется явный user prompt:
     `"This is your final planning turn. Do not call tools..."`
   - Для **исполнителя (executor)** такого механизма нет вообще: на шаге `max_iterations` исполнитель может снова вызвать tool call и быть оборван по лимиту без возможности финализировать результат или зафиксировать выводы.

---

## 4. Оценка риска токенов (Token Cost)

1. **Если позиция обновляется только в `system` prompt (как сейчас по #1793)**:
   - Прирост токенов = **0 токенов**. Заменяется `Step 1 of 30` на `Step 2 of 30` (длина строки неизменна, ~5 токенов).
   - *Нюанс*: для провайдеров с префиксным кэшированием (prompt caching) изменение `system` prompt на каждом ходе инвалидирует кэш system prompt, если кэширование идет с 0-го токена.
2. **Если передавать позицию в хвосте диалога (в tool_result или отдельном user message)**:
   - Строка вида `[Step 12/30 | Remaining: 18]` занимает ~8-12 токенов.
   - За цикл из 20-30 итераций кумулятивный прирост в истории:
     $\sum_{i=1}^{N} 10 \approx 10 \times \frac{N(N+1)}{2}$ токенов всего контекста за цикл.
     При $N=20$: $\approx 2 100$ суммарных токенов за весь цикл.
   - В терминах затрат: это пренебрежимо мало (< 0.1% от типичного контекста цикла в 100k-300k токенов).
3. **Рекомендация по минимализму**:
   - Либо инжектировать краткий префикс/суффикс `[Step i/N]` в `tool_result`.
   - Либо подавать warning только на пороге исчерпания (например, при оставшихся 3 и 1 шагах: `N-2`, `N-1`, `N`), по аналогии с #1893.
