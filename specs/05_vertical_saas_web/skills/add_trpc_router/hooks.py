# specs/05_vertical_saas_web/skills/add_trpc_router/hooks.py

from __future__ import annotations
from typing: Dict, Any, List
from kernel.state import FileChange
from ...SKILL_DEFINITION import HookContext

async def pre_render(ctx: HookContext) -> Dict[str, Any]:
    inputs = ctx.inputs
    
    # 1. Auto-derive Zod validation strings from field types
    for field in inputs.get("fields", []):
        if field.get("type") == "string" and not field.get("validation"):
            if field["name"].endswith("_email") or field["name"] == "email":
                field["validation"] = "email"
            elif field["name"].endswith("_url") or field["name"] == "url":
                field["validation"] = "url"
            elif field["name"] in ("name", "title", "description"):
                field["validation"] = "min(1).max(255)"
    
    # 2. Ensure permissions object exists
    inputs.setdefault("permissions", {
        "list": "protected", "get": "protected", 
        "create": "protected", "update": "protected", "delete": "admin"
    })
    
    return inputs

async def post_render(ctx: HookContext, file_changes: List[FileChange]) -> List[FileChange]:
    # The VerticalImpl.on_task_complete handles root router registration.
    # But we can also generate a test file here if needed.
    return file_changes

async def validate(ctx: HookContext) -> List[Any]:
    # Typecheck is run as post_script
    return []
