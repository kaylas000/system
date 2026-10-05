# specs/02_infra/tools/TOOL_REGISTRY.py
"""
Central Tool Registry.
- Registers tools (implementations of ITool).
- Handles: Permissions, Logging, Retries, Metrics, Sandbox Injection.
- Provides OpenAI Function Calling schemas for LLM.
"""

from __future__ import annotations
import json
import time
import logging
from typing import Dict, Any, Optional, List, Type
from pydantic import BaseModel, Field

from kernel.protocols import ITool, ToolResult, ISandbox, AgentState
from kernel.state import TokenUsage

logger = logging.getLogger("autogen.tools")


class ToolRegistry:
    def __init__(self, sandbox_manager: "SandboxManager"):
        self._tools: Dict[str, ITool] = {}
        self._sandbox_manager = sandbox_manager
        # Metrics
        self._call_counts: Dict[str, int] = {}
        self._error_counts: Dict[str, int] = {}
        self._latencies: Dict[str, List[float]] = {}

    def register(self, tool: ITool) -> None:
        if tool.name in self._tools:
            logger.warning(f"Overriding tool: {tool.name}")
        self._tools[tool.name] = tool
        logger.info(f"Registered tool: {tool.name}")

    def get(self, name: str) -> Optional[ITool]:
        return self._tools.get(name)

    def get_all_schemas(self) -> List[Dict[str, Any]]:
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
        self, tool_name: str, args: Dict[str, Any], state: AgentState, sandbox_id: Optional[str] = None
    ) -> ToolResult:
        """Main entry point. Injects sandbox_id from state if not provided."""
        tool = self._tools.get(tool_name)
        if not tool:
            return ToolResult(success=False, error=f"Tool '{tool_name}' not found")

        # Resolve Sandbox
        target_sandbox_id = sandbox_id or state.get("sandbox_id")
        if not target_sandbox_id:
            return ToolResult(success=False, error="No sandbox_id in state or args")

        start = time.perf_counter()
        self._call_counts[tool_name] = self._call_counts.get(tool_name, 0) + 1

        try:
            logger.debug(f"Executing tool: {tool_name} with args: {json.dumps(args, default=str)[:200]}")
            result = await tool.execute(target_sandbox_id, args, state)

            latency = time.perf_counter() - start
            self._latencies.setdefault(tool_name, []).append(latency)

            if not result.success:
                self._error_counts[tool_name] = self._error_counts.get(tool_name, 0) + 1
                logger.warning(f"Tool {tool_name} failed: {result.error}")

            return result
        except Exception as e:
            self._error_counts[tool_name] = self._error_counts.get(tool_name, 0) + 1
            logger.exception(f"Tool {tool_name} crashed")
            return ToolResult(success=False, error=f"Tool execution exception: {str(e)}")

    def get_metrics(self) -> Dict[str, Any]:
        return {
            "calls": self._call_counts,
            "errors": self._error_counts,
            "avg_latency_ms": {k: sum(v) / len(v) * 1000 for k, v in self._latencies.items() if v},
        }
