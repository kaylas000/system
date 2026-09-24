"""``documenter`` node: delegate final docs / artifacts to the vertical."""

from __future__ import annotations

from typing import Any

from ...state import AgentState, RunStatus, utcnow
from ..deps import KernelDeps
from ._common import log, merged_metadata

NODE = "documenter"


async def documenter_node(state: AgentState, deps: KernelDeps) -> dict[str, Any]:
    delta = dict(await deps.vertical.finalize(state) or {})
    vertical_logs = list(delta.pop("logs", []))
    meta = delta.pop("metadata", None)
    update: dict[str, Any] = {
        **delta,
        "status": RunStatus.PACKAGING,
        "updated_at": utcnow(),
        "logs": [*vertical_logs, log(NODE, "documentation finalized")],
    }
    if meta:
        update["metadata"] = merged_metadata(state, **meta)
    return update
