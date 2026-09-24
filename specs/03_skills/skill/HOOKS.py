# specs/03_skills/skill/HOOKS.py
"""
Standard Hook Interface & Built-in Helpers.
Hooks allow imperative logic where templates are insufficient:
- Complex conditional file generation
- Interacting with LSP (e.g., add import to existing file)
- Running shell commands not covered by post_scripts
- Modifying existing files (AST manipulation via Tree-sitter/LSP)
"""

from __future__ import annotations
from typing import Dict, Any, List, Optional
from pathlib import Path
from kernel.state import FileChange, VerificationGateResult, VerificationGateStatus
from kernel.protocols import ISandbox, ITool
from .SKILL_DEFINITION import HookContext

# --- Example Hook Implementation (for reference) ---

# File: verticals/saas_web/skills/add_prisma_model/hooks.py


async def pre_render(ctx: HookContext) -> Dict[str, Any]:
    """
    Runs BEFORE template rendering.
    Use to: fetch data, compute derived values, validate complex rules.
    Return dict to MERGE into inputs.
    """
    inputs = ctx.inputs
    model_name = inputs["model_name"]

    # Auto-generate table name if not provided
    if "table_name" not in inputs:
        inputs["table_name"] = snake_case(model_name) + "s"

    # Check if model already exists (via LSP/Grep)
    # This prevents overwriting
    # skill_output = ctx.state.get("skill_outputs", {}).get("init_prisma")
    # ...

    return inputs


async def post_render(ctx: HookContext, file_changes: List[FileChange]) -> List[FileChange]:
    """
    Runs AFTER templates rendered but BEFORE files written to sandbox.
    Use to: modify generated FileChanges, add extra files based on logic.
    """
    # Example: If adding a relation, also update the related model file
    if ctx.inputs.get("add_relation"):
        related_model = ctx.inputs["related_model"]
        # Use LSP Tool to find related model file and insert relation field
        # lsp_tool = ctx.tool_registry.get("lsp")
        # ...
        pass
    return file_changes


async def validate(ctx: HookContext) -> List[VerificationGateResult]:
    """
    Runs AFTER post_scripts.
    Use for: Custom validation logic beyond shell commands.
    Returns list of VerificationGateResult.
    """
    results = []
    # Example: Check Prisma syntax via `prisma validate`
    # This is already covered by post_scripts usually.
    # But here we can do semantic checks:
    # "Ensure every model has @id field"
    # Parse generated schema.prisma with Tree-sitter...
    return results


# --- Built-in Helpers for Hooks ---


class HookHelpers:
    @staticmethod
    async def run_shell(ctx: HookContext, command: str, workdir: str = None) -> Dict:
        shell_tool = ctx.tool_registry.get("shell")
        return (
            await shell_tool.execute(ctx.sandbox_id, {"command": command, "workdir": workdir or ctx.workspace}, {})
        ).data

    @staticmethod
    async def lsp_add_import(ctx: HookContext, file_path: str, import_stmt: str) -> List[FileChange]:
        """Use LSP to safely add import to existing file."""
        # 1. Read file
        fs_tool = ctx.tool_registry.get("filesystem")
        read_res = await fs_tool.execute(ctx.sandbox_id, {"action": "read", "path": file_path}, {})
        if not read_res.success:
            return []
        content = read_res.data["content"]

        # 2. Check if already exists
        if import_stmt in content:
            return []

        # 3. Use LSP to find best insertion point (after last import)
        lsp_tool = ctx.tool_registry.get("lsp")
        # This is complex - usually better to use Tree-sitter in hook directly
        # Simplified: prepend to file
        new_content = import_stmt + "\n" + content
        return [FileChange(path=file_path, content=new_content, action="update")]

    @staticmethod
    async def tree_sitter_edit(ctx: HookContext, file_path: str, edits: List[Dict]) -> FileChange:
        """Perform precise AST edits using Tree-sitter (Python side, reads file, edits, returns change)."""
        # Requires file content locally. Read via FS tool.
        fs_tool = ctx.tool_registry.get("filesystem")
        read_res = await fs_tool.execute(ctx.sandbox_id, {"action": "read", "path": file_path}, {})
        if not read_res.success:
            raise FileNotFoundError(file_path)

        code = read_res.data["content"]
        # Apply edits using tree-sitter (see tree_sitter_helpers.py)
        # new_code = apply_edits(code, edits)
        # return FileChange(path=file_path, content=new_code, action="update")
        pass
