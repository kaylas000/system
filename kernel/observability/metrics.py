"""
Prometheus metrics (``specs/06_ops/observability/OTEL_CONFIG.py`` ``METRICS`` + ``METRICS.py`` which
has no content in the spec — ``MISSING_FILES.md``; written by the agent).

Metrics are exposed in Prometheus text format (``/metrics`` of the API, see ``kernel.api``). If
``prometheus_client`` is not installed every instrument is a no-op, so the kernel never depends on it.
Names follow Prometheus conventions (``_total`` counters, ``_seconds`` histograms); the spec's
``autogen.run.duration`` becomes ``autogen_run_duration_seconds``.
"""

from __future__ import annotations

from typing import Any

try:  # optional dependency (extra "ops")
    from prometheus_client import CONTENT_TYPE_LATEST as PROMETHEUS_CONTENT_TYPE
    from prometheus_client import REGISTRY, CollectorRegistry, Counter, Gauge, Histogram, generate_latest

    PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without the extra
    PROMETHEUS_AVAILABLE = False
    PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


class _NoOp:
    def labels(self, *args: Any, **kwargs: Any) -> _NoOp:
        return self

    def inc(self, *args: Any, **kwargs: Any) -> None: ...
    def dec(self, *args: Any, **kwargs: Any) -> None: ...
    def set(self, *args: Any, **kwargs: Any) -> None: ...
    def observe(self, *args: Any, **kwargs: Any) -> None: ...


_LATENCY_BUCKETS = (0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600)
_RUN_BUCKETS = (10, 30, 60, 120, 300, 600, 1200, 1800, 3600, 7200)


def _make(kind: str, name: str, doc: str, labels: tuple[str, ...], **kw: Any) -> Any:
    if not PROMETHEUS_AVAILABLE:
        return _NoOp()
    cls = {"counter": Counter, "gauge": Gauge, "histogram": Histogram}[kind]
    return cls(name, doc, labels, **kw)


RUN_DURATION = _make(
    "histogram",
    "autogen_run_duration_seconds",
    "Run latency from creation to final status",
    ("vertical", "status"),
    buckets=_RUN_BUCKETS,
)
RUNS_TOTAL = _make("counter", "autogen_runs_total", "Finished runs by final status", ("vertical", "status"))
RUNS_ACTIVE = _make("gauge", "autogen_runs_active", "Runs currently executing", ("vertical",))
NODE_DURATION = _make(
    "histogram", "autogen_node_duration_seconds", "Graph node latency", ("node", "vertical"), buckets=_LATENCY_BUCKETS
)
NODE_ERRORS = _make("counter", "autogen_node_errors_total", "Unhandled node exceptions", ("node", "vertical"))
INTERRUPTS = _make("counter", "autogen_interrupts_total", "Human-in-the-loop interrupts", ("type", "vertical"))
LLM_REQUESTS = _make("counter", "autogen_llm_requests_total", "LLM calls", ("model", "status"))
LLM_LATENCY = _make(
    "histogram", "autogen_llm_latency_seconds", "LLM call latency", ("model",), buckets=_LATENCY_BUCKETS
)
LLM_TOKENS = _make("counter", "autogen_llm_tokens_total", "LLM tokens", ("model", "type"))
LLM_COST = _make("counter", "autogen_llm_cost_usd_total", "LLM cost in USD", ("model",))
GATE_RESULTS = _make(
    "counter", "autogen_gate_results_total", "Verification gate results", ("gate", "status", "vertical")
)
SANDBOX_EXEC = _make(
    "histogram",
    "autogen_sandbox_exec_duration_seconds",
    "Sandbox command latency",
    ("provider",),
    buckets=_LATENCY_BUCKETS,
)
BUDGET_EXCEEDED = _make("counter", "autogen_budget_exceeded_total", "Budget limit hits", ("scope",))
QUEUE_DEPTH = _make("gauge", "autogen_queue_depth", "Runs waiting for a free slot", ())


def render_latest(registry: Any = None) -> bytes:
    """Prometheus text exposition of ``registry`` (default: global registry)."""
    if not PROMETHEUS_AVAILABLE:
        return b"# prometheus_client not installed\n"
    return bytes(generate_latest(registry or REGISTRY))


def record_llm_call(model: str, usage: dict[str, int], cost_usd: float, seconds: float, ok: bool = True) -> None:
    model = model or "unknown"
    LLM_REQUESTS.labels(model=model, status="ok" if ok else "error").inc()
    LLM_LATENCY.labels(model=model).observe(seconds)
    if ok:
        LLM_TOKENS.labels(model=model, type="prompt").inc(int(usage.get("prompt_tokens", 0)))
        LLM_TOKENS.labels(model=model, type="completion").inc(int(usage.get("completion_tokens", 0)))
        if cost_usd > 0:
            LLM_COST.labels(model=model).inc(cost_usd)


def record_gate_result(gate_id: str, status: str, vertical: str) -> None:
    GATE_RESULTS.labels(gate=gate_id, status=status, vertical=vertical or "unknown").inc()


__all__ = [
    "BUDGET_EXCEEDED",
    "GATE_RESULTS",
    "INTERRUPTS",
    "LLM_COST",
    "LLM_LATENCY",
    "LLM_REQUESTS",
    "LLM_TOKENS",
    "NODE_DURATION",
    "NODE_ERRORS",
    "PROMETHEUS_AVAILABLE",
    "PROMETHEUS_CONTENT_TYPE",
    "QUEUE_DEPTH",
    "RUNS_ACTIVE",
    "RUNS_TOTAL",
    "RUN_DURATION",
    "SANDBOX_EXEC",
    "CollectorRegistry",
    "record_gate_result",
    "record_llm_call",
    "render_latest",
]
