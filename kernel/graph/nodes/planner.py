"""``planner`` node: PRD -> SPEC.md, ARCHITECTURE.md and a task DAG."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, ValidationError

from ...protocols import LLMMessage
from ...state import AgentState, InterruptType, RunStatus, Task, TokenUsage, utcnow, validate_dag
from ..deps import KernelDeps
from ._common import add_usage, log
from ._rag import with_rag_context

NODE = "planner"
MAX_ATTEMPTS = 2  # spec: 1 retry on parse failure


class PlannedTask(BaseModel):
    id: str
    name: str
    description: str
    skill_id: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)


class PlannerOutput(BaseModel):
    spec_markdown: str
    architecture_markdown: str = ""
    task_graph: list[PlannedTask]


def build_user_message(state: AgentState) -> str:
    parts = [f"USER REQUIREMENTS:\n{state.get('user_prompt', '')}"]
    if state.get("constraints"):
        parts.append("CONSTRAINTS:\n" + "\n".join(f"- {c}" for c in state.get("constraints", [])))
    feedback = (state.get("human_decision") or {}).get("comment")
    if feedback:
        parts.append(f"HUMAN FEEDBACK ON PREVIOUS PLAN:\n{feedback}")
    parts.append("Return the PlannerOutput JSON.")
    return "\n\n".join(parts)


def tasks_from_plan(plan: PlannerOutput, known_skills: set[str]) -> list[Task]:
    tasks: list[Task] = []
    for pt in plan.task_graph:
        skill_id = pt.skill_id if pt.skill_id and pt.skill_id in known_skills else None
        tasks.append(
            Task(
                id=pt.id,
                name=pt.name,
                description=pt.description,
                skill_id=skill_id,
                inputs=pt.inputs,
                depends_on=pt.depends_on,
            )
        )
    return tasks


async def planner_node(state: AgentState, deps: KernelDeps) -> dict[str, Any]:
    view, rag_ids = await with_rag_context(state, deps, "planning", str(state.get("user_prompt", "")))
    messages = [
        LLMMessage(role="system", content=deps.vertical.get_planner_prompt(view)),
        LLMMessage(role="user", content=build_user_message(state)),
    ]
    usage: TokenUsage = state.get("token_usage") or TokenUsage()
    plan: PlannerOutput | None = None
    last_error = "no output"
    for _attempt in range(MAX_ATTEMPTS):
        resp = await deps.llm.achat(messages, model=deps.settings.llm.planner_model, response_model=PlannerOutput)
        usage = add_usage(usage, resp)
        try:
            plan = (
                resp.parsed
                if isinstance(resp.parsed, PlannerOutput)
                else PlannerOutput.model_validate_json(resp.content)
            )
            break
        except (ValidationError, ValueError) as exc:
            last_error = str(exc)[:500]

    base: dict[str, Any] = {"token_usage": usage, "updated_at": utcnow(), "human_decision": None}
    if plan is None:
        return {
            **base,
            "error": f"Planner output could not be parsed: {last_error}",
            "interrupt_type": InterruptType.INVALID_PLAN,
            "status": RunStatus.NEEDS_HUMAN_INPUT,
            "logs": [log(NODE, "failed to parse planner output")],
        }

    tasks = tasks_from_plan(plan, set(deps.vertical.skills))
    max_tasks = int(deps.vertical.manifest.planner.get("max_tasks", 0) or 0)
    problem = None
    if not tasks:
        problem = "Planner produced empty task graph."
    elif max_tasks and len(tasks) > max_tasks:
        problem = f"Planner produced {len(tasks)} tasks, limit is {max_tasks}."
    else:
        dag_error = validate_dag(tasks)
        if dag_error:
            problem = f"Planner produced invalid DAG: {dag_error}"

    update: dict[str, Any] = {
        **base,
        "spec_markdown": plan.spec_markdown,
        "architecture_markdown": plan.architecture_markdown,
        "task_graph": tasks,
        "current_task_id": None,
    }
    if problem:
        return {
            **update,
            "error": problem,
            "interrupt_type": InterruptType.INVALID_PLAN,
            "status": RunStatus.NEEDS_HUMAN_INPUT,
            "logs": [log(NODE, problem)],
        }
    if deps.settings.hitl.plan_review:
        return {
            **update,
            "error": None,
            "interrupt_type": InterruptType.PLAN_REVIEW,
            "status": RunStatus.NEEDS_HUMAN_INPUT,
            "logs": [log(NODE, f"plan with {len(tasks)} tasks awaits human review")],
        }
    return {
        **update,
        "error": None,
        "interrupt_type": None,
        "status": RunStatus.CODING,
        "logs": [log(NODE, f"plan with {len(tasks)} tasks" + (f", rag {len(rag_ids)} chunk(s)" if rag_ids else ""))],
    }
