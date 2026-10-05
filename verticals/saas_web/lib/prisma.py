"""
Minimal Prisma schema helpers for saas_web hooks (written by the agent).

Only what the skills need: find/parse a ``model`` block, append blocks, add a line to
the ``User`` model. Not a general Prisma parser (no multi-line attributes, no views).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SCALARS = {"String", "Int", "BigInt", "Float", "Decimal", "DateTime", "Boolean", "Json", "Bytes"}
USER_RELATIONS_MARKER = "// relations: generated models are added below this line"

_BLOCK_RE = r"^(model|enum)\s+{name}\s*\{{(.*?)^\}}"
_FIELD_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s+([A-Za-z_]\w*)(\[\])?(\?)?(.*)$")


@dataclass
class PrismaField:
    name: str
    type: str
    optional: bool = False
    is_list: bool = False
    attributes: str = ""
    is_relation: bool = False

    @property
    def is_id(self) -> bool:
        return "@id" in self.attributes

    @property
    def has_default(self) -> bool:
        return "@default(" in self.attributes or "@updatedAt" in self.attributes


@dataclass
class PrismaModel:
    name: str
    fields: list[PrismaField] = field(default_factory=list)
    enums: set[str] = field(default_factory=set)

    def get(self, name: str) -> PrismaField | None:
        return next((f for f in self.fields if f.name == name), None)


def find_block(schema: str, kind: str, name: str) -> re.Match[str] | None:
    m = re.search(_BLOCK_RE.format(name=re.escape(name)), schema, flags=re.M | re.S)
    return m if m and m.group(1) == kind else None


def enum_names(schema: str) -> set[str]:
    return set(re.findall(r"^enum\s+(\w+)\s*\{", schema, flags=re.M))


def model_names(schema: str) -> set[str]:
    return set(re.findall(r"^model\s+(\w+)\s*\{", schema, flags=re.M))


def parse_model(schema: str, name: str) -> PrismaModel:
    block = find_block(schema, "model", name)
    if block is None:
        raise ValueError(f"model {name} not found in schema.prisma")
    enums = enum_names(schema)
    models = model_names(schema)
    out = PrismaModel(name=name, enums=enums)
    for line in block.group(2).splitlines():
        stripped = line.split("//", 1)[0].rstrip()
        if not stripped.strip() or stripped.strip().startswith("@@"):
            continue
        m = _FIELD_RE.match(stripped)
        if not m:
            continue
        fname, ftype, is_list, optional, attrs = m.groups()
        out.fields.append(
            PrismaField(
                fname,
                ftype,
                optional=bool(optional),
                is_list=bool(is_list),
                attributes=attrs.strip(),
                is_relation=ftype in models,
            )
        )
    return out


def append_block(schema: str, block: str) -> str:
    return schema.rstrip("\n") + "\n\n" + block.strip("\n") + "\n"


def add_user_relation(schema: str, line: str) -> str:
    """Insert ``line`` into ``model User`` (after the marker, else before the closing brace)."""
    block = find_block(schema, "model", "User")
    if block is None:
        raise ValueError("model User not found (init_prisma_postgres must run first)")
    body = block.group(2)
    if re.search(rf"^\s*{re.escape(line.split()[0])}\s", body, flags=re.M):
        return schema
    if USER_RELATIONS_MARKER in body:
        new_body = body.replace(USER_RELATIONS_MARKER, f"{USER_RELATIONS_MARKER}\n  {line}", 1)
    else:
        new_body = body.rstrip("\n") + f"\n  {line}\n"
    return schema[: block.start(2)] + new_body + schema[block.end(2) :]
