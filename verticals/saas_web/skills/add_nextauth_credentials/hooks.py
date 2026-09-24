"""add_nextauth_credentials hooks (written by the agent)."""

from __future__ import annotations

from kernel.skills import HookContext
from kernel.skills.hooks import append_env_example
from kernel.state import FileChange
from verticals.saas_web.lib.trpc import register_trpc_router


async def post_render(ctx: HookContext, file_changes: list[FileChange]) -> list[FileChange]:
    env_vars = {"AUTH_SECRET": '""', "AUTH_TRUST_HOST": "true"}
    if ctx.inputs.get("github_oauth"):
        env_vars |= {"AUTH_GITHUB_ID": '""', "AUTH_GITHUB_SECRET": '""'}
    extra: list[FileChange] = []
    env = await append_env_example(
        ctx, env_vars, comment="Auth.js (generate AUTH_SECRET with: openssl rand -base64 32)"
    )
    if env:
        extra.append(env)
    root = await register_trpc_router(ctx, "auth", "authRouter", "@/server/api/routers/auth")
    if root:
        extra.append(root)
    return [*file_changes, *extra]
