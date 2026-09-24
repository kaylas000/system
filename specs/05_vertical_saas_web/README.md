# Ответ №5: Vertical Spec — SaaS Web Implementation (Next.js 14+ / React / tRPC / Prisma)

Это **первая вертикаль**, валидирующая всю платформу. Она должна генерировать **Production-Ready** SaaS приложения с авторизацией, биллингом, админкой и CI/CD «из коробки».

Сохрани в структуру:
```text
specs/05_vertical_saas_web/
├── MANIFEST.yaml
├── VERTICAL_IMPL.py
├── skills/
│   ├── CATALOG.md
│   ├── init_nextjs_app_router/          (из Ответа №3)
│   ├── init_prisma_postgres/
│   ├── add_trpc_router/
│   ├── add_nextauth_credentials/
│   ├── add_stripe_billing/
│   ├── add_shadcn_ui/
│   ├── add_dockerfile_prod/
│   ├── add_github_actions_ci/
│   ├── add_playwright_e2e/
│   └── add_admin_dashboard/
├── prompts/
│   ├── PLANNER.j2
│   ├── CODER.j2
│   ├── FIXER.j2
│   ├── REVIEWER.j2
│   └── DOCUMENTER.j2
├── verification/
│   ├── GATES.yaml
│   └── PARSERS.py
└── evals/
    ├── DATASET.jsonl
    └── RUN_EVALS.py
```

---


<!-- ... файлы спецификации лежат в этой папке ... -->

### 🎯 Инструкция для Агента-Разработчика (Prompt Snippet для Ответа №5)

> **CONTEXT FOR NEXT STEP (Ops Spec):**
> Ты реализуешь `specs/05_vertical_saas_web/`.
> 1. Создай `verticals/saas_web/` с `MANIFEST.yaml` и `VERTICAL_IMPL.py`.
> 2. Реализуй `SaasWebVertical` класс, наследующий `IVertical`.
> 3. Создай все 10+ скиллов в `skills/` (минимально: `init_nextjs`, `init_prisma`, `add_trpc`, `add_nextauth`, `add_shadcn`, `add_dockerfile`, `add_ci`). Используй структуру из Ответа №3.
> 4. Реализуй `verification/GATES.yaml` и `PARSERS.py` для парсинга ESLint, TSC, Vitest, Next Build, Playwright.
> 5. Напиши промпты в `prompts/` (Planner, Coder, Fixer, Reviewer, Documenter) с Jinja2.
> 6. Настрой `PromptCompiler` в `prompts/compiler.py` (загрузка few-shot примеров).
> 7. **Интеграционный тест (`tests/verticals/test_saas_web.py`):**
>    *   Запуск `GenerateRequest` с промптом `"Simple Todo App with Auth"`.
>    *   Проверка прохождения графа: `init_nextjs` -> `init_prisma` -> `add_trpc` -> `add_nextauth` -> `add_shadcn` -> `docker` -> `ci`.
>    *   Проверка артефактов: `pnpm run build` успешен, `docker build` успешен, `pnpm test:e2e` проходит (хотя бы smoke test).
>    *   Запуск `RUN_EVALS.py` на `DATASET.jsonl` -> `pass_rate > 0.8`.

---

### ✅ Чек-лист готовности Vertical Spec (Definition of Done для Ответа №5)

- [ ] `MANIFEST.yaml` валиден, все скиллы обнаруживаются `SkillRegistry`.
- [ ] `SaasWebVertical` имплементирует `IVertical` полностью (initialize, prompts, gates, skills, finalize).
- [ ] Все ключевые скиллы (`init_nextjs`, `add_trpc`, `add_nextauth`, `add_stripe`, `add_docker`, `add_ci`) имеют `skill.yaml`, `templates/`, `hooks.py` и проходят валидацию.
- [ ] `GATES.yaml` покрывает все этапы: Lint, Typecheck, Prisma, Unit Test, Build, Docker, E2E, Security.
- [ ] `PARSERS.py` извлекает `files_to_fix` и `issues` для Fixer Agent.
- [ ] Промпты используют `retrieved_context` и `skill_output` глобалы.
- [ ] Eval Dataset запускается и дает `pass_rate > 80%` на базовых сценариях.

---

---
