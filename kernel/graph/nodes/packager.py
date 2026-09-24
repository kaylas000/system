"""``packager`` node: archive the workspace and produce ``ArtifactMetadata``."""

from __future__ import annotations

import contextlib
from typing import Any

from ...state import AgentState, ArtifactMetadata, RunStatus, TaskStatus, TokenUsage, utcnow
from ..deps import KernelDeps
from ._common import log

NODE = "packager"


async def packager_node(state: AgentState, deps: KernelDeps) -> dict[str, Any]:
    run_id = state.get("run_id", "run")
    sandbox_id = state["sandbox_id"]
    workspace = state.get("workspace_path", deps.settings.sandbox.workspace_path)
    out_dir = deps.settings.artifacts.local_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = out_dir / f"{run_id}.tar.gz"

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
        },
        total_token_usage=state.get("token_usage") or TokenUsage(),
        duration_seconds=round(duration, 3),
    )
    with contextlib.suppress(Exception):  # sandbox cleanup is best-effort
        await deps.sandbox.close(sandbox_id)
    return {
        "final_artifact": artifact,
        "status": RunStatus.COMPLETED,
        "updated_at": utcnow(),
        "logs": [log(NODE, f"artifact {artifact_path}")],
    }
