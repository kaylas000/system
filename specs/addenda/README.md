# Дополнения к ТЗ (addenda)

Тексты, присланные пользователем после ТЗ. Хранятся **дословно, как получены**: артефакты копирования (двойные пустые строки, HTML-сущности `&gt;` `&lt;` `&amp;` во втором файле) не исправлены. `TECHNICAL_SPECIFICATION.md` не изменялся.

| Файл | Что это | Разбор |
|---|---|---|
| `01_phase1_patch_v1.md` | Первый пакет недостающих файлов для фазы 1 (Dockerfile песочницы, шаблоны `init_nextjs_app_router`, скиллы `init_prisma_postgres`, `add_nextauth_credentials`, `add_shadcn_ui`, `add_dockerfile_prod`, правка `add_trpc_router`) | `specs/ISSUES.md`, раздел 10.1 |
| `02_phase1_patch_v2.md` | Исправленный пакет: правки к v1, шаблоны nextauth, новый скилл `init_trpc_setup`, gate `docker_build`, новый `DOCKERFILE.sandbox.base` | `specs/ISSUES.md`, раздел 10.2 |

Приоритет при реализации: ТЗ → v2 → v1, с исправлениями из `ISSUES.md`. Файл `02_…` предлагал заменить `specs/ISSUES.md` своим трекером — этого не сделано, чтобы не потерять уже зафиксированные решения; его трекер сохранён внутри `02_…`.
