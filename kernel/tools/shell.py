"""
Shell tool (port of ``specs/02_infra/tools/SHELL_TOOL.py``).

Fixes vs. the spec (``specs/ISSUES.md`` I-10):
* the allowlist was never applied (``_is_dangerous`` always returned False) and
  the denylist was plain substring matching; here ``ShellPolicy`` tokenizes the
  command, checks the first word of *every* segment of a pipeline / ``&&`` /
  ``;`` chain against the allowlist and matches the denylist with regexes;
* with the allowlist on, command substitution (``$(...)``, backticks) and
  process substitution are rejected - they would bypass the check;
* timeout goes to ``ISandbox.exec(timeout_sec=...)`` so the process is killed
  inside the sandbox (``asyncio.wait_for`` around the call left it running);
* ``workdir`` is resolved inside the workspace.

The policy is defense in depth against LLM mistakes, NOT a security boundary
(``python -c`` / ``node -e`` can do anything): isolation comes from the sandbox.
``env`` / ``xargs`` / ``sudo`` are left out of the default allowlist because they
run an arbitrary second command.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Any

from ..protocols import ISandbox, ToolResult
from ..state import AgentState
from ._paths import DEFAULT_WORKSPACE, UnsafePathError, resolve, workspace_of

MAX_TIMEOUT_SEC = 1800

DEFAULT_ALLOWLIST: frozenset[str] = frozenset(
    # spec ALLOWLIST_PREFIXES
    {"pnpm", "npm", "python", "pip", "pytest", "mypy", "ruff", "tsc", "eslint", "go", "cargo", "terraform", "git"}
    | {"cat", "ls", "head", "tail"}
    # additions needed by the SaaS-web skills / verification gates
    | {"npx", "node", "yarn", "bun", "bunx", "corepack", "prisma", "next", "vitest", "jest", "playwright", "prettier"}
    | {"python3", "uv", "make"}
    | {"mkdir", "touch", "cp", "mv", "rm", "echo", "printf", "pwd", "find", "grep", "rg", "sed", "awk", "wc", "sort"}
    | {"uniq", "diff", "test", "[", "true", "false", "sleep", "cd", "which", "tree", "du", "stat", "file", "tar"}
)

DEFAULT_DENY_PATTERNS: tuple[str, ...] = (
    r"\brm\s+(-[a-zA-Z]*\s+)*(/|/\*|~|\$HOME)(\s|$)",  # rm -rf /, rm -rf ~
    r"\bmkfs(\.\w+)?\b",
    r"\bdd\s+[^|;&]*\bif=",
    r"\b(shutdown|reboot|halt|poweroff)\b",
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",  # fork bomb
    r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z|da)?sh\b",  # curl ... | sh
    r"\bchmod\s+(-[a-zA-Z]+\s+)*[0-7]*777\s+/(\s|$)",
    r">\s*/dev/(sd[a-z]|nvme|disk)",
)

_SEPARATORS = {";", "&&", "||", "|", "&", "|&", ";;", "(", ")", "\n"}
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass
class ShellPolicy:
    allowlist_enabled: bool = True
    allowlist: frozenset[str] = DEFAULT_ALLOWLIST
    deny_patterns: tuple[str, ...] = DEFAULT_DENY_PATTERNS
    _compiled: list[re.Pattern[str]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._compiled = [re.compile(p) for p in self.deny_patterns]

    def check(self, command: str) -> str | None:
        """Return the reason the command is blocked, or ``None`` if allowed."""
        if not command.strip():
            return "empty command"
        for pattern in self._compiled:
            if pattern.search(command):
                return f"matches denylist pattern {pattern.pattern!r}"
        if not self.allowlist_enabled:
            return None
        if "`" in command or "$(" in command or "<(" in command or ">(" in command:
            return "command/process substitution is not allowed when the allowlist is enabled"
        try:
            words = self.command_words(command)
        except ValueError as exc:
            return str(exc)
        for word in words:
            if word not in self.allowlist:
                return f"'{word}' is not in the allowlist"
        return None

    @staticmethod
    def command_words(command: str) -> list[str]:
        """First word of every simple command in a pipeline / list (env assignments skipped)."""
        lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        words: list[str] = []
        expect_cmd = True
        try:
            tokens = list(lexer)
        except ValueError as exc:  # unbalanced quotes
            raise ValueError(f"cannot parse command: {exc}") from exc
        for tok in tokens:
            if tok in _SEPARATORS or set(tok) <= set(";&|()"):
                expect_cmd = True
                continue
            if expect_cmd:
                if _ENV_ASSIGN.match(tok):
                    continue
                words.append(tok.rsplit("/", 1)[-1] if tok.startswith(("/", "./")) else tok)
                expect_cmd = False
        return words


SHELL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "Command to execute"},
        "workdir": {"type": "string", "default": ".", "description": "Working directory (relative to workspace)"},
        "env": {"type": "object", "additionalProperties": {"type": "string"}, "description": "Extra env vars"},
        "timeout": {"type": "integer", "default": 120, "description": "Timeout in seconds"},
    },
    "required": ["command"],
}


class ShellTool:
    name = "shell"
    description = "Execute shell commands in the sandbox. Use for builds, tests, linters, package managers."

    def __init__(
        self, sandbox: ISandbox, policy: ShellPolicy | None = None, default_workspace: str = DEFAULT_WORKSPACE
    ) -> None:
        self.parameters_json_schema: dict[str, Any] = SHELL_SCHEMA
        self.sandbox = sandbox
        self.policy = policy or ShellPolicy()
        self.default_workspace = default_workspace

    async def run(
        self, sandbox_id: str, command: str, workdir: str = ".", state: AgentState | None = None
    ) -> ToolResult:
        return await self.execute(sandbox_id, {"command": command, "workdir": workdir}, state or {})

    async def execute(self, sandbox_id: str, args: dict[str, Any], state: AgentState) -> ToolResult:
        command = str(args.get("command") or "").strip()
        reason = self.policy.check(command)
        if reason:
            return ToolResult(success=False, error=f"Command blocked by security policy: {reason}")
        try:
            workdir = resolve(workspace_of(state, self.default_workspace), args.get("workdir"))
        except UnsafePathError as exc:
            return ToolResult(success=False, error=str(exc))
        env = {str(k): str(v) for k, v in (args.get("env") or {}).items()}
        timeout = min(MAX_TIMEOUT_SEC, max(1, int(args.get("timeout") or 120)))

        result = await self.sandbox.exec(sandbox_id, command, workdir=workdir, env=env or None, timeout_sec=timeout)
        error = None
        if result.exit_code == 124:
            error = f"Command timed out after {timeout}s"
        elif result.exit_code != 0:
            error = f"exit code {result.exit_code}"
        return ToolResult(success=result.exit_code == 0, data=result.model_dump(), error=error)
