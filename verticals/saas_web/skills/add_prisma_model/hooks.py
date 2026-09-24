"""add_prisma_model hooks (written by the agent): merge the rendered block into schema.prisma."""

from __future__ import annotations

from typing import Any

from kernel.skills import HookContext
from kernel.skills.hooks import read_file
from kernel.state import FileChange
from verticals.saas_web.lib.prisma import add_user_relation, append_block, enum_names, find_block

SCHEMA = "prisma/schema.prisma"
RESERVED = {"id", "createdAt", "updatedAt", "userId", "user"}


def _plural(word: str) -> str:
    if word.endswith("y") and word[-2:-1] not in "aeiou":
        return word[:-1] + "ies"
    return word + ("es" if word.endswith(("s", "x", "ch", "sh")) else "s")


async def pre_render(ctx: HookContext) -> dict[str, Any]:
    fields = [f for f in ctx.inputs["fields"] if f["name"] not in RESERVED]
    declared = {e["name"] for e in ctx.inputs.get("enums", [])}
    schema = await read_file(ctx, SCHEMA) or ""
    known = declared | enum_names(schema)
    for f in fields:
        if f["type"] == "Enum" and f.get("enum") not in known:
            raise ValueError(f"field {f['name']}: enum {f.get('enum')!r} is not declared in `enums` or schema.prisma")
    enums = [e for e in ctx.inputs.get("enums", []) if e["name"] not in enum_names(schema)]
    return {"fields": fields, "enums": enums}


async def post_render(ctx: HookContext, file_changes: list[FileChange]) -> list[FileChange]:
    block = next(c for c in file_changes if c.path == "prisma/model.prisma")
    rest = [c for c in file_changes if c.path != "prisma/model.prisma"]
    schema = await read_file(ctx, SCHEMA)
    if schema is None:
        raise FileNotFoundError(f"{SCHEMA} not found - run init_prisma_postgres first")
    name = ctx.inputs["model_name"]
    if find_block(schema, "model", name) is not None:
        raise ValueError(f"model {name} already exists in {SCHEMA}")
    updated = append_block(schema, block.content)
    if ctx.inputs.get("owned_by_user", True):
        relation = name[0].lower() + name[1:]
        updated = add_user_relation(updated, f"{_plural(relation)} {name}[]")
    return [*rest, FileChange(path=SCHEMA, content=updated, action="update")]
