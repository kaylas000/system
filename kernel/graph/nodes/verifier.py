"""``verifier`` node: run verification gates for the current task in parallel."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ...protocols import IVerificationGate
from ...state import (
    AgentState,
    InterruptType,
    RunStatus,
    TaskStatus,
    VerificationGateResult,
    VerificationGateStatus,
    get_current_task,
    update_task_in_graph,
    utcnow,
)
from ..deps import KernelDeps
from ._common import log, merged_metadata

NODE = "verifier"


async def _run_gate(
    gate: IVerificationGate, deps: KernelDeps, sandbox_id: str, workspace: str, state: AgentState
) -> VerificationGateResult:
    start = time.perf_counter()
    try:
        return await gate.execute(deps.sandbox, sandbox_id, workspace, state)
    except Exception as exc:  # infrastructure failure, not a code failure
        return VerificationGateResult(
            gate_id=gate.id,
            name=gate.name,
            status=VerificationGateStatus.ERROR,
            command="",
            exit_code=-1,
            stderr=f"{type(exc).__name__}: {exc}",
            duration_ms=int((time.perf_counter() - start) * 1000),
        )


async def run_gates(
    gates: list[IVerificationGate], deps: KernelDeps, sandbox_id: str, workspace: str, state: AgentState
) -> list[VerificationGateResult]:
    """Run gates in parallel; gates with ``exclusive = True`` run afterwards, one at a time."""
    parallel = [g for g in gates if not getattr(g, "exclusive", False)]
    exclusive = [g for g in gates if getattr(g, "exclusive", False)]
    results = list(await asyncio.gather(*(_run_gate(g, deps, sandbox_id, workspace, state) for g in parallel)))
    for gate in exclusive:
        results.append(await _run_gate(gate, deps, sandbox_id, workspace, state))
    return results


async def verifier_node(state: AgentState, deps: KernelDeps) -> dict[str, Any]:
    task = get_current_task(state)
    if task is None:
        raise RuntimeError("verifier called without current task")

    gates = deps.vertical.get_verification_gates(state, task)
    sandbox_id = state["sandbox_id"]
    workspace = state.get("workspace_path", deps.settings.sandbox.workspace_path)
    results = await run_gates(gates, deps, sandbox_id, workspace, state)

    base: dict[str, Any] = {
        "current_gate_results": results,
        "verification_history": results,
        "updated_at": utcnow(),
    }
    summary = ", ".join(f"{r.gate_id}={r.status.value}" for r in results) or "no gates"

    infra = [r.gate_id for r in results if r.status == VerificationGateStatus.ERROR]
    if infra:
        return {
            **base,
            "error": f"Infrastructure failure in gates: {infra}",
            "interrupt_type": InterruptType.INFRA_ERROR,
            "status": RunStatus.NEEDS_HUMAN_INPUT,
            "logs": [log(NODE, f"{task.id}: {summary}")],
        }

    failed = [r for r in results if r.status == VerificationGateStatus.FAILED]
    if not failed:
        done = task.model_copy(
            update={
                "status": TaskStatus.COMPLETED,
                "verification_results": results,
                "error_summary": None,
            }
        )
        new_state: AgentState = {**state, "task_graph": update_task_in_graph(state, done)}
        await deps.vertical.on_task_complete(new_state, done)
        return {
            **base,
            "task_graph": new_state["task_graph"],
            "fix_attempt_count": 0,
            "status": RunStatus.CODING,
            "logs": [log(NODE, f"{task.id}: passed ({summary})")],
        }

    error_summary = "\n".join(f"[{r.gate_id}] {(r.stderr or r.stdout)[-2000:]}" for r in failed)
    failed_task = task.model_copy(
        update={
            "status": TaskStatus.VERIFICATION_FAILED,
            "verification_results": results,
            "error_summary": error_summary,
        }
    )
    files_to_fix = sorted({f for r in failed for f in r.files_to_fix})
    update: dict[str, Any] = {
        **base,
        "task_graph": update_task_in_graph(state, failed_task),
        "metadata": merged_metadata(state, files_to_fix=files_to_fix, failed_gate_ids=[r.gate_id for r in failed]),
    }
    max_retries = state.get("max_fix_retries", deps.settings.kernel.default_max_retries)
    if task.retry_count >= max_retries:
        return {
            **update,
            "error": f"Task {task.id} exceeded max fix retries ({task.retry_count}).",
            "interrupt_type": InterruptType.GATE_FAILURE,
            "status": RunStatus.NEEDS_HUMAN_INPUT,
            "logs": [log(NODE, f"{task.id}: failed after {task.retry_count} fixes ({summary})")],
        }
    return {
        **update,
        "fix_attempt_count": task.retry_count + 1,
        "status": RunStatus.FIXING,
        "logs": [log(NODE, f"{task.id}: failed ({summary}), fix attempt {task.retry_count + 1}/{max_retries}")],
    }
