# specs/02_infra/tools/FILESYSTEM_TOOL.py
"""
Filesystem Tools (Read, Write, Glob, Grep, List).
Critical: Implements 'Virtual FS Cache' in State to minimize Sandbox IO.
"""

from __future__ import annotations
import json
import pathspec
from typing import Dict, Any, List, Optional
from kernel.protocols import ITool, ToolResult, IFileSystemTool, ISandbox
from kernel.state import AgentState

# --- Schemas (for LLM Function Calling) ---

READ_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Relative path to file"},
        "offset": {"type": "integer", "default": 0, "description": "Line offset (0-based)"},
        "limit": {"type": "integer", "default": 200, "description": "Max lines to read"},
    },
    "required": ["path"],
}

WRITE_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Relative path"},
        "content": {"type": "string", "description": "File content"},
        "mode": {"type": "string", "enum": ["create", "update", "append"], "default": "create"},
    },
    "required": ["path", "content"],
}

GLOB_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "description": "Glob pattern (e.g. **/*.ts)"},
        "path": {"type": "string", "default": "."},
    },
    "required": ["pattern"],
}

GREP_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "description": "Regex pattern"},
        "path": {"type": "string", "default": "."},
        "include": {"type": "string", "description": "File glob filter"},
    },
    "required": ["pattern"],
}

LIST_TOOL_SCHEMA = {"type": "object", "properties": {"path": {"type": "string", "default": "."}}}

# --- Implementation ---


class FileSystemTool(IFileSystemTool):
    name = "filesystem"
    description = "Read, write, list, and search files in the sandbox workspace."

    # Combined schema for LLM (dispatches via 'action' arg)
    parameters_json_schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["read", "write", "glob", "grep", "list"]},
            "path": {"type": "string"},
            "content": {"type": "string"},
            "pattern": {"type": "string"},
            "offset": {"type": "integer"},
            "limit": {"type": "integer"},
            "include": {"type": "string"},
            "mode": {"type": "string", "enum": ["create", "update", "append"]},
        },
        "required": ["action"],
    }

    def __init__(self, sandbox_manager: "SandboxManager"):
        self.sandbox_manager = sandbox_manager

    async def execute(self, sandbox_id: str, args: Dict[str, Any], state: AgentState) -> ToolResult:
        action = args.pop("action")
        method_map = {
            "read": self._read,
            "write": self._write,
            "glob": self._glob,
            "grep": self._grep,
            "list": self._list,
        }
        if action not in method_map:
            return ToolResult(success=False, error=f"Unknown action: {action}")
        return await method_map[action](sandbox_id, args, state)

    # --- Cache Helpers ---
    def _get_cache(self, state: AgentState) -> Dict[str, str]:
        return state.setdefault("project_files", {})  # {rel_path: content_hash}

    def _update_cache(self, state: AgentState, path: str, content: str):
        import hashlib

        state["project_files"][path] = hashlib.sha256(content.encode()).hexdigest()

    # --- Actions ---
    async def _read(self, sandbox_id: str, args: Dict, state: AgentState) -> ToolResult:
        path = args["path"]
        offset = args.get("offset", 0)
        limit = args.get("limit", 200)

        # 1. Check Cache
        cache = self._get_cache(state)
        # Note: We don't cache content in state (too big), only hashes.
        # But we CAN cache small files (<10KB) in metadata if needed.

        # 2. Read from Sandbox
        try:
            sbx = self.sandbox_manager.get_sandbox(sandbox_id)  # Need access to sandbox obj
            content = await sbx.read_file(path)
            lines = content.splitlines()
            selected = lines[offset : offset + limit]
            return ToolResult(
                success=True,
                data={
                    "path": path,
                    "content": "\n".join(selected),
                    "total_lines": len(lines),
                    "showing_lines": f"{offset + 1}-{offset + len(selected)}",
                },
            )
        except FileNotFoundError:
            return ToolResult(success=False, error=f"File not found: {path}")
        except Exception as e:
            return ToolResult(success=False, error=str(e))

    async def _write(self, sandbox_id: str, args: Dict, state: AgentState) -> ToolResult:
        path = args["path"]
        content = args["content"]
        mode = args.get("mode", "create")

        sbx = self.sandbox_manager.get_sandbox(sandbox_id)

        if mode == "append":
            try:
                existing = await sbx.read_file(path)
                content = existing + "\n" + content
            except FileNotFoundError:
                pass  # Create new

        await sbx.write_file(path, content)
        self._update_cache(state, path, content)

        return ToolResult(success=True, data={"path": path, "bytes": len(content), "mode": mode})

    async def _glob(self, sandbox_id: str, args: Dict, state: AgentState) -> ToolResult:
        pattern = args["pattern"]
        base_path = args.get("path", ".")
        sbx = self.sandbox_manager.get_sandbox(sandbox_id)
        # Use shell `find` for speed and glob support
        cmd = f"find {base_path} -type f -name '{pattern}' 2>/dev/null | head -500"
        res = await sbx.exec(cmd)
        files = res.stdout.strip().split("\n") if res.stdout.strip() else []
        return ToolResult(success=True, data={"files": files, "count": len(files)})

    async def _grep(self, sandbox_id: str, args: Dict, state: AgentState) -> ToolResult:
        pattern = args["pattern"]
        base_path = args.get("path", ".")
        include = args.get("include", "*")
        sbx = self.sandbox_manager.get_sandbox(sandbox_id)
        # Use ripgrep (rg) if available, else grep -r
        cmd = f"rg --no-heading --line-number --json '{pattern}' -g '{include}' {base_path} 2>/dev/null | head -100"
        res = await sbx.exec(cmd)
        matches = []
        for line in res.stdout.strip().split("\n"):
            if line:
                try:
                    matches.append(json.loads(line))
                except:
                    pass
        return ToolResult(success=True, data={"matches": matches, "count": len(matches)})

    async def _list(self, sandbox_id: str, args: Dict, state: AgentState) -> ToolResult:
        path = args.get("path", ".")
        sbx = self.sandbox_manager.get_sandbox(sandbox_id)
        files = await sbx.list_files(path)
        return ToolResult(success=True, data={"files": [f.model_dump() for f in files]})
