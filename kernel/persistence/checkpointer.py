"""LangGraph checkpointer factory: Postgres in production, in-memory for dev/tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from ..config import Settings
from .serde import kernel_serde


def memory_checkpointer() -> InMemorySaver:
    return InMemorySaver(serde=kernel_serde())


@asynccontextmanager
async def open_checkpointer(settings: Settings) -> AsyncIterator[Any]:
    """Yield a checkpointer: ``AsyncPostgresSaver`` if ``database.postgres_dsn`` is set, else in-memory."""
    dsn = settings.database.postgres_dsn
    if not dsn:
        yield memory_checkpointer()
        return
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    except ImportError as exc:  # pragma: no cover - optional extra
        raise RuntimeError("Postgres checkpointer requires `pip install .[postgres]`") from exc
    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:  # pragma: no cover - needs Postgres
        saver.serde = kernel_serde()
        await saver.setup()
        yield saver
