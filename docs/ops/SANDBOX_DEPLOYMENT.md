# Песочницы в эксплуатации (написано агентом; в ТЗ файла нет — MISSING_FILES #39)

Сгенерированный код выполняется в песочнице (`AUTOGEN_SANDBOX__PROVIDER`):

| Провайдер | Когда | Настройка | Изоляция |
|---|---|---|---|
| `e2b` | прод по умолчанию | `AUTOGEN_SANDBOX__API_KEY`, шаблон `AUTOGEN_SANDBOX__E2B_DEFAULT_TEMPLATE` или карта `AUTOGEN_SANDBOX__E2B_TEMPLATE_MAP` (JSON) | microVM у E2B |
| `docker` | свой кластер или хост | ядру нужен доступ к Docker daemon; `AUTOGEN_SANDBOX__DOCKER_NETWORK`, `CPU`, `MEMORY_MB` | контейнер; доступ к docker.sock — это root на хосте, выносите на отдельные узлы (в Terraform есть `enable_sandbox_nodes`) |
| `local` | только разработка и тесты | — | **нет** |

Лимиты: `AUTOGEN_SANDBOX__MAX_CONCURRENT` (квота `SandboxManager`, по умолчанию 20), `TTL_MINUTES` (закрывает простаивающие),
`TIMEOUT_SEC` на команду. Число одновременных run ограничено `AUTOGEN_KERNEL__MAX_CONCURRENT_RUNS`; остальные ждут в очереди
(метрика `autogen_queue_depth`).

Масштабирование E2B упирается в лимит параллельных песочниц тарифа: держите `MAX_CONCURRENT` не выше него.
Daytona и Modal из ТЗ не реализованы (нет адаптеров `ISandbox`).
