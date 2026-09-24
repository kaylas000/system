"""``packager`` node: archive the workspace and produce ``ArtifactMetadata``."""

from __future__ import annotations

import contextlib
from typing import Any

from ...state import (
    AgentState,
    ArtifactMetadata,
    RunStatus,
    TaskStatus,
    TokenUsage,
    VerificationGateStatus,
    utcnow,
)
from ..deps import KernelDeps
from ._common import log
from .verifier import _run_gate

NODE = "packager"


async def packager_node(state: AgentState, deps: KernelDeps) -> dict[str, Any]:
    run_id = state.get("run_id", "run")
    sandbox_id = state["sandbox_id"]
    workspace = state.get("workspace_path", deps.settings.sandbox.workspace_path)
    out_dir = deps.settings.artifacts.local_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = out_dir / f"{run_id}.tar.gz"

    # Project-level verification (every blocking gate once, sequentially - `next build` and
    # linters must not race on the same workspace). Recorded in the quality report, not blocking:
    # all per-task gates already passed at this point.
    final: list[Any] = []
    if deps.settings.kernel.final_verification:
        for gate in deps.vertical.get_verification_gates(state, None):
            final.append(await _run_gate(gate, deps, sandbox_id, workspace, state))

    await deps.sandbox.download_dir(sandbox_id, workspace, str(artifact_path))

    tasks = state.get("task_graph", [])
    created = state.get("created_at")
    duration = (utcnow() - created).total_seconds() if created else 0.0
    artifact = ArtifactMetadata(
        project_name=str((state.get("metadata") or {}).get("project_name") or run_id),
        vertical_id=state.get("vertical_id", ""),
        tech_stack=state.get("tech_stack_hints", {}),
        artifact_path=str(artifact_path),
        quality_report={
            "tasks_total": len(tasks),
            "tasks_completed": sum(t.status == TaskStatus.COMPLETED for t in tasks),
            "tasks_skipped": sum(t.status == TaskStatus.SKIPPED for t in tasks),
            "gate_runs": len(state.get("verification_history", [])),
            "final_verification": {r.gate_id: r.status.value for r in final},
            "final_verification_passed": all(r.status == VerificationGateStatus.PASSED for r in final),
        },
        total_token_usage=state.get("token_usage") or TokenUsage(),
        duration_seconds=round(duration, 3),
    )
    summary = ", ".join(f"{r.gate_id}={r.status.value}" for r in final)
    with contextlib.suppress(Exception):  # sandbox cleanup is best-effort
        await deps.sandbox.close(sandbox_id)
    return {
        "final_artifact": artifact,
        "status": RunStatus.COMPLETED,
        "updated_at": utcnow(),
        "verification_history": final,
        "logs": [
            *([log(NODE, f"final verification: {summary}")] if final else []),
            log(NODE, f"artifact {artifact_path}"),
        ],
    }
