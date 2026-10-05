# specs/06_ops/observability/OTEL_CONFIG.py
"""
OpenTelemetry Instrumentation for Kernel.
Traces: LangGraph execution, LLM calls, Tool calls, Sandbox exec.
Metrics: Latency, Token Usage, Cost, Error Rates, Queue Depths.
Logs: Structured JSON -> Loki/Elastic.
"""

from __future__ import annotations
import os
from typing import Optional
from opentelemetry import trace, metrics, baggage
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.propagators.tracecontext import TraceContextTextMapPropagator
from opentelemetry.propagators.baggage import BaggagePropagator
from opentelemetry.semconv.resource import ResourceAttributes
from opentelemetry.sdk.resources import Resource


# --- Custom Attributes (Semantic Conventions) ---
class Attr:
    RUN_ID = "autogen.run_id"
    THREAD_ID = "autogen.thread_id"
    VERTICAL = "autogen.vertical"
    NODE_NAME = "autogen.node_name"
    TASK_ID = "autogen.task_id"
    SKILL_ID = "autogen.skill_id"
    MODEL_NAME = "autogen.model_name"
    TOKEN_USAGE = "autogen.token_usage"
    COST_USD = "autogen.cost_usd"
    SANDBOX_ID = "autogen.sandbox_id"
    GATE_ID = "autogen.gate_id"
    GATE_STATUS = "autogen.gate_status"


def setup_otel(service_name: str = "autogen-kernel", endpoint: str = None):
    """Initialize OpenTelemetry SDK."""
    endpoint = endpoint or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

    resource = Resource.create(
        {
            ResourceAttributes.SERVICE_NAME: service_name,
            ResourceAttributes.SERVICE_VERSION: os.getenv("APP_VERSION", "dev"),
            ResourceAttributes.DEPLOYMENT_ENVIRONMENT: os.getenv("ENVIRONMENT", "development"),
        }
    )

    # 1. Tracer Provider
    trace_provider = TracerProvider(resource=resource)
    trace_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True)))
    # trace_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter())) # Debug
    trace.set_tracer_provider(trace_provider)

    # 2. Meter Provider
    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=endpoint, insecure=True), export_interval_millis=10000
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    # 3. Propagators (W3C TraceContext + Baggage)
    set_global_textmap(CompositePropagator([TraceContextTextMapPropagator(), BaggagePropagator()]))

    # 4. Auto-Instrumentation
    LoggingInstrumentor().instrument(set_logging_format=True)  # JSON Logs
    HTTPXClientInstrumentor().instrument()
    RequestsInstrumentor().instrument()

    return trace.get_tracer(__name__), metrics.get_meter(__name__)


# --- Metric Instruments (Created once) ---
meter = metrics.get_meter("autogen.kernel")

METRICS = {
    "run_duration": meter.create_histogram("autogen.run.duration", unit="s", description="Total run latency"),
    "node_duration": meter.create_histogram("autogen.node.duration", unit="s", description="Node execution latency"),
    "llm_tokens": meter.create_counter("autogen.llm.tokens", unit="1", description="Tokens used (prompt/completion)"),
    "llm_cost": meter.create_counter("autogen.llm.cost", unit="USD", description="Cost in USD"),
    "llm_errors": meter.create_counter("autogen.llm.errors", unit="1", description="LLM API errors"),
    "tool_calls": meter.create_counter("autogen.tool.calls", unit="1", description="Tool invocations"),
    "tool_errors": meter.create_counter("autogen.tool.errors", unit="1", description="Tool execution errors"),
    "sandbox_exec_duration": meter.create_histogram("autogen.sandbox.exec.duration", unit="s"),
    "gate_status": meter.create_counter("autogen.gate.status", unit="1", description="Gate Pass/Fail/Error"),
    "active_runs": meter.create_up_down_counter("autogen.runs.active", unit="1", description="Concurrent active runs"),
    "queue_depth": meter.create_up_down_counter("autogen.queue.depth", unit="1", description="Pending runs in queue"),
}


# --- Helper Decorators for Nodes ---
def trace_node(node_name: str):
    """Decorator to trace a LangGraph node execution."""

    def decorator(func):
        async def wrapper(state, config, *args, **kwargs):
            tracer = trace.get_tracer(__name__)
            run_id = config.get("configurable", {}).get("thread_id", "unknown")
            vertical = (
                config.get("configurable", {}).get("vertical", {}).manifest.id
                if config.get("configurable", {}).get("vertical")
                else "unknown"
            )

            with tracer.start_as_current_span(
                f"node.{node_name}",
                attributes={Attr.RUN_ID: run_id, Attr.VERTICAL: vertical, Attr.NODE_NAME: node_name},
            ) as span:
                # Inject Baggage for downstream correlation
                ctx = baggage.set_baggage("run_id", run_id)
                token = baggage.attach(ctx)
                try:
                    METRICS["active_runs"].add(1, {Attr.VERTICAL: vertical})
                    result = await func(state, config, *args, **kwargs)
                    span.set_status(trace.StatusCode.OK)
                    return result
                except Exception as e:
                    span.record_exception(e)
                    span.set_status(trace.StatusCode.ERROR, str(e))
                    raise
                finally:
                    baggage.detach(token)
                    METRICS["active_runs"].add(-1, {Attr.VERTICAL: vertical})

        return wrapper

    return decorator


def record_llm_usage(model: str, usage: dict, cost: float, vertical: str):
    attrs = {Attr.MODEL_NAME: model, Attr.VERTICAL: vertical}
    METRICS["llm_tokens"].add(usage.get("prompt_tokens", 0), {**attrs, "type": "prompt"})
    METRICS["llm_tokens"].add(usage.get("completion_tokens", 0), {**attrs, "type": "completion"})
    METRICS["llm_cost"].add(cost, attrs)


def record_gate_result(gate_id: str, status: str, vertical: str):
    METRICS["gate_status"].add(1, {Attr.GATE_ID: gate_id, Attr.GATE_STATUS: status, Attr.VERTICAL: vertical})
