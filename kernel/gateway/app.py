"""
Public Gateway API (``specs/07_gateway/api/OPENAPI_SPEC.yaml``, ``GATEWAY_ARCH.md``; written by the agent).

All routes are under ``/v1`` (the spec's ``servers`` URL ends in ``/v1``); ``/health`` and ``/metrics``
stay at the root. Every ``/v1`` route is authenticated (API key or JWT) and counted against the tenant's
rpm; runs are visible only inside the owner tenant (other tenants get 404).

    GET    /v1/verticals                 installed verticals the caller may use
    GET    /v1/verticals/{id}            one vertical
    POST   /v1/generate                  202 + Location; Idempotency-Key header; auto-routing
    GET    /v1/runs                      runs of the tenant (newest first)
    GET    /v1/runs/{id}                 status (HITL router)
    DELETE /v1/runs/{id}                 cancel the current execution
    GET    /v1/runs/{id}/logs            JSON (offset/limit) or SSE with ``Accept: text/event-stream``
    GET    /v1/runs/{id}/events          SSE: node / interrupt / done / error events
    GET    /v1/runs/{id}/interrupt       pending HITL interrupt
    POST   /v1/runs/{id}/interrupt       human decision (also /approve) — takes a concurrent-run slot
    WS     /v1/ws/runs/{id}              live events (``?access_token=`` for browsers)

Quotas: rpm on every call, rpd per generation, concurrent executing runs per tenant (slot released when
the execution stops: done, error, HITL pause or cancel). ``X-Request-ID`` is accepted or generated and
returned on every response and attached to logs.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.exceptions import WebSocketException
from starlette.requests import HTTPConnection

from kernel.api import create_app
from kernel.api.hitl import build_hitl_router
from kernel.config import Settings, get_settings
from kernel.observability import metrics as m
from kernel.observability import request_id_var
from kernel.protocols import GenerateRequest, GenerateResponse, VerticalManifest
from kernel.runner import new_run_id
from kernel.service import RunEvent, RunManager, RunNotFoundError

from .auth import GatewayAuth, QuotaExceeded, RateLimiter, build_rate_limiter, quota_http_error, require_role
from .router import RoutingDecision, UnknownVerticalError, VerticalRouter, manifest_summary
from .store import GatewayStore, Principal, RunRecord
from .webhooks import WebhookSender, WebhookURLError, validate_webhook_url

logger = logging.getLogger(__name__)

TERMINAL_EVENTS = ("done", "error", "cancelled")
SSE_HEARTBEAT_SECONDS = 15.0


class GenerateBody(GenerateRequest):
    prompt: str = Field(min_length=1, max_length=50_000)


class GatewayGenerateResponse(GenerateResponse):
    vertical_id: str
    routing: dict[str, Any] = Field(default_factory=dict)
    status_url: str
    events_url: str
    logs_url: str


class RunListItem(BaseModel):
    run_id: str
    vertical_id: str
    kind: str
    parent_id: str | None = None
    user_id: str
    created_at: float
    status: str | None = None
    running: bool = False
    progress: float | None = None


StopListener = Callable[[RunEvent, RunRecord], Any]


@dataclass
class GatewayServices:
    settings: Settings
    store: GatewayStore
    limiter: RateLimiter
    auth: GatewayAuth
    router: VerticalRouter
    webhooks: WebhookSender
    # extra listeners for stopped executions (the composer subscribes here)
    stop_listeners: list[StopListener] = field(default_factory=list)

    async def on_run_stopped(self, event: RunEvent) -> None:
        rec = await self.store.get_run(event.run_id)
        if rec is None:
            return
        await self.limiter.release(rec.tenant_id, rec.run_id)
        if rec.webhook_url:
            self.webhooks.dispatch(
                rec.webhook_url,
                {
                    "event": event.type,
                    "run_id": rec.run_id,
                    "vertical_id": rec.vertical_id,
                    "data": event.data,
                    "ts": event.ts,
                },
            )
        for listener in list(self.stop_listeners):
            try:
                result = listener(event, rec)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("stop listener failed for run %s", rec.run_id)


def get_services(conn: HTTPConnection) -> GatewayServices:
    services: GatewayServices = conn.app.state.gateway
    return services


def get_manager(conn: HTTPConnection) -> RunManager:
    manager: RunManager | None = conn.app.state.run_manager
    if manager is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "service is starting")
    return manager


async def current_principal(request: Request) -> Principal:
    try:
        return await get_services(request).auth.principal(request)
    except HTTPException as exc:
        m.GATEWAY_REJECTED.labels(reason=str(exc.status_code)).inc()
        raise


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


def _base_url(conn: HTTPConnection, settings: Settings) -> str:
    base = settings.gateway.public_base_url or str(conn.base_url)
    return base.rstrip("/")


def _run_urls(conn: HTTPConnection, settings: Settings, run_id: str) -> dict[str, str]:
    base = _base_url(conn, settings)
    ws_base = "wss://" + base[8:] if base.startswith("https://") else "ws://" + base.removeprefix("http://")
    return {
        "status_url": f"{base}/v1/runs/{run_id}",
        "events_url": f"{base}/v1/runs/{run_id}/events",
        "logs_url": f"{base}/v1/runs/{run_id}/logs",
        "stream_url": f"{ws_base}/v1/ws/runs/{run_id}",
    }


async def _owned_run(services: GatewayServices, principal: Principal, run_id: str) -> RunRecord:
    rec = await services.store.get_run(run_id)
    if rec is None or rec.tenant_id != principal.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return rec


async def run_guard(conn: HTTPConnection, run_id: str) -> AsyncIterator[None]:
    """Dependency of every ``/v1/runs/{run_id}...`` route: auth, tenant isolation, RBAC, concurrent slot."""
    services = get_services(conn)
    is_ws = conn.scope.get("type") == "websocket"
    try:
        principal = await services.auth.principal(conn)
        rec = await _owned_run(services, principal, run_id)
    except HTTPException as exc:
        if is_ws:
            raise WebSocketException(code=1008, reason=str(exc.detail)) from None
        raise
    method = conn.scope.get("method", "GET")
    if method in ("GET", "HEAD") or is_ws:
        require_role(principal, "viewer")
        yield
        return
    require_role(principal, "developer")
    if method != "POST":  # DELETE handles its own slot
        yield
        return
    try:  # a human decision resumes execution -> needs a free concurrent slot
        await services.limiter.acquire(rec.tenant_id, run_id, services.auth.limits(principal))
    except QuotaExceeded as exc:
        m.GATEWAY_REJECTED.labels(reason=exc.limit).inc()
        raise quota_http_error(exc) from exc
    try:
        yield
    finally:
        manager: RunManager | None = conn.app.state.run_manager
        if manager is None or not manager.is_running(run_id):  # resume rejected or already finished
            await services.limiter.release(rec.tenant_id, run_id)


def _sse(event: str, data: Any, event_id: str | None = None) -> str:
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def _pump_events(manager: RunManager, run_id: str, replay: bool) -> tuple[asyncio.Queue[RunEvent], Any]:
    queue: asyncio.Queue[RunEvent] = asyncio.Queue()

    async def pump() -> None:
        async for ev in manager.bus.subscribe(run_id, replay=replay):
            await queue.put(ev)

    task = asyncio.create_task(pump())
    await asyncio.sleep(0)  # subscribe before the caller reads state
    return queue, task


def _is_finished(summary: Mapping[str, Any]) -> bool:
    return not summary.get("running") and summary.get("interrupt_type") in (None, "none")


def build_gateway_router() -> APIRouter:
    router = APIRouter(prefix="/v1")

    # --- verticals ---------------------------------------------------------------------------
    @router.get("/verticals", tags=["verticals"])
    async def list_verticals(request: Request, principal: CurrentPrincipal) -> list[dict[str, Any]]:
        require_role(principal, "viewer")
        verticals = get_services(request).router.verticals
        return [manifest_summary(mf) for vid, mf in sorted(verticals.items()) if principal.can_use_vertical(vid)]

    @router.get("/verticals/{vertical_id}", tags=["verticals"])
    async def get_vertical(vertical_id: str, request: Request, principal: CurrentPrincipal) -> dict[str, Any]:
        require_role(principal, "viewer")
        manifest: VerticalManifest | None = get_services(request).router.verticals.get(vertical_id)
        if manifest is None or not principal.can_use_vertical(vertical_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "vertical not found")
        info = manifest_summary(manifest)
        info["runtime"] = manifest.runtime
        info["verification"] = manifest.verification
        info["compatible_kernels"] = list(manifest.compatible_kernels)
        return info

    # --- generation --------------------------------------------------------------------------
    @router.post("/generate", tags=["runs"], status_code=status.HTTP_202_ACCEPTED, response_model=None)
    async def generate(
        body: GenerateBody,
        request: Request,
        response: Response,
        principal: CurrentPrincipal,
        idempotency_key: str | None = Header(default=None, max_length=200),
    ) -> GatewayGenerateResponse | JSONResponse:
        require_role(principal, "developer")
        services = get_services(request)
        manager = get_manager(request)
        settings = services.settings
        request_hash = hashlib.sha256(body.model_dump_json().encode()).hexdigest()

        if idempotency_key:
            existing = await services.store.get_run_by_idempotency_key(principal.tenant_id, idempotency_key)
            if existing is not None:
                return await _replay(request, services, manager, existing, request_hash)

        if body.max_budget_usd is not None and body.max_budget_usd > settings.gateway.max_budget_usd:
            raise HTTPException(400, f"max_budget_usd exceeds the limit {settings.gateway.max_budget_usd}")
        if body.webhook_url:
            try:
                await validate_webhook_url(body.webhook_url, settings.gateway.allow_private_webhooks)
            except WebhookURLError as exc:
                raise HTTPException(400, str(exc)) from None
        try:
            decision = await services.router.route(body.prompt, body.vertical_id, body.tech_stack_hints)
        except UnknownVerticalError as exc:
            raise HTTPException(400, str(exc)) from None
        if not principal.can_use_vertical(decision.vertical_id):
            raise HTTPException(403, f"no access to vertical {decision.vertical_id!r}")
        m.GATEWAY_ROUTING.labels(method=decision.method, vertical=decision.vertical_id).inc()

        limits = services.auth.limits(principal)
        run_id = new_run_id()
        try:
            await services.limiter.count_generation(principal.tenant_id, limits)
            await services.limiter.acquire(principal.tenant_id, run_id, limits)
        except QuotaExceeded as exc:
            m.GATEWAY_REJECTED.labels(reason=exc.limit).inc()
            raise quota_http_error(exc) from exc

        rec = await services.store.add_run(
            RunRecord(
                run_id=run_id,
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                vertical_id=decision.vertical_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                webhook_url=body.webhook_url,
                routing=decision.model_dump(mode="json"),
            )
        )
        if rec.run_id != run_id:  # concurrent retry with the same Idempotency-Key won the race
            await services.limiter.release(principal.tenant_id, run_id)
            return await _replay(request, services, manager, rec, request_hash)

        run_request = GenerateRequest(**body.model_dump(exclude={"vertical_id"}), vertical_id=decision.vertical_id)
        try:
            await manager.start(run_request, run_id=run_id)
        except Exception:
            await services.limiter.release(principal.tenant_id, run_id)
            await services.store.delete_run(run_id)
            raise
        logger.info(
            "run %s started: vertical=%s method=%s tenant=%s",
            run_id,
            decision.vertical_id,
            decision.method,
            principal.tenant_id,
        )
        urls = _run_urls(request, settings, run_id)
        response.headers["Location"] = urls["status_url"]
        return _generate_response(rec, "queued", "generation started", urls, decision)

    # --- runs --------------------------------------------------------------------------------
    @router.get("/runs", tags=["runs"], response_model=list[RunListItem])
    async def list_runs(
        request: Request,
        principal: CurrentPrincipal,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        parent_id: str | None = None,
        with_status: bool = True,
    ) -> list[RunListItem]:
        require_role(principal, "viewer")
        services = get_services(request)
        manager = get_manager(request)
        recs = await services.store.list_runs(principal.tenant_id, limit, offset, parent_id)
        items = []
        for rec in recs:
            item = RunListItem(
                run_id=rec.run_id,
                vertical_id=rec.vertical_id,
                kind=rec.kind,
                parent_id=rec.parent_id,
                user_id=rec.user_id,
                created_at=rec.created_at,
                running=manager.is_running(rec.run_id),
            )
            if with_status and rec.kind == "run":
                with contextlib.suppress(RunNotFoundError):
                    st = await manager.status(rec.run_id)
                    item.status, item.progress = st.get("status"), st.get("progress")
            items.append(item)
        return items

    @router.delete("/runs/{run_id}", tags=["runs"], dependencies=[Depends(run_guard)])
    async def cancel_run(run_id: str, request: Request) -> dict[str, Any]:
        cancelled = await get_manager(request).cancel(run_id)
        if not cancelled:
            raise HTTPException(status.HTTP_409_CONFLICT, "run is not executing")
        return {"run_id": run_id, "status": "cancelled"}

    @router.get("/runs/{run_id}/logs", tags=["runs"], dependencies=[Depends(run_guard)], response_model=None)
    async def run_logs(
        run_id: str,
        request: Request,
        offset: int = Query(0, ge=0),
        limit: int = Query(200, ge=1, le=2000),
        stream: bool = False,
        last_event_id: str | None = Header(default=None),
    ) -> dict[str, Any] | StreamingResponse:
        manager = get_manager(request)
        wants_sse = stream or "text/event-stream" in request.headers.get("accept", "")
        if not wants_sse:
            try:
                return {"run_id": run_id, "offset": offset, "logs": await manager.logs(run_id, offset, limit)}
            except RunNotFoundError:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found") from None
        if last_event_id and last_event_id.isdigit():
            offset = int(last_event_id) + 1
        return StreamingResponse(
            _log_stream(manager, run_id, offset), media_type="text/event-stream", headers=_SSE_HEADERS
        )

    @router.get("/runs/{run_id}/events", tags=["runs"], dependencies=[Depends(run_guard)])
    async def run_events(run_id: str, request: Request) -> StreamingResponse:
        manager = get_manager(request)
        return StreamingResponse(_event_stream(manager, run_id), media_type="text/event-stream", headers=_SSE_HEADERS)

    return router


_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


async def _log_stream(manager: RunManager, run_id: str, offset: int) -> AsyncIterator[str]:
    """New log lines (``id`` = line index, so ``Last-Event-ID`` resumes); ends when the run finishes."""
    queue, pump = await _pump_events(manager, run_id, replay=False)
    try:
        while True:
            try:
                lines = await manager.logs(run_id, offset, 2000)
            except RunNotFoundError:
                lines = []
            for line in lines:
                yield _sse("log", {"line": line}, str(offset))
                offset += 1
            if len(lines) == 2000:
                continue
            try:
                summary = await manager.status(run_id)
            except RunNotFoundError:
                summary = {"running": manager.is_running(run_id)}
            if not summary.get("running"):
                yield _sse("end", {"status": summary.get("status"), "interrupt_type": summary.get("interrupt_type")})
                return
            try:
                ev = await asyncio.wait_for(queue.get(), SSE_HEARTBEAT_SECONDS)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            if ev.type in (*TERMINAL_EVENTS, "interrupt"):
                await manager.wait(run_id)
    finally:
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)


async def _event_stream(manager: RunManager, run_id: str) -> AsyncIterator[str]:
    """Current state, then live events; ends on done / error / cancelled (stays open across HITL pauses)."""
    queue, pump = await _pump_events(manager, run_id, replay=False)
    try:
        try:
            summary = await manager.status(run_id)
        except RunNotFoundError:
            summary = {"run_id": run_id, "status": "unknown", "running": manager.is_running(run_id)}
        yield _sse("state", summary)
        if _is_finished(summary):
            return
        while True:
            try:
                ev = await asyncio.wait_for(queue.get(), SSE_HEARTBEAT_SECONDS)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            yield _sse(ev.type, ev.model_dump(mode="json"))
            if ev.type in TERMINAL_EVENTS:
                return
    finally:
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)


def _generate_response(
    rec: RunRecord, run_status: str, message: str, urls: dict[str, str], decision: RoutingDecision | None = None
) -> GatewayGenerateResponse:
    return GatewayGenerateResponse(
        run_id=rec.run_id,
        status=run_status,
        message=message,
        vertical_id=rec.vertical_id,
        routing=decision.model_dump(mode="json") if decision is not None else rec.routing,
        **urls,
    )


async def _replay(
    request: Request, services: GatewayServices, manager: RunManager, rec: RunRecord, request_hash: str
) -> JSONResponse:
    if rec.request_hash and rec.request_hash != request_hash:
        raise HTTPException(422, "Idempotency-Key was used with a different request")
    try:
        run_status = str((await manager.status(rec.run_id)).get("status") or "queued")
    except RunNotFoundError:
        run_status = "unknown"
    urls = _run_urls(request, services.settings, rec.run_id)
    body = _generate_response(rec, run_status, "existing run (Idempotency-Key)", urls)
    return JSONResponse(
        body.model_dump(mode="json"),
        status_code=status.HTTP_200_OK,
        headers={"Location": urls["status_url"], "Idempotent-Replayed": "true"},
    )


class RequestIDMiddleware:
    """Accept or generate ``X-Request-ID``; expose it to logs and echo it on the response (HTTP and SSE)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        incoming = dict(scope.get("headers") or []).get(b"x-request-id", b"").decode("latin-1")
        rid = "".join(ch for ch in incoming if ch.isalnum() or ch in "-_.:")[:128] or uuid.uuid4().hex
        token = request_id_var.set(rid)

        async def send_with_id(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((b"x-request-id", rid.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_id_var.reset(token)


def create_gateway_app(
    settings: Settings | None = None,
    *,
    run_manager: RunManager | None = None,
    setup: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
    manifests: Mapping[str, VerticalManifest] | Callable[[], Mapping[str, VerticalManifest]] | None = None,
    llm_client: Any = None,
    store: GatewayStore | None = None,
    limiter: RateLimiter | None = None,
    vertical_router: VerticalRouter | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    cfg = settings.gateway
    store = store or GatewayStore(cfg.db_path)
    limiter = limiter or build_rate_limiter(settings)
    if vertical_router is None:
        vertical_router = VerticalRouter(
            manifests if manifests is not None else {},
            llm_client,
            default_vertical=settings.kernel.default_vertical,
            classifier_model=cfg.classifier_model,
        )
    services = GatewayServices(
        settings=settings,
        store=store,
        limiter=limiter,
        auth=GatewayAuth(settings, store, limiter),
        router=vertical_router,
        webhooks=WebhookSender(settings),
    )

    @asynccontextmanager
    async def gateway_setup(app: FastAPI) -> AsyncIterator[None]:
        async with contextlib.AsyncExitStack() as stack:
            if setup is not None:
                await stack.enter_async_context(setup(app))
            manager: RunManager | None = app.state.run_manager
            if manager is not None and services.on_run_stopped not in manager.stop_hooks:
                manager.stop_hooks.append(services.on_run_stopped)
            try:
                yield
            finally:
                await services.webhooks.drain()

    app = create_app(
        run_manager,
        settings,
        setup=gateway_setup,
        cors_origins=cfg.cors_origins,
        title="AutoGen Platform API",
        include_hitl=False,
    )
    app.state.gateway = services
    if run_manager is not None:  # tests without lifespan
        run_manager.stop_hooks.append(services.on_run_stopped)

    app.add_middleware(RequestIDMiddleware)
    app.include_router(build_gateway_router())
    app.include_router(build_hitl_router([Depends(run_guard)]), prefix="/v1")
    return app
