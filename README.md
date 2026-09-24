# system — Agentic Platform

Платформа автоматической генерации проектов на LangGraph: ядро не зависит от предметной области, вертикали (SaaS Web и др.) подключаются как плагины.

- **ТЗ:** [`TECHNICAL_SPECIFICATION.md`](TECHNICAL_SPECIFICATION.md) — все 7 частей дословно (не редактируется).
- **Спецификации по разделам:** [`specs/`](specs/) — раскладка ТЗ по файлам (`scripts/split_spec.py`).
- **Дефекты ТЗ и принятые решения:** [`specs/ISSUES.md`](specs/ISSUES.md).
- **CI:** [`ci/kernel-ci.yml`](ci/kernel-ci.yml) — чтобы включить, скопируйте в `.github/workflows/` (у токена агента нет права на изменение workflow).

## Статус

| Фаза | Раздел ТЗ | Статус |
|---|---|---|
| 0 | Раскладка `specs/`, `ISSUES.md`, CI | ✅ |
| 1 | Kernel: state, протоколы, граф, HITL, чекпойнты | ✅ (LocalSandbox и тестовый LLM; продовые адаптеры — фаза 2) |
| 2 | Infra: песочницы (E2B/Docker), инструменты, LLM-шлюз | ⏳ следующая |
| 3 | Skills: движок скиллов, реестр, загрузчик вертикалей | — |
| 5 | Вертикаль SaaS Web — до первого собранного проекта | — |
| 4 | Knowledge (RAG) | — |
| 6 | Ops: CI/CD, наблюдаемость, бюджет, HITL API, деплой | — |
| 7 | Gateway: API, роутер, оркестратор, auth, CLI | — |

Порядок: сначала «тонкий срез» Kernel → Infra → Skills → SaaS Web до первого рабочего результата, затем Knowledge, Ops, Gateway.

## Структура ядра

```
kernel/
  state.py            AgentState, модели задач/гейтов/артефактов, validate_dag
  protocols.py        ILLMClient, ISandbox, ITool, IVertical, IVerificationGate, ...
  config.py           Settings (env AUTOGEN_*, разделитель __)
  runner.py           start_run / resume_run / pending_interrupts
  graph/
    builder.py        build_graph(): узлы, рёбра, обработка ошибок узлов
    routing.py        чистые функции условных рёбер
    deps.py           внедрение зависимостей через config["configurable"]
    nodes/            initialize, planner, get_next_task, coder, verifier, fixer,
                      documenter, packager, human_review
  persistence/        чекпойнтер (InMemory / Postgres) и allowlist сериализации
  sandbox/local.py    LocalSandbox — только для разработки и тестов, без изоляции
  tools/registry.py   ToolRegistry
tests/kernel/         unit + e2e на фейковом LLM
```

## Разработка

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"          # + ".[postgres]" для AsyncPostgresSaver

ruff check . && ruff format --check .
mypy kernel
pytest -q
python scripts/lint_specs.py     # отчёт о синтаксических ошибках в specs/
```

Пример запуска графа (с любой реализацией `IVertical` / `ILLMClient` / `ISandbox`):

```python
from kernel.graph import build_graph
from kernel.persistence import memory_checkpointer
from kernel.protocols import GenerateRequest
from kernel.runner import resume_run, start_run

graph = build_graph(vertical, llm=llm, sandbox=sandbox, checkpointer=memory_checkpointer())
run_id, state = await start_run(graph, GenerateRequest(prompt="..."))
if state["status"] == "needs_human_input":
    state = await resume_run(graph, run_id, {"action": "retry"})
```
