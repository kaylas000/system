from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pytest

from kernel.config import Settings
from kernel.graph import build_graph
from kernel.observability import configure_logging, render_latest, run_id_var
from kernel.persistence import memory_checkpointer
from kernel.protocols import GenerateRequest
from kernel.runner import start_run
from kernel.sandbox import LocalSandbox
from kernel.state import RunStatus
from tests.kernel.fakes import FakeLLM, FakeVertical, code, plan

prometheus_client = pytest.importorskip("prometheus_client")


def _sample(name: str, **labels: str) -> float:
    value = prometheus_client.REGISTRY.get_sample_value(name, labels)
    return float(value or 0.0)


async def _run(tmp_path: Path, settings: Settings) -> dict:
    llm = FakeLLM([plan(("t1", [])), code("a.txt", "BUG"), code("a.txt", "fixed")])
    graph = build_graph(
        FakeVertical(),
        llm=llm,
        sandbox=LocalSandbox(tmp_path / "sb"),
        settings=settings,
        checkpointer=memory_checkpointer(),
    )
    _, result = await start_run(graph, GenerateRequest(prompt="x", vertical_id="fake"), settings=settings)
    return result


async def test_metrics_after_a_run(tmp_path: Path, settings: Settings) -> None:
    before_runs = _sample("autogen_run_duration_seconds_count", vertical="fake", status="completed")
    before_fail = _sample("autogen_gate_results_total", gate="no_bug", status="failed", vertical="fake")
    result = await _run(tmp_path, settings)
    assert result["status"] == RunStatus.COMPLETED

    assert _sample("autogen_run_duration_seconds_count", vertical="fake", status="completed") == before_runs + 1
    assert _sample("autogen_runs_total", vertical="fake", status="completed") >= 1
    assert _sample("autogen_node_duration_seconds_count", node="planner", vertical="fake") >= 1
    assert _sample("autogen_llm_requests_total", model="router/planner", status="ok") >= 1
    assert _sample("autogen_llm_tokens_total", model="router/coder", type="prompt") >= 10
    assert _sample("autogen_llm_cost_usd_total", model="router/coder") > 0
    assert _sample("autogen_gate_results_total", gate="no_bug", status="failed", vertical="fake") == before_fail + 1
    assert _sample("autogen_sandbox_exec_duration_seconds_count", provider="LocalSandbox") >= 0

    text = render_latest().decode()
    assert "autogen_run_duration_seconds_bucket" in text
    assert "# TYPE autogen_llm_tokens_total counter" in text


async def test_spans_for_nodes_llm_and_gates(tmp_path: Path, settings: Settings) -> None:
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    original = trace.get_tracer
    trace.get_tracer = lambda *a, **k: tracer  # type: ignore[assignment]
    try:
        result = await _run(tmp_path, settings)
    finally:
        trace.get_tracer = original  # type: ignore[assignment]
    names = [s.name for s in exporter.get_finished_spans()]
    for expected in (
        "node.initialize",
        "node.planner",
        "node.coder",
        "node.verifier",
        "node.fixer",
        "node.packager",
        "llm.chat",
        "gate.no_bug",
    ):
        assert expected in names, (expected, names)
    planner = next(s for s in exporter.get_finished_spans() if s.name == "node.planner")
    assert planner.attributes["autogen.run_id"] == result["run_id"]
    assert planner.attributes["autogen.vertical"] == "fake"
    llm_span = next(s for s in exporter.get_finished_spans() if s.name == "llm.chat")
    assert llm_span.attributes["autogen.tokens"] == 15
    assert llm_span.parent is not None  # nested under a node span


def test_json_logging_carries_run_id() -> None:
    buf = io.StringIO()
    handler = configure_logging("INFO", json_logs=True, stream=buf)
    try:
        token = run_id_var.set("run_abc")
        logging.getLogger("autogen.test").info("hello", extra={"task_id": "t1"})
        run_id_var.reset(token)
        line = json.loads(buf.getvalue().strip().splitlines()[-1])
        assert line["msg"] == "hello" and line["run_id"] == "run_abc" and line["task_id"] == "t1"
        assert line["level"] == "INFO"
    finally:
        logging.getLogger().removeHandler(handler)
