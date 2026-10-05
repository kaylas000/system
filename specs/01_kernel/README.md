# Ответ №1: Kernel Spec — Спецификация Ядра Платформы

Это **фундамент**. Весь остальной код (вертикали, скиллы, инструменты) будет реализовывать протоколы, определенные здесь, и оперировать стейтом, описанным ниже.

Сохрани эти файлы в структуру:
```text
specs/01_kernel/
├── ARCHITECTURE.md
├── state.py
├── graph_topology.md
└── protocols.py
```

---


<!-- ... файлы спецификации лежат в этой папке ... -->

### 🎯 Инструкция для Агента-Разработчика (Prompt Snippet)

> **CONTEXT FOR NEXT STEP (Infra Spec):**
> Ты реализуешь `specs/01_kernel/`.
> 1. Создай пакет `kernel/` с файлами: `config.py`, `state.py`, `protocols.py`.
> 2. Реализуй `graph/builder.py`: функция `build_graph(vertical: IVertical) -> CompiledStateGraph`.
>    *   Используй `StateGraph(AgentState)`.
>    *   Добавь ноды-заглушки (raise NotImplementedError) для: `initialize`, `planner`, `get_next_task`, `coder`, `verifier`, `fixer`, `documenter`, `packager`, `human_review`.
>    *   Подключи `routing.py` логику через `add_conditional_edges`.
>    *   Настрой `PostgresSaver` чекпоинтер.
> 3. Напиши `kernel/graph/routing.py` ровно как в спеке.
> 4. Напиши `kernel/tools/registry.py`: класс `ToolRegistry` с методами `register(tool: ITool)`, `execute(name, args, state)`.
> 5. **Тест:** `tests/kernel/test_graph_compilation.py` — проверяет, что граф компилируется без ошибок, имеет все ноды и ребра из `graph_topology.md`.

---

### ✅ Чек-лист готовности Кернела (Definition of Done для Ответа №1)

- [ ] `state.py` проходит `mypy --strict` и `pydantic` валидацию на примере JSON.
- [ ] `protocols.py` не имеет зависимостей от реализаций (нет импортов `e2b`, `qdrant`, `langchain`).
- [ ] `graph_topology.md` покрывает все циклы (Fix Loop, Task Loop, Human Loop).
- [ ] `ARCHITECTURE.md` содержит актуальные Mermaid диаграммы.
- [ ] Готов к внедрению `verticals/saas_web/` (следующий шаг).

---

---
