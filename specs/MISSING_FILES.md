# Файлы, которых нет в ТЗ

Файлы из этого списка упоминаются в `TECHNICAL_SPECIFICATION.md`: в дереве каталогов, в коде или в манифесте. Но **текста у них в ТЗ нет**. Список получен автоматически: дерево каждой части сравнено с тем, что извлекается в `specs/`, и дополнено упоминаниями в коде.

**Приоритет:**
- **A** — нужен для первого собранного проекта (тонкий срез Kernel → Infra → Skills → SaaS Web);
- **B** — нужен позже.

**Решение** — отметьте вариант:
- **Я** — вы присылаете текст;
- **Агент** — пишу я, в заголовке файла будет пометка «написано агентом, в ТЗ отсутствует».

Колонка «Что о файле известно из ТЗ» — это всё, что о нём сказано в ТЗ. Остальное придётся додумывать.

## Часть 2 — Infra

| # | Файл | Пр. | Что о файле известно из ТЗ | Решение |
|---|---|---|---|---|
| 1 | `02_infra/llm_gateway/COST_TRACKER.py` | A | Только имя в дереве (стр. 1107) | ☐ Я ☐ Агент |
| 2 | `02_infra/sandbox/PROVIDERS.md` | B | Только имя в дереве (стр. 1097) | ☐ Я ☐ Агент |

`DOCKERFILE.sandbox` в этот список **не входит**: его текст есть в ТЗ внутри `SANDBOX_API.md` (варианты `.base` и `.saas_web`, стр. 1153–1222). Вынесу его сам, без изменений.

## Часть 3 — Skills

| # | Файл | Пр. | Что о файле известно из ТЗ | Решение |
|---|---|---|---|---|
| 3 | `kernel/vertical/DEFAULT_VERTICAL.py` (`GenericVertical`) | A | Импортируется в `VERTICAL_LOADER.py` (стр. 3484); в инструкции сказано, что это базовая реализация `IVertical` (стр. 3893) | ☐ Я ☐ Агент |
| 4 | `03_skills/examples/init_nextjs_app_router/templates/tsconfig.json.j2` | A | Имя в дереве (стр. 2500) | ☐ Я ☐ Агент |
| 5 | `03_skills/examples/init_nextjs_app_router/templates/src/app/layout.tsx.j2` | A | Имя в дереве (стр. 2501) | ☐ Я ☐ Агент |
| 6 | `03_skills/registry/MARKETPLACE_CLIENT.py` | B | Только имя в дереве (стр. 2494) | ☐ Я ☐ Агент |

## Часть 4 — Knowledge

| # | Файл | Пр. | Что о файле известно из ТЗ | Решение |
|---|---|---|---|---|
| 7 | `04_knowledge/ingestion/EMBEDDING.py` | B | Только имя в дереве (стр. 3935) | ☐ Я ☐ Агент |
| 8 | `04_knowledge/retrieval/HYBRID_SEARCH.py` | B | Только имя в дереве (стр. 3943) | ☐ Я ☐ Агент |
| 9 | `04_knowledge/retrieval/GRAPH_TRAVERSAL.py` | B | Только имя в дереве (стр. 3944) | ☐ Я ☐ Агент |

## Часть 5 — Вертикаль SaaS Web

Каждый скилл — это папка с `skill.yaml`, `hooks.py` и `templates/` (формат задан в части 3). Для каждого скилла в `skills/CATALOG.md` есть одна строка: зависимости, что делает скилл и какие файлы создаёт. Полный пример в ТЗ есть только для `add_trpc_router`.

| # | Файл / папка | Пр. | Что о файле известно из ТЗ | Решение |
|---|---|---|---|---|
| 10 | `skills/init_prisma_postgres/` | A | Строка в `CATALOG.md` | ☐ Я ☐ Агент |
| 11 | `skills/add_nextauth_credentials/` | A | Строка в `CATALOG.md` | ☐ Я ☐ Агент |
| 12 | `skills/add_shadcn_ui/` | A | Строка в `CATALOG.md` | ☐ Я ☐ Агент |
| 13 | `skills/add_dockerfile_prod/` | A | Строка в `CATALOG.md` | ☐ Я ☐ Агент |
| 14 | `skills/add_stripe_billing/` | B | Строка в `CATALOG.md` | ☐ Я ☐ Агент |
| 15 | `skills/add_github_actions_ci/` | B | Строка в `CATALOG.md` | ☐ Я ☐ Агент |
| 16 | `skills/add_playwright_e2e/` | B | Строка в `CATALOG.md` | ☐ Я ☐ Агент |
| 17 | `skills/add_admin_dashboard/` | B | Строка в `CATALOG.md` | ☐ Я ☐ Агент |
| 18 | `skills/init_trpc_setup/` | A | Указан в `depends_on` у `add_trpc_router` (стр. 6318), больше нигде не упоминается. Можно не писать, а убрать эту зависимость | ☐ Я ☐ Агент ☐ Убрать зависимость |
| 19 | `prompts/FIXER.j2` | A | Путь в `MANIFEST.yaml` (стр. 5627), вызов в `VERTICAL_IMPL.py` (стр. 5727) | ☐ Я ☐ Агент |
| 20 | `prompts/DOCUMENTER.j2` | A | Путь в `MANIFEST.yaml` (стр. 5629) | ☐ Я ☐ Агент |
| 21 | `prompts/REVIEWER.j2` | B | Путь в `MANIFEST.yaml` (стр. 5628) | ☐ Я ☐ Агент |
| 22 | `prompts/compiler.py` (`PromptCompiler`) | A | Упомянут в инструкции (стр. 6653) | ☐ Я ☐ Агент |
| 23 | `prompts/examples/planner_saas.jsonl` | B | Путь в `MANIFEST.yaml` (стр. 5606) | ☐ Я ☐ Агент |

`init_nextjs_app_router` есть в части 3 (`03_skills/examples/`), в этот список он не входит.

## Часть 6 — Ops

| # | Файл | Пр. | Что о файле известно из ТЗ (комментарий в дереве) | Решение |
|---|---|---|---|---|
| 24 | `ci_cd/RELEASE_WORKFLOW.yml` | B | «Semantic Release / Changelog» | ☐ Я ☐ Агент |
| 25 | `ci_cd/SECURITY_SCAN.yml` | B | «SAST/DAST/Deps Scan» | ☐ Я ☐ Агент |
| 26 | `observability/LANGSMITH_SETUP.md` | B | — | ☐ Я ☐ Агент |
| 27 | `observability/METRICS.py` | B | «Prometheus Metrics Definitions» | ☐ Я ☐ Агент |
| 28 | `observability/LOGGING_CONFIG.py` | B | «Structured Logging (JSON)» | ☐ Я ☐ Агент |
| 29 | `cost_control/CACHE_STRATEGY.md` | B | «Prompt/Embedding/Response Caching» | ☐ Я ☐ Агент |
| 30 | `cost_control/COST_REPORTER.py` | B | «Daily/Run Cost Reports» | ☐ Я ☐ Агент |
| 31 | `hitl/WEBSOCKET_MANAGER.py` | B | «Real-time Log/State Streaming» | ☐ Я ☐ Агент |
| 32 | `hitl/UI_COMPONENTS.md` | B | «React Components Spec (for Frontend Team)» | ☐ Я ☐ Агент |
| 33 | `hitl/APPROVAL_WORKFLOW.py` | B | «Approve/Edit/Abort Logic» (частично уже реализовано в ядре, `human_review`) | ☐ Я ☐ Агент |
| 34 | `deployment/DOCKERCOMPOSE.yml` | B | «Local Dev Stack (Kernel, DBs, Sandboxes)» | ☐ Я ☐ Агент |
| 35 | `deployment/HELM_CHART/Chart.yaml` | B | — (есть `values.yaml`) | ☐ Я ☐ Агент |
| 36 | `deployment/HELM_CHART/templates/` | B | — | ☐ Я ☐ Агент |
| 37 | `deployment/TERRAFORM/modules/` | B | — (есть `main.tf`) | ☐ Я ☐ Агент |
| 38 | `deployment/TERRAFORM/environments/` | B | — | ☐ Я ☐ Агент |
| 39 | `deployment/SANDBOX_DEPLOYMENT.md` | B | «E2B/Daytona/Modal Scaling Config» | ☐ Я ☐ Агент |
| 40 | `scripts/DEPLOY.sh` | B | — | ☐ Я ☐ Агент |
| 41 | `scripts/MIGRATE_DB.sh` | B | — | ☐ Я ☐ Агент |
| 42 | `scripts/SEED_KNOWLEDGE.sh` | B | — | ☐ Я ☐ Агент |

## Часть 7 — Gateway

| # | Файл | Пр. | Что о файле известно из ТЗ | Решение |
|---|---|---|---|---|
| 43 | `api/ROUTES.py` | B | Эндпоинты описаны в `OPENAPI_SPEC.yaml` | ☐ Я ☐ Агент |
| 44 | `api/MIDDLEWARE.py` | B | Только имя в дереве | ☐ Я ☐ Агент |
| 45 | `api/WEBSOCKETS.py` | B | Только имя в дереве | ☐ Я ☐ Агент |
| 46 | `router/ROUTER.py` | B | Только имя в дереве (классификатор `CLASSIFIER.py` есть) | ☐ Я ☐ Агент |
| 47 | `router/CAPABILITY_MATCHER.py` | B | Только имя в дереве | ☐ Я ☐ Агент |
| 48 | `orchestrator/CONTRACT_RESOLVER.py` | B | Только имя в дереве | ☐ Я ☐ Агент |
| 49 | `orchestrator/DEPENDENCY_GRAPH.py` | B | Только имя в дереве | ☐ Я ☐ Агент |
| 50 | `auth/QUOTAS.py` | B | Только имя в дереве | ☐ Я ☐ Агент |
| 51 | `auth/API_KEYS.py` | B | Только имя в дереве | ☐ Я ☐ Агент |
| 52 | `cli/CONFIG.yaml` | B | Только имя в дереве | ☐ Я ☐ Агент |

---

**Итого:** 52 позиции. Приоритет A у 12 из них: 1, 3, 4, 5, 10–13, 18, 19, 20, 22. Остальные нужны только для поздних фаз.
