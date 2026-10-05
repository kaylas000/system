"""``fixer`` node: analyse failed gates and produce patches."""

from __future__ import annotations

from typing import Any

from ...protocols import LLMMessage
from ...state import (
    AgentState,
    InterruptType,
    RunStatus,
    TaskStatus,
    TokenUsage,
    VerificationGateStatus,
    get_current_task,
    update_task_in_graph,
    utcnow,
)
from ..deps import KernelDeps
from ._common import add_usage, content_hashes, log, sanitize_changes
from ._rag import with_rag_context
from .coder import CodeChanges

NODE = "fixer"
MAX_FILE_CHARS = 20_000


async def _read_files(deps: KernelDeps, sandbox_id: str, workspace: str, paths: list[str]) -> str:
    chunks: list[str] = []
    for path in paths[:10]:
        full = path if path.startswith("/") else f"{workspace.rstrip('/')}/{path}"
        try:
            content = await deps.sandbox.read_file(sandbox_id, full)
        except Exception as exc:
            content = f"<unreadable: {exc}>"
        chunks.append(f"--- {path} ---\n{content[:MAX_FILE_CHARS]}")
    return "\n\n".join(chunks)


async def fixer_node(state: AgentState, deps: KernelDeps) -> dict[str, Any]:
    task = get_current_task(state)
    if task is None:
        raise RuntimeError("fixer called without current task")

    sandbox_id = state["sandbox_id"]
    workspace = state.get("workspace_path", deps.settings.sandbox.workspace_path)
    failed = [r for r in state.get("current_gate_results", []) if r.status == VerificationGateStatus.FAILED]
    files_to_fix = list((state.get("metadata") or {}).get("files_to_fix", []))
    if not files_to_fix:  # gate without a parser: show the files this task touched
        files_to_fix = list(dict.fromkeys(c.path for c in task.file_changes if c.action != "delete"))[-10:]
    report = "\n\n".join(
        f"GATE {r.gate_id} (`{r.command}`, exit {r.exit_code}):\n{r.stdout[-4000:]}\n{r.stderr[-4000:]}" for r in failed
    )
    user = (
        f"TASK {task.id}: {task.name}\n{task.description}\n\nFAILED GATES:\n{report}\n\n"
        f"FILES:\n{await _read_files(deps, sandbox_id, workspace, files_to_fix)}\n\n"
        "Return CodeChanges JSON with the complete fixed content of every file you change."
    )
    view, rag_ids = await with_rag_context(state, deps, "fixing", report, files=files_to_fix)
    messages = [
        LLMMessage(role="system", content=deps.vertical.get_fixer_prompt(view, task)),
        LLMMessage(role="user", content=user),
    ]
    usage: TokenUsage = state.get("token_usage") or TokenUsage()
    resp = await deps.llm.achat(messages, model=deps.settings.llm.fixer_model, response_model=CodeChanges)
    usage = add_usage(usage, resp)
    output = resp.parsed if isinstance(resp.parsed, CodeChanges) else CodeChanges.model_validate_json(resp.content)
    changes = sanitize_changes(output.file_changes)

    retried = task.model_copy(update={"retry_count": task.retry_count + 1, "status": TaskStatus.IN_PROGRESS})
    if not changes:
        return {
            "task_graph": update_task_in_graph(state, retried),
            "token_usage": usage,
            "error": f"Fixer failed: no patches produced for {task.id}.",
            "interrupt_type": InterruptType.GATE_FAILURE,
            "status": RunStatus.NEEDS_HUMAN_INPUT,
            "logs": [log(NODE, f"{task.id}: no patches")],
        }

    await deps.sandbox.write_files(sandbox_id, changes, workspace)
    retried = retried.model_copy(update={"file_changes": [*task.file_changes, *changes]})
    return {
        "task_graph": update_task_in_graph(state, retried),
        "project_files": content_hashes(changes),
        "token_usage": usage,
        "status": RunStatus.VERIFYING,
        "updated_at": utcnow(),
        "logs": [
            log(
                NODE,
                f"{task.id}: attempt {retried.retry_count}, {len(changes)} patch(es)"
                + (f", rag {len(rag_ids)} chunk(s)" if rag_ids else ""),
            )
        ],
    }
