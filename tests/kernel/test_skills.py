"""Skills framework: definition, templates, executor, gates, registry, loader, GenericVertical e2e."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kernel.config import Settings
from kernel.graph import build_graph
from kernel.persistence import memory_checkpointer
from kernel.prompts import PromptCompiler, split_messages
from kernel.protocols import GenerateRequest, IVertical, SandboxSpec
from kernel.runner import start_run
from kernel.sandbox import LocalSandbox
from kernel.skills import (
    GenericVertical,
    LoadedSkill,
    SkillExecutionError,
    SkillInputError,
    SkillLoadError,
    SkillRegistry,
    TemplateRenderError,
    VerticalLoader,
    apply_schema_defaults,
    render_skill_templates,
)
from kernel.skills.hooks import add_import
from kernel.skills.templates import pascal_case, snake_case
from kernel.state import FileChange, RunStatus, TaskStatus, VerificationGateStatus

from .fakes import FakeLLM, code, plan

ROOT = Path(__file__).parent.parent / "fixtures" / "verticals"
DEMO = ROOT / "demo"
SPEC_EXAMPLES = Path(__file__).parents[2] / "specs" / "03_skills" / "examples"


@pytest.fixture
async def sbx(tmp_path: Path) -> Any:
    sandbox = LocalSandbox(tmp_path)
    sid = await sandbox.create(SandboxSpec(image="x"))
    yield sandbox, sid
    await sandbox.close(sid)


def demo() -> GenericVertical:
    v = VerticalLoader(ROOT).load_vertical("demo")
    assert isinstance(v, GenericVertical)
    return v


# ------------------------------------------------------------------ definition/templates
def test_case_filters() -> None:
    assert snake_case("BlogPost") == "blog_post" and snake_case("my-app") == "my_app"
    assert snake_case("HTTPServer") == "http_server" and pascal_case("my-cool_app") == "MyCoolApp"


def test_schema_defaults() -> None:
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "string", "default": "x"},
            "items": {"type": "array", "items": {"type": "object", "properties": {"f": {"default": False}}}},
        },
    }
    assert apply_schema_defaults(schema, {"items": [{}, {"f": True}]}) == {
        "a": "x",
        "items": [{"f": False}, {"f": True}],
    }


def test_load_skill_and_validate_inputs() -> None:
    skill = LoadedSkill.load_from_dir(DEMO / "skills" / "init_demo")
    assert skill.inputs_json_schema["required"] == ["project_name"] and skill.pre_render is not None
    assert skill.validate_inputs({"project_name": "demo-app"}) == {"project_name": "demo-app", "greeting": "hello"}
    with pytest.raises(SkillInputError, match="project_name"):
        skill.validate_inputs({"project_name": "Bad Name"})
    with pytest.raises(SkillLoadError):
        LoadedSkill.load_from_dir(DEMO / "skills" / "broken")


def test_spec_example_skills_load() -> None:
    """The example skills from the spec parse (manifest + schema); their hooks use spec-relative imports."""
    for name in ("init_nextjs_app_router", "add_prisma_model"):
        d = SPEC_EXAMPLES / name
        import yaml

        data = yaml.safe_load((d / "skill.yaml").read_text())
        data["inputs_json_schema"] = data.pop("inputs")
        skill = LoadedSkill.model_validate(data)
        assert skill.id == name
    init = LoadedSkill.model_validate(
        {**yaml.safe_load((SPEC_EXAMPLES / "init_nextjs_app_router" / "skill.yaml").read_text()), "inputs": None}
    )
    changes = render_skill_templates(
        SPEC_EXAMPLES / "init_nextjs_app_router" / "templates", {"project_name": "my-app", "package_manager": "pnpm"}
    )
    assert changes[0].path == "package.json" and '"name": "my-app"' in changes[0].content
    assert init.post_scripts


def test_render_rejects_escaping_paths(tmp_path: Path) -> None:
    (tmp_path / "{{ p }}.j2").write_text("x")
    with pytest.raises(TemplateRenderError, match="escapes"):
        render_skill_templates(tmp_path, {"p": "../../etc/x"})
    with pytest.raises(TemplateRenderError):
        render_skill_templates(tmp_path, {})  # StrictUndefined


def test_add_import() -> None:
    src = '"use client";\nimport a from "a";\n\nexport const x = 1;\n'
    out = add_import(src, 'import b from "b";')
    assert out.splitlines()[:3] == ['"use client";', 'import a from "a";', 'import b from "b";']
    assert add_import(out, 'import b from "b";') == out


# ------------------------------------------------------------------ registry
def test_registry_and_dependencies() -> None:
    reg = SkillRegistry(DEMO / "skills", vertical_id="demo")
    assert set(reg.skills) == {"init_demo", "add_module"}
    assert "broken" in reg.load_errors
    assert reg.resolve_dependencies(["add_module"]) == ["init_demo", "add_module"]
    assert reg.providers_of("demo_project") == ["init_demo"]
    assert [s.id for s in reg.list_skills(category="feature")] == ["add_module"]
    with pytest.raises(KeyError):
        reg.resolve_dependencies(["nope"])
    with pytest.raises(SkillLoadError):
        SkillRegistry(DEMO / "skills", strict=True)
    other = SkillRegistry(SPEC_EXAMPLES, vertical_id="other_vertical")  # compatible_verticals filter
    assert other.skills == {}


def test_registry_cycle(tmp_path: Path) -> None:
    for sid, dep in (("a", "b"), ("b", "a")):
        (tmp_path / sid).mkdir()
        (tmp_path / sid / "skill.yaml").write_text(
            f"id: {sid}\nname: {sid}\nversion: 1.0.0\ndescription: x\ndepends_on: [{dep}]\n"
        )
    with pytest.raises(ValueError, match="Circular"):
        SkillRegistry(tmp_path).resolve_dependencies(["a"])


# ------------------------------------------------------------------ executor + gates
async def test_executor_and_skill_gate(sbx: Any) -> None:
    sandbox, sid = sbx
    v = demo()
    res = await v.get_skill_executor("init_demo").execute(sandbox, sid, "/workspace", {"project_name": "demo-app"}, {})
    assert {c.path: c.action for c in res.file_changes} == {"README.txt": "create", "src/main.txt": "create"}
    assert res.outputs == {"project_name": "demo-app", "kind": "demo"}
    assert res.metadata["scripts"][0]["command"] == "echo demo-app > .initialized"
    assert await sandbox.read_file(sid, "/workspace/README.txt") == "DEMO-APP: hello from DemoApp\n"

    state: Any = {"skill_outputs": {"init_demo": res.outputs}, "workspace_path": "/workspace"}
    res2 = await v.get_skill_executor("add_module").execute(sandbox, sid, "/workspace", {"name": "BlogPost"}, state)
    assert {c.path: c.action for c in res2.file_changes} == {
        "src/modules/blog_post.txt": "create",
        "src/main.txt": "update",
    }
    assert await sandbox.read_file(sid, "/workspace/src/modules/blog_post.txt") == "module BlogPost of demo-app\n"
    assert "uses BlogPost" in await sandbox.read_file(sid, "/workspace/src/main.txt")

    from kernel.state import Task

    task = Task(id="t1", name="init", description="d", skill_id="init_demo", inputs={"project_name": "demo-app"})
    gates = v.get_verification_gates(state, task)
    assert [g.id for g in gates] == ["no_todo", "text_only", "style", "skill:init_demo"]
    results = {g.id: await g.execute(sandbox, sid, "/workspace", state) for g in gates}
    assert all(r.status == VerificationGateStatus.PASSED for r in results.values()), results
    assert "non-blocking" in results["style"].stderr

    mod_task = Task(id="t2", name="m", description="d", skill_id="add_module", inputs={"name": "Other"})
    gate = v.get_verification_gates(state, mod_task)[-1]
    failed = await gate.execute(sandbox, sid, "/workspace", state)
    assert failed.status == VerificationGateStatus.FAILED and "not registered" in failed.stderr

    await sandbox.write_file(sid, "/workspace/src/todo.txt", "TODO later")
    todo = await v.gates["no_todo"].execute(sandbox, sid, "/workspace", state)
    assert todo.status == VerificationGateStatus.FAILED and todo.files_to_fix == ["src/todo.txt"]
    assert [g.id for g in v.get_verification_gates(state, None)] == ["no_todo", "text_only"]


async def test_post_script_failure_raises(sbx: Any, tmp_path: Path) -> None:
    sandbox, sid = sbx
    d = tmp_path / "failing"
    d.mkdir()
    (d / "skill.yaml").write_text("id: failing\nname: f\nversion: 1.0.0\ndescription: x\npost_scripts: ['exit 3']\n")
    reg = SkillRegistry(tmp_path)
    with pytest.raises(SkillExecutionError, match="exit code 3"):
        await reg.get_executor("failing").execute(sandbox, sid, "/workspace", {})


# ------------------------------------------------------------------ prompts / loader
def test_prompts_and_loader() -> None:
    loader = VerticalLoader(ROOT)
    assert list(loader.discover()) == ["demo"]
    v = loader.load_vertical("demo")
    assert isinstance(v, IVertical) and loader.load_vertical("demo") is v
    state: Any = {"user_prompt": "Make it", "tech_stack_hints": {"language": "text"}}
    assert v.get_planner_prompt(state).startswith("DEMO PLANNER for Demo Vertical. Skills: add_module init_demo")
    from kernel.state import Task

    task = Task(id="t9", name="n", description="desc", inputs={"k": 1})
    coder = v.get_coder_prompt(state, task)  # kernel default CODER.j2
    assert "id: t9" in coder and '"k": 1' in coder
    assert "attempt 1 of 3" in v.get_fixer_prompt(state, task)
    with pytest.raises(Exception, match="not found"):
        loader.load_vertical("missing")


def test_prompt_compiler_defaults_and_split(tmp_path: Path) -> None:
    (tmp_path / "X.j2").write_text("<system>\nS {{ a.b.c }}\n</system>\n<user>U {{ name }}</user>")
    pc = PromptCompiler(tmp_path)
    out = pc.render("X.j2", name="n")
    assert split_messages(out) == ("S", "U n")
    assert pc.has("FIXER.j2") and pc.has("DOCUMENTER.j2") and not pc.has("NOPE.j2")
    assert "name" in pc.undeclared("X.j2")


def test_spec_saas_prompts_render() -> None:
    """The SaaS-web prompts from the spec render with the kernel context (no crash on missing vars)."""
    pc = PromptCompiler(Path(__file__).parents[2] / "specs" / "05_vertical_saas_web" / "prompts")
    v = demo()
    state: Any = {"user_prompt": "CRM", "constraints": ["c1"], "tech_stack_hints": {}}
    text = pc.render_for_state("PLANNER.j2", state, skills=v.skills)
    assert "init_demo" in text and "CRM" in text and "- c1" in text


# ------------------------------------------------------------------ end to end
async def test_generic_vertical_e2e(tmp_path: Path, settings: Settings) -> None:
    from kernel.graph.nodes.coder import CodeChanges

    v = demo()
    llm = FakeLLM(
        [
            plan(("t1", []), ("t2", ["t1"]), ("t3", ["t2"])),
            code("src/extra.txt", "extra TODO"),  # t3 freeform -> no_todo fails
            code("src/extra.txt", "extra done"),  # fixer
            CodeChanges(file_changes=[FileChange(path="README.md", content="# Demo", action="create")]),
        ]
    )
    # attach skills to the planned tasks
    first = llm.script[0]
    llm.script[0] = first.model_copy(
        update={
            "task_graph": [
                first.task_graph[0].model_copy(update={"skill_id": "init_demo", "inputs": {"project_name": "shop"}}),
                first.task_graph[1].model_copy(update={"skill_id": "add_module", "inputs": {"name": "Cart"}}),
                first.task_graph[2],
            ]
        }
    )
    sandbox = LocalSandbox(tmp_path / "sbx")
    graph = build_graph(v, llm=llm, sandbox=sandbox, settings=settings, checkpointer=memory_checkpointer())
    _, result = await start_run(graph, GenerateRequest(prompt="shop", vertical_id="demo"), settings=settings)

    assert result["status"] == RunStatus.COMPLETED, result.get("error")
    assert [t.status for t in result["task_graph"]] == [TaskStatus.COMPLETED] * 3
    assert result["skill_outputs"]["init_demo"]["project_name"] == "shop"
    assert {"README.txt", "src/main.txt", "src/modules/cart.txt", "src/extra.txt", "README.md"} <= set(
        result["project_files"]
    )
    assert result["metadata"]["project_structure"] == {"src": "sources"}
    assert "DEMO PLANNER" in llm.calls[0]["messages"][0].content
    fixer_user = llm.calls[2]["messages"][1].content
    assert "src/extra.txt" in fixer_user  # parser -> files_to_fix -> fixer sees the file
    assert any("docs written: README.md" in line for line in result["logs"])


async def test_exclusive_gates_run_after_parallel_ones() -> None:
    import asyncio

    from kernel.graph.nodes.verifier import run_gates
    from kernel.state import VerificationGateResult, VerificationGateStatus

    order: list[str] = []

    class Gate:
        def __init__(self, gid: str, exclusive: bool, delay: float) -> None:
            self.id = self.name = gid
            self.description = ""
            self.exclusive = exclusive
            self.delay = delay

        async def execute(
            self, sandbox: object, sandbox_id: str, workspace: str, state: object
        ) -> VerificationGateResult:
            order.append(f"start:{self.id}")
            await asyncio.sleep(self.delay)
            order.append(f"end:{self.id}")
            return VerificationGateResult(
                gate_id=self.id,
                name=self.id,
                status=VerificationGateStatus.PASSED,
                command="",
                exit_code=0,
                duration_ms=0,
            )

    gates = [Gate("build", True, 0), Gate("lint", False, 0.02), Gate("tsc", False, 0.01)]
    from types import SimpleNamespace

    results = await run_gates(gates, SimpleNamespace(sandbox=None), "sb", "/workspace", {})  # type: ignore[arg-type]
    assert [r.gate_id for r in results] == ["lint", "tsc", "build"]
    assert order.index("start:build") > max(order.index("end:lint"), order.index("end:tsc"))


def test_mentioned_paths() -> None:
    from kernel.graph.nodes._common import mentioned_paths

    text = "Update src/app/dashboard/page.tsx and ./prisma/schema.prisma; see src/app/(auth)/login/page.tsx."
    assert mentioned_paths(text) == [
        "src/app/dashboard/page.tsx",
        "prisma/schema.prisma",
        "src/app/(auth)/login/page.tsx",
    ]
