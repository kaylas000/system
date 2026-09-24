# specs/02_infra/tools/LSP_TOOL.py
"""
Language Server Protocol (LSP) Client Tool.
CRITICAL for Agent Code Intelligence.
Allows Agent to: Go to Definition, Find References, Hover, Document Symbols.
Implementation: JSON-RPC 2.0 over stdio to language servers in Sandbox.
"""

from __future__ import annotations
import json
import asyncio
import uuid
from pathlib import Path
from typing import Dict, Any, List, Optional, Literal
from dataclasses import dataclass, field
from kernel.protocols import ITool, ToolResult, ILSPTool, ISandbox
from kernel.state import AgentState

LSP_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["goto_definition", "find_references", "hover", "document_symbols", "workspace_symbols"],
        },
        "file_path": {"type": "string", "description": "Absolute or relative path in workspace"},
        "line": {"type": "integer", "description": "1-based line number"},
        "character": {"type": "integer", "description": "1-based character offset"},
        "query": {"type": "string", "description": "For workspace_symbols"},
    },
    "required": ["action"],
}

# --- LSP Client (Manages one client per language per sandbox) ---


@dataclass
class LSPClient:
    """Manages JSON-RPC connection to a language server process."""

    sandbox: ISandbox
    language_id: str
    command: List[str]  # e.g. ["typescript-language-server", "--stdio"]
    root_uri: str  # file:///workspace
    _process: Any = field(default=None, init=False)  # asyncio subprocess
    _request_id: int = field(default=0, init=False)
    _pending: Dict[int, asyncio.Future] = field(default_factory=dict, init=False)
    _initialized: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    async def start(self):
        # Launch process in sandbox via shell 'coproc' or dedicated exec?
        # E2B/Daytona allow running background processes.
        # Simplified: We assume sandbox has a daemon manager or we use a persistent shell.
        # BEST: Use Sandbox `process.start` (E2B) or `session.command` (Daytona) for background LSP.
        # Here we simulate via a persistent connection.
        pass

    async def _send_request(self, method: str, params: Dict) -> Any:
        self._request_id += 1
        req_id = self._request_id
        future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        # Write to stdin of LSP process
        # await self._process.stdin.write((json.dumps(msg) + "\r\n").encode())
        # ... read response from stdout in background task ...
        return await future

    async def initialize(self):
        if self._initialized:
            return
        await self._send_request(
            "initialize",
            {
                "processId": None,
                "rootUri": self.root_uri,
                "capabilities": {},
                "workspaceFolders": [{"uri": self.root_uri, "name": "workspace"}],
            },
        )
        await self._send_request("initialized", {})
        self._initialized = True

    async def shutdown(self):
        await self._send_request("shutdown", {})
        await self._send_request("exit", {})

    # --- High Level API ---
    async def goto_definition(self, file: str, line: int, char: int) -> List[Dict]:
        uri = Path(file).resolve().as_uri()
        return await self._send_request(
            "textDocument/definition",
            {"textDocument": {"uri": uri}, "position": {"line": line - 1, "character": char - 1}},
        )

    async def find_references(self, file: str, line: int, char: int, include_declaration=True) -> List[Dict]:
        uri = Path(file).resolve().as_uri()
        return await self._send_request(
            "textDocument/references",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line - 1, "character": char - 1},
                "context": {"includeDeclaration": include_declaration},
            },
        )

    async def hover(self, file: str, line: int, char: int) -> Dict:
        uri = Path(file).resolve().as_uri()
        return await self._send_request(
            "textDocument/hover", {"textDocument": {"uri": uri}, "position": {"line": line - 1, "character": char - 1}}
        )

    async def document_symbols(self, file: str) -> List[Dict]:
        uri = Path(file).resolve().as_uri()
        return await self._send_request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})

    async def workspace_symbols(self, query: str) -> List[Dict]:
        return await self._send_request("workspace/symbol", {"query": query})


# --- Tool Wrapper ---


class LSPTool(ILSPTool):
    name = "lsp"
    description = "Code Intelligence: Go to Definition, Find References, Hover, Symbols. Use to understand codebase before editing."
    parameters_json_schema = LSP_TOOL_SCHEMA

    # Language -> LSP Command Mapping (Must match base image)
    LSP_COMMANDS = {
        "typescript": ["typescript-language-server", "--stdio"],
        "javascript": ["typescript-language-server", "--stdio"],
        "python": ["pylsp"],  # or basedpyright-langserver
        "go": ["gopls"],
        "rust": ["rust-analyzer"],
        "terraform": ["terraform-ls", "serve"],
    }

    def __init__(self, sandbox_manager: "SandboxManager"):
        self.sandbox_manager = sandbox_manager
        self._clients: Dict[str, Dict[str, LSPClient]] = {}  # sandbox_id -> {lang: client}

    async def _get_client(self, sandbox_id: str, language: str) -> LSPClient:
        if sandbox_id not in self._clients:
            self._clients[sandbox_id] = {}
        if language not in self._clients[sandbox_id]:
            cmd = self.LSP_COMMANDS.get(language)
            if not cmd:
                raise ValueError(f"No LSP for language: {language}")
            sbx = self.sandbox_manager.get_sandbox(sandbox_id)
            # Need a way to start background process in sandbox.
            # E2B: `sandbox.process.start(cmd, ...)` returns Process handle with stdin/stdout.
            # For spec, we assume `sbx.start_background_process(cmd)` returns (stdin_writer, stdout_reader).
            stdin, stdout = await sbx.start_background_process(cmd)
            client = LSPClient(sandbox=sbx, language_id=language, command=cmd, root_uri="file:///workspace")
            client._process = (stdin, stdout)  # Hack for spec
            # Start reader task
            asyncio.create_task(client._read_loop(stdout))
            await client.initialize()
            self._clients[sandbox_id][language] = client
        return self._clients[sandbox_id][language]

    def _detect_language(self, file_path: str) -> str:
        ext = Path(file_path).suffix.lower()
        mapping = {
            ".ts": "typescript",
            ".tsx": "typescript",
            ".js": "javascript",
            ".jsx": "javascript",
            ".py": "python",
            ".go": "go",
            ".rs": "rust",
            ".tf": "terraform",
            ".hcl": "terraform",
        }
        return mapping.get(ext, "typescript")  # Default fallback

    async def execute(self, sandbox_id: str, args: Dict[str, Any], state: AgentState) -> ToolResult:
        action = args["action"]
        file_path = args.get("file_path")

        if action == "workspace_symbols":
            lang = "typescript"  # Default
            client = await self._get_client(sandbox_id, lang)
            result = await client.workspace_symbols(args["query"])
            return ToolResult(success=True, data={"symbols": result})

        if not file_path:
            return ToolResult(success=False, error="file_path required for this action")

        language = self._detect_language(file_path)
        client = await self._get_client(sandbox_id, language)

        line = args["line"]
        char = args["character"]

        try:
            if action == "goto_definition":
                result = await client.goto_definition(file_path, line, char)
            elif action == "find_references":
                result = await client.find_references(file_path, line, char)
            elif action == "hover":
                result = await client.hover(file_path, line, char)
            elif action == "document_symbols":
                result = await client.document_symbols(file_path)
            else:
                return ToolResult(success=False, error=f"Unknown action: {action}")

            return ToolResult(success=True, data=result)
        except Exception as e:
            return ToolResult(success=False, error=f"LSP Error: {str(e)}")
