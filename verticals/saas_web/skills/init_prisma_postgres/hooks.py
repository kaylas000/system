"""init_prisma_postgres hooks (written by the agent; replaces the broken addenda version, A-01)."""

from __future__ import annotations

from kernel.skills import HookContext
from kernel.skills.hooks import append_env_example
from kernel.state import FileChange


async def post_render(ctx: HookContext, file_changes: list[FileChange]) -> list[FileChange]:
    env = await append_env_example(
        ctx,
        {"DATABASE_URL": '"postgresql://postgres:postgres@localhost:5432/app?schema=public"'},
        comment="Database (PostgreSQL)",
    )
    return [*file_changes, *([env] if env else [])]
