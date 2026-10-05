# specs/03_skills/examples/add_prisma_model/hooks.py
"""
Advanced Hook: Appends to existing schema.prisma using Tree-sitter.
This avoids full file rewrite and preserves formatting/comments.
"""

from __future__ import annotations
from typing import Dict, Any, List
from kernel.state import FileChange
from ..SKILL_DEFINITION import HookContext


async def pre_render(ctx: HookContext) -> Dict[str, Any]:
    inputs = ctx.inputs
    # Ensure table_name default
    if "table_name" not in inputs:
        from ..TEMPLATE_ENGINE import snake_case

        inputs["table_name"] = snake_case(inputs["model_name"]) + "s"
    return inputs


async def post_render(ctx: HookContext, file_changes: List[FileChange]) -> List[FileChange]:
    """
    Instead of writing new file, we want to APPEND to prisma/schema.prisma.
    The template renders the *new model block*. We inject it into existing file.
    """
    inputs = ctx.inputs
    model_block = None

    # Find the rendered model block in file_changes (there should be only one .j2 usually)
    for fc in file_changes:
        if fc.path.endswith("schema.prisma"):  # Our template targets this path
            model_block = fc.content
            # Remove from list, we handle manually
            file_changes.remove(fc)
            break

    if not model_block:
        return file_changes  # Should not happen

    # Read existing schema.prisma from Sandbox
    fs_tool = ctx.tool_registry.get("filesystem")
    read_res = await fs_tool.execute(ctx.sandbox_id, {"action": "read", "path": "prisma/schema.prisma"}, {})

    if not read_res.success:
        # File doesn't exist? Create it (fallback)
        file_changes.append(FileChange(path="prisma/schema.prisma", content=model_block, action="create"))
        return file_changes

    existing_content = read_res.data["content"]

    # Find insertion point: Before last `}` of file? Or after last model?
    # Robust way: Use Tree-sitter to find last top-level declaration.
    # Simplified heuristic: Insert before last non-empty line if it's `}` or just append.
    lines = existing_content.splitlines()

    # Find last line that is not whitespace/comment
    insert_idx = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if stripped and not stripped.startswith("//"):
            # Insert after this line
            insert_idx = i + 1
            break

    # Ensure blank line before new model
    new_lines = lines[:insert_idx] + ["", ""] + model_block.splitlines() + [""]
    new_content = "\n".join(new_lines)

    file_changes.append(FileChange(path="prisma/schema.prisma", content=new_content, action="update"))

    return file_changes


async def validate(ctx: HookContext) -> List[VerificationGateResult]:
    # Validation is handled by post_scripts: `prisma validate`
    return []
