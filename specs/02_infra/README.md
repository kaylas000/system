# Ответ №2: Infra Spec — Инфраструктура Исполнения и Инструменты

Это **мышцы и нервы** системы. Без качественной реализации этого слоя агент будет «слепым и парализованным».

Сохрани в структуру:
```text
specs/02_infra/
├── sandbox/
│   ├── SANDBOX_API.md
│   ├── DOCKERFILE.sandbox
│   ├── SANDBOX_MANAGER.py
│   └── PROVIDERS.md
├── tools/
│   ├── TOOL_REGISTRY.py
│   ├── FILESYSTEM_TOOL.py
│   ├── SHELL_TOOL.py
│   ├── LSP_TOOL.py
│   ├── RAG_TOOL.py
│   └── GIT_TOOL.py
└── llm_gateway/
    ├── LLM_CLIENT.py
    └── COST_TRACKER.py
```

---


<!-- ... файлы спецификации лежат в этой папке ... -->

### 🎯 Инструкция для Агента-Разработчика (Prompt Snippet для Ответа №2)

> **CONTEXT FOR NEXT STEP (Vertical Spec):**
> Ты реализуешь `specs/02_infra/`.
> 1. Создай `kernel/tools/` с файлами: `registry.py`, `filesystem.py`, `shell.py`, `lsp.py`, `rag.py`, `git.py`.
>    *   Все классы должны наследовать протоколы из `kernel.protocols`.
>    *   `ToolRegistry` должен инжектироваться в ноды через `config["configurable"]["tool_registry"]`.
> 2. Создай `kernel/sandbox/manager.py` с классом `SandboxManager` (пул, TTL, affinity).
>    *   Реализуй `E2BSandbox` адаптер для `ISandbox`.
>    *   Напиши `Dockerfile.sandbox.base` и `Dockerfile.sandbox.saas_web` (как в спеке).
> 3. Создай `kernel/llm/client.py` с `LiteLLMClient`.
> 4. **Интеграционный тест (`tests/infra/test_tools_integration.py`):**
>    *   Запуск `SandboxManager` (mock или реальный E2B если есть ключи).
>    *   Регистрация всех инструментов в `ToolRegistry`.
>    *   Тест `filesystem.write -> read -> glob -> grep`.
>    *   Тест `shell.exec("pnpm --version")` -> успех.
>    *   Тест `lsp.goto_definition` на простом TS файле (проверка, что LSP сервер запускается и отвечает).
>    *   Тест `rag.retrieve` (мок Qdrant/Kuzu).

---

### ✅ Чек-лист готовности Инфраструктуры (Definition of Done для Ответа №2)

- [ ] `Dockerfile.sandbox.base` собирается без ошибок (`docker build -t autogen-base .`).
- [ ] `E2BSandbox` (или `DockerSandbox`) проходит все методы `ISandbox` в тестах.
- [ ] `ToolRegistry` выдает корректные OpenAI Function Schemas для всех инструментов.
- [ ] `LSPTool` успешно подключается к `typescript-language-server` в песочнице и возвращает определения.
- [ ] `RagTool` выполняет запрос к моку Qdrant/Kuzu и возвращает `RagResult`.
- [ ] `ShellTool` блокирует опасные команды (`rm -rf /`) и пропускает разрешенные (`pnpm test`).
- [ ] Токены и стоимость трекаются в `LLMClient` и возвращаются в `LLMResponse`.

---

---
