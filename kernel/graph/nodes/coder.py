"""``coder`` node: execute a Skill or freeform LLM coding, apply FileChanges to the sandbox."""

from __future__ import annotations

import json
from typing import Any, cast

from pydantic import BaseModel, Field

from ...protocols import LLMMessage
from ...state import AgentState, FileChange, RunStatus, Task, TokenUsage, get_current_task, update_task_in_graph, utcnow
from ..deps import KernelDeps
from ._common import (
    add_usage,
    content_hashes,
    list_workspace_files,
    log,
    mentioned_paths,
    read_context_files,
    sanitize_changes,
)

NODE = "coder"


class CodeChanges(BaseModel):
    """Structured output for freeform coding and fixing."""

    file_changes: list[FileChange] = Field(default_factory=list)
    summary: str = ""


def task_user_message(task: Task) -> str:
    return (
        f"TASK {task.id}: {task.name}\n\n{task.description}\n\n"
        f"INPUTS: {task.inputs}\n\nReturn CodeChanges JSON with the complete content of every changed file."
    )


async def coder_node(state: AgentState, deps: KernelDeps) -> dict[str, Any]:
    task = get_current_task(state)
    if task is None:
        raise RuntimeError("coder called without current task")

    sandbox_id = state["sandbox_id"]
    workspace = state.get("workspace_path", deps.settings.sandbox.workspace_path)
    usage: TokenUsage = state.get("token_usage") or TokenUsage()

    extra: dict[str, Any] = {}
    if task.skill_id and task.skill_id in deps.vertical.skills:
        executor = deps.vertical.get_skill_executor(task.skill_id)
        result = await executor.execute(deps.sandbox, sandbox_id, workspace, task.inputs, state)
        changes = list(result.file_changes)
        extra["skill_outputs"] = {**(state.get("skill_outputs") or {}), task.skill_id: dict(result.outputs)}
        how = f"skill {task.skill_id}"
    else:
        tree = await list_workspace_files(deps.sandbox, sandbox_id, workspace)
        wanted: list[str] = list(getattr(deps.vertical, "context_files", lambda s, t: [])(state, task))
        wanted += mentioned_paths(task.description, json.dumps(task.inputs, default=str))
        present = set(tree)
        open_files = await read_context_files(
            deps.sandbox, sandbox_id, workspace, [p for p in dict.fromkeys(wanted) if p in present]
        )
        view = cast(AgentState, {**state, "file_tree": tree, "open_files": open_files})
        messages = [
            LLMMessage(role="system", content=deps.vertical.get_coder_prompt(view, task)),
            LLMMessage(role="user", content=task_user_message(task)),
        ]
        resp = await deps.llm.achat(messages, model=deps.settings.llm.default_model, response_model=CodeChanges)
        usage = add_usage(usage, resp)
        output = resp.parsed if isinstance(resp.parsed, CodeChanges) else CodeChanges.model_validate_json(resp.content)
        changes = sanitize_changes(output.file_changes)
        await deps.sandbox.write_files(sandbox_id, changes, workspace)
        how = "freeform coding"

    updated = task.model_copy(update={"file_changes": [*task.file_changes, *changes]})
    return {
        "task_graph": update_task_in_graph(state, updated),
        "project_files": content_hashes(changes),
        "token_usage": usage,
        "status": RunStatus.VERIFYING,
        "updated_at": utcnow(),
        "logs": [log(NODE, f"{task.id}: {how}, {len(changes)} file change(s)")],
        **extra,
    }
