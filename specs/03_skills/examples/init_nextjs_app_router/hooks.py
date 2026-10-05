# specs/03_skills/examples/init_nextjs_app_router/hooks.py
"""
Hooks for init_nextjs_app_router skill.
"""

from __future__ import annotations
from typing import Dict, Any, List
from kernel.state import FileChange, VerificationGateResult, VerificationGateStatus
from ..SKILL_DEFINITION import HookContext


async def pre_render(ctx: HookContext) -> Dict[str, Any]:
    inputs = ctx.inputs

    # Derive values
    if "project_name" in inputs:
        # Ensure valid npm name
        inputs["project_name"] = inputs["project_name"].lower().replace("_", "-")

    # Set default ports for dev server
    inputs["dev_port"] = 3000

    return inputs


async def post_render(ctx: HookContext, file_changes: List[FileChange]) -> List[FileChange]:
    # No post-render modifications needed for this skill
    return file_changes


async def validate(ctx: HookContext) -> List[VerificationGateResult]:
    """
    Custom validation: Check if package.json has correct structure.
    """
    results = []
    # We could parse package.json here and validate versions match
    # For now, rely on post_scripts validation (pnpm install, build)
    return results
