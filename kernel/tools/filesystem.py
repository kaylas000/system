"""
Filesystem tool (port of ``specs/02_infra/tools/FILESYSTEM_TOOL.py``).

Fixes vs. the spec (``specs/ISSUES.md`` I-09):
* takes an ``ISandbox`` (the spec called a non-existent ``sandbox_manager.get_sandbox``);
* does not mutate ``args`` (``args.pop``) or ``state`` (LangGraph discards such
  writes) - the content hash is returned in ``metadata["sha256"]`` instead;
* no shell interpolation of LLM input: glob/list filter a ``find`` listing in
  Python, grep passes the pattern through ``shlex.quote`` + ``-e``;
* every path is resolved inside the workspace (relative paths are relative to it);
* ``mode``: ``create`` refuses to overwrite, ``update`` requires an existing file,
  ``append`` creates or appends.
"""

from __future__ import annotations

import hashlib
import posixpath
import shlex
from typing import Any

from ..protocols import CommandResult, ISandbox, ToolResult
from ..state import AgentState
from ._paths import DEFAULT_WORKSPACE, IGNORED_DIRS, UnsafePathError, glob_to_regex, relative, resolve, workspace_of

MAX_READ_LINES = 2000
MAX_GLOB_RESULTS = 500
MAX_GREP_MATCHES = 200


def _arg(ws: str, full: str) -> str:
    """Workspace-relative, shell-quoted path argument ("./x" so it can never look like an option)."""
    rel = relative(ws, full)
    return shlex.quote("." if rel == "." else f"./{rel}")


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


FS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["read", "write", "glob", "grep", "list"]},
        "path": {"type": "string", "description": "Path relative to the workspace (default '.')"},
        "content": {"type": "string", "description": "write: file content"},
        "pattern": {"type": "string", "description": "glob: e.g. src/**/*.ts; grep: extended regex"},
        "offset": {"type": "integer", "description": "read: first line (0-based)", "default": 0},
        "limit": {"type": "integer", "description": "read: max lines", "default": 200},
        "include": {"type": "string", "description": "grep: file glob filter, e.g. *.tsx"},
        "mode": {"type": "string", "enum": ["create", "update", "append"], "default": "create"},
    },
    "required": ["action"],
}


class FileSystemTool:
    name = "filesystem"
    description = "Read, write, list, and search files in the sandbox workspace. Paths are relative to the workspace."

    def __init__(self, sandbox: ISandbox, default_workspace: str = DEFAULT_WORKSPACE) -> None:
        self.parameters_json_schema: dict[str, Any] = FS_SCHEMA
        self.sandbox = sandbox
        self.default_workspace = default_workspace

    async def execute(self, sandbox_id: str, args: dict[str, Any], state: AgentState) -> ToolResult:
        action = args.get("action")
        handlers = {
            "read": self._read,
            "write": self._write,
            "glob": self._glob,
            "grep": self._grep,
            "list": self._list,
        }
        handler = handlers.get(str(action))
        if handler is None:
            return ToolResult(success=False, error=f"Unknown action: {action}")
        ws = workspace_of(state, self.default_workspace)
        try:
            return await handler(sandbox_id, ws, args)
        except UnsafePathError as exc:
            return ToolResult(success=False, error=str(exc))
        except KeyError as exc:
            return ToolResult(success=False, error=f"Missing argument: {exc.args[0]}")

    # -- convenience API (IFileSystemTool in the spec) -------------------------------
    async def read(self, sandbox_id: str, path: str, state: AgentState | None = None) -> ToolResult:
        return await self.execute(sandbox_id, {"action": "read", "path": path}, state or {})

    async def write(
        self, sandbox_id: str, path: str, content: str, mode: str = "create", state: AgentState | None = None
    ) -> ToolResult:
        args = {"action": "write", "path": path, "content": content, "mode": mode}
        return await self.execute(sandbox_id, args, state or {})

    async def glob(self, sandbox_id: str, pattern: str, state: AgentState | None = None) -> ToolResult:
        return await self.execute(sandbox_id, {"action": "glob", "pattern": pattern}, state or {})

    async def grep(self, sandbox_id: str, pattern: str, path: str = ".", state: AgentState | None = None) -> ToolResult:
        args = {"action": "grep", "pattern": pattern, "path": path}
        return await self.execute(sandbox_id, args, state or {})

    # -- actions ---------------------------------------------------------------------
    # Commands run with workdir=<workspace> and workspace-relative paths, so they behave the same on
    # every backend (LocalSandbox maps only its own API paths, not paths inside shell commands).
    async def _sh(self, sandbox_id: str, ws: str, cmd: str) -> CommandResult:
        return await self.sandbox.exec(sandbox_id, cmd, workdir=ws)

    async def _exists(self, sandbox_id: str, ws: str, full: str) -> bool:
        return (await self._sh(sandbox_id, ws, f"test -e {_arg(ws, full)}")).exit_code == 0

    async def _read(self, sandbox_id: str, ws: str, args: dict[str, Any]) -> ToolResult:
        full = resolve(ws, args["path"])
        offset = max(0, int(args.get("offset") or 0))
        limit = min(MAX_READ_LINES, max(1, int(args.get("limit") or 200)))
        try:
            content = await self.sandbox.read_file(sandbox_id, full)
        except (FileNotFoundError, IsADirectoryError):
            return ToolResult(success=False, error=f"File not found: {relative(ws, full)}")
        lines = content.splitlines()
        selected = lines[offset : offset + limit]
        return ToolResult(
            success=True,
            data={
                "path": relative(ws, full),
                "content": "\n".join(selected),
                "total_lines": len(lines),
                "showing_lines": f"{offset + 1}-{offset + len(selected)}",
                "truncated": offset + len(selected) < len(lines),
            },
            metadata={"sha256": _sha(content)},
        )

    async def _write(self, sandbox_id: str, ws: str, args: dict[str, Any]) -> ToolResult:
        full = resolve(ws, args["path"])
        if full == ws:
            return ToolResult(success=False, error="Cannot write to the workspace root")
        content = str(args["content"])
        mode = args.get("mode") or "create"
        rel = relative(ws, full)
        if mode not in ("create", "update", "append"):
            return ToolResult(success=False, error=f"Unknown mode: {mode}")
        exists = await self._exists(sandbox_id, ws, full)
        if mode == "create" and exists:
            return ToolResult(success=False, error=f"File exists: {rel} (use mode=update to overwrite)")
        if mode == "update" and not exists:
            return ToolResult(success=False, error=f"File not found: {rel} (use mode=create)")
        if mode == "append" and exists:
            existing = await self.sandbox.read_file(sandbox_id, full)
            sep = "" if not existing or existing.endswith("\n") else "\n"
            content = existing + sep + content
        await self.sandbox.write_file(sandbox_id, full, content)
        return ToolResult(
            success=True,
            data={"path": rel, "bytes": len(content.encode()), "mode": mode},
            metadata={"sha256": _sha(content)},
        )

    async def _listing(self, sandbox_id: str, ws: str, base: str) -> list[tuple[str, bool, int]]:
        """(absolute path, is_dir, size) under ``base``, pruning IGNORED_DIRS."""
        prune = " -o ".join(f"-name {shlex.quote(d)}" for d in IGNORED_DIRS)
        cmd = f"find {_arg(ws, base)} -mindepth 1 \\( {prune} \\) -prune -o -printf '%y\\t%s\\t%p\\n' 2>/dev/null"
        res = await self._sh(sandbox_id, ws, cmd)
        entries: list[tuple[str, bool, int]] = []
        for line in res.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3 and parts[1].isdigit():
                entries.append((posixpath.normpath(posixpath.join(ws, parts[2])), parts[0] == "d", int(parts[1])))
        return sorted(entries)

    async def _glob(self, sandbox_id: str, ws: str, args: dict[str, Any]) -> ToolResult:
        base = resolve(ws, args.get("path"))
        regex = glob_to_regex(str(args["pattern"]))
        files = [
            relative(ws, p)
            for p, is_dir, _ in await self._listing(sandbox_id, ws, base)
            if not is_dir
            if regex.match(relative(base, p))
        ]
        return ToolResult(
            success=True,
            data={"files": files[:MAX_GLOB_RESULTS], "count": len(files), "truncated": len(files) > MAX_GLOB_RESULTS},
        )

    async def _list(self, sandbox_id: str, ws: str, args: dict[str, Any]) -> ToolResult:
        base = resolve(ws, args.get("path"))
        entries = await self._listing(sandbox_id, ws, base)
        return ToolResult(
            success=True,
            data={"files": [{"path": relative(ws, p), "is_dir": d, "size": s} for p, d, s in entries[:2000]]},
        )

    async def _grep(self, sandbox_id: str, ws: str, args: dict[str, Any]) -> ToolResult:
        base = resolve(ws, args.get("path"))
        excludes = " ".join(f"--exclude-dir={shlex.quote(d)}" for d in IGNORED_DIRS)
        include = f"--include={shlex.quote(str(args['include']))}" if args.get("include") else ""
        cmd = (
            f"grep -rHInE {excludes} {include} -e {shlex.quote(str(args['pattern']))} -- {_arg(ws, base)} "
            f"2>/dev/null | head -n {MAX_GREP_MATCHES + 1}"
        )
        res = await self._sh(sandbox_id, ws, cmd)
        matches = []
        for line in res.stdout.splitlines():
            path, _, rest = line.partition(":")
            lineno, _, text = rest.partition(":")
            if lineno.isdigit():
                matches.append({"path": posixpath.normpath(path), "line": int(lineno), "text": text[:500]})
        return ToolResult(
            success=True,
            data={
                "matches": matches[:MAX_GREP_MATCHES],
                "count": min(len(matches), MAX_GREP_MATCHES),
                "truncated": len(matches) > MAX_GREP_MATCHES,
            },
        )
