"""High-level helpers to start / resume kernel runs on a compiled graph."""

from __future__ import annotations

import uuid
from typing import Any

from langgraph.types import Command

from .config import Settings, get_settings
from .protocols import GenerateRequest
from .state import AgentState, RunStatus, TokenUsage, utcnow


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:12]}"


def initial_state(request: GenerateRequest, run_id: str, settings: Settings | None = None) -> AgentState:
    settings = settings or get_settings()
    now = utcnow()
    return AgentState(
        run_id=run_id,
        thread_id=run_id,
        status=RunStatus.QUEUED,
        created_at=now,
        updated_at=now,
        user_prompt=request.prompt,
        vertical_id=request.vertical_id or "",
        tech_stack_hints=dict(request.tech_stack_hints),
        constraints=list(request.constraints),
        context_files=list(request.context_files),
        max_budget_usd=request.max_budget_usd,
        task_graph=[],
        current_task_id=None,
        project_files={},
        verification_history=[],
        current_gate_results=[],
        fix_attempt_count=0,
        max_fix_retries=settings.kernel.default_max_retries,
        final_artifact=None,
        token_usage=TokenUsage(),
        logs=[],
        human_interrupt_reason=None,
        error=None,
        interrupt_type=None,
        human_decision=None,
        next_after_human=None,
        failed_node=None,
        metadata={},
    )


def run_config(run_id: str, settings: Settings | None = None, **configurable: Any) -> dict[str, Any]:
    """LangGraph config. Explicit ``settings`` are also passed to nodes via ``configurable``."""
    if settings is not None:
        configurable.setdefault("settings", settings)
    return {
        "configurable": {"thread_id": run_id, **configurable},
        "recursion_limit": (settings or get_settings()).kernel.recursion_limit,
    }


async def start_run(
    graph: Any,
    request: GenerateRequest,
    *,
    run_id: str | None = None,
    settings: Settings | None = None,
    **configurable: Any,
) -> tuple[str, dict[str, Any]]:
    """Run until completion or the first interrupt. Returns ``(run_id, final_values)``."""
    run_id = run_id or new_run_id()
    result = await graph.ainvoke(initial_state(request, run_id, settings), run_config(run_id, settings, **configurable))
    return run_id, result


async def resume_run(
    graph: Any, run_id: str, decision: dict[str, Any], *, settings: Settings | None = None, **configurable: Any
) -> dict[str, Any]:
    """Resume an interrupted run with a human decision ``{"action", "comment"?, "edited_data"?}``."""
    result: dict[str, Any] = await graph.ainvoke(Command(resume=decision), run_config(run_id, settings, **configurable))
    return result


async def pending_interrupts(graph: Any, run_id: str) -> list[Any]:
    """Return interrupt payloads currently waiting for a human (empty if none)."""
    snapshot = await graph.aget_state({"configurable": {"thread_id": run_id}})
    return [i.value for task in snapshot.tasks for i in task.interrupts]
