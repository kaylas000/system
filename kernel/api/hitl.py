"""
Human-in-the-loop HTTP/WebSocket API (``specs/06_ops/hitl/HITL_API.py``; rewritten by the agent).

    GET  /runs/{run_id}              status, progress, current task, usage
    GET  /runs/{run_id}/logs         log lines (offset/limit)
    GET  /runs/{run_id}/interrupt    pending interrupt payload (404 if none)
    POST /runs/{run_id}/interrupt    human decision {action, comment?, edited_data?} -> 202, run resumes
    POST /runs/{run_id}/approve      alias of the above (spec name)
    WS   /ws/runs/{run_id}           snapshot + live events (node / interrupt / done / error); "ping" -> "pong"

Paths follow ``specs/07_gateway/api/OPENAPI_SPEC.yaml``; authentication is added by the gateway (phase 7)
through the ``dependencies`` argument of ``build_hitl_router``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, params, status
from pydantic import BaseModel

from ..service import InvalidActionError, RunConflictError, RunManager, RunNotFoundError

Action = Literal["approve", "reject", "edit", "abort", "retry", "skip_gate"]


class ApprovalRequest(BaseModel):
    action: Action
    comment: str | None = None
    edited_data: dict[str, Any] | None = None


class InterruptPayload(BaseModel):
    run_id: str | None = None
    interrupt_type: str
    payload: dict[str, Any] = {}
    actions: list[str] = []
    created_at: str | None = None


def get_manager(request: Request) -> RunManager:
    manager: RunManager = request.app.state.run_manager
    return manager


def build_hitl_router(dependencies: Sequence[params.Depends] | None = None, include_logs: bool = True) -> APIRouter:
    """``include_logs=False``: the caller serves ``/runs/{run_id}/logs`` itself (the gateway adds SSE)."""
    router = APIRouter(tags=["runs"], dependencies=list(dependencies or []))

    @router.get("/runs/{run_id}")
    async def run_status(run_id: str, request: Request) -> dict[str, Any]:
        try:
            return await get_manager(request).status(run_id)
        except RunNotFoundError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found") from None

    if include_logs:

        @router.get("/runs/{run_id}/logs")
        async def run_logs(
            run_id: str, request: Request, offset: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=2000)
        ) -> dict[str, Any]:
            try:
                lines = await get_manager(request).logs(run_id, offset, limit)
            except RunNotFoundError:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found") from None
            return {"run_id": run_id, "offset": offset, "logs": lines}

    @router.get("/runs/{run_id}/interrupt", response_model=InterruptPayload)
    async def get_interrupt(run_id: str, request: Request) -> dict[str, Any]:
        manager = get_manager(request)
        if manager.is_running(run_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "run is executing, no interrupt")
        payload = await manager.pending_interrupt(run_id)
        if payload is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no active interrupt")
        return payload

    async def _decide(run_id: str, body: ApprovalRequest, request: Request) -> dict[str, Any]:
        try:
            await get_manager(request).resume(run_id, body.model_dump())
        except RunConflictError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
        except InvalidActionError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
        return {"run_id": run_id, "status": "resumed", "action": body.action}

    @router.post("/runs/{run_id}/interrupt", status_code=status.HTTP_202_ACCEPTED)
    async def submit_decision(run_id: str, body: ApprovalRequest, request: Request) -> dict[str, Any]:
        return await _decide(run_id, body, request)

    @router.post("/runs/{run_id}/approve", status_code=status.HTTP_202_ACCEPTED)
    async def approve(run_id: str, body: ApprovalRequest, request: Request) -> dict[str, Any]:
        return await _decide(run_id, body, request)

    @router.websocket("/ws/runs/{run_id}")
    async def run_events(websocket: WebSocket, run_id: str) -> None:
        manager: RunManager = websocket.app.state.run_manager
        await websocket.accept()
        try:
            with contextlib.suppress(RunNotFoundError):
                await websocket.send_json({"type": "state", "run_id": run_id, "data": await manager.status(run_id)})

            async def pump() -> None:
                async for event in manager.bus.subscribe(run_id, replay=False):
                    await websocket.send_json(event.model_dump(mode="json"))

            pump_task = asyncio.create_task(pump())
            try:
                while True:
                    msg = await websocket.receive_text()
                    if msg == "ping":
                        await websocket.send_text("pong")
            finally:
                pump_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pump_task
        except WebSocketDisconnect:
            return

    return router
