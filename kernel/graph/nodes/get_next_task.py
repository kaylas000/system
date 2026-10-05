"""``get_next_task`` node: pure logic, no LLM. Selects the next runnable task."""

from __future__ import annotations

from typing import Any

from ...state import (
    AgentState,
    InterruptType,
    NodeName,
    RunStatus,
    TaskStatus,
    find_runnable_task,
    update_task_in_graph,
    utcnow,
)
from ..deps import KernelDeps
from ._common import log

NODE = "get_next_task"


async def get_next_task_node(state: AgentState, deps: KernelDeps | None = None) -> dict[str, Any]:
    tasks = state.get("task_graph", [])
    task = find_runnable_task(tasks)
    if task is not None:
        started = task.model_copy(update={"status": TaskStatus.IN_PROGRESS, "assigned_agent": NodeName.CODER})
        return {
            "task_graph": update_task_in_graph(state, started),
            "current_task_id": task.id,
            "current_gate_results": [],
            "fix_attempt_count": 0,
            "status": RunStatus.CODING,
            "updated_at": utcnow(),
            "logs": [log(NODE, f"next task {task.id}: {task.name}")],
        }

    blocked = [t.id for t in tasks if t.status not in (TaskStatus.COMPLETED, TaskStatus.SKIPPED)]
    if blocked:
        return {
            "current_task_id": None,
            "error": f"Deadlock: tasks {blocked} cannot run (unmet or failed dependencies).",
            "interrupt_type": InterruptType.NODE_ERROR,
            "failed_node": NODE,
            "status": RunStatus.NEEDS_HUMAN_INPUT,
            "logs": [log(NODE, f"deadlock, blocked={blocked}")],
        }
    return {
        "current_task_id": None,
        "status": RunStatus.DOCUMENTING,
        "updated_at": utcnow(),
        "logs": [log(NODE, "all tasks done")],
    }
