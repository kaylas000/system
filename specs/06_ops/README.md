# Ответ №6: Ops Spec — CI/CD, Observability, Cost Control, HITL UI, Deployment

Это **нервная система и кожа** платформы. Без этого слоя система — «пет-проект, который работает у меня на машине». С этим слоем — **Enterprise-ready Platform**.

Сохрани в структуру:
```text
specs/06_ops/
├── ci_cd/
│   ├── KERNEL_CI.yml              # CI для самого Kernel
│   ├── GENERATED_PROJECT_CI.yml   # Шаблон CI для генерируемых проектов (Vertical-specific)
│   ├── RELEASE_WORKFLOW.yml       # Semantic Release / Changelog
│   └── SECURITY_SCAN.yml          # SAST/DAST/Deps Scan
├── observability/
│   ├── LANGSMITH_SETUP.md
│   ├── OTEL_CONFIG.py             # OpenTelemetry Instrumentation
│   ├── METRICS.py                 # Prometheus Metrics Definitions
│   ├── GRAFANA_DASHBOARDS.json    # JSON Dashboards
│   └── LOGGING_CONFIG.py          # Structured Logging (JSON)
├── cost_control/
│   ├── BUDGET_MANAGER.py          # Token/Cost Limits Enforcement
│   ├── MODEL_ROUTER.yaml          # LiteLLM Router Config
│   ├── CACHE_STRATEGY.md          # Prompt/Embedding/Response Caching
│   └── COST_REPORTER.py           # Daily/Run Cost Reports
├── hitl/
│   ├── HITL_API.py                # FastAPI Endpoints for Human Review
│   ├── WEBSOCKET_MANAGER.py       # Real-time Log/State Streaming
│   ├── UI_COMPONENTS.md           # React Components Spec (for Frontend Team)
│   └── APPROVAL_WORKFLOW.py       # Approve/Edit/Abort Logic
├── deployment/
│   ├── DOCKERFILE.kernel          # Kernel Production Image
│   ├── DOCKERCOMPOSE.yml          # Local Dev Stack (Kernel, DBs, Sandboxes)
│   ├── HELM_CHART/                # Kubernetes Deployment
│   │   ├── Chart.yaml
│   │   ├── values.yaml
│   │   └── templates/
│   ├── TERRAFORM/                 # Cloud Infra (AWS/GCP/Azure)
│   │   ├── main.tf
│   │   ├── modules/
│   │   └── environments/
│   └── SANDBOX_DEPLOYMENT.md      # E2B/Daytona/Modal Scaling Config
└── scripts/
    ├── DEPLOY.sh
    ├── MIGRATE_DB.sh
    └── SEED_KNOWLEDGE.sh
```

---


<!-- ... файлы спецификации лежат в этой папке ... -->

### 🎯 Инструкция для Агента-Разработчика (Prompt Snippet для Ответа №6)

> **CONTEXT FOR NEXT STEP (Gateway Spec - Optional):**
> Ты реализуешь `specs/06_ops/`.
> 1. Создай `.github/workflows/` с `kernel-ci.yml` и шаблон `generated-project-ci.yml.j2` (для рендеринга скиллом `add_github_actions_ci`).
> 2. Реализуй `kernel/observability/` с `otel.py` (setup_otel, metrics, trace_node декоратор).
> 3. Интегрируй `BudgetManager` в `LLMClient` (вызов `record_actual_usage` после каждого `achat`).
> 4. Реализуй `HITL_API` (FastAPI) с WebSocket менеджером и эндпоинтами `/interrupt`, `/approve`, `/ws`.
> 5. Напиши `DOCKERFILE.kernel` (multi-stage, non-root, uv).
> 6. Напиши `HELM_CHART/values.yaml` и `TERRAFORM/main.tf` для AWS EKS + RDS + Redis.
> 7. **Интеграционный тест (`tests/ops/test_observability.py`):**
>    *   Запуск Kernel с включенным OTEL -> Проверка появления метрик в Prometheus (`autogen_run_duration`).
>    *   Тест `BudgetManager`: Запуск задачи с лимитом $0.01 -> Ожидание `BudgetExceededError`.
>    *   Тест HITL: Запуск графа с прерыванием `plan_review` -> GET `/interrupt` возвращает payload -> POST `/approve` с `action: approve` -> Граф продолжается.
>    *   Деплой Helm чарта в Kind (local k8s) -> Проверка готовности подов, ingress, PVC для Kuzu.

---

### ✅ Чек-лист готовности Ops Spec (Definition of Done для Ответа №6)

- [ ] `kernel-ci.yml` проходит: Lint, Typecheck, Unit Tests, Contract Tests, Docker Build, Push.
- [ ] `GENERATED_PROJECT_CI.yml.j2` рендерится скиллом `add_github_actions_ci` и проходит в сгенерированном проекте.
- [ ] `OTEL_CONFIG` инициализируется без ошибок, метрики отдаются в Prometheus/Otel Collector.
- [ ] `GRAFANA_DASHBOARDS.json` импортируется и отображает данные.
- [ ] `BudgetManager` блокирует запуски при превышении `$/run` и `tokens/min`.
- [ ] `MODEL_ROUTER.yaml` загружается в LiteLLM Proxy, фоллбэки работают.
- [ ] `HITL_API` поднимается, WebSocket стримит логи, `/approve` резюмирует граф.
- [ ] `DOCKERFILE.kernel` собирается, образ < 1GB, проходит `trivy` scan (Critical=0).
- [ ] Helm Chart деплоится в Kind/K3s, поды становятся Ready, Ingress работает.
- [ ] Terraform `plan` проходит без ошибок для AWS.

---

---
