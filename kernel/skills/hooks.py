"""
Helpers for skill hooks (port of ``specs/03_skills/skill/HOOKS.py``).

The spec file mixed an example hook with helpers; ``lsp_add_import`` prepended
the import to the file and ``tree_sitter_edit`` returned ``None``. Here the helpers
work on the sandbox directly and do deterministic text edits; LSP / tree-sitter
edits are out of scope until the LSP tool exists (ISSUES SK-09).
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..protocols import CommandResult
from ..state import FileChange
from .definition import HookContext


def _full(ctx: HookContext, path: str) -> str:
    return path if path.startswith("/") else f"{ctx.workspace.rstrip('/')}/{path}"


async def run_shell(
    ctx: HookContext, command: str, workdir: str | None = None, timeout_sec: int = 300
) -> CommandResult:
    return await ctx.sandbox.exec(ctx.sandbox_id, command, workdir=workdir or ctx.workspace, timeout_sec=timeout_sec)  # type: ignore[no-any-return]


async def read_file(ctx: HookContext, path: str) -> str | None:
    """File content, or ``None`` if it does not exist."""
    try:
        return str(await ctx.sandbox.read_file(ctx.sandbox_id, _full(ctx, path)))
    except (FileNotFoundError, IsADirectoryError):
        return None


_IMPORT_RE = re.compile(r"^\s*(import\s|from\s+\S+\s+import\s|export\s+\*\s+from\s|export\s+\{[^}]*\}\s+from\s)")


def add_import(content: str, import_stmt: str) -> str:
    """Insert ``import_stmt`` after the last top-of-file import (idempotent). Handles "use client"."""
    if import_stmt.strip() in (line.strip() for line in content.splitlines()):
        return content
    lines = content.splitlines()
    insert_at = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if _IMPORT_RE.match(line) or stripped in ('"use client";', "'use client';", '"use server";', "'use server';"):
            insert_at = i + 1
        elif stripped and not stripped.startswith(("//", "/*", "*")) and insert_at:
            break
    lines.insert(insert_at, import_stmt)
    return "\n".join(lines) + ("\n" if content.endswith("\n") or not content else "")


def insert_after_marker(content: str, marker: str, text: str) -> str:
    """Insert ``text`` on the line after the first line containing ``marker`` (idempotent)."""
    if text.strip() and text.strip() in content:
        return content
    lines = content.splitlines()
    for i, line in enumerate(lines):
        if marker in line:
            lines.insert(i + 1, text)
            return "\n".join(lines) + ("\n" if content.endswith("\n") else "")
    raise ValueError(f"marker {marker!r} not found")


async def update_file(ctx: HookContext, path: str, new_content: str) -> FileChange:
    """FileChange for an edited existing file (returned from ``post_render``)."""
    return FileChange(path=path, content=new_content, action="update")


def _env_keys(content: str) -> set[str]:
    keys: set[str] = set()
    for line in content.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            keys.add(stripped.split("=", 1)[0].removeprefix("export ").strip())
    return keys


def merge_env(content: str, variables: dict[str, str], comment: str | None = None) -> str:
    """Append ``KEY=value`` lines for keys not yet present (idempotent). Values are written as given."""
    present = _env_keys(content)
    missing = {k: v for k, v in variables.items() if k not in present}
    if not missing:
        return content
    block = ([f"# {comment}"] if comment else []) + [f"{k}={v}" for k, v in missing.items()]
    base = content.rstrip("\n")
    return (base + "\n\n" if base else "") + "\n".join(block) + "\n"


async def append_env_example(
    ctx: HookContext, variables: dict[str, str], comment: str | None = None, path: str = ".env.example"
) -> FileChange | None:
    """``FileChange`` adding missing variables to ``.env.example`` (``None`` if nothing to add)."""
    current = await read_file(ctx, path)
    updated = merge_env(current or "", variables, comment)
    if current is not None and updated == current:
        return None
    return FileChange(path=path, content=updated, action="update" if current is not None else "create")


def deep_merge(base: Any, patch: Any) -> Any:
    """Recursive dict merge; ``patch`` wins for non-dict values."""
    if isinstance(base, dict) and isinstance(patch, dict):
        out = dict(base)
        for k, v in patch.items():
            out[k] = deep_merge(base.get(k), v) if k in base else v
        return out
    return patch


async def merge_json_file(ctx: HookContext, path: str, patch: dict[str, Any]) -> FileChange:
    """``FileChange`` with ``patch`` deep-merged into the JSON file at ``path`` (e.g. package.json scripts)."""
    current = await read_file(ctx, path)
    data = json.loads(current) if current else {}
    merged = deep_merge(data, patch)
    return FileChange(
        path=path, content=json.dumps(merged, indent=2) + "\n", action="update" if current is not None else "create"
    )
