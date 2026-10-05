from kernel.skills import HookContext


async def pre_render(ctx: HookContext) -> dict:
    return {"title": ctx.inputs["project_name"].upper()}
