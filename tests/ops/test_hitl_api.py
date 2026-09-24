from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
import httpx
from fastapi.testclient import TestClient

from kernel.api import create_app
from kernel.config import HITLSection, Settings
from kernel.graph import build_graph
from kernel.persistence import memory_checkpointer
from kernel.protocols import GenerateRequest
from kernel.sandbox import LocalSandbox
from kernel.service import RunManager
from tests.kernel.fakes import FakeLLM, FakeVertical, code, plan


def _manager(tmp_path: Path, settings: Settings, responses: list[Any]) -> tuple[RunManager, FakeLLM]:
    settings = settings.model_copy(update={"hitl": HITLSection(plan_review=True)})
    llm = FakeLLM(responses)
    vertical = FakeVertical()
    graph = build_graph(None, checkpointer=memory_checkpointer())
    manager = RunManager(graph, lambda _vid: vertical, settings, llm_client=llm, sandbox=LocalSandbox(tmp_path / "sb"))
    return manager, llm


async def _wait_status(client: httpx.AsyncClient, run_id: str, wanted: str) -> dict[str, Any]:
    for _ in range(200):
        body = (await client.get(f"/runs/{run_id}")).json()
        if body.get("status") == wanted and not body.get("running"):
            return body
        await asyncio.sleep(0.02)
    raise AssertionError(f"status {wanted} not reached: {body}")


async def test_plan_review_get_then_approve(tmp_path: Path, settings: Settings) -> None:
    """DoD: plan_review interrupt -> GET payload -> POST approve -> run completes."""
    manager, _ = _manager(tmp_path, settings, [plan(("t1", [])), code("a.txt", "one")])
    app = create_app(manager, settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        run_id = await manager.start(GenerateRequest(prompt="build it", vertical_id="fake"))
        await asyncio.wait_for(manager.wait(run_id), 10)
        status = await _wait_status(client, run_id, "needs_human_input")
        assert status["interrupt_type"] == "plan_review" and status["tasks_total"] == 1

        r = await client.get(f"/runs/{run_id}/interrupt")
        assert r.status_code == 200
        payload = r.json()
        assert payload["interrupt_type"] == "plan_review"
        assert "approve" in payload["actions"]
        assert [t["id"] for t in payload["payload"]["task_graph"]] == ["t1"]

        # action not offered for this interrupt -> 400; unknown action -> 422
        assert (await client.post(f"/runs/{run_id}/interrupt", json={"action": "skip_gate"})).status_code == 400
        assert (await client.post(f"/runs/{run_id}/interrupt", json={"action": "yolo"})).status_code == 422

        r = await client.post(f"/runs/{run_id}/interrupt", json={"action": "approve", "comment": "lgtm"})
        assert r.status_code == 202, r.text
        # second decision while it is running (or after it finished) -> 409
        assert (await client.post(f"/runs/{run_id}/approve", json={"action": "approve"})).status_code == 409
        await asyncio.wait_for(manager.wait(run_id), 10)

        done = await _wait_status(client, run_id, "completed")
        assert done["progress"] == 1.0 and done["token_usage"]["total_tokens"] > 0
        assert (await client.get(f"/runs/{run_id}/interrupt")).status_code == 404
        logs = (await client.get(f"/runs/{run_id}/logs", params={"limit": 1000})).json()["logs"]
        assert any("planner" in line for line in logs)

        assert (await client.get("/runs/nope")).status_code == 404
        assert (await client.get("/health")).json()["status"] == "ok"
        metrics = await client.get("/metrics")
        assert metrics.status_code == 200 and "autogen_runs_total" in metrics.text


async def test_event_bus_streams_node_updates(tmp_path: Path, settings: Settings) -> None:
    manager, _ = _manager(tmp_path, settings, [plan(("t1", [])), code("a.txt", "one")])
    run_id = await manager.start(GenerateRequest(prompt="x", vertical_id="fake"), run_id="run_bus")
    await asyncio.wait_for(manager.wait(run_id), 10)
    events = manager.bus.history(run_id)
    assert [e.data.get("node") for e in events if e.type == "node"][:2] == ["initialize", "planner"]
    assert events[-1].type == "interrupt" and events[-1].data["interrupt_type"] == "plan_review"
    await manager.resume(run_id, {"action": "approve"})
    await asyncio.wait_for(manager.wait(run_id), 10)
    assert manager.bus.history(run_id)[-1].type == "done"


def test_websocket_snapshot_and_live_events(tmp_path: Path, settings: Settings) -> None:
    manager, _ = _manager(tmp_path, settings, [plan(("t1", [])), code("a.txt", "one")])
    app = create_app(manager, settings)
    with TestClient(app) as client:
        run_id = client.portal.call(manager.start, GenerateRequest(prompt="x", vertical_id="fake"))
        client.portal.call(manager.wait, run_id)
        with client.websocket_connect(f"/ws/runs/{run_id}") as ws:
            snap = ws.receive_json()
            assert snap["type"] == "state" and snap["data"]["interrupt_type"] == "plan_review"
            ws.send_text("ping")
            assert ws.receive_text() == "pong"
            assert client.post(f"/runs/{run_id}/interrupt", json={"action": "approve"}).status_code == 202
            seen = []
            while True:
                ev = ws.receive_json()
                seen.append(ev)
                if ev["type"] in ("done", "error", "interrupt"):
                    break
            assert seen[-1]["type"] == "done" and seen[-1]["data"]["status"] == "completed"
            assert {"coder", "verifier", "packager"} <= {e["data"].get("node") for e in seen}
