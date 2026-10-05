"""add_dockerfile_prod hooks (written by the agent)."""

from __future__ import annotations

from typing import Any

from kernel.skills import HookContext
from kernel.skills.hooks import read_file


async def pre_render(ctx: HookContext) -> dict[str, Any]:
    pm = (ctx.state.get("skill_outputs") or {}).get("init_nextjs_app_router", {}).get("package_manager", "pnpm")
    project = await read_file(ctx, "package.json") or ""
    return {
        "package_manager": pm,
        "has_prisma": bool(await read_file(ctx, "prisma/schema.prisma")),
        "has_auth": '"next-auth"' in project and bool(await read_file(ctx, "src/server/auth/index.ts")),
    }
