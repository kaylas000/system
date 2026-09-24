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

Публичный API — `/v1` (Gateway, фаза 7), авторизация включена. Выпустить ключ и проверить:

```bash
docker compose -f deploy/docker-compose.yml exec kernel python -m kernel.main keys create --tenant dev --user me --role admin
export AUTOGEN_API_URL=http://localhost:8000 AUTOGEN_API_KEY=agk_dev_...
autogen verticals                       # pip install -e ".[cli]"
autogen generate "Todo app with auth" --watch --download ./out
```

Для локальной отладки без ключей: `GATEWAY_AUTH_ENABLED=false` в `deploy/.env` (все запросы — анонимный admin).

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

LiteLLM, Postgres, Redis, Qdrant ставятся отдельно (управляемые сервисы или свои чарты). Redis нужен Gateway
для лимитов (`AUTOGEN_GATEWAY__REDIS_URL`); ключи API: `kubectl -n autogen exec deploy/kernel-autogen-kernel --
python -m kernel.main keys create --tenant <t> --user <u>`. JWT: `AUTOGEN_GATEWAY__JWKS_URL` (Auth0/Clerk/Keycloak)
или `AUTOGEN_GATEWAY__JWT_SECRET` в секрете.

## Ограничения

- **Одна реплика ядра.** Запущенные run и события WebSocket живут в памяти процесса (чекпойнты — в Postgres). Для нескольких реплик нужна общая очередь и шина событий (Redis) — ISSUES O-15.
- Дневной лимит бюджета, ключи API, реестр run и композиций хранятся в SQLite на томе `/data` — для одной реплики (ISSUES O-15, G-14).
- Исходящие webhooks: NetworkPolicy ограничивает только вход; адреса webhook проверяются (запрещены приватные сети, `AUTOGEN_GATEWAY__ALLOW_PRIVATE_WEBHOOKS`).
- Terraform: принятые находки checkov (CMK вместо ключей AWS, логирование S3, репликация) — на усмотрение владельца аккаунта.
