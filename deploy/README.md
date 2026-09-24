# Деплой (фаза 6)

Всё в этой папке написано агентом по `specs/06_ops/deployment/` с исправлением дефектов (см. `specs/ISSUES.md`, раздел O).

| Что | Путь | Чем проверено здесь |
|---|---|---|
| Образ ядра | `docker/Dockerfile.kernel` | hadolint, checkov; шаги сборки повторены в чистом venv (`pip install -r requirements-kernel.lock` + `kernel.main` стартует). `docker build` не запускался — нет Docker |
| Образ Knowledge CLI | `docker/Dockerfile.knowledge` | hadolint, checkov |
| Зафиксированные зависимости | `docker/requirements-*.lock` | `uv pip compile`; обновить: `scripts/lock_requirements.sh` |
| Локальный стек | `docker-compose.yml`, `.env.example` | схема compose-spec (тест); `docker compose up` не запускался |
| LiteLLM | `litellm/config.yaml` | `scripts/check_litellm_config.py` + `litellm.Router` (тест) |
| Helm | `helm/autogen-kernel/` | `helm lint --strict`, `helm template`, строгая схема Kubernetes 1.33, checkov. В кластере (kind) не ставился |
| AWS | `terraform/aws/` | разбор HCL, проверка ссылок, checkov. `terraform init/validate/plan` **не запускались** — реестр Terraform недоступен из среды |
| Метрики / алерты / дашборд | `prometheus/`, `grafana/`, `otel/` | тест: все метрики в запросах существуют в `kernel/observability/metrics.py` |

## Быстрый старт (одна машина)

```bash
cp deploy/.env.example deploy/.env      # вписать ключи и пароли
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build
# + Prometheus / Grafana / Jaeger:
docker compose -f deploy/docker-compose.yml --env-file deploy/.env --profile observability up -d
```

API: http://localhost:8000/docs · метрики: `/metrics` · Grafana: http://localhost:3000

## Kubernetes

```bash
kubectl create namespace autogen
kubectl -n autogen create secret generic autogen-secrets \
  --from-literal=AUTOGEN_DATABASE__POSTGRES_DSN=postgresql://... \
  --from-literal=AUTOGEN_LLM__API_KEY=sk-... \
  --from-literal=AUTOGEN_SANDBOX__API_KEY=e2b_...
helm upgrade --install kernel deploy/helm/autogen-kernel -n autogen \
  --set image.repository=<registry>/autogen-kernel --set image.tag=<tag>
```

LiteLLM, Postgres, Redis, Qdrant ставятся отдельно (управляемые сервисы или свои чарты).

## Ограничения

- **Одна реплика ядра.** Запущенные run и события WebSocket живут в памяти процесса (чекпойнты — в Postgres). Для нескольких реплик нужна общая очередь и шина событий (Redis) — ISSUES O-xx.
- Дневной лимит бюджета хранится в SQLite на томе `/data` — для одной реплики.
- Terraform: принятые находки checkov (CMK вместо ключей AWS, логирование S3, репликация) — на усмотрение владельца аккаунта.
