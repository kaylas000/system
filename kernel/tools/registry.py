"""
Central Tool Registry (port of ``specs/02_infra/tools/TOOL_REGISTRY.py``).

Differences from the spec (ISSUES I-01): ``sandbox_manager`` is optional (the
spec referenced an undefined ``SandboxManager``); ``AgentState`` is imported
from ``kernel.state``; latencies are kept as a bounded window.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from typing import Any

from ..protocols import ITool, ToolResult
from ..state import AgentState

logger = logging.getLogger("autogen.tools")
LATENCY_WINDOW = 1000


class ToolRegistry:
    def __init__(self, sandbox_manager: Any | None = None) -> None:
        self._tools: dict[str, ITool] = {}
        self._sandbox_manager = sandbox_manager
        self._call_counts: dict[str, int] = {}
        self._error_counts: dict[str, int] = {}
        self._latencies: dict[str, deque[float]] = {}

    def register(self, tool: ITool) -> None:
        if tool.name in self._tools:
            logger.warning("Overriding tool: %s", tool.name)
        self._tools[tool.name] = tool
        logger.info("Registered tool: %s", tool.name)

    def get(self, name: str) -> ITool | None:
        return self._tools.get(name)

    @property
    def names(self) -> list[str]:
        return sorted(self._tools)

    def get_all_schemas(self) -> list[dict[str, Any]]:
        """Return OpenAI Function Calling compatible schemas."""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters_json_schema,
                },
            }
            for tool in self._tools.values()
        ]

    async def execute(
        self, tool_name: str, args: dict[str, Any], state: AgentState, sandbox_id: str | None = None
    ) -> ToolResult:
        """Main entry point. Injects sandbox_id from state if not provided."""
        tool = self._tools.get(tool_name)
        if tool is None:
            return ToolResult(success=False, error=f"Tool '{tool_name}' not found")
        target_sandbox_id = sandbox_id or state.get("sandbox_id")
        if not target_sandbox_id:
            return ToolResult(success=False, error="No sandbox_id in state or args")

        start = time.perf_counter()
        self._call_counts[tool_name] = self._call_counts.get(tool_name, 0) + 1
        try:
            logger.debug("Executing tool %s args=%s", tool_name, json.dumps(args, default=str)[:200])
            result = await tool.execute(target_sandbox_id, args, state)
        except Exception as exc:
            self._error_counts[tool_name] = self._error_counts.get(tool_name, 0) + 1
            logger.exception("Tool %s crashed", tool_name)
            return ToolResult(success=False, error=f"Tool execution exception: {exc}")
        finally:
            self._latencies.setdefault(tool_name, deque(maxlen=LATENCY_WINDOW)).append(time.perf_counter() - start)
        if not result.success:
            self._error_counts[tool_name] = self._error_counts.get(tool_name, 0) + 1
            logger.warning("Tool %s failed: %s", tool_name, result.error)
        return result

    def get_metrics(self) -> dict[str, Any]:
        return {
            "calls": dict(self._call_counts),
            "errors": dict(self._error_counts),
            "avg_latency_ms": {k: sum(v) / len(v) * 1000 for k, v in self._latencies.items() if v},
        }
