"""
Run manager + event bus (``specs/06_ops/hitl/HITL_API.py`` background resume,
``WEBSOCKET_MANAGER.py`` and ``APPROVAL_WORKFLOW.py`` — the last two have no content in the spec;
written by the agent).

* one compiled graph (checkpointer shared by all runs); the vertical is resolved per run from
  ``GenerateRequest.vertical_id`` (and from the checkpointed ``vertical_id`` on resume);
* runs execute as asyncio tasks, limited by ``max_concurrent`` (queue depth / active runs metrics);
* every node update is published on the ``EventBus`` (new log lines, status, current task,
  interrupt) — consumed by the WebSocket endpoint; late subscribers get the recent history;
* ``resume`` validates the action against the interrupt payload before resuming (409/400 in the API).

Spec defects fixed (ISSUES O-xx): ``background_tasks.resume_graph`` does not exist; the graph was
rebuilt with a hard-coded ``saas_web`` vertical on every resume; the interrupt was read from
``checkpoint.metadata["interrupts"]`` (LangGraph keeps it in pending writes / ``StateSnapshot.tasks``).
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any

from langgraph.types import Command
from pydantic import BaseModel, Field
from pydantic_core import to_jsonable_python

from ..config import Settings, get_settings
from ..observability import metrics as m
from ..protocols import GenerateRequest, IVertical
from ..runner import initial_state, new_run_id, run_config
from ..state import TaskStatus, utcnow

logger = logging.getLogger(__name__)

VerticalResolver = Callable[[str], IVertical]
StopHook = Callable[["RunEvent"], Awaitable[None]]


class RunEvent(BaseModel):
    type: str  # node | interrupt | done | error | cancelled | state
    run_id: str
    data: dict[str, Any] = Field(default_factory=dict)
    ts: str = Field(default_factory=lambda: utcnow().isoformat())


class EventBus:
    """In-process pub/sub per run with a bounded history for late subscribers."""

    def __init__(self, history: int = 200) -> None:
        self._subs: dict[str, set[asyncio.Queue[RunEvent]]] = defaultdict(set)
        self._history: dict[str, deque[RunEvent]] = defaultdict(lambda: deque(maxlen=history))

    def publish(self, event: RunEvent) -> None:
        self._history[event.run_id].append(event)
        for q in list(self._subs.get(event.run_id, ())):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:  # slow consumer: drop oldest
                q.get_nowait()
                q.put_nowait(event)

    def history(self, run_id: str) -> list[RunEvent]:
        return list(self._history.get(run_id, ()))

    async def subscribe(self, run_id: str, replay: bool = True) -> AsyncIterator[RunEvent]:
        q: asyncio.Queue[RunEvent] = asyncio.Queue(maxsize=1000)
        self._subs[run_id].add(q)
        try:
            if replay:
                for ev in self.history(run_id):
                    yield ev
            while True:
                yield await q.get()
        finally:
            self._subs[run_id].discard(q)

    def subscriber_count(self, run_id: str) -> int:
        return len(self._subs.get(run_id, ()))


class RunConflictError(RuntimeError):
    """Run is executing / has no pending interrupt."""


class InvalidActionError(ValueError):
    pass


class RunNotFoundError(KeyError):
    pass


def jsonable(value: Any) -> Any:
    return to_jsonable_python(value, fallback=str)


def summarize_state(values: Mapping[str, Any]) -> dict[str, Any]:
    tasks = values.get("task_graph") or []
    done = sum(1 for t in tasks if getattr(t, "status", None) in (TaskStatus.COMPLETED, TaskStatus.SKIPPED))
    status = values.get("status")
    usage = values.get("token_usage")
    return {
        "run_id": values.get("run_id"),
        "status": getattr(status, "value", status),
        "vertical_id": values.get("vertical_id"),
        "progress": round(done / len(tasks), 3) if tasks else 0.0,
        "tasks_total": len(tasks),
        "tasks_done": done,
        "current_task_id": values.get("current_task_id"),
        "interrupt_type": getattr(values.get("interrupt_type"), "value", values.get("interrupt_type")),
        "error": values.get("error"),
        "token_usage": jsonable(usage) if usage is not None else None,
        "final_artifact": jsonable(values.get("final_artifact")),
        "created_at": jsonable(values.get("created_at")),
        "updated_at": jsonable(values.get("updated_at")),
    }


class RunManager:
    def __init__(
        self,
        graph: Any,
        resolve_vertical: VerticalResolver,
        settings: Settings | None = None,
        max_concurrent: int | None = None,
        bus: EventBus | None = None,
        **configurable: Any,
    ) -> None:
        self.graph = graph
        self.resolve_vertical = resolve_vertical
        self.settings = settings or get_settings()
        self.bus = bus or EventBus()
        self.configurable = configurable  # llm_client, sandbox, retriever, budget, ...
        self._slots = asyncio.Semaphore(max_concurrent or self.settings.kernel.max_concurrent_runs)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._waiting = 0
        # called once per execution when it stops: done / interrupt / error / cancelled (gateway: quotas, webhooks)
        self.stop_hooks: list[StopHook] = []

    # --- public API -----------------------------------------------------------------------
    async def start(self, request: GenerateRequest, run_id: str | None = None) -> str:
        run_id = run_id or new_run_id()
        vertical = self.resolve_vertical(request.vertical_id or "")
        state = initial_state(request, run_id, self.settings)
        self._spawn(run_id, state, vertical)
        return run_id

    async def resume(self, run_id: str, decision: dict[str, Any]) -> None:
        if self.is_running(run_id):
            raise RunConflictError(f"run {run_id} is executing")
        interrupt = await self.pending_interrupt(run_id)
        if interrupt is None:
            raise RunConflictError(f"run {run_id} has no pending interrupt")
        action = decision.get("action")
        allowed = list(interrupt.get("actions") or [])
        if action not in allowed:
            raise InvalidActionError(f"action {action!r} not allowed for {interrupt.get('interrupt_type')}: {allowed}")
        values = await self.get_state(run_id) or {}
        vertical = self.resolve_vertical(str(values.get("vertical_id") or ""))
        clean = {k: v for k, v in decision.items() if v is not None}
        self._spawn(run_id, Command(resume=clean), vertical)

    def is_running(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        return task is not None and not task.done()

    async def wait(self, run_id: str) -> None:
        """Wait until the current execution of ``run_id`` stops (wrap in ``asyncio.timeout`` to bound it)."""
        task = self._tasks.get(run_id)
        if task is not None:
            await asyncio.shield(task)

    async def get_state(self, run_id: str) -> dict[str, Any] | None:
        snapshot = await self.graph.aget_state({"configurable": {"thread_id": run_id}})
        values = dict(snapshot.values or {}) if snapshot else {}
        return values or None

    async def pending_interrupt(self, run_id: str) -> dict[str, Any] | None:
        snapshot = await self.graph.aget_state({"configurable": {"thread_id": run_id}})
        if not snapshot:
            return None
        for task in snapshot.tasks:
            for intr in task.interrupts:
                return dict(intr.value) if isinstance(intr.value, Mapping) else {"payload": intr.value}
        return None

    async def status(self, run_id: str) -> dict[str, Any]:
        values = await self.get_state(run_id)
        if values is None:
            if self.is_running(run_id):
                return {"run_id": run_id, "status": "queued", "progress": 0.0, "running": True}
            raise RunNotFoundError(run_id)
        summary = summarize_state(values)
        summary["running"] = self.is_running(run_id)
        return summary

    async def logs(self, run_id: str, offset: int = 0, limit: int = 200) -> list[str]:
        values = await self.get_state(run_id)
        if values is None:
            raise RunNotFoundError(run_id)
        return list(values.get("logs") or [])[offset : offset + limit]

    async def cancel(self, run_id: str) -> bool:
        """Stop the current execution (the checkpoint stays; the run can not be resumed past the cancel)."""
        task = self._tasks.get(run_id)
        if task is None or task.done():
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    def active_runs(self) -> list[str]:
        return [rid for rid, t in self._tasks.items() if not t.done()]

    async def shutdown(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    # --- internals ------------------------------------------------------------------------
    def _spawn(self, run_id: str, graph_input: Any, vertical: IVertical) -> None:
        task = asyncio.create_task(self._execute(run_id, graph_input, vertical), name=f"run:{run_id}")
        self._tasks[run_id] = task

    async def _execute(self, run_id: str, graph_input: Any, vertical: IVertical) -> None:
        vertical_id = getattr(getattr(vertical, "manifest", None), "id", "unknown")
        config = run_config(run_id, self.settings, vertical=vertical, **self.configurable)
        self._waiting += 1
        m.QUEUE_DEPTH.set(self._waiting)
        async with self._slots:
            self._waiting -= 1
            m.QUEUE_DEPTH.set(self._waiting)
            m.RUNS_ACTIVE.labels(vertical=vertical_id).inc()
            final: RunEvent | None = None
            try:
                async for chunk in self.graph.astream(graph_input, config, stream_mode="updates"):
                    self._publish_chunk(run_id, chunk)
                interrupt = await self.pending_interrupt(run_id)
                values = await self.get_state(run_id) or {}
                if interrupt is not None:
                    final = RunEvent(type="interrupt", run_id=run_id, data=jsonable(interrupt))
                else:
                    final = RunEvent(type="done", run_id=run_id, data=summarize_state(values))
            except asyncio.CancelledError:
                final = RunEvent(type="cancelled", run_id=run_id)
                raise
            except Exception as exc:  # graph-level failure (checkpointer, recursion limit, ...)
                logger.exception("run %s crashed", run_id)
                final = RunEvent(type="error", run_id=run_id, data={"error": f"{type(exc).__name__}: {exc}"})
            finally:
                m.RUNS_ACTIVE.labels(vertical=vertical_id).dec()
                if final is not None:
                    self.bus.publish(final)
                    for hook in list(self.stop_hooks):
                        try:
                            await hook(final)
                        except Exception:
                            logger.exception("stop hook failed for run %s", run_id)

    def _publish_chunk(self, run_id: str, chunk: Mapping[str, Any]) -> None:
        for node, update in chunk.items():
            if node == "__interrupt__" or not isinstance(update, Mapping):
                continue
            data: dict[str, Any] = {"node": node}
            for key in ("status", "current_task_id", "error", "interrupt_type"):
                if key in update:
                    data[key] = jsonable(update[key])
            if update.get("logs"):
                data["logs"] = list(update["logs"])
            self.bus.publish(RunEvent(type="node", run_id=run_id, data=data))
