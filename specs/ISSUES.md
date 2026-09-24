# ISSUES — дефекты ТЗ, пробелы и принятые решения

`TECHNICAL_SPECIFICATION.md` и файлы в `specs/` хранятся **дословно**. Все исправления делаются в коде реализации; здесь зафиксировано, *что* в ТЗ не так и *какое решение* принято.

Статусы: **fixed** — исправлено в реализации; **open** — будет исправлено в соответствующей фазе; **decision** — проектное решение/отклонение от ТЗ.

Синтаксические проблемы воспроизводятся командой `python scripts/lint_specs.py`.

---

## 1. Синтаксис (проверено `scripts/lint_specs.py`)

| ID | Файл(ы) | Проблема | Статус |
|---|---|---|---|
| S-01 | 16 файлов частей 3–7: `03_skills/registry/VERTICAL_LOADER.py`, `04_knowledge/cli/INGEST_CLI.py`, `04_knowledge/ingestion/{ENRICHMENT,INGESTION_PIPELINE}.py`, `04_knowledge/retrieval/{RERANKING,RETRIEVAL_STRATEGIES}.py`, `04_knowledge/storage/KUZU_CLIENT.py`, `05_vertical_saas_web/VERTICAL_IMPL.py`, `05_vertical_saas_web/evals/RUN_EVALS.py`, `05_vertical_saas_web/skills/add_trpc_router/hooks.py`, `05_vertical_saas_web/verification/PARSERS.py`, `06_ops/cost_control/BUDGET_MANAGER.py`, `06_ops/hitl/HITL_API.py`, `07_gateway/auth/AUTH_MANAGER.py`, `07_gateway/orchestrator/CROSS_VERTICAL.py`, `07_gateway/router/CLASSIFIER.py` | `from typing: ...` вместо `from typing import ...` — `SyntaxError` | open (исправляется при переносе каждого файла) |
| S-02 | `06_ops/ci_cd/KERNEL_CI.yml:38`, `06_ops/ci_cd/GENERATED_PROJECT_CI.yml:29` | `with: { python-version: ${{ env.X }} }` — `${{` внутри flow-mapping YAML невалиден | open (фаза 6); `ci/kernel-ci.yml` написан без этой ошибки |
| S-03 | `01_kernel/protocols.py` | `Literal` используется без импорта | fixed (`kernel/protocols.py`) |

## 2. Kernel (часть 1) — решения фазы 1

| ID | Где в ТЗ | Проблема | Решение | Статус |
|---|---|---|---|---|
| K-01 | `state.py` `TokenUsage` | `model_name` обязателен → `TokenUsage()` падает; `calculate_total_tokens` содержит мёртвый код | `model_name=""` по умолчанию; `add()` объединяет имена моделей через `+` | fixed |
| K-02 | `state.py` `AgentState` | Нет `sandbox_id`, хотя его читают `TOOL_REGISTRY.py` и вертикаль | Поле `sandbox_id` добавлено | fixed |
| K-03 | `state.py` | Нет полей для HITL-протокола | Добавлены `interrupt_type`, `human_decision`, `next_after_human`, `failed_node`, enum `InterruptType` | fixed |
| K-04 | `state.py` | `datetime.now()` без таймзоны | `utcnow()` (UTC) | fixed |
| K-05 | `state.py` / `graph_topology.md` | `validate_dag` — заглушка | Алгоритм Кана: дубли, неизвестные зависимости, self-dep, циклы | fixed |
| K-06 | `ARCHITECTURE.md` vs `protocols.py` | Расположение протоколов: `kernel/protocols.py` vs `kernel/vertical/protocols.py` | Основной файл `kernel/protocols.py`; `kernel/vertical` реэкспортирует | decision |
| K-07 | `protocols.py` `ISandbox.exec(stream=True)` | Параметр противоречит возвращаемому `CommandResult` | Убран; стриминг — отдельный метод в фазе 2 | decision |
| K-08 | `protocols.py` `VerticalManifest` | Поля (skills/verifier) не совпадают с `05_vertical_saas_web/MANIFEST.yaml` | Модель повторяет формат `MANIFEST.yaml`, `extra="allow"` | decision |
| K-09 | `protocols.py` `IVertical` | Нет `get_fixer_prompt`, хотя `VERTICAL_IMPL.py` его реализует и Fixer он нужен | Метод добавлен в протокол | fixed |
| K-10 | `protocols.py` `LLMResponse`, `IVertical.finalize` | Нет поля для structured output; `finalize` не возвращает изменения state | `LLMResponse.parsed`; `finalize` возвращает частичный dict | decision |
| K-11 | `graph_topology.md` `routing.py` | Роутеры мутируют `state` (LangGraph такие записи теряет); `route_get_next_task` возвращает `"human_review"`, которого нет в `Literal` | Роутеры — чистые функции, все изменения state в узлах | fixed |
| K-12 | `graph_topology.md` | Не описано условное ребро из `human_review`; нет ребра coder→human_review | См. раздел 2.1 | decision |
| K-13 | `ARCHITECTURE.md` `recursion_limit: 50` | При 25 задачах и циклах verify→fix лимит исчерпывается | `kernel.recursion_limit = 100` | decision |
| K-14 | — | Пути `FileChange` от LLM не проверяются (`../../etc/passwd`, абсолютные) | `sanitize_changes()` в узлах + проверка выхода из корня в `LocalSandbox` | fixed |
| K-15 | langgraph-checkpoint 4.x | Десериализация незарегистрированных типов из чекпойнта: предупреждение, в будущем — блокировка | `kernel/persistence/serde.py`: явный allowlist всех типов state; тесты используют его, пропущенный тип ломает CI | fixed |

### 2.1 Отклонения от топологии графа (K-12)

- Каждый узел, кроме `human_review`, обёрнут: необработанное исключение → `interrupt_type=node_error`, `failed_node=<узел>` → `human_review`. Действие `retry` перезапускает упавший узел.
- `skip_gate` помечает текущую задачу `SKIPPED` и ведёт в `get_next_task` (в ТЗ — в `verifier`, что приводило бы к повторному провалу). Зависимые задачи считают `SKIPPED` выполненной.
- `retry`/`edit` после `gate_failure` сбрасывают бюджет исправлений задачи (`retry_count=0`) и ведут в `verifier`.
- План: `approve`/`edit` → `get_next_task`; `reject`/`retry` → `planner` с комментарием человека в промпте. Отредактированный план проходит `validate_dag`; невалидная правка отклоняется, граф продолжает ждать.
- Недопустимое для данного `interrupt_type` действие не меняет состояние — граф снова ждёт решения.
- Project-level гейты (`get_verification_gates(state, None)`) пока не вызываются — подключаются вместе с вертикалью SaaS Web.
- Зависимости (vertical, llm, sandbox, settings, tool_registry) передаются через `config["configurable"]` или умолчания `build_graph(...)`, а не через глобальные объекты.

## 3. Infra (часть 2)

| ID | Где | Проблема | Статус |
|---|---|---|---|
| I-01 | `02_infra/tools/TOOL_REGISTRY.py` | Ссылается на неопределённый `SandboxManager`; импортирует `AgentState` из `kernel.protocols` | fixed (`kernel/tools/registry.py`: `sandbox_manager` необязателен) |
| I-02 | `02_infra/sandbox/SANDBOX_MANAGER.py` | `E2BSandbox` не соответствует протоколу `ISandbox` (методы без `sandbox_id`, `create` — classmethod) | fixed (`kernel/sandbox/e2b.py`: один объект на все песочницы, словарь `id → AsyncSandbox`) |
| I-03 | часть 2 | Нет файлов `PROVIDERS.md`, `COST_TRACKER.py` | `COST_TRACKER` — написан агентом (`kernel/llm/cost_tracker.py`, в ТЗ отсутствует); `PROVIDERS.md` — open |
| I-04 | `02_infra/llm_gateway/LLM_CLIENT.py` | Стоимость и usage берутся из приватного `_raw_response` Instructor; глобальный `settings`; `gateway_url` обязателен | fixed (`kernel/llm/litellm_client.py`): структурированный вывод без Instructor — JSON Schema в промпте + `response_format`, если модель поддерживает, + валидация pydantic и повтор с текстом ошибки (`llm.max_structured_retries`); usage и стоимость суммируются по всем попыткам; `llm.gateway_url = None` → прямые провайдеры |
| I-05 | `02_infra/llm_gateway/LLM_CLIENT.py` | `estimate_tokens` падает без сети (tiktoken качает кодировки) | fixed: откат на оценку «4 символа = 1 токен» |
| I-06 | часть 1/2 | `InterruptType.BUDGET_EXCEEDED` объявлен, но нигде не выставляется; в `human_review` для него только `abort` | fixed: обёртка узла в `kernel/graph/builder.py` после каждого узла сравнивает `token_usage` с `max_budget_usd` и `llm.max_tokens_per_run`; `approve` с `edited_data.max_budget_usd` / `max_tokens_per_run` продолжает run по роутеру узла, на котором сработал лимит (`routing.ROUTERS`) |
| I-07 | `SANDBOX_MANAGER.py::E2BSandbox` | Используются несуществующие в e2b v2 API: `env_vars=`, `files.write_bytes`, `download`, `close`; tar-архив пишется как один файл; ненулевой exit code в e2b — исключение, в ТЗ не обработано; `timeout` в e2b — время жизни песочницы, а не команды | fixed (`kernel/sandbox/e2b.py`, API сверен с e2b 2.51): `AsyncSandbox.create(template, timeout, envs, metadata)`, `files.write_files`, `CommandExitException` → `CommandResult`, `download_dir` через `tar` + `files.read(format="bytes")`; `SandboxSpec.image` → шаблон E2B через `sandbox.e2b_template_map` |
| I-08 | `SANDBOX_MANAGER.py::SandboxManager` | `acquire()` как контекстный менеджер не подходит графу (песочница живёт между узлами и паузами HITL); тёплое переиспользование песочниц между run переносит файлы и секреты; при исчерпании пула — сразу ошибка; провайдер захардкожен (`TODO: Factory`) | fixed (`kernel/sandbox/manager.py`): менеджер сам реализует `ISandbox`, квота через семафор с ожиданием, TTL по последнему обращению, `stop()` закрывает всё, без переиспользования; фабрика `create_sandbox(settings)` для `e2b` / `docker` / `local`. Добавлен `DockerSandbox` (docker CLI). Daytona не реализован |
| I-09 | `02_infra/tools/FILESYSTEM_TOOL.py` | `sandbox_manager.get_sandbox()` не существует; `args.pop` мутирует вход; запись в `state` (LangGraph её отбрасывает); shell-инъекция в glob/grep (`'{pattern}'`); `find -name` не понимает `**/`; `rg` может отсутствовать; пути не ограничены workspace; режим `update` объявлен, но не реализован | fixed (`kernel/tools/filesystem.py`): `ISandbox`, хэш в `metadata["sha256"]`, glob через разбор листинга `find` в Python (с `**`, `{a,b}`), grep через `grep -rHInE -e <quoted>`, все пути внутри workspace; `create` не перезаписывает, `update` требует существующий файл |
| I-10 | `02_infra/tools/SHELL_TOOL.py` | Allowlist не применяется (`_is_dangerous` всегда `False`); denylist — поиск подстроки; таймаут через `asyncio.wait_for` оставляет процесс работать в песочнице | fixed (`kernel/tools/shell.py`): `ShellPolicy` проверяет первое слово каждой команды в цепочке, запрещает `$(...)` и обратные кавычки при включённом allowlist, denylist — регулярные выражения; таймаут передаётся в `ISandbox.exec`. Это защита от ошибок LLM, не граница безопасности |
| I-11 | `02_infra/tools/GIT_TOOL.py` | Аргументы и сообщение коммита подставляются в строку команды (инъекция); `push` доступен агенту без HITL; путь `/workspace` захардкожен | fixed (`kernel/tools/git.py`): `shlex.quote`, запрет `-c`, `--exec`, `--upload-pack`, `--output` и т. п.; `push` только при `allow_push=True` |
| I-12 | `02_infra/tools/LSP_TOOL.py`, `RAG_TOOL.py` | — | отложено: LSP — после вертикали, RAG — фаза 4 |

## 4. Skills (часть 3)

| ID | Проблема | Статус |
|---|---|---|
| SK-01 | Упомянуты, но не даны: `MARKETPLACE_CLIENT.py`, `tsconfig.json.j2`, `layout.tsx.j2`, `DEFAULT_VERTICAL.py` (`GenericVertical`) | `GenericVertical` — написан агентом (`kernel/skills/generic_vertical.py`); `prompts/compiler.py` — написан агентом (`kernel/prompts/compiler.py`); шаблоны — фаза 5; marketplace — open |
| SK-02 | `ISkillExecutor.execute` в ядре возвращал только `list[FileChange]`, в `SKILL_EXECUTOR.py` — dict с outputs; state не передавался, `skill_output()` в шаблонах не работал | fixed: `SkillResult` (file_changes, outputs, metadata), параметр `state`; outputs сохраняются в `state["skill_outputs"]` |
| SK-03 | `MANIFEST_SCHEMA.yaml` — схема для skill.yaml, но в `SKILL_DEFINITION.py` поля называются иначе (`inputs_schema`/`outputs_schema` вместо `inputs`/`outputs`) — входная схема и outputs терялись | fixed: `kernel/skills/definition.py` читает `inputs`/`outputs` |
| SK-04 | `SKILL_DEFINITION.py`: хуки объявлены полями `_pre_render` и т. п.; pydantic считает имена с `_` приватными, `cls(**data, **hooks)` их молча отбрасывает — хуки не вызывались никогда. Дефолты из JSON Schema не применялись, а шаблоны на них рассчитаны | fixed: `PrivateAttr`, проверка `async def`, `apply_schema_defaults` перед валидацией |
| SK-05 | `TEMPLATE_ENGINE.py`: `workspace_root` (путь внутри песочницы) добавлялся в локальный загрузчик шаблонов, create/update определялись `Path.exists()` на хосте; `env()` давал шаблонам доступ к окружению оркестратора (утечка секретов в код); шаблоны примеров вызывают `snake_case(x)` как функцию, а она зарегистрирована только как фильтр | fixed: `kernel/skills/templates.py` — SandboxedEnvironment, фильтры также как globals, без `env()`, `{{ }}` в путях с проверкой выхода за workspace; create/update проверяет executor одной командой в песочнице |
| SK-06 | `SKILL_EXECUTOR.py`: `sandbox.write_files(file_changes)` без `sandbox_id`; `StrictUndefined` без импорта; outputs возвращались сырыми шаблонами; упавший post_script игнорировался; validation выполнялась один раз внутри executor — после исправления фиксером не перепроверялась | fixed: `kernel/skills/executor.py`; упавший post_script → `SkillExecutionError` (→ NODE_ERROR → человек); validation — отдельный гейт `SkillValidationGate`, перезапускается после каждого исправления |
| SK-07 | `SKILL_REGISTRY.py`: в алгоритме Кана in-degree считался у зависимости, а не у зависимого — порядок получался обратным (зависимые раньше зависимостей); неизвестная зависимость → `KeyError`; ошибки загрузки — `print` | fixed: `kernel/skills/registry.py` |
| SK-08 | `VERTICAL_LOADER.py`: `from typing: Dict` (S-01); абсолютные пути хоста записывались в manifest (попадают в checkpoint); одна битая вертикаль роняла `discover`; `VerticalImpl` получал только manifest, а `SaaSWebVertical` в части 5 ожидает `(manifest, tool_registry, skill_registry)` | fixed: `kernel/skills/loader.py`, единая сигнатура `VerticalImpl(manifest, vertical_dir, tool_registry=...)` = `GenericVertical` |
| SK-09 | `HOOKS.py`: `lsp_add_import` вставлял импорт в начало файла, `tree_sitter_edit` возвращал `None` | fixed частично: `kernel/skills/hooks.py` — детерминированные текстовые правки (`add_import`, `insert_after_marker`); LSP/tree-sitter — после LSP-инструмента |
| SK-10 | Примеры `examples/*/hooks.py` импортируют `..SKILL_DEFINITION` относительным импортом из не-пакета | в коде вертикали хуки импортируют `kernel.skills` |
| SK-11 | Ядро: `documenter` только вызывал `vertical.finalize`, README/ARCHITECTURE никто не писал | fixed: при наличии `vertical.get_documenter_prompt` документер пишет `.md` через LLM (`kernel.generate_docs`); ошибка документации не роняет run |
| SK-12 | Ядро: если у гейта нет парсера, `files_to_fix` пуст и фиксер не видел ни одного файла | fixed: фиксер берёт файлы текущей задачи |

## 5. Knowledge (часть 4)

| ID | Где | Проблема | Статус |
|---|---|---|---|
| KN-01 | часть 4 | Нет `EMBEDDING.py`, `HYBRID_SEARCH.py`, `GRAPH_TRAVERSAL.py` | open |

## 6. Вертикаль SaaS Web (часть 5)

| ID | Где | Проблема | Статус |
|---|---|---|---|
| V-01 | `skills/add_trpc_router/skill.yaml:10` | `depends_on: [init_trpc_setup, ...]` — такого скилла нет ни в `MANIFEST.yaml`, ни в `CATALOG.md` | open |
| V-02 | `CATALOG.md` / `README.md` vs `MANIFEST.yaml` | `init_prisma` vs `init_prisma_postgres`, `add_trpc` vs `add_trpc_router` — разные имена одного скилла | open (канон: имена из `MANIFEST.yaml`) |
| V-03 | `verification/PARSERS.py:165` | `parser(self, result)` для функций с одним аргументом → `TypeError` | open |
| V-04 | `verification/PARSERS.py:117,129,145` | `result.files_to_fix.add(...)` у `list` → `AttributeError` | open |
| V-05 | часть 5 | Из 10 скиллов дан только `add_trpc_router`; нет промптов `FIXER.j2`, `REVIEWER.j2`, `DOCUMENTER.j2`; упомянуты, но не даны `prompts/compiler.py`, `prompts/examples/planner_saas.jsonl` | open |

## 7. Ops (часть 6)

| ID | Где | Проблема | Статус |
|---|---|---|---|
| O-01 | `ci_cd/GENERATED_PROJECT_CI.yml` | Шаблон проходит через Jinja: `${{ github.* }}` надо обернуть в `{% raw %}` | open |
| O-02 | `cost_control/BUDGET_MANAGER.py` | Двойной учёт: резерв + фактическая стоимость | open |
| O-03 | `hitl/HITL_API.py:109` | `background_tasks.resume_graph(...)` не существует (нужно `add_task`) | open |
| O-04 | `hitl/HITL_API.py:162` | `Path` не импортирован | open |
| O-05 | `hitl/HITL_API.py:18` | `from kernel.config import settings` — в реализации `get_settings()` | open |
| O-06 | `deployment/TERRAFORM/main.tf` | Не пройдёт `terraform validate` | open |
| O-07 | часть 6 | Нет: `RELEASE_WORKFLOW.yml`, `SECURITY_SCAN.yml`, `LANGSMITH_SETUP.md`, `METRICS.py`, `LOGGING_CONFIG.py`, `CACHE_STRATEGY.md`, `COST_REPORTER.py`, `WEBSOCKET_MANAGER.py`, `UI_COMPONENTS.md`, `APPROVAL_WORKFLOW.py`, `DOCKERCOMPOSE.yml`, `Chart.yaml`, `templates/`, `TERRAFORM/modules/`, `TERRAFORM/environments/`, `SANDBOX_DEPLOYMENT.md`, три скрипта `scripts/` | open |

## 8. Gateway (часть 7)

| ID | Где | Проблема | Статус |
|---|---|---|---|
| G-01 | `router/CLASSIFIER.py:127` | `method="fallback"` вне `Literal["rule","llm","capability","explicit"]` | open |
| G-02 | `api/OPENAPI_SPEC.yaml:198` | `responses:` на верхнем уровне, вне `components`; у ответов 200 нет `description` | open |
| G-03 | часть 7 | Нет: `ROUTES.py`, `MIDDLEWARE.py`, `WEBSOCKETS.py`, `ROUTER.py`, `CAPABILITY_MATCHER.py`, `CONTRACT_RESOLVER.py`, `DEPENDENCY_GRAPH.py`, `QUOTAS.py`, `API_KEYS.py`, `CONFIG.yaml` | open |

## 9. Общее

| ID | Проблема | Статус |
|---|---|---|
| X-01 | Версии моделей и библиотек в ТЗ, вероятно, устарели (в окружении — langgraph 1.2, langchain-core 1.x). Реализация ориентируется на актуальные версии | decision |

## 10. Дополнения (`specs/addenda/`)

### 10.1 `01_phase1_patch_v1.md` — заменён версией v2

Дефекты v1 (подтверждены: пакеты проверены в реестре npm, остальное сверено с ТЗ): несуществующие `@radix-ui/react-button|input|textarea`, `@trivy/plugin`; `@hookform/resolvers/zod` как имя пакета; `tailwindcss-animate` не в зависимостях; `layout.tsx`/`providers.tsx` импортируют файлы будущих скиллов и неописанный псевдоним `~`; нет `output: 'standalone'` и папки `public`; `Role` в `seed.ts`; `from typing:` в хуках; несуществующие зависимости `init_trpc_setup`, `add_tailwind_content_paths`; deprecated `shadcn-ui`; категория `infra` вне схемы; `docker build` внутри песочницы; ложное утверждение, что `add_trpc_router/hooks.py` генерирует базу tRPC. Большая часть исправлена в v2.

### 10.2 `02_phase1_patch_v2.md` — остающиеся дефекты

| ID | Где | Проблема | Статус |
|---|---|---|---|
| A-01 | Q, R, S | Утверждается, что `FIXER.j2`, `DOCUMENTER.j2`, `COST_TRACKER.py`, `DEFAULT_VERTICAL.py`, `prompts/compiler.py` были выданы «в ответе №7». В части 7 ТЗ их нет (только имена в деревьях/манифесте), в присланных текстах тоже. **Файлы по-прежнему отсутствуют** | open |
| A-02 | M `server.ts.j2` | Синтаксическая ошибка: `create({ … }),` → `)` без закрывающей `}` | open |
| A-03 | M `server.ts.j2` | `getServerSession` не экспортируется из `next-auth` v5 (проверено в `next-auth@5.0.0-beta.32`); в v5 используется `auth()` | open |
| A-04 | M `server.ts.j2` / I | Импорт `authOptions` из `@/lib/auth`: такого экспорта в `auth.ts.j2` нет, а сам файл создаёт `add_nextauth_credentials`, который **зависит** от `init_trpc_setup` → цикл, `typecheck` в `init_trpc_setup` упадёт | open |
| A-05 | M `providers.tsx.j2` | `TRPCReactProvider` не экспортируется из `react.tsx.j2` (там `trpc` и `createTRPCClient`); нужно `trpc.Provider` | open |
| A-06 | M | Нет обработчика `src/app/api/trpc/[trpc]/route.ts` — клиент стучится в `/api/trpc`, получит 404 | open |
| A-07 | M `root.ts.j2` vs ТЗ `VERTICAL_IMPL.py` | `_update_root_router` в ТЗ ищет `mergeRouters({`, а `root.ts` использует `createTRPCRouter({})` → роутеры не регистрируются. В v2 утверждается, что метод ищет `createTRPCRouter({` — это не так, нужно исправить в реализации | open |
| A-08 | J `auth.ts.j2` / A | `@auth/prisma-adapter` не в `package.json`; `Google` используется без импорта | open |
| A-09 | J `middleware.ts.j2` | Middleware импортирует `auth` с Prisma-адаптером → Prisma в edge runtime; для NextAuth v5 нужен отдельный edge-совместимый `auth.config.ts` | open |
| A-10 | J `login/page.tsx.j2` | `{% if error %}…{{ error }}` — Jinja выполняется при генерации, а `error` — состояние React: ошибка входа не будет показана. Импортирует компоненты shadcn, но скилл не зависит от `add_shadcn_ui` | open |
| A-11 | K `components.json.j2` | Невалидный JSON: `"prefix: ""` | open |
| A-12 | K, L, M `hooks.py` | Снова `from typing:` — `SyntaxError` | open |
| A-13 | K | `bunx` — bun в образе не установлен (сработает только запасной `npx`); `shadcn@latest` не зафиксирован — поведение CLI меняется между версиями | open |
| A-14 | N `GATES.yaml` | Gate `docker_build` в `GATES.yaml` ТЗ уже есть — дубль id; парсера `parse_docker_build` в `PARSERS.py` нет; поле `runs_in` никем не читается | open |
| A-15 | O `DOCKERFILE.sandbox.base` | `uv pip install --system` на Ubuntu 24.04 падает (externally managed — проверено на Debian с тем же маркером); после `fnm install` `npm` не попадает в PATH того же `RUN`; node/cargo остаются в `/root` (недоступно пользователю `autogen`), `rust-analyzer` — симлинк в `/root`; `tsc` в HEALTHCHECK глобально не установлен | open |
| A-16 | A `package.json.j2` vs ТЗ | Расходится с `03_skills/examples/init_nextjs_app_router/templates/package.json.j2` (vitest vs jest, версии); `packageManager` зашит как pnpm, хотя вход скилла допускает npm/yarn/bun | decision: берётся v2, `packageManager` из входа |
| A-17 | O vs ТЗ `SANDBOX_API.md` | Два разных `DOCKERFILE.sandbox.*` (в ТЗ и в дополнении), разные теги образа (`my-registry/…:latest`, `ghcr.io/autogen/sandbox-base:v1`, в `MANIFEST.yaml` — `sandbox-saas-web:v2.1.0`) | decision в фазе 2 |
