"""
Deterministic edits of ``src/server/api/root.ts`` (written by the agent).

The spec registered routers from ``VerticalImpl.on_task_complete`` by searching for
``mergeRouters(`` which does not exist in tRPC 11 roots (ISSUES A-07, V-xx); here the
router is registered by the skill hook in the same change set.
"""

from __future__ import annotations

import re

from kernel.skills import HookContext
from kernel.skills.hooks import add_import, read_file
from kernel.state import FileChange

ROOT_PATH = "src/server/api/root.ts"
_ROUTER_OPEN = re.compile(r"createTRPCRouter\(\{[ \t]*\n")


def add_router_to_root(content: str, key: str, symbol: str, module: str) -> str:
    """Add ``import { symbol } from "module"`` and ``key: symbol,`` to ``appRouter`` (idempotent)."""
    content = add_import(content, f'import {{ {symbol} }} from "{module}";')
    if re.search(rf"^\s*{re.escape(key)}\s*:", content, flags=re.M):
        return content
    match = _ROUTER_OPEN.search(content)
    if match is None:
        raise ValueError(f"{ROOT_PATH}: `createTRPCRouter({{` block not found")
    return content[: match.end()] + f"  {key}: {symbol},\n" + content[match.end() :]


async def register_trpc_router(ctx: HookContext, key: str, symbol: str, module: str) -> FileChange | None:
    current = await read_file(ctx, ROOT_PATH)
    if current is None:
        raise FileNotFoundError(f"{ROOT_PATH} not found - run init_trpc_setup first")
    updated = add_router_to_root(current, key, symbol, module)
    if updated == current:
        return None
    return FileChange(path=ROOT_PATH, content=updated, action="update")
