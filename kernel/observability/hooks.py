"""
Kernel instrumentation points (written by the agent): graph nodes, LLM calls, sandbox commands,
gates. All of them are cheap no-ops when Prometheus / OpenTelemetry are not installed.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from ..protocols import LLMMessage, LLMResponse
from ..state import AgentState, RunStatus, utcnow
from . import metrics as m
from .logging_config import node_var, run_id_var
from .tracing import set_span_attributes, span

FINAL_STATUSES = {RunStatus.COMPLETED, RunStatus.FAILED}


@contextmanager
def observe_node(name: str, run_id: str, vertical: str) -> Iterator[Any]:
    """Span ``node.<name>`` + ``autogen_node_duration_seconds`` + log context (run_id, node)."""
    t_run = run_id_var.set(run_id)
    t_node = node_var.set(name)
    started = time.perf_counter()
    try:
        with span(
            f"node.{name}", **{"autogen.run_id": run_id, "autogen.vertical": vertical, "autogen.node_name": name}
        ) as s:
            yield s
    finally:
        m.NODE_DURATION.labels(node=name, vertical=vertical).observe(time.perf_counter() - started)
        node_var.reset(t_node)
        run_id_var.reset(t_run)


def record_node_error(name: str, vertical: str) -> None:
    m.NODE_ERRORS.labels(node=name, vertical=vertical).inc()


def record_node_result(name: str, state: AgentState, result: dict[str, Any], vertical: str) -> None:
    interrupt = result.get("interrupt_type")
    if interrupt:
        m.INTERRUPTS.labels(type=str(getattr(interrupt, "value", interrupt)), vertical=vertical).inc()
    status = result.get("status")
    if status in FINAL_STATUSES:
        created = state.get("created_at")
        if isinstance(created, datetime):
            m.RUN_DURATION.labels(vertical=vertical, status=str(status.value)).observe(
                max(0.0, (utcnow() - created).total_seconds())
            )
        m.RUNS_TOTAL.labels(vertical=vertical, status=str(status.value)).inc()


class MeteredLLM:
    """``ILLMClient`` wrapper: span ``llm.chat``, latency/token/cost metrics, optional budget manager."""

    def __init__(self, inner: Any, budget: Any | None = None) -> None:
        self.inner = inner
        self.budget = budget  # kernel.llm.budget.BudgetManager (duck-typed: before_call / after_call)

    async def achat(
        self,
        messages: list[LLMMessage],
        model: str,
        response_model: type[BaseModel] | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        if self.budget is not None:
            await self.budget.before_call(model, self.estimate_tokens(messages, model) + (max_tokens or 0))
        started = time.perf_counter()
        with span("llm.chat", **{"autogen.model_name": model, "autogen.run_id": run_id_var.get()}) as s:
            try:
                resp: LLMResponse = await self.inner.achat(
                    messages,
                    model=model,
                    response_model=response_model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    **kwargs,
                )
            except Exception:
                m.record_llm_call(model, {}, 0.0, time.perf_counter() - started, ok=False)
                raise
            set_span_attributes(
                s, **{"autogen.tokens": int(resp.usage.get("total_tokens", 0)), "autogen.cost_usd": resp.cost_usd}
            )
        m.record_llm_call(resp.model or model, resp.usage, resp.cost_usd, time.perf_counter() - started)
        if self.budget is not None:
            await self.budget.after_call(resp)
        return resp

    def astream_chat(self, messages: list[LLMMessage], model: str, **kwargs: Any) -> AsyncIterator[str]:
        result: AsyncIterator[str] = self.inner.astream_chat(messages, model, **kwargs)
        return result

    def estimate_tokens(self, messages: list[LLMMessage], model: str) -> int:
        try:
            return int(self.inner.estimate_tokens(messages, model))
        except Exception:
            return sum(len(msg.content) for msg in messages) // 4

    def __getattr__(self, item: str) -> Any:  # settings, cost trackers, ... of the wrapped client
        return getattr(self.inner, item)


class MeteredSandbox:
    """``ISandbox`` proxy timing ``exec`` (``autogen_sandbox_exec_duration_seconds``)."""

    def __init__(self, inner: Any, provider: str = "") -> None:
        self.inner = inner
        self.provider = provider or type(inner).__name__

    async def exec(self, sandbox_id: str, command: str, *args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return await self.inner.exec(sandbox_id, command, *args, **kwargs)
        finally:
            m.SANDBOX_EXEC.labels(provider=self.provider).observe(time.perf_counter() - started)

    def __getattr__(self, item: str) -> Any:
        return getattr(self.inner, item)
