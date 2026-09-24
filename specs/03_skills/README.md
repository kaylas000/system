# Ответ №3: Skills Framework Spec — Система Навыков (Skills), Манифесты, Плагины

Это **руки** системы. Скиллы позволяют агенту не «писать код с нуля» (медленно, с галлюцинациями), а **инстанцировать проверенные паттерны** (мгновенно, детерминированно).

Сохрани в структуру:
```text
specs/03_skills/
├── SKILL_SPEC.md
├── MANIFEST_SCHEMA.yaml
├── skill/
│   ├── SKILL_DEFINITION.py
│   ├── SKILL_EXECUTOR.py
│   ├── TEMPLATE_ENGINE.py
│   └── HOOKS.py
├── registry/
│   ├── SKILL_REGISTRY.py
│   ├── VERTICAL_LOADER.py
│   └── MARKETPLACE_CLIENT.py
└── examples/
    ├── init_nextjs_app_router/
    │   ├── skill.yaml
    │   ├── templates/
    │   │   ├── package.json.j2
    │   │   ├── tsconfig.json.j2
    │   │   └── src/app/layout.tsx.j2
    │   └── hooks.py
    └── add_prisma_model/
        ├── skill.yaml
        ├── templates/
        │   └── prisma/schema.prisma.j2
        └── hooks.py
```

---


<!-- ... файлы спецификации лежат в этой папке ... -->

### 🎯 Инструкция для Агента-Разработчика (Prompt Snippet для Ответа №3)

> **CONTEXT FOR NEXT STEP (Vertical Spec):**
> Ты реализуешь `specs/03_skills/`.
> 1. Создай `kernel/skills/` пакет: `models.py` (SkillDef, SkillOutput), `engine.py` (TemplateEngine), `executor.py` (SkillExecutor), `hooks.py` (протоколы хуков).
> 2. Создай `kernel/skills/registry.py` (SkillRegistry с топологической сортировкой зависимостей).
> 3. Созди `kernel/vertical/loader.py` (VerticalLoader, GenericVertical).
> 4. Реализуй `GenericVertical` (базовая реализация `IVertical`), которая:
>    *   Инициализирует `SkillRegistry`.
>    *   Имплементирует `get_verification_gates` (читает gates из manifest + skill validations).
>    *   Имплементирует `get_skill_executor`.
>    *   Имплементирует промпты через `PromptCompiler` (пока заглушка).
> 5. Создай структуру примеров в `verticals/saas_web/skills/` (два скилла выше).
> 6. **Интеграционный тест (`tests/skills/test_skill_execution.py`):**
>    *   Запуск `SkillExecutor` для `init_nextjs_app_router` в песочнице.
>    *   Проверка, что `package.json`, `tsconfig.json`, `prisma/schema.prisma` созданы.
>    *   Проверка, что `post_scripts` (`pnpm install`, `lint`, `typecheck`, `build`) прошли успешно (статус PASSED).
>    *   Запуск `add_prisma_model` с зависимостью от первого.
>    *   Проверка, что схема обновилась, миграция создалась.

---

### ✅ Чек-лист готовности Skills Framework (Definition of Done для Ответа №3)

- [ ] `SkillDef` загружается из `skill.yaml` + `hooks.py` без ошибок.
- [ ] `TemplateEngine` рендерит Jinja2 с кастомными фильтрами (`to_json`, `snake_case`, `skill_output`).
- [ ] `SkillExecutor` выполняет полный цикл: Hooks -> Render -> Write -> PostScripts -> Validation.
- [ ] `SkillRegistry` корректно разрешает зависимости (`depends_on`) и возвращает топологический порядок.
- [ ] `VerticalLoader` находит вертикали, загружает манифесты и создает `GenericVertical`.
- [ ] Пример `init_nextjs_app_router` проходит все гейты (`pnpm build` успешен).
- [ ] Пример `add_prisma_model` корректно аппендит модель в существующую схему через хук.

---

---
