"""
``human_review`` node: pauses the graph with ``langgraph.types.interrupt``.

Resume with ``Command(resume={"action": ..., "edited_data": ..., "comment": ...})``.

Decision table (``specs/01_kernel/graph_topology.md`` §6, extended — ISSUES K-12):

=================  =====================================  ==============================
interrupt_type      action                                 next node
=================  =====================================  ==============================
any                 abort                                  __end__ (sandbox closed)
plan_review         approve                                get_next_task
plan_review         edit  (edited_data.task_graph)         get_next_task
plan_review/invalid reject | retry | approve(invalid)      planner (comment -> feedback)
gate_failure        retry | approve | edit(file_changes)   verifier (fix budget reset)
gate/infra/node     skip_gate                              get_next_task (task SKIPPED)
infra_error         retry                                  verifier
node_error          retry                                  failed node
=================  =====================================  ==============================
"""

from __future__ import annotations

import contextlib
from typing import Any

from langgraph.types import interrupt
from pydantic import ValidationError

from ...state import (
    AgentState,
    FileChange,
    InterruptType,
    RunStatus,
    Task,
    TaskStatus,
    get_current_task,
    update_task_in_graph,
    utcnow,
    validate_dag,
)
from ..deps import KernelDeps
from ._common import log, sanitize_changes

NODE = "human_review"

ACTIONS: dict[InterruptType, list[str]] = {
    InterruptType.PLAN_REVIEW: ["approve", "edit", "reject", "abort"],
    InterruptType.INVALID_PLAN: ["retry", "edit", "abort"],
    InterruptType.GATE_FAILURE: ["retry", "edit", "skip_gate", "abort"],
    InterruptType.INFRA_ERROR: ["retry", "skip_gate", "abort"],
    InterruptType.NODE_ERROR: ["retry", "skip_gate", "abort"],
    InterruptType.DESTRUCTIVE_ACTION: ["approve", "abort"],
    InterruptType.BUDGET_EXCEEDED: ["abort"],
}


def build_interrupt_payload(state: AgentState, itype: InterruptType) -> dict[str, Any]:
    task = get_current_task(state)
    payload: dict[str, Any] = {"error": state.get("error")}
    if itype in (InterruptType.PLAN_REVIEW, InterruptType.INVALID_PLAN):
        payload["spec_markdown"] = state.get("spec_markdown", "")
        payload["task_graph"] = [t.model_dump(mode="json") for t in state.get("task_graph", [])]
    if task is not None:
        payload["task"] = task.model_dump(mode="json", exclude={"file_changes"})
        payload["gate_results"] = [r.model_dump(mode="json") for r in state.get("current_gate_results", [])]
    if state.get("failed_node"):
        payload["failed_node"] = state.get("failed_node")
    return {
        "run_id": state.get("run_id"),
        "interrupt_type": itype.value,
        "payload": payload,
        "actions": ACTIONS.get(itype, ["abort"]),
        "created_at": utcnow().isoformat(),
    }


def _skip_current(state: AgentState) -> dict[str, Any]:
    task = get_current_task(state)
    if task is None:
        return {"next_after_human": "get_next_task"}
    skipped = task.model_copy(update={"status": TaskStatus.SKIPPED})
    return {"task_graph": update_task_in_graph(state, skipped), "next_after_human": "get_next_task"}


def _reset_fix_budget(state: AgentState) -> dict[str, Any]:
    task = get_current_task(state)
    if task is None:
        return {}
    fresh = task.model_copy(update={"retry_count": 0, "status": TaskStatus.IN_PROGRESS})
    return {"task_graph": update_task_in_graph(state, fresh), "fix_attempt_count": 0}


async def human_review_node(state: AgentState, deps: KernelDeps) -> dict[str, Any]:
    itype = InterruptType(state.get("interrupt_type") or InterruptType.NODE_ERROR)
    decision_raw = interrupt(build_interrupt_payload(state, itype))
    decision: dict[str, Any] = dict(decision_raw) if isinstance(decision_raw, dict) else {"action": str(decision_raw)}
    action = str(decision.get("action", "")).lower()
    edited = decision.get("edited_data") or {}

    base: dict[str, Any] = {
        "human_decision": decision,
        "human_interrupt_reason": state.get("error"),
        "updated_at": utcnow(),
    }
    cleared: dict[str, Any] = {"error": None, "interrupt_type": None, "failed_node": None}

    def reject(msg: str) -> dict[str, Any]:  # keep waiting: loop back into human_review
        return {**base, "next_after_human": NODE, "logs": [log(NODE, msg)]}

    if action not in ACTIONS.get(itype, ["abort"]):
        return reject(f"action '{action}' not allowed for {itype.value}")

    if action == "abort":
        if state.get("sandbox_id"):
            with contextlib.suppress(Exception):
                await deps.sandbox.close(state["sandbox_id"])
        return {
            **base,
            "status": RunStatus.FAILED,
            "next_after_human": "__end__",
            "logs": [log(NODE, f"aborted by human ({itype.value})")],
        }

    update: dict[str, Any] = {**base, **cleared, "status": RunStatus.CODING}

    if itype in (InterruptType.PLAN_REVIEW, InterruptType.INVALID_PLAN):
        if action == "edit":
            try:
                tasks = [Task.model_validate(t) for t in edited.get("task_graph", [])]
            except ValidationError as exc:
                return reject(f"edited task_graph invalid: {exc.error_count()} error(s)")
            problem = "empty task graph" if not tasks else validate_dag(tasks)
            if problem:
                return reject(f"edited task_graph rejected: {problem}")
            update.update(task_graph=tasks, current_task_id=None, next_after_human="get_next_task")
        elif action == "approve" and itype == InterruptType.PLAN_REVIEW:
            update["next_after_human"] = "get_next_task"
        else:  # reject / retry -> re-plan with human feedback
            update.update(next_after_human="planner", status=RunStatus.PLANNING)
        update["logs"] = [log(NODE, f"{itype.value}: {action} -> {update['next_after_human']}")]
        return update

    if action == "skip_gate":
        update.update(_skip_current(state))
    elif itype == InterruptType.GATE_FAILURE:
        if action == "edit" and edited.get("file_changes"):
            changes = sanitize_changes([FileChange.model_validate(c) for c in edited["file_changes"]])
            workspace = state.get("workspace_path", deps.settings.sandbox.workspace_path)
            await deps.sandbox.write_files(state["sandbox_id"], changes, workspace)
        update.update(_reset_fix_budget(state), next_after_human="verifier", status=RunStatus.VERIFYING)
    elif itype == InterruptType.INFRA_ERROR:
        update.update(next_after_human="verifier", status=RunStatus.VERIFYING)
    elif itype == InterruptType.NODE_ERROR:
        update["next_after_human"] = state.get("failed_node") or "__end__"
    elif itype == InterruptType.DESTRUCTIVE_ACTION:
        update["next_after_human"] = state.get("failed_node") or "__end__"
    update["logs"] = [log(NODE, f"{itype.value}: {action} -> {update['next_after_human']}")]
    return update
