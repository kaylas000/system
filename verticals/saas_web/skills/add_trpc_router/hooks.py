"""add_trpc_router hooks (written by the agent)."""

from __future__ import annotations

from typing import Any

from kernel.skills import HookContext
from kernel.skills.hooks import read_file
from kernel.state import FileChange
from verticals.saas_web.lib.fields import editable_fields
from verticals.saas_web.lib.prisma import parse_model
from verticals.saas_web.lib.trpc import register_trpc_router

PROC = {"public": "publicProcedure", "protected": "protectedProcedure", "admin": "adminProcedure"}


async def pre_render(ctx: HookContext) -> dict[str, Any]:
    schema = await read_file(ctx, "prisma/schema.prisma")
    if schema is None:
        raise FileNotFoundError("prisma/schema.prisma not found")
    model = parse_model(schema, ctx.inputs["model_name"])
    owned = model.get("userId") is not None
    fields = editable_fields(model)
    enum_imports = sorted({f["type"] for f in fields if f["kind"] == "enum"})
    perms = ctx.inputs.get("permissions") or {}
    read_proc = PROC["protected" if owned else perms.get("read", "protected")]
    write_proc = PROC["protected" if owned else perms.get("write", "protected")]
    name = ctx.inputs["model_name"]
    return {
        "owned": owned,
        "zod_fields": fields,
        "enum_imports": enum_imports,
        "read_proc": read_proc,
        "write_proc": write_proc,
        "procedure_imports": sorted({"createTRPCRouter", read_proc, write_proc}),
        "delegate": name[0].lower() + name[1:],
        "order_by": '{ createdAt: "desc" }' if model.get("createdAt") else '{ id: "asc" }',
    }


async def post_render(ctx: HookContext, file_changes: list[FileChange]) -> list[FileChange]:
    name = ctx.inputs["router_name"]
    root = await register_trpc_router(ctx, name, f"{name}Router", f"@/server/api/routers/{name}")
    return [*file_changes, *([root] if root else [])]
