"""Prisma model -> form/zod field descriptions shared by add_trpc_router and add_crud_page (written by the agent)."""

from __future__ import annotations

from typing import Any

from .prisma import PrismaField, PrismaModel

SKIP = {"createdAt", "updatedAt", "userId"}


def kind_of(f: PrismaField, enums: set[str]) -> str | None:
    if f.type == "String":
        return "text" if "@db.Text" in f.attributes else "string"
    if f.type in ("Int", "BigInt"):
        return "int"
    if f.type in ("Float", "Decimal"):
        return "float"
    return {"Boolean": "boolean", "DateTime": "date", "Json": "json"}.get(
        f.type, "enum" if f.type in enums else ""
    ) or None


def zod_for(f: PrismaField, kind: str) -> str:
    lname = f.name.lower()
    if kind in ("string", "text"):
        base = "z.string().trim()"
        if "email" in lname:
            base += ".email()"
        elif lname.endswith("url"):
            base += ".url()"
        elif not f.optional:
            base += ".min(1)"
        base += ".max(10000)" if kind == "text" else ".max(255)"
    elif kind == "int":
        base = "z.number().int()"
    elif kind == "float":
        base = "z.number()"
    elif kind == "boolean":
        base = "z.boolean()"
    elif kind == "date":
        base = "z.coerce.date()"
    elif kind == "json":
        # Prisma Json inputs reject plain `null` (needs Prisma.JsonNull) -> optional, never nullish
        return "z.record(z.any())" + (".optional()" if f.optional or f.has_default else "")
    else:
        base = f"z.nativeEnum({f.type})"
    if f.optional:
        return base + ".nullish()"
    if f.has_default:
        return base + ".optional()"
    return base


def editable_fields(model: PrismaModel) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in model.fields:
        if f.is_id or f.is_relation or f.is_list or f.name in SKIP or "@updatedAt" in f.attributes:
            continue
        kind = kind_of(f, model.enums)
        if kind is None:
            continue
        out.append(
            {
                "name": f.name,
                "label": "".join(" " + c.lower() if c.isupper() else c for c in f.name).capitalize(),
                "kind": kind,
                "type": f.type,
                "optional": f.optional,
                "has_default": f.has_default,
                "required": not f.optional and not f.has_default,
                "zod": zod_for(f, kind),
            }
        )
    return out
