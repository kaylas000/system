# specs/02_infra/tools/GIT_TOOL.py
"""
Git Tool for Sandbox.
Enables: Commit history, Diff generation, Branch management, PR preparation.
"""

from __future__ import annotations
from typing import Dict, Any, List
from kernel.protocols import ITool, ToolResult
from kernel.state import AgentState

GIT_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["init", "status", "diff", "add", "commit", "log", "branch", "push"]},
        "args": {"type": "array", "items": {"type": "string"}, "description": "Git subcommand args"},
        "message": {"type": "string", "description": "Commit message"},
    },
    "required": ["action"],
}


class GitTool(ITool):
    name = "git"
    description = "Git operations in sandbox workspace. Use for version control, diffs, commits."
    parameters_json_schema = GIT_TOOL_SCHEMA

    def __init__(self, sandbox_manager: "SandboxManager"):
        self.sandbox_manager = sandbox_manager

    async def execute(self, sandbox_id: str, args: Dict[str, Any], state: AgentState) -> ToolResult:
        action = args["action"]
        git_args = args.get("args", [])
        message = args.get("message")

        sbx = self.sandbox_manager.get_sandbox(sandbox_id)

        try:
            if action == "init":
                cmd = "git init && git config user.email 'agent@autogen.local' && git config user.name 'AutoGen Agent'"
            elif action == "add":
                cmd = f"git add {' '.join(git_args)}"
            elif action == "commit":
                if not message:
                    return ToolResult(success=False, error="Message required for commit")
                cmd = f'git commit -m "{message}"'
            elif action == "diff":
                cmd = f"git diff {' '.join(git_args)}"
            elif action == "status":
                cmd = "git status --porcelain"
            elif action == "log":
                cmd = f"git log --oneline -n 20 {' '.join(git_args)}"
            elif action == "branch":
                cmd = f"git branch {' '.join(git_args)}"
            elif action == "push":
                cmd = f"git push {' '.join(git_args)}"
            else:
                return ToolResult(success=False, error=f"Unknown git action: {action}")

            res = await sbx.exec(cmd, workdir="/workspace")
            return ToolResult(success=res.exit_code == 0, data=res.model_dump())
        except Exception as e:
            return ToolResult(success=False, error=str(e))
