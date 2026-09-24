# specs/02_infra/tools/SHELL_TOOL.py
"""
Shell Execution Tool.
Supports: Streaming output, Timeout, Working Directory, Environment Variables.
Security: Command allowlist/denylist (configurable per Vertical).
"""

from __future__ import annotations
import shlex
import asyncio
from typing import Dict, Any, Optional, AsyncGenerator
from kernel.protocols import ITool, ToolResult, IShellTool, ISandbox
from kernel.state import AgentState

SHELL_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "Command to execute"},
        "workdir": {"type": "string", "default": "/workspace", "description": "Working directory"},
        "env": {"type": "object", "description": "Additional env vars"},
        "timeout": {"type": "integer", "default": 120, "description": "Timeout in seconds"},
        "stream": {"type": "boolean", "default": False, "description": "Stream output to logs"},
    },
    "required": ["command"],
}


class ShellTool(IShellTool):
    name = "shell"
    description = "Execute shell commands in the sandbox. Use for builds, tests, linters, git."
    parameters_json_schema = SHELL_TOOL_SCHEMA

    # Security: Commands that are NEVER allowed
    DENYLIST = {"rm -rf /", "mkfs", "dd if=", "shutdown", "reboot", ":(){ :|:& };:"}
    # Allowlist (optional, if enabled in vertical config)
    ALLOWLIST_PREFIXES = (
        "pnpm ",
        "npm ",
        "python ",
        "pip ",
        "pytest ",
        "mypy ",
        "ruff ",
        "tsc ",
        "eslint ",
        "go ",
        "cargo ",
        "terraform ",
        "git ",
        "cat ",
        "ls ",
        "head ",
        "tail ",
    )

    def __init__(self, sandbox_manager: "SandboxManager", allowlist_enabled: bool = True):
        self.sandbox_manager = sandbox_manager
        self.allowlist_enabled = allowlist_enabled

    async def execute(self, sandbox_id: str, args: Dict[str, Any], state: AgentState) -> ToolResult:
        command = args["command"].strip()
        workdir = args.get("workdir", "/workspace")
        env = args.get("env", {})
        timeout = args.get("timeout", 120)
        stream = args.get("stream", False)

        # Security Check
        if self._is_dangerous(command):
            return ToolResult(success=False, error=f"Command blocked by security policy: {command}")

        sbx = self.sandbox_manager.get_sandbox(sandbox_id)

        try:
            if stream:
                # For streaming, we need a different sandbox API (async generator)
                # This is a simplified blocking version. Real impl uses `proc = await sandbox.process.start(...)`
                pass

            # Merge env: State env > Tool args env > Sandbox default
            merged_env = {**state.get("sandbox_env", {}), **env}

            result = await asyncio.wait_for(sbx.exec(command, workdir=workdir, env=merged_env), timeout=timeout)

            return ToolResult(success=result.exit_code == 0, data=result.model_dump())

        except asyncio.TimeoutError:
            return ToolResult(
                success=False,
                error=f"Command timed out after {timeout}s",
                data={"exit_code": -1, "stdout": "", "stderr": "TIMEOUT"},
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e))

    def _is_dangerous(self, cmd: str) -> bool:
        if not self.allowlist_enabled:
            return False
        # Check denylist
        for bad in self.DENYLIST:
            if bad in cmd:
                return True
        # Check allowlist prefix (first word)
        first_word = shlex.split(cmd)[0] if cmd else ""
        # Allowlist check logic...
        return False  # Simplified
