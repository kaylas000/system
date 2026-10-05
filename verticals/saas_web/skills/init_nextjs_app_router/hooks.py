"""init_nextjs_app_router hooks (written by the agent)."""

from __future__ import annotations

import secrets
from typing import Any

from kernel.skills import HookContext


async def pre_render(ctx: HookContext) -> dict[str, Any]:
    name: str = ctx.inputs["project_name"]
    title = ctx.inputs.get("title") or " ".join(w.capitalize() for w in name.split("-") if w)
    # dev-only secret for the local .env (never committed: .env is in .gitignore)
    return {"title": title, "dev_auth_secret": secrets.token_urlsafe(32)}
