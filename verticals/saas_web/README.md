# Вертикаль `saas_web`

Генерирует SaaS-приложение: **Next.js 14 (App Router) + tRPC 11 + Prisma 5 (PostgreSQL) + Auth.js v5 + Tailwind/shadcn/ui**,
с Dockerfile, docker-compose и GitHub Actions CI.

Источник — `specs/05_vertical_saas_web` и `specs/addenda` (с исправлениями A-xx, V-xx из `specs/ISSUES.md`).
Файлы, которых в ТЗ не было, помечены в шапке «written by the agent».

## Скиллы

| Скилл | Что делает |
|---|---|
| `init_nextjs_app_router` | Проект Next.js 14, TS strict, Tailwind (тема shadcn), ESLint, Prettier, Vitest; `package.json` со всем стеком; установка зависимостей (pnpm или npm) |
| `init_prisma_postgres` | `schema.prisma` (User/Account/Session/VerificationToken, роль, hash пароля), `src/server/db.ts`, seed |
| `init_trpc_setup` | tRPC 11: контекст, public/protected/admin procedures, root router, route handler, React Query provider, RSC caller |
| `add_nextauth_credentials` | Auth.js v5: e-mail/пароль (bcrypt), опционально GitHub, JWT, middleware, страницы входа/регистрации, `/dashboard` |
| `add_shadcn_ui` | `components.json`, Button/Input/Label/Card/Badge |
| `add_prisma_model` | Модель в `schema.prisma` (+ связь с владельцем User) |
| `add_trpc_router` | CRUD-роутер для модели (поля и Zod выводятся из схемы), регистрация в root |
| `add_crud_page` | Страница `/dashboard/<модель>`: список, создание, переключатели, удаление |
| `add_dockerfile_prod` | Многоступенчатый Dockerfile (standalone), `.dockerignore`, docker-compose с PostgreSQL, `/api/health` |
| `add_github_actions_ci` | CI: install, prisma, lint, typecheck, tests, build, docker build |

Всё, что не покрыто скиллами, делает LLM-кодер (`skill_id: null`) — он видит дерево файлов и содержимое ключевых
файлов (`manifest.yaml → coder.context_files`).

## Проверки (гейты)

`verification/GATES.yaml`: `lint_ts`, `typecheck_ts` — после каждой задачи; `prisma_validate`, `test_unit`,
`build_nextjs` — по тегам скилла; в конце — все блокирующие гейты один раз (packager, `quality_report.final_verification`).

## Проверка без LLM

```bash
python scripts/e2e_saas_skills.py --pm pnpm --keep /tmp/saas-e2e
# в сети без доступа к binaries.prisma.sh:  --fake-prisma-engines
```

Прогоняет стандартный план (Todo-приложение с авторизацией) через скиллы и гейты, в конце — `next build`.
Результат 2026-09-24: 10/10 задач и финальная проверка пройдены для `--pm npm` и `--pm pnpm`.

## Запуск сгенерированного проекта

```bash
docker compose up -d db
cp .env.example .env    # задать AUTH_SECRET
pnpm db:push && pnpm dev
```
