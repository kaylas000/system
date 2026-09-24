"""
OpenTelemetry tracing (``specs/06_ops/observability/OTEL_CONFIG.py``; rewritten by the agent).

* ``setup_tracing(settings)`` installs a TracerProvider with an OTLP/gRPC exporter when
  ``observability.otel_endpoint`` is set (and the SDK is installed); otherwise spans are no-ops.
* ``span(name, **attrs)`` — context manager used by the graph wrapper (``node.<name>``), the metered
  LLM client (``llm.chat``) and gates (``gate.<id>``).
* ``setup_langsmith(settings)`` turns on LangGraph's native LangSmith tracing (env vars).

Spec defects (ISSUES O-xx): module-level metric creation before ``setup_otel`` binds instruments to
the no-op provider; ``trace_node`` expects ``(state, config)`` but reads the vertical from config;
auto-instrumenting ``requests``/``logging`` required packages that are not dependencies.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from ..config import Settings

logger = logging.getLogger(__name__)


class Attr:
    RUN_ID = "autogen.run_id"
    VERTICAL = "autogen.vertical"
    NODE_NAME = "autogen.node_name"
    TASK_ID = "autogen.task_id"
    SKILL_ID = "autogen.skill_id"
    MODEL_NAME = "autogen.model_name"
    TOKENS = "autogen.tokens"
    COST_USD = "autogen.cost_usd"
    SANDBOX_ID = "autogen.sandbox_id"
    GATE_ID = "autogen.gate_id"
    GATE_STATUS = "autogen.gate_status"


try:
    from opentelemetry import trace as _trace

    OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _trace = None  # type: ignore[assignment]
    OTEL_AVAILABLE = False

_configured = False


def setup_tracing(settings: Settings, service_name: str = "autogen-kernel") -> bool:
    """Configure OTLP export. Returns True if a real exporter was installed."""
    global _configured
    endpoint = settings.observability.otel_endpoint
    if _configured or not endpoint or not OTEL_AVAILABLE:
        return _configured
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        logger.warning("OTEL endpoint configured but SDK/exporter missing: %s", exc)
        return False
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": os.getenv("APP_VERSION", "dev"),
            "deployment.environment": os.getenv("ENVIRONMENT", "development"),
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=endpoint.startswith("http://")))
    )
    _trace.set_tracer_provider(provider)
    _configured = True
    logger.info("OpenTelemetry tracing -> %s", endpoint)
    return True


def setup_langsmith(settings: Settings) -> bool:
    key = settings.observability.langsmith_api_key
    if key is None:
        return False
    os.environ.setdefault("LANGSMITH_TRACING", "true")
    os.environ.setdefault("LANGSMITH_API_KEY", key.get_secret_value())
    os.environ.setdefault("LANGSMITH_PROJECT", settings.observability.langsmith_project)
    return True


def _clean(attrs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in attrs.items() if isinstance(v, str | bool | int | float) and v != ""}


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[Any]:
    """Start a span (no-op without OpenTelemetry). Exceptions are recorded and re-raised."""
    if not OTEL_AVAILABLE:
        yield None
        return
    tracer = _trace.get_tracer("autogen.kernel")
    with tracer.start_as_current_span(name, attributes=_clean(attrs), record_exception=True) as s:
        yield s


def set_span_attributes(s: Any, **attrs: Any) -> None:
    if s is not None:
        s.set_attributes(_clean(attrs))
