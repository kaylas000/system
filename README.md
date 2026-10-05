# system — Agentic Platform

Платформа автоматической генерации проектов на LangGraph: ядро не зависит от предметной области, вертикали (SaaS Web и др.) подключаются как плагины.

- **ТЗ:** [`TECHNICAL_SPECIFICATION.md`](TECHNICAL_SPECIFICATION.md) — все 7 частей дословно (не редактируется).
- **Спецификации по разделам:** [`specs/`](specs/) — раскладка ТЗ по файлам (`scripts/split_spec.py`).
- **Дефекты ТЗ и принятые решения:** [`specs/ISSUES.md`](specs/ISSUES.md).
- **CI:** [`ci/kernel-ci.yml`](ci/kernel-ci.yml) — проверки, интеграция с Postgres, линт деплоя, сборка образов + Trivy, установка в kind. Чтобы включить, скопируйте в `.github/workflows/` (у токена агента нет права на изменение workflow).

## Статус

| Фаза | Раздел ТЗ | Статус |
|---|---|---|
| 0 | Раскладка `specs/`, `ISSUES.md`, CI | ✅ |
| 1 | Kernel: state, протоколы, граф, HITL, чекпойнты | ✅ |
| 2 | Infra: песочницы (E2B/Docker), инструменты, LLM-шлюз, бюджет | ✅ (проверено на моках; с реальными E2B и LLM не запускалось — нет ключей. LSP/RAG-инструменты отложены) |
| 3 | Skills: движок скиллов, реестр, загрузчик вертикалей | ✅ |
| 5 | Вертикаль SaaS Web — до первого собранного проекта | ✅ скиллы и гейты (проверено `next build`); запуск с LLM ждёт ключей |
| 4 | Knowledge (RAG): парсинг кода, Qdrant, граф кода, поиск для planner/coder/fixer | ✅ (проверено на тестовом репо офлайн-эмбеддером; с реальными эмбеддингами/LLM-обогащением не запускалось — нет ключей) |
| 6 | Ops: CI/CD, наблюдаемость, бюджет, HITL API, деплой | ✅ метрики, трейсы, JSON-логи, бюджет (run/день/токены в минуту), отчёт о расходах, API run и HITL + WebSocket, Docker/compose/Helm/Terraform. Helm проверен `helm lint/template` и схемой k8s; Docker, kind, Trivy, `terraform plan` — только в CI (здесь нет Docker и реестров) |
| 7 | Gateway: API, роутер, оркестратор, auth, CLI | ✅ `/v1` API (ключи API и JWT, роли, лимиты по тарифу, идемпотентность, SSE, webhooks), выбор вертикали, композиция нескольких вертикалей с общими контрактами, CLI `autogen`. Проверено тестами на фейковых вертикалях: реальная вертикаль пока одна (`saas_web`), с LLM не запускалось — нет ключей (ISSUES G-11) |

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
  llm/                LiteLLMClient (прямые провайдеры или OpenAI-совместимый шлюз: OmniRoute / LiteLLM proxy), CostTracker
  sandbox/            E2BSandbox, DockerSandbox, LocalSandbox (только dev/тесты, без изоляции),
                      SandboxManager (квота, TTL) и фабрика create_sandbox(settings)
  tools/              filesystem, shell (ShellPolicy), git, ToolRegistry, default_tool_registry
  skills/             skill.yaml + шаблоны + хуки, SkillExecutor, гейты, SkillRegistry,
                      VerticalLoader, GenericVertical (вертикаль без своего кода)
  prompts/            PromptCompiler + промпты по умолчанию (PLANNER/CODER/FIXER/DOCUMENTER)
  observability/      Prometheus-метрики, OpenTelemetry-трейсы, JSON-логи, обёртки MeteredLLM/MeteredSandbox
  llm/budget.py       BudgetManager: лимит на run (до вызова), на день (SQLite), токены в минуту, алерты
  llm/cost_report.py  autogen-costs — отчёт о расходах по дням / моделям / вертикалям / run
  service/runs.py     RunManager: run в фоне, очередь, шина событий
  api/                FastAPI: /health, /metrics, /runs/{id}, /runs/{id}/logs, /runs/{id}/interrupt, WS /ws/runs/{id}
  main.py             python -m kernel.main serve | check | migrate | keys | token
  gateway/            публичный API /v1 (фаза 7):
    app.py            generate, runs, SSE, HITL под /v1, compositions, X-Request-ID, webhooks
    router.py         выбор вертикали: явная → ключевые слова → tech_stack → LLM → по умолчанию
    auth.py           ключи API / JWT, роли viewer < developer < admin, лимиты rpm / rpd / concurrent
    store.py          SQLite: ключи (SHA-256), реестр run, идемпотентность, композиции
    composer.py       композиция: план LLM → контракты shared/ → подпроекты по уровням DAG → архив
    cli.py            autogen — клиент командной строки
  knowledge/          база знаний (RAG):
    ingestion/        parsers (tree-sitter: TS/TSX/JS, Python, Go), chunking + граф, enrichment (LLM),
                      embedding (litellm / офлайн hashing, BM25 sparse), pipeline (инкрементальный)
    storage/          qdrant_store (dense + BM25, RRF), graph_store (SQLite: вызовы, импорты, наследование)
    retrieval/        engine (coding / planning / fixing), reranking (эвристика / LLM / cross-encoder)
    cli.py            autogen-knowledge ingest | reindex | search | callers | stats | delete
tests/kernel/         unit + e2e на фейковом LLM; E2B, docker и litellm замоканы
verticals/saas_web/   вертикаль Next.js + tRPC + Prisma + Auth.js (см. verticals/saas_web/README.md)
tests/verticals/      загрузка вертикали, хелперы хуков, парсеры, рендер цепочки скиллов
tests/knowledge/      парсеры, чанки, Qdrant, граф, ingestion → retrieval, подключение к графу ядра
tests/gateway/        роутер, auth и лимиты, API /v1, композиция, CLI
tests/ops/            метрики, трейсы, бюджет, API и WebSocket, конфиг LiteLLM, статические проверки деплоя
deploy/               Dockerfile, docker-compose, Helm, Terraform AWS, LiteLLM, Prometheus, Grafana (deploy/README.md)
docs/ops/             LangSmith, кэширование, песочницы в эксплуатации
scripts/e2e_saas_skills.py  полная сборка Todo-приложения скиллами без LLM (npm install, гейты, next build)
```

## Настройка (переменные окружения)

```bash
# LLM через OpenAI-совместимый шлюз: OmniRoute (http://<host>:20128/v1) или LiteLLM proxy (http://litellm:4000).
# Без шлюза — прямые провайдеры (ANTHROPIC_API_KEY, OPENAI_API_KEY, модели вида anthropic/claude-...).
AUTOGEN_LLM__GATEWAY_URL=http://localhost:20128/v1
AUTOGEN_LLM__API_KEY=...                 # ключ шлюза (OmniRoute: Dashboard -> Endpoints)
AUTOGEN_LLM__MODEL_ALIASES='{"*": "auto/coding", "router/classifier": "auto/fast"}'   # имена ядра -> модели шлюза
AUTOGEN_LLM__EXTRA_HEADERS='{"x-omniroute-compression": "off", "x-omniroute-no-memory": "true"}'
python -m kernel.main llm-check          # по одному короткому вызову на каждую модель: ключ и алиасы работают?
AUTOGEN_SANDBOX__PROVIDER=e2b            # e2b | docker | local
AUTOGEN_SANDBOX__API_KEY=...             # ключ E2B
AUTOGEN_SANDBOX__E2B_DEFAULT_TEMPLATE=... # шаблон E2B с node/pnpm (иначе базовый)
```

База знаний (по умолчанию выключена; нужен `pip install -e ".[knowledge]"`):

```bash
AUTOGEN_VECTOR_DB__QDRANT_URL=http://qdrant:6333   # без него — встроенный Qdrant в .data/qdrant
AUTOGEN_KNOWLEDGE__EMBEDDER=litellm                # litellm (text-embedding-3-small, 768) | hashing (офлайн)
AUTOGEN_KNOWLEDGE__ENRICHMENT_MODEL=router/enricher

autogen-knowledge ingest https://github.com/org/repo --name my_repo   # или локальная папка; --no-enrich без LLM
autogen-knowledge search "stream a server response" --context
autogen-knowledge callers getUser
```

Чтобы агенты получали найденное в промптах, передайте движок в граф:
`build_graph(..., retriever=build_knowledge_base(settings, llm).engine)` (`kernel.knowledge.factory`).
Поиск ограничивается репозиториями из `rag_collections` манифеста вертикали, если они проиндексированы.

Лимит расходов на run — `GenerateRequest.max_budget_usd`; при превышении run встаёт на паузу `budget_exceeded`, продолжить — `{"action": "approve", "edited_data": {"max_budget_usd": 5}}`.

Общие лимиты и алерты:

```bash
AUTOGEN_BUDGET__MAX_COST_USD_PER_DAY=20      # все run вместе; при достижении новые вызовы LLM отклоняются
AUTOGEN_BUDGET__MAX_COST_USD_PER_RUN=5       # для запросов без max_budget_usd
AUTOGEN_BUDGET__MAX_TOKENS_PER_MINUTE=400000
AUTOGEN_BUDGET__ALERT_WEBHOOK_URL=https://hooks.slack.com/...   # предупреждение на 80 %
autogen-costs --days 7                       # отчёт о расходах
```

## Сервис

```bash
pip install -e ".[ops,llm,postgres]"
python -m kernel.main serve --port 8000      # API: /docs, метрики: /metrics
```

Все маршруты API — под `/v1`, с авторизацией (кроме `/health`, `/metrics`, `/docs`). Ключ API:

```bash
python -m kernel.main keys create --tenant acme --user alice --role developer   # ключ показывается один раз
python -m kernel.main keys list | keys revoke <префикс>
python -m kernel.main token --tenant acme --user alice    # JWT HS256 (нужен AUTOGEN_GATEWAY__JWT_SECRET)
AUTOGEN_GATEWAY__AUTH_ENABLED=false                       # только для локальной отладки
```

| Метод | Путь | Что делает |
|---|---|---|
| POST | `/v1/generate` | запуск: 202 + `Location`; заголовок `Idempotency-Key`; без `vertical_id` вертикаль выбирается автоматически |
| GET | `/v1/verticals`, `/v1/verticals/{id}` | доступные вертикали |
| GET | `/v1/runs`, `/v1/runs/{id}` | список run тенанта, статус |
| GET | `/v1/runs/{id}/logs` | лог: JSON или SSE (`Accept: text/event-stream`, `Last-Event-ID`) |
| GET | `/v1/runs/{id}/events` | SSE: узлы графа, пауза, завершение |
| GET / POST | `/v1/runs/{id}/interrupt` | пауза для решения человека / решение `{"action", "comment", "edited_data"}` |
| DELETE | `/v1/runs/{id}` | отменить |
| GET | `/v1/runs/{id}/artifact` | архив готового проекта |
| WS | `/v1/ws/runs/{id}` | живые события (`?access_token=` для браузера) |
| POST | `/v1/compositions` | система из нескольких вертикалей; `plan` — готовый план, `dry_run` — только план |
| GET / DELETE | `/v1/compositions/{id}` (+ `/events`, `/artifact`) | статус, SSE, отмена, общий архив |

Лимиты по тарифу (`free` / `pro` / `enterprise`, `AUTOGEN_GATEWAY__TIERS`): запросов в минуту, генераций в сутки,
одновременных run; при превышении — 429 и `Retry-After`. Для нескольких процессов лимиты хранятся в Redis
(`AUTOGEN_GATEWAY__REDIS_URL`). `webhook_url` получает POST при остановке run (подпись `X-Autogen-Signature`,
если задан `AUTOGEN_GATEWAY__WEBHOOK_SECRET`).

CLI (`pip install -e ".[cli]"`):

```bash
export AUTOGEN_API_URL=http://localhost:8000 AUTOGEN_API_KEY=agk_acme_...
autogen verticals
autogen generate "Todo app with auth" --watch --download ./out   # на паузах спрашивает решение
autogen compose "Next.js frontend + billing API + Terraform" --dry-run --save-plan plan.json
autogen compose --plan plan.json --watch --download ./out
```

Развёртывание — [`deploy/README.md`](deploy/README.md).

## Разработка

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,knowledge,ops,llm,cli]" # + ".[postgres]", ".[e2b]"

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
