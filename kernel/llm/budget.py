"""
Budget manager (``specs/06_ops/cost_control/BUDGET_MANAGER.py``; rewritten by the agent).

Complements the per-run post-node check (``check_budget`` in the graph wrapper) with limits that
need shared state and must act *before* an LLM call:

* **per run, pre-flight** — a node doing several LLM calls stops as soon as the run's limit
  (``GenerateRequest.max_budget_usd`` or ``budget.max_cost_usd_per_run``) is reached;
* **per day** — total spend of all runs (``budget.max_cost_usd_per_day``), stored in a
  ``UsageStore`` (in-memory or SQLite, survives restarts);
* **tokens per minute** — sliding window; callers wait (bounded) instead of hitting provider 429s;
* **alerts** — once per scope when ``alert_threshold_pct`` is crossed (log + optional webhook).

``BudgetExceededError`` raised here is turned into a ``BUDGET_EXCEEDED`` interrupt by the graph
wrapper, so the human can raise the limit and resume.

Spec defects fixed (ISSUES O-02): one global ``BudgetState`` shared by all runs (run costs mixed up),
``reset_run`` sets tokens to ``0.0``, ``asyncio.sleep`` while holding the lock (blocks every run),
``TokenUsage`` has no ``cost_usd`` for the ``vertical`` signature used, alerts only ``print``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Protocol

from ..config import Settings
from ..protocols import LLMResponse
from ..state import AgentState, utcnow
from .cost_tracker import BudgetExceededError

logger = logging.getLogger(__name__)


@dataclass
class BudgetConfig:
    max_cost_usd_per_run: float | None = None  # default when the request has no max_budget_usd
    max_tokens_per_run: int | None = None
    max_cost_usd_per_day: float | None = None
    max_tokens_per_minute: int | None = None
    alert_threshold_pct: float = 0.8
    rate_limit_max_wait_s: float = 60.0

    @classmethod
    def from_settings(cls, settings: Settings) -> BudgetConfig:
        b = settings.budget
        return cls(
            max_cost_usd_per_run=b.max_cost_usd_per_run,
            max_tokens_per_run=settings.llm.max_tokens_per_run,
            max_cost_usd_per_day=b.max_cost_usd_per_day,
            max_tokens_per_minute=b.max_tokens_per_minute,
            alert_threshold_pct=b.alert_threshold_pct,
            rate_limit_max_wait_s=b.rate_limit_max_wait_s,
        )


@dataclass
class UsageRecord:
    run_id: str
    vertical: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    ts: float = field(default_factory=time.time)

    @property
    def day(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(self.ts))


class UsageStore(Protocol):
    async def add(self, record: UsageRecord) -> None: ...
    async def day_total(self, day: str) -> tuple[float, int]: ...
    async def records(self, since_day: str, until_day: str) -> list[UsageRecord]: ...


class InMemoryUsageStore:
    def __init__(self) -> None:
        self._records: list[UsageRecord] = []
        self._lock = threading.Lock()

    async def add(self, record: UsageRecord) -> None:
        with self._lock:
            self._records.append(record)

    async def day_total(self, day: str) -> tuple[float, int]:
        with self._lock:
            rows = [r for r in self._records if r.day == day]
        return round(sum(r.cost_usd for r in rows), 6), sum(r.total_tokens for r in rows)

    async def records(self, since_day: str, until_day: str) -> list[UsageRecord]:
        with self._lock:
            return [r for r in self._records if since_day <= r.day <= until_day]


class SqliteUsageStore:
    """Single-node persistent store. For several API replicas use a shared DB (ISSUES O-15)."""

    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS llm_usage (
                    ts REAL NOT NULL, day TEXT NOT NULL, run_id TEXT, vertical TEXT, model TEXT,
                    prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER, cost_usd REAL)"""
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS llm_usage_day ON llm_usage(day)")

    async def _run(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        def locked() -> Any:
            with self._lock:
                return fn(self._conn)

        return await asyncio.to_thread(locked)

    async def add(self, record: UsageRecord) -> None:
        def ins(c: sqlite3.Connection) -> None:
            with c:
                c.execute(
                    "INSERT INTO llm_usage VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        record.ts,
                        record.day,
                        record.run_id,
                        record.vertical,
                        record.model,
                        record.prompt_tokens,
                        record.completion_tokens,
                        record.total_tokens,
                        record.cost_usd,
                    ),
                )

        await self._run(ins)

    async def day_total(self, day: str) -> tuple[float, int]:
        row = await self._run(
            lambda c: c.execute(
                "SELECT COALESCE(SUM(cost_usd),0), COALESCE(SUM(total_tokens),0) FROM llm_usage WHERE day = ?", (day,)
            ).fetchone()
        )
        return round(float(row[0]), 6), int(row[1])

    async def records(self, since_day: str, until_day: str) -> list[UsageRecord]:
        rows = await self._run(
            lambda c: c.execute(
                "SELECT run_id, vertical, model, prompt_tokens, completion_tokens, total_tokens, cost_usd, ts "
                "FROM llm_usage WHERE day BETWEEN ? AND ? ORDER BY ts",
                (since_day, until_day),
            ).fetchall()
        )
        return [
            UsageRecord(
                run_id=r[0],
                vertical=r[1],
                model=r[2],
                prompt_tokens=r[3],
                completion_tokens=r[4],
                total_tokens=r[5],
                cost_usd=r[6],
                ts=r[7],
            )
            for r in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


@dataclass
class BudgetAlert:
    scope: str  # "run" | "day"
    key: str  # run id or date
    spent_usd: float
    limit_usd: float

    @property
    def pct(self) -> float:
        return self.spent_usd / self.limit_usd if self.limit_usd else 0.0

    def text(self) -> str:
        return (
            f"Budget alert: {self.scope} {self.key} at {self.pct:.0%} (${self.spent_usd:.4f} of ${self.limit_usd:.2f})"
        )


AlertSink = Callable[[BudgetAlert], Awaitable[None] | None]


def webhook_alert_sink(url: str, timeout: float = 5.0) -> AlertSink:
    """Slack-compatible webhook (``{"text": ...}``)."""

    async def send(alert: BudgetAlert) -> None:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                await client.post(
                    url, content=json.dumps({"text": alert.text()}), headers={"Content-Type": "application/json"}
                )
        except Exception as exc:  # alerts must never break a run
            logger.warning("budget alert webhook failed: %s", exc)

    return send


@dataclass
class _RunScope:
    run_id: str
    vertical: str
    spent_before: float
    tokens_before: int
    max_cost: float | None
    max_tokens: int | None
    spent_now: float = 0.0
    tokens_now: int = 0


_current: ContextVar[_RunScope | None] = ContextVar("autogen_budget_run", default=None)


class BudgetManager:
    def __init__(
        self,
        config: BudgetConfig | None = None,
        store: UsageStore | None = None,
        alert_sinks: list[AlertSink] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config or BudgetConfig()
        self.store: UsageStore = store or InMemoryUsageStore()
        self.alert_sinks = list(alert_sinks or [])
        self._clock = clock
        self._window: deque[tuple[float, int]] = deque()
        self._window_lock = asyncio.Lock()
        self._alerted: set[tuple[str, str]] = set()

    # --- run scope (set by the graph wrapper around every node) -------------------------
    @contextmanager
    def run_scope(self, run_id: str, state: AgentState | None = None) -> Iterator[_RunScope]:
        st = state or {}
        usage = st.get("token_usage")
        max_cost = st.get("max_budget_usd") or self.config.max_cost_usd_per_run
        max_tokens = (st.get("metadata") or {}).get("max_tokens_per_run") or self.config.max_tokens_per_run
        scope = _RunScope(
            run_id=run_id,
            vertical=str(st.get("vertical_id", "")),
            spent_before=float(getattr(usage, "cost_usd", 0.0) or 0.0),
            tokens_before=int(getattr(usage, "total_tokens", 0) or 0),
            max_cost=float(max_cost) if max_cost else None,
            max_tokens=int(max_tokens) if max_tokens else None,
        )
        token = _current.set(scope)
        try:
            yield scope
        finally:
            _current.reset(token)

    # --- hooks called by MeteredLLM -----------------------------------------------------
    async def before_call(self, model: str, estimated_tokens: int = 0) -> None:
        from ..observability.metrics import BUDGET_EXCEEDED

        scope = _current.get()
        if scope is not None:
            spent = scope.spent_before + scope.spent_now
            if scope.max_cost is not None and spent >= scope.max_cost:
                BUDGET_EXCEEDED.labels(scope="run").inc()
                raise BudgetExceededError(
                    f"Budget exceeded: ${spent:.4f} >= ${scope.max_cost:.4f} (run {scope.run_id})"
                )
            tokens = scope.tokens_before + scope.tokens_now
            if scope.max_tokens is not None and tokens >= scope.max_tokens:
                BUDGET_EXCEEDED.labels(scope="run_tokens").inc()
                raise BudgetExceededError(f"Token limit exceeded: {tokens} >= {scope.max_tokens} (run {scope.run_id})")
        if self.config.max_cost_usd_per_day:
            day_cost, _ = await self.store.day_total(utcnow().date().isoformat())
            if day_cost >= self.config.max_cost_usd_per_day:
                BUDGET_EXCEEDED.labels(scope="day").inc()
                raise BudgetExceededError(
                    f"Daily budget exhausted: ${day_cost:.4f} >= ${self.config.max_cost_usd_per_day:.2f}"
                )
        await self._rate_limit(estimated_tokens)

    async def after_call(self, resp: LLMResponse) -> None:
        scope = _current.get()
        total = int(resp.usage.get("total_tokens", 0) or 0)
        record = UsageRecord(
            run_id=scope.run_id if scope else "",
            vertical=scope.vertical if scope else "",
            model=resp.model,
            prompt_tokens=int(resp.usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(resp.usage.get("completion_tokens", 0) or 0),
            total_tokens=total,
            cost_usd=float(resp.cost_usd or 0.0),
        )
        await self.store.add(record)
        async with self._window_lock:
            self._window.append((self._clock(), total))
        if scope is not None:
            scope.spent_now += record.cost_usd
            scope.tokens_now += total
            if scope.max_cost:
                await self._maybe_alert(
                    BudgetAlert("run", scope.run_id, scope.spent_before + scope.spent_now, scope.max_cost)
                )
        if self.config.max_cost_usd_per_day:
            day = utcnow().date().isoformat()
            day_cost, _ = await self.store.day_total(day)
            await self._maybe_alert(BudgetAlert("day", day, day_cost, self.config.max_cost_usd_per_day))

    # --- internals ------------------------------------------------------------------------
    async def _rate_limit(self, estimated_tokens: int) -> None:
        limit = self.config.max_tokens_per_minute
        if not limit:
            return
        deadline = self._clock() + self.config.rate_limit_max_wait_s
        while True:
            async with self._window_lock:
                now = self._clock()
                while self._window and now - self._window[0][0] >= 60:
                    self._window.popleft()
                used = sum(t for _, t in self._window)
                if used + estimated_tokens <= limit or not self._window:
                    return
                wait = 60 - (now - self._window[0][0])
            if self._clock() + wait > deadline:
                from ..observability.metrics import BUDGET_EXCEEDED

                BUDGET_EXCEEDED.labels(scope="tokens_per_minute").inc()
                raise BudgetExceededError(f"Token rate limit: {used} tokens in the last minute (limit {limit}/min)")
            await asyncio.sleep(max(wait, 0.01))  # lock released while waiting

    async def _maybe_alert(self, alert: BudgetAlert) -> None:
        if alert.pct < self.config.alert_threshold_pct or (alert.scope, alert.key) in self._alerted:
            return
        self._alerted.add((alert.scope, alert.key))
        logger.warning(alert.text())
        for sink in self.alert_sinks:
            try:
                res = sink(alert)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as exc:
                logger.warning("budget alert sink failed: %s", exc)

    async def remaining(self) -> dict[str, float | None]:
        day_cost, _ = await self.store.day_total(utcnow().date().isoformat())
        scope = _current.get()
        run_left = None
        if scope is not None and scope.max_cost is not None:
            run_left = max(0.0, scope.max_cost - scope.spent_before - scope.spent_now)
        day_left = (
            None if not self.config.max_cost_usd_per_day else max(0.0, self.config.max_cost_usd_per_day - day_cost)
        )
        return {"run_cost_remaining": run_left, "daily_cost_remaining": day_left}


def build_budget_manager(settings: Settings) -> BudgetManager:
    b = settings.budget
    store: UsageStore = SqliteUsageStore(b.usage_db_path) if b.usage_db_path else InMemoryUsageStore()
    sinks: list[AlertSink] = [webhook_alert_sink(b.alert_webhook_url)] if b.alert_webhook_url else []
    return BudgetManager(BudgetConfig.from_settings(settings), store, sinks)


def day_range(days: int, until: date | None = None) -> tuple[str, str]:
    end = until or utcnow().date()
    return (end - timedelta(days=max(days, 1) - 1)).isoformat(), end.isoformat()
