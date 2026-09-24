"""Test doubles: scripted LLM, minimal vertical, shell-based gate."""

from __future__ import annotations

import shlex
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from kernel.graph.nodes.coder import CodeChanges
from kernel.graph.nodes.planner import PlannedTask, PlannerOutput
from kernel.protocols import (
    GenerateRequest,
    ISandbox,
    IVerificationGate,
    LLMMessage,
    LLMResponse,
    SkillDef,
    VerticalManifest,
)
from kernel.state import AgentState, FileChange, Task, VerificationGateResult, VerificationGateStatus, get_current_task

Scripted = BaseModel | Exception | Callable[[list[LLMMessage], str, type[BaseModel] | None], BaseModel]


class FakeLLM:
    """Returns scripted responses in order; records every call."""

    def __init__(self, script: list[Scripted]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def achat(
        self,
        messages: list[LLMMessage],
        model: str,
        response_model: type[BaseModel] | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        self.calls.append({"messages": messages, "model": model, "response_model": response_model})
        if not self.script:
            raise AssertionError(f"FakeLLM script exhausted (call #{len(self.calls)}, model={model})")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        out = item if isinstance(item, BaseModel) else item(messages, model, response_model)
        if response_model is not None:
            assert isinstance(out, response_model), f"expected {response_model.__name__}, got {type(out).__name__}"
        return LLMResponse(
            content=out.model_dump_json(),
            parsed=out,
            model=model,
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            cost_usd=0.001,
        )

    async def astream_chat(self, messages: list[LLMMessage], model: str, **kwargs: Any):  # type: ignore[no-untyped-def]
        yield "fake"

    def estimate_tokens(self, messages: list[LLMMessage], model: str) -> int:
        return sum(len(m.content) // 4 for m in messages)


class NoBugGate:
    """Fails if a file touched by the current task contains BUG (uses real sandbox exec)."""

    id = "no_bug"
    name = "No BUG marker"
    description = "grep for BUG"
    runs_on_every_task = True

    def __init__(self) -> None:
        self.runs = 0

    async def execute(
        self, sandbox: ISandbox, sandbox_id: str, workspace: str, state: AgentState
    ) -> VerificationGateResult:
        self.runs += 1
        task = get_current_task(state)
        paths = sorted({c.path for c in task.file_changes}) if task else []
        cmd = "grep -l BUG " + " ".join(shlex.quote(p) for p in paths) + " 2>/dev/null || true" if paths else "true"
        res = await sandbox.exec(sandbox_id, cmd, workdir=workspace)
        files = [line.removeprefix("./") for line in res.stdout.split() if line]
        return VerificationGateResult(
            gate_id=self.id,
            name=self.name,
            status=VerificationGateStatus.FAILED if files else VerificationGateStatus.PASSED,
            command=cmd,
            exit_code=1 if files else 0,
            stdout=res.stdout,
            stderr="BUG found in: " + ", ".join(files) if files else "",
            duration_ms=res.duration_ms,
            files_to_fix=files,
        )


class ExplodingGate(NoBugGate):
    id = "exploding"
    name = "Exploding gate"

    async def execute(
        self, sandbox: ISandbox, sandbox_id: str, workspace: str, state: AgentState
    ) -> VerificationGateResult:
        self.runs += 1
        raise ConnectionError("sandbox unreachable")


class FakeVertical:
    def __init__(self, gates: list[IVerificationGate] | None = None, max_tasks: int = 25) -> None:
        self._manifest = VerticalManifest(
            id="fake",
            name="Fake Vertical",
            version="0.0.1",
            tech_stack={"language": "text"},
            planner={"max_tasks": max_tasks},
        )
        self.gates: list[IVerificationGate] = gates if gates is not None else [NoBugGate()]
        self.completed: list[str] = []
        self.finalized = False

    @property
    def manifest(self) -> VerticalManifest:
        return self._manifest

    @property
    def skills(self) -> dict[str, SkillDef]:
        return {}

    async def initialize_state(self, request: GenerateRequest) -> dict[str, Any]:
        return {"metadata": {"project_name": "demo"}}

    def get_planner_prompt(self, state: AgentState) -> str:
        return "You are a planner."

    def get_coder_prompt(self, state: AgentState, task: Task) -> str:
        return f"You are a coder for {task.id}."

    def get_fixer_prompt(self, state: AgentState, task: Task) -> str:
        return "You are a fixer."

    def get_verification_gates(self, state: AgentState, task: Task | None) -> list[IVerificationGate]:
        return self.gates

    def get_skill_executor(self, skill_id: str) -> Any:
        raise KeyError(skill_id)

    async def on_task_complete(self, state: AgentState, task: Task) -> None:
        self.completed.append(task.id)

    async def finalize(self, state: AgentState) -> dict[str, Any]:
        self.finalized = True
        return {"metadata": {"readme": "generated"}, "logs": ["fake vertical finalized"]}


# -- script helpers ------------------------------------------------------------------


def plan(*tasks: tuple[str, list[str]]) -> PlannerOutput:
    return PlannerOutput(
        spec_markdown="# SPEC",
        architecture_markdown="# ARCH",
        task_graph=[
            PlannedTask(id=tid, name=f"Task {tid}", description=f"do {tid}", depends_on=deps) for tid, deps in tasks
        ],
    )


def code(path: str, content: str) -> CodeChanges:
    return CodeChanges(file_changes=[FileChange(path=path, content=content, action="create")], summary="ok")
