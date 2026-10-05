"""
Knowledge-base context for planner / coder / fixer prompts (phase 4 integration; written by the agent).

``deps.retriever`` is an optional ``kernel.knowledge.retrieval.engine.RetrievalEngine`` (or anything with
the same ``retrieve_for_planning/coding/fixing`` methods). The result is rendered into
``metadata.rag_context`` of a *view* of the state that is only used for prompt rendering, so retrieved
text never bloats the checkpointed state. Retrieval errors are logged and ignored: RAG is an
enhancement, it must never fail a run.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, cast

from ...state import AgentState
from ..deps import KernelDeps

logger = logging.getLogger(__name__)


async def with_rag_context(
    state: AgentState,
    deps: KernelDeps,
    strategy: str,
    query: str,
    files: Sequence[str] = (),
) -> tuple[AgentState, list[str]]:
    """Return ``(state view with metadata.rag_context, retrieved chunk ids)``."""
    retriever = deps.retriever
    if retriever is None or not query.strip():
        return state, []
    k = deps.settings.knowledge
    try:
        if strategy == "planning":
            result = await retriever.retrieve_for_planning(query, state, top_k=k.top_k_planning)
        elif strategy == "fixing":
            result = await retriever.retrieve_for_fixing(query, state, failed_files=list(files), top_k=k.top_k_fixing)
        else:
            result = await retriever.retrieve_for_coding(query, state, current_files=list(files), top_k=k.top_k_coding)
        from ...knowledge.retrieval.engine import format_context

        context = format_context(result, max_chars=k.max_context_chars)
        ids = [h.id for h in result.hits]
    except Exception as exc:
        logger.warning("knowledge retrieval (%s) failed: %s", strategy, exc)
        return state, []
    if not context:
        return state, []
    metadata: dict[str, Any] = {**(state.get("metadata") or {}), "rag_context": context}
    return cast(AgentState, {**state, "metadata": metadata}), ids
