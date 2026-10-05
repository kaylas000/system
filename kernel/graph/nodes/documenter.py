"""``documenter`` node: vertical ``finalize`` + LLM-written docs (README.md, ARCHITECTURE.md).

Docs are written only if the vertical provides ``get_documenter_prompt(state, doc_sources)``
(optional extension of ``IVertical``) and ``settings.kernel.generate_docs`` is on. Only
Markdown files are accepted from the LLM. A documentation failure is logged, not fatal:
the code is already verified and should still be packaged.
"""

from __future__ import annotations

from typing import Any

from ...protocols import LLMMessage
from ...state import AgentState, FileChange, RunStatus, TokenUsage, utcnow
from ..deps import KernelDeps
from ._common import add_usage, content_hashes, log, merged_metadata, sanitize_changes

NODE = "documenter"
DOC_SOURCES = ("package.json", "pyproject.toml", ".env.example", "prisma/schema.prisma", "docker-compose.yml")
MAX_SOURCE_CHARS = 6000


async def _doc_sources(state: AgentState, deps: KernelDeps, workspace: str) -> list[dict[str, str]]:
    files = state.get("project_files") or {}
    out: list[dict[str, str]] = []
    for path in DOC_SOURCES:
        if files and path not in files:
            continue
        try:
            content = await deps.sandbox.read_file(state["sandbox_id"], f"{workspace.rstrip('/')}/{path}")
        except Exception:
            continue
        out.append({"path": path, "content": content[:MAX_SOURCE_CHARS]})
    return out


async def _write_docs(state: AgentState, deps: KernelDeps) -> dict[str, Any]:
    from .coder import CodeChanges

    workspace = state.get("workspace_path", deps.settings.sandbox.workspace_path)
    prompt_fn = deps.vertical.get_documenter_prompt  # type: ignore[attr-defined]
    system = prompt_fn(state, await _doc_sources(state, deps, workspace))
    messages = [
        LLMMessage(role="system", content=system),
        LLMMessage(role="user", content="Return CodeChanges JSON with README.md and ARCHITECTURE.md."),
    ]
    resp = await deps.llm.achat(messages, model=deps.settings.llm.default_model, response_model=CodeChanges)
    usage = add_usage(state.get("token_usage") or TokenUsage(), resp)
    output = resp.parsed if isinstance(resp.parsed, CodeChanges) else CodeChanges.model_validate_json(resp.content)
    docs: list[FileChange] = [
        c.model_copy(update={"action": "update" if c.path in (state.get("project_files") or {}) else "create"})
        for c in sanitize_changes(output.file_changes)
        if c.path.lower().endswith(".md") and c.action != "delete"
    ]
    if docs:
        await deps.sandbox.write_files(state["sandbox_id"], docs, workspace)
    return {
        "token_usage": usage,
        "project_files": content_hashes(docs),
        "logs": [log(NODE, f"docs written: {', '.join(c.path for c in docs) or 'none'}")],
    }


async def documenter_node(state: AgentState, deps: KernelDeps) -> dict[str, Any]:
    delta = dict(await deps.vertical.finalize(state) or {})
    vertical_logs = list(delta.pop("logs", []))
    meta = delta.pop("metadata", None)
    update: dict[str, Any] = {**delta, "status": RunStatus.PACKAGING, "updated_at": utcnow()}
    logs = [*vertical_logs]

    if deps.settings.kernel.generate_docs and callable(getattr(deps.vertical, "get_documenter_prompt", None)):
        try:
            docs = await _write_docs(state, deps)
        except Exception as exc:
            logs.append(log(NODE, f"WARNING documentation skipped: {type(exc).__name__}: {exc}"))
        else:
            logs.extend(docs.pop("logs"))
            update.update(docs)

    if meta:
        update["metadata"] = merged_metadata(state, **meta)
    update["logs"] = [*logs, log(NODE, "documentation finalized")]
    return update
