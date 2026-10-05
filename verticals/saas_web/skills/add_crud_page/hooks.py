"""add_crud_page hooks (written by the agent): form/list JSX derived from the Prisma model."""

from __future__ import annotations

from typing import Any

from kernel.skills import HookContext
from kernel.skills.hooks import add_import, insert_after_marker, read_file
from kernel.state import FileChange
from verticals.saas_web.lib.fields import editable_fields
from verticals.saas_web.lib.prisma import parse_model

DASHBOARD = "src/app/dashboard/page.tsx"
MARKER = "dashboard sections: generated content is added below this line"


def _plural(word: str) -> str:
    if word.endswith("y") and word[-2:-1] not in "aeiou":
        return word[:-1] + "ies"
    return word + ("es" if word.endswith(("s", "x", "ch", "sh")) else "s")


def _words(name: str) -> list[str]:
    out: list[str] = []
    for ch in name:
        if ch.isupper() and out:
            out.append(" ")
        out.append(ch.lower())
    return "".join(out).split()


def form_value(f: dict[str, Any]) -> str | None:
    get = f'form.get("{f["name"]}")'
    kind, req = f["kind"], f["required"]
    if kind in ("string", "text"):
        return f'String({get} ?? "")' if req else f'String({get} ?? "") || undefined'
    if kind in ("int", "float"):
        return f"Number({get})" if req else f"{get} ? Number({get}) : undefined"
    if kind == "boolean":
        return f'{get} === "on"'
    if kind == "date":
        return f"new Date(String({get}))" if req else f"{get} ? new Date(String({get})) : undefined"
    if kind == "enum":
        return (
            f"String({get}) as {f['type']}" if req else f'(String({get} ?? "") || undefined) as {f["type"]} | undefined'
        )
    if kind == "json":
        return "{}" if req else None
    return None


def form_input(f: dict[str, Any]) -> str | None:
    name, label, kind = f["name"], f["label"], f["kind"]
    required = " required" if f["required"] else ""
    if kind == "string":
        lname = name.lower()
        typ = "email" if "email" in lname else "url" if lname.endswith("url") else "text"
        return f'<input name="{name}" type="{typ}" placeholder="{label}"{required} maxLength={{255}} className={{inputClass}} />'
    if kind == "text":
        return f'<textarea name="{name}" placeholder="{label}"{required} rows={{3}} className={{inputClass}} />'
    if kind in ("int", "float"):
        step = "1" if kind == "int" else "any"
        return f'<input name="{name}" type="number" step="{step}" placeholder="{label}"{required} className={{inputClass}} />'
    if kind == "date":
        return (
            f'<label className="flex flex-col gap-1 text-sm">{label}'
            f'<input name="{name}" type="date"{required} className={{inputClass}} /></label>'
        )
    if kind == "boolean":
        return f'<label className="flex items-center gap-2 text-sm"><input name="{name}" type="checkbox" /> {label}</label>'
    if kind == "enum":
        return (
            f'<select name="{name}"{required} className={{inputClass}} defaultValue="">'
            f'<option value="" disabled>{label}</option>'
            f"{{Object.values({f['type']}).map((v) => (<option key={{v}} value={{v}}>{{v}}</option>))}}"
            "</select>"
        )
    return None


def display(f: dict[str, Any], primary: str | None) -> str | None:
    name, kind = f["name"], f["kind"]
    if kind == "boolean":
        return (
            f'<label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={{item.{name}}} '
            f"onChange={{() => update.mutate({{ id: item.id, {name}: !item.{name} }})}} /> {f['label']}</label>"
        )
    if kind == "date":
        return (
            f'<span className="text-sm text-muted-foreground">'
            f'{{item.{name} ? new Date(item.{name}).toLocaleDateString() : ""}}</span>'
        )
    if kind == "json":
        return None
    cls = "font-medium" if name == primary else "text-sm text-muted-foreground"
    return f'<span className="{cls}">{{String(item.{name} ?? "")}}</span>'


async def pre_render(ctx: HookContext) -> dict[str, Any]:
    schema = await read_file(ctx, "prisma/schema.prisma")
    if schema is None:
        raise FileNotFoundError("prisma/schema.prisma not found")
    model_name: str = ctx.inputs["model_name"]
    fields = editable_fields(parse_model(schema, model_name))
    primary = next((f["name"] for f in fields if f["kind"] == "string"), None)
    for f in fields:
        f["value"] = form_value(f)
        f["input"] = form_input(f)
        f["display"] = display(f, primary)
    words = _words(model_name)
    words[-1] = _plural(words[-1])
    router: str = ctx.inputs["router_name"]
    return {
        "fields": fields,
        "enum_imports": sorted({f["type"] for f in fields if f["kind"] == "enum"}),
        "component": router[0].upper() + router[1:] + "Manager",
        "title": ctx.inputs.get("title") or " ".join(words).capitalize(),
        "segment": ctx.inputs.get("segment") or "-".join(words),
    }


async def post_render(ctx: HookContext, file_changes: list[FileChange]) -> list[FileChange]:
    page = await read_file(ctx, DASHBOARD)
    if page is None or MARKER not in page:
        return file_changes
    segment = next(c.path.split("/")[3] for c in file_changes if c.path.startswith("src/app/dashboard/"))
    title = ctx.inputs.get("title") or segment.replace("-", " ").capitalize()
    link = f'      <Link href="/dashboard/{segment}" className="underline">{title}</Link>'
    updated = insert_after_marker(add_import(page, 'import Link from "next/link";'), MARKER, link)
    if updated == page:
        return file_changes
    return [*file_changes, FileChange(path=DASHBOARD, content=updated, action="update")]
