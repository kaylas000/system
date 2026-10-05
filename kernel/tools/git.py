"""
Git tool (port of ``specs/02_infra/tools/GIT_TOOL.py``).

Fixes vs. the spec (``specs/ISSUES.md`` I-11):
* arguments and the commit message are passed through ``shlex.quote`` - the spec
  interpolated them into the command (``git commit -m "{message}"`` = injection);
* options that execute programs or write outside the repo (``-c``, ``--exec``,
  ``--upload-pack``, ``--receive-pack``, ``--output``, ``--git-dir``,
  ``--work-tree``) are rejected;
* ``push`` is disabled unless ``allow_push=True`` (publishing is a destructive
  action and must go through HITL, graph_topology §6);
* runs in the run's workspace (``state["workspace_path"]``), not a hardcoded path.
"""

from __future__ import annotations

import shlex
from typing import Any

from ..protocols import ISandbox, ToolResult
from ..state import AgentState
from ._paths import DEFAULT_WORKSPACE, workspace_of

ACTIONS = ("init", "status", "diff", "add", "commit", "log", "branch", "push")
FORBIDDEN_OPTIONS = ("-c", "--exec", "--upload-pack", "--receive-pack", "--output", "--git-dir", "--work-tree", "-C")
AGENT_IDENTITY = ("AutoGen Agent", "agent@autogen.local")


def _forbidden(arg: str) -> bool:
    if arg.startswith("-c") and not arg.startswith("--"):  # -c key=value, -ckey=value
        return True
    return any(arg == opt or arg.startswith(opt + "=") for opt in FORBIDDEN_OPTIONS)


GIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "args": {"type": "array", "items": {"type": "string"}, "description": "Extra arguments"},
        "message": {"type": "string", "description": "Commit message (commit)"},
    },
    "required": ["action"],
}


class GitTool:
    name = "git"
    description = "Git operations in the sandbox workspace: init, status, diff, add, commit, log, branch."

    def __init__(self, sandbox: ISandbox, *, allow_push: bool = False, default_workspace: str = DEFAULT_WORKSPACE):
        self.parameters_json_schema: dict[str, Any] = GIT_SCHEMA
        self.sandbox = sandbox
        self.allow_push = allow_push
        self.default_workspace = default_workspace

    def build_command(self, action: str, git_args: list[str], message: str | None) -> str:
        for arg in git_args:
            if _forbidden(arg):
                raise ValueError(f"git option not allowed: {arg}")
        quoted = " ".join(shlex.quote(a) for a in git_args)
        name, email = AGENT_IDENTITY
        if action == "init":
            return (
                "git init -q -b main && "
                f"git config user.name {shlex.quote(name)} && git config user.email {shlex.quote(email)}"
            )
        if action == "status":
            return "git status --porcelain"
        if action == "add":
            return f"git add -- {quoted or '.'}"
        if action == "commit":
            if not message:
                raise ValueError("Message required for commit")
            return f"git commit -q -m {shlex.quote(message)} {quoted}".rstrip()
        if action == "diff":
            return f"git --no-pager diff --no-color {quoted}".rstrip()
        if action == "log":
            return f"git --no-pager log --oneline -n 20 {quoted}".rstrip()
        if action == "branch":
            return f"git branch {quoted}".rstrip()
        if action == "push":
            if not self.allow_push:
                raise PermissionError("git push is disabled for agents (requires human approval)")
            return f"git push {quoted}".rstrip()
        raise ValueError(f"Unknown git action: {action}")

    async def execute(self, sandbox_id: str, args: dict[str, Any], state: AgentState) -> ToolResult:
        git_args = [str(a) for a in (args.get("args") or [])]
        try:
            cmd = self.build_command(str(args.get("action")), git_args, args.get("message"))
        except (ValueError, PermissionError) as exc:
            return ToolResult(success=False, error=str(exc))
        workdir = workspace_of(state, self.default_workspace)
        res = await self.sandbox.exec(sandbox_id, cmd, workdir=workdir, env={"GIT_TERMINAL_PROMPT": "0"})
        return ToolResult(
            success=res.exit_code == 0,
            data=res.model_dump(),
            error=None if res.exit_code == 0 else (res.stderr.strip() or f"exit code {res.exit_code}")[:2000],
        )
