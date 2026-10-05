"""saas_web vertical: loading, hook helpers, parsers, template rendering (no network, no npm)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kernel.protocols import SandboxSpec
from kernel.sandbox.local import LocalSandbox
from kernel.skills import GenericVertical, VerticalLoader
from kernel.state import Task, VerificationGateResult, VerificationGateStatus
from verticals.saas_web.lib.fields import editable_fields
from verticals.saas_web.lib.prisma import add_user_relation, parse_model
from verticals.saas_web.lib.trpc import add_router_to_root
from verticals.saas_web.verification.parsers import parse_eslint, parse_prisma, parse_tsc, rel

ROOT = Path(__file__).resolve().parents[2]
WS = "/workspace"

EXPECTED_SKILLS = {
    "init_nextjs_app_router",
    "init_prisma_postgres",
    "init_trpc_setup",
    "add_nextauth_credentials",
    "add_shadcn_ui",
    "add_prisma_model",
    "add_trpc_router",
    "add_crud_page",
    "add_dockerfile_prod",
    "add_github_actions_ci",
}


@pytest.fixture(scope="module")
def vertical() -> GenericVertical:
    loader = VerticalLoader(ROOT / "verticals")
    v = loader.load_vertical("saas_web")
    assert not loader.errors
    return v


def test_vertical_loads_all_skills(vertical: GenericVertical) -> None:
    assert set(vertical.skills) == EXPECTED_SKILLS
    assert not vertical.skill_registry.load_errors
    assert set(vertical.manifest.key_skills) <= EXPECTED_SKILLS
    for skill in vertical.skills.values():
        for dep in skill.depends_on:
            assert dep in vertical.skills, f"{skill.id} depends on unknown {dep}"


def test_dependency_order(vertical: GenericVertical) -> None:
    order = vertical.skill_registry.resolve_dependencies(["add_crud_page"])
    assert order[0] == "init_nextjs_app_router"
    assert order.index("add_prisma_model") < order.index("add_trpc_router") < order.index("add_crud_page")
    assert order.index("init_trpc_setup") < order.index("add_nextauth_credentials")


def test_gates(vertical: GenericVertical) -> None:
    assert vertical.gates["build_nextjs"].exclusive is True
    assert vertical.gates["security_audit"].config.severity == "warning"
    task = Task(id="t1", name="model", description="", skill_id="add_prisma_model", inputs={})
    ids = [g.id for g in vertical.get_verification_gates({}, task)]
    assert ids[:2] == ["lint_ts", "typecheck_ts"]
    assert "prisma_validate" in ids and "skill:add_prisma_model" in ids and "build_nextjs" not in ids
    final = {g.id for g in vertical.get_verification_gates({}, None)}
    assert final == {"lint_ts", "typecheck_ts", "prisma_validate", "test_unit", "build_nextjs"}


def test_prompts_render(vertical: GenericVertical) -> None:
    planner = vertical.get_planner_prompt({"user_prompt": "Todo app", "tech_stack_hints": {}})
    for skill_id in EXPECTED_SKILLS:
        assert skill_id in planner
    task = Task(id="t9", name="Widget", description="edit src/app/page.tsx", skill_id=None)
    coder = vertical.get_coder_prompt(
        {"file_tree": ["src/app/page.tsx"], "open_files": [{"path": "src/app/page.tsx", "content": "PAGE"}]},  # type: ignore[typeddict-unknown-key]
        task,
    )
    assert "--- src/app/page.tsx ---\nPAGE" in coder
    assert "src/server/api/root.ts" in vertical.context_files({}, task)


ROOT_TS = """import { healthRouter } from "@/server/api/routers/health";
import { createCallerFactory, createTRPCRouter } from "@/server/api/trpc";

export const appRouter = createTRPCRouter({
  health: healthRouter,
});
"""


def test_add_router_to_root_idempotent() -> None:
    once = add_router_to_root(ROOT_TS, "todo", "todoRouter", "@/server/api/routers/todo")
    assert 'import { todoRouter } from "@/server/api/routers/todo";' in once
    assert "createTRPCRouter({\n  todo: todoRouter,\n  health: healthRouter," in once
    assert add_router_to_root(once, "todo", "todoRouter", "@/server/api/routers/todo") == once
    with pytest.raises(ValueError):
        add_router_to_root("export const x = 1;\n", "a", "aRouter", "m")


SCHEMA = (ROOT / "verticals/saas_web/skills/init_prisma_postgres/templates/prisma/schema.prisma.j2").read_text()
TODO = """
enum Priority {
  LOW
  HIGH
}

model Todo {
  id        String    @id @default(cuid())
  title     String
  notes     String?   @db.Text
  done      Boolean   @default(false)
  priority  Priority  @default(LOW)
  meta      Json?
  userId    String
  user      User      @relation(fields: [userId], references: [id], onDelete: Cascade)
  createdAt DateTime  @default(now())
  updatedAt DateTime  @updatedAt

  @@index([userId])
}
"""


def test_prisma_helpers_and_fields() -> None:
    schema = add_user_relation(SCHEMA + TODO, "todos Todo[]")
    assert "// relations: generated models are added below this line\n  todos Todo[]" in schema
    assert add_user_relation(schema, "todos Todo[]") == schema
    user = parse_model(schema, "User")
    assert user.get("todos") is not None and user.get("todos").is_relation  # type: ignore[union-attr]
    model = parse_model(schema, "Todo")
    fields = {f["name"]: f for f in editable_fields(model)}
    assert set(fields) == {"title", "notes", "done", "priority", "meta"}
    assert fields["title"]["zod"] == "z.string().trim().min(1).max(255)"
    assert fields["notes"]["zod"] == "z.string().trim().max(10000).nullish()" and fields["notes"]["kind"] == "text"
    assert fields["done"]["zod"] == "z.boolean().optional()"
    assert fields["priority"]["zod"] == "z.nativeEnum(Priority).optional()"
    assert fields["meta"]["zod"] == "z.record(z.any()).optional()"  # Json never nullish
    with pytest.raises(ValueError):
        parse_model(schema, "Missing")


def _result(stdout: str, code: int = 1) -> VerificationGateResult:
    return VerificationGateResult(
        gate_id="g",
        name="g",
        status=VerificationGateStatus.FAILED,
        command="c",
        exit_code=code,
        stdout=stdout,
        duration_ms=1,
    )


def test_parsers() -> None:
    tsc = parse_tsc(_result("src/app/page.tsx(3,7): error TS2322: Type 'number' is not assignable to type 'string'.\n"))
    assert tsc.files_to_fix == ["src/app/page.tsx"] and tsc.issues[0]["code"] == "TS2322"
    lint = parse_eslint(
        _result(
            "\n./src/app/(auth)/login/page.tsx\n3:7  Error: 'x' is assigned a value but never used.  "
            "@typescript-eslint/no-unused-vars\n\ninfo  - Need to disable some ESLint rules?\n"
        )
    )
    assert lint.files_to_fix == ["src/app/(auth)/login/page.tsx"]
    assert lint.issues[0]["rule"] == "@typescript-eslint/no-unused-vars" and lint.issues[0]["line"] == 3
    prisma = parse_prisma(_result('error: Type "Todo" is neither a built-in type\n  -->  prisma/schema.prisma:29\n'))
    assert prisma.issues[0]["line"] == 29 and prisma.files_to_fix == ["prisma/schema.prisma"]
    assert rel("/tmp/x/workspace/src/a.ts") == "src/a.ts"
    assert rel("node_modules/x/index.d.ts") is None


async def _run_skill(
    vertical: GenericVertical,
    sandbox: LocalSandbox,
    sid: str,
    skill_id: str,
    inputs: dict[str, Any],
    state: dict[str, Any],
) -> list[str]:
    skill = vertical.skills[skill_id]
    executor = vertical.get_skill_executor(skill_id)
    object.__setattr__(executor, "skill_def", skill.model_copy(update={"post_scripts": []}))  # no npm in unit tests
    result = await executor.execute(sandbox, sid, WS, inputs, state)  # type: ignore[arg-type]
    state.setdefault("skill_outputs", {})[skill_id] = dict(result.outputs)
    return [c.path for c in result.file_changes]


async def test_skill_chain_renders(vertical: GenericVertical, tmp_path: Path) -> None:
    sandbox = LocalSandbox(tmp_path)
    sid = await sandbox.create(SandboxSpec(image="local"))
    state: dict[str, Any] = {}
    paths = await _run_skill(vertical, sandbox, sid, "init_nextjs_app_router", {"project_name": "my-app"}, state)
    assert {"package.json", "tsconfig.json", ".env", ".eslintrc.json", "src/app/layout.tsx"} <= set(paths)
    assert state["skill_outputs"]["init_nextjs_app_router"]["package_manager"] == "pnpm"
    await _run_skill(vertical, sandbox, sid, "init_prisma_postgres", {}, state)
    await _run_skill(vertical, sandbox, sid, "init_trpc_setup", {}, state)
    await _run_skill(vertical, sandbox, sid, "add_nextauth_credentials", {}, state)
    await _run_skill(
        vertical,
        sandbox,
        sid,
        "add_prisma_model",
        {
            "model_name": "BlogPost",
            "fields": [
                {"name": "title", "type": "String"},
                {"name": "published", "type": "Boolean", "default": "false"},
            ],
        },
        state,
    )
    await _run_skill(
        vertical, sandbox, sid, "add_trpc_router", {"router_name": "blogPost", "model_name": "BlogPost"}, state
    )
    paths = await _run_skill(
        vertical, sandbox, sid, "add_crud_page", {"router_name": "blogPost", "model_name": "BlogPost"}, state
    )
    await _run_skill(vertical, sandbox, sid, "add_dockerfile_prod", {}, state)

    ws = sandbox.host_path(sid, WS)
    read = lambda p: (ws / p).read_text()  # noqa: E731
    schema = read("prisma/schema.prisma")
    assert "model BlogPost {" in schema and "blogPosts BlogPost[]" in schema
    root = read("src/server/api/root.ts")
    assert "blogPost: blogPostRouter," in root and "auth: authRouter," in root
    router = read("src/server/api/routers/blogPost.ts")
    assert "ctx.db.blogPost.findMany" in router and "userId: ctx.session.user.id" in router
    assert "src/app/dashboard/blog-posts/page.tsx" in paths
    assert "<BlogPostManager />" in read("src/app/dashboard/blog-posts/page.tsx")
    assert 'href="/dashboard/blog-posts"' in read("src/app/dashboard/page.tsx")
    env_example = read(".env.example")
    assert "DATABASE_URL=" in env_example and "AUTH_SECRET=" in env_example
    assert "getSession" in read("src/server/session.ts") and "auth()" in read("src/server/session.ts")
    dockerfile = read("Dockerfile")
    assert "pnpm install --frozen-lockfile" in dockerfile and "npx prisma generate" in dockerfile
    await sandbox.close(sid)
