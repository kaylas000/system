"""
FastAPI application factory (written by the agent). Phase 6: health, Prometheus metrics and the HITL
router; the gateway (phase 7) adds ``/generate``, verticals and auth on top of ``create_app``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from typing import Any

from fastapi import FastAPI, Response, params

from ..config import Settings, get_settings
from ..observability import PROMETHEUS_CONTENT_TYPE, render_latest
from ..service import RunManager
from .hitl import build_hitl_router


def create_app(
    run_manager: RunManager | None = None,
    settings: Settings | None = None,
    setup: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
    auth_dependencies: Sequence[params.Depends] | None = None,
    cors_origins: Sequence[str] = (),
    title: str = "AutoGen Kernel API",
    include_hitl: bool = True,
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            if setup is not None:  # e.g. open the Postgres checkpointer and set app.state.run_manager
                await stack.enter_async_context(setup(app))
            try:
                yield
            finally:
                manager: RunManager | None = app.state.run_manager
                if manager is not None:
                    await manager.shutdown()

    app = FastAPI(title=title, lifespan=lifespan)
    app.state.run_manager = run_manager
    app.state.settings = settings
    if cors_origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(CORSMiddleware, allow_origins=list(cors_origins), allow_methods=["*"], allow_headers=["*"])

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, Any]:
        manager: RunManager | None = app.state.run_manager
        if manager is None:
            return {"status": "starting", "active_runs": 0}
        return {"status": "ok", "active_runs": len(manager.active_runs())}

    @app.get("/metrics", tags=["ops"], include_in_schema=False)
    async def metrics() -> Response:
        return Response(render_latest(), media_type=PROMETHEUS_CONTENT_TYPE)

    if include_hitl:  # the gateway mounts its own tenant-aware copy under /v1
        app.include_router(build_hitl_router(auth_dependencies))
    return app
