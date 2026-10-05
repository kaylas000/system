from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from kernel.config import GatewaySection, HITLSection, Settings, TierLimits
from kernel.gateway.app import create_gateway_app
from kernel.gateway.auth import InMemoryRateLimiter
from kernel.gateway.store import GatewayStore, Principal
from kernel.gateway.webhooks import WebhookSender, WebhookURLError, sign, validate_webhook_url
from kernel.graph import build_graph
from kernel.persistence import memory_checkpointer
from kernel.protocols import VerticalManifest
from kernel.sandbox import LocalSandbox
from kernel.service import RunManager
from tests.kernel.fakes import FakeLLM, FakeVertical, code, plan

OTHER = VerticalManifest(
    id="other", name="Other", version="1.0", tech_stack={"language": "go"}, routing={"keywords": ["grpc"]}
)


class Env:
    def __init__(
        self,
        tmp_path: Path,
        settings: Settings,
        responses: list[Any],
        llm: Any = None,
        plan_review: bool = True,
        **gateway: Any,
    ) -> None:
        cfg: dict[str, Any] = {"auth_enabled": True, "jwt_secret": "x" * 40, "allow_private_webhooks": True}
        cfg.update(gateway)
        self.settings = settings.model_copy(
            update={"hitl": HITLSection(plan_review=plan_review), "gateway": GatewaySection(**cfg)}
        )
        self.llm = llm or FakeLLM(responses)
        vertical = FakeVertical()
        graph = build_graph(None, checkpointer=memory_checkpointer())
        self.manager = RunManager(
            graph, lambda _vid: vertical, self.settings, llm_client=self.llm, sandbox=LocalSandbox(tmp_path / "sb")
        )
        self.store = GatewayStore(":memory:")
        self.limiter = InMemoryRateLimiter()
        manifests = {"fake": vertical.manifest, "other": OTHER}
        self.app = create_gateway_app(
            self.settings,
            run_manager=self.manager,
            manifests=manifests,
            store=self.store,
            limiter=self.limiter,
            llm_client=self.llm,
        )
        self.services = self.app.state.gateway
        self.webhook_calls: list[tuple[str, dict[str, Any]]] = []
        self.services.webhooks.dispatch = lambda url, payload: self.webhook_calls.append((url, payload))

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://gw")

    async def key(self, tenant: str = "acme", role: str = "developer", **kw: Any) -> dict[str, str]:
        key = await self.store.create_api_key(
            Principal(user_id=f"{role}@{tenant}", tenant_id=tenant, roles=[role], **kw)
        )
        return {"X-API-Key": key}


async def _wait(env: Env, run_id: str) -> None:
    await asyncio.wait_for(env.manager.wait(run_id), 10)


async def test_auth_required_and_request_id(tmp_path: Path, settings: Settings) -> None:
    env = Env(tmp_path, settings, [])
    async with env.client() as c:
        assert (await c.get("/health")).status_code == 200
        r = await c.get("/v1/verticals")
        assert r.status_code == 401 and r.headers["x-request-id"]
        assert (await c.post("/v1/generate", json={"prompt": "x"})).status_code == 401
        assert (await c.get("/v1/runs/whatever")).status_code == 401
        assert (await c.get("/runs/whatever")).status_code == 404  # unauthenticated kernel routes are not mounted
        schema = (await c.get("/openapi.json")).json()
        for path in ("/v1/generate", "/v1/runs", "/v1/runs/{run_id}/interrupt", "/v1/compositions", "/v1/verticals"):
            assert path in schema["paths"], path
        assert not any(p.startswith("/runs") for p in schema["paths"])

        h = await env.key(vertical_access=["fake"])
        r = await c.get("/v1/verticals", headers={**h, "X-Request-ID": "req-123"})
        assert r.status_code == 200 and r.headers["x-request-id"] == "req-123"
        assert [v["id"] for v in r.json()] == ["fake"]  # vertical_access filter
        assert (await c.get("/v1/verticals/other", headers=h)).status_code == 404
        assert (await c.get("/v1/verticals/fake", headers=h)).json()["tech_stack"] == {"language": "text"}
        assert (await c.get("/v1/verticals", headers={**h, "X-Tenant-ID": "evil"})).status_code == 403


async def test_generate_hitl_flow_with_tenant_isolation(tmp_path: Path, settings: Settings) -> None:
    env = Env(tmp_path, settings, [plan(("t1", [])), code("a.txt", "one")])
    dev, viewer, stranger = await env.key(), await env.key(role="viewer"), await env.key(tenant="beta", role="admin")
    async with env.client() as c:
        r = await c.post("/v1/generate", json={"prompt": "build it", "vertical_id": "fake"}, headers=dev)
        assert r.status_code == 202, r.text
        body = r.json()
        run_id = body["run_id"]
        assert r.headers["location"] == body["status_url"] == f"http://gw/v1/runs/{run_id}"
        assert body["stream_url"] == f"ws://gw/v1/ws/runs/{run_id}"
        assert body["vertical_id"] == "fake" and body["routing"]["method"] == "explicit"
        await _wait(env, run_id)
        assert await env.limiter.active("acme") == 0  # slot released at the HITL pause

        status = (await c.get(f"/v1/runs/{run_id}", headers=viewer)).json()
        assert status["interrupt_type"] == "plan_review"
        runs = (await c.get("/v1/runs", headers=viewer)).json()
        assert [x["run_id"] for x in runs] == [run_id] and runs[0]["status"] == "needs_human_input"

        # other tenant sees nothing
        for path in ("", "/interrupt", "/logs", "/events"):
            assert (await c.get(f"/v1/runs/{run_id}{path}", headers=stranger)).status_code == 404
        assert (
            await c.post(f"/v1/runs/{run_id}/interrupt", json={"action": "approve"}, headers=stranger)
        ).status_code == 404
        assert (await c.get("/v1/runs", headers=stranger)).json() == []

        # viewer can read but not decide
        assert (await c.get(f"/v1/runs/{run_id}/interrupt", headers=viewer)).status_code == 200
        assert (
            await c.post(f"/v1/runs/{run_id}/interrupt", json={"action": "approve"}, headers=viewer)
        ).status_code == 403
        # invalid action: slot is not leaked
        assert (
            await c.post(f"/v1/runs/{run_id}/interrupt", json={"action": "skip_gate"}, headers=dev)
        ).status_code == 400
        assert await env.limiter.active("acme") == 0

        r = await c.post(f"/v1/runs/{run_id}/interrupt", json={"action": "approve"}, headers=dev)
        assert r.status_code == 202, r.text
        await _wait(env, run_id)
        assert await env.limiter.active("acme") == 0
        status = (await c.get(f"/v1/runs/{run_id}", headers=dev)).json()
        assert status["status"] == "completed"
        assert (await c.delete(f"/v1/runs/{run_id}", headers=dev)).status_code == 409

        logs = (await c.get(f"/v1/runs/{run_id}/logs", params={"limit": 1000}, headers=dev)).json()["logs"]
        assert any("planner" in line for line in logs)

        sse = await c.get(f"/v1/runs/{run_id}/logs", headers={**dev, "Accept": "text/event-stream"})
        assert sse.headers["content-type"].startswith("text/event-stream")
        events = _parse_sse(sse.text)
        assert [e["event"] for e in events].count("log") == len(logs) and events[-1]["event"] == "end"
        assert events[0]["id"] == "0"
        resumed = await c.get(
            f"/v1/runs/{run_id}/logs?stream=true", headers={**dev, "Last-Event-ID": str(len(logs) - 2)}
        )
        assert [e["event"] for e in _parse_sse(resumed.text)] == ["log", "end"]

        ev = _parse_sse((await c.get(f"/v1/runs/{run_id}/events", headers=dev)).text)
        assert ev[0]["event"] == "state" and ev[0]["data"]["status"] == "completed" and len(ev) == 1


def _parse_sse(text: str) -> list[dict[str, Any]]:
    out = []
    for block in text.strip().split("\n\n"):
        item: dict[str, Any] = {}
        for line in block.splitlines():
            if line.startswith(":"):
                continue
            k, _, v = line.partition(": ")
            item[k] = json.loads(v) if k == "data" else v
        if item:
            out.append(item)
    return out


async def test_idempotency_and_routing(tmp_path: Path, settings: Settings) -> None:
    env = Env(tmp_path, settings, [plan(("t1", []))])
    dev = await env.key(vertical_access=["fake"])
    async with env.client() as c:
        req = {"prompt": "build it", "vertical_id": "fake"}
        r1 = await c.post("/v1/generate", json=req, headers={**dev, "Idempotency-Key": "k1"})
        assert r1.status_code == 202
        r2 = await c.post("/v1/generate", json=req, headers={**dev, "Idempotency-Key": "k1"})
        assert r2.status_code == 200 and r2.json()["run_id"] == r1.json()["run_id"]
        assert r2.headers["idempotent-replayed"] == "true"
        r3 = await c.post("/v1/generate", json={**req, "prompt": "else"}, headers={**dev, "Idempotency-Key": "k1"})
        assert r3.status_code == 422
        await _wait(env, r1.json()["run_id"])

        r = await c.post("/v1/generate", json={"prompt": "x", "vertical_id": "nope"}, headers=dev)
        assert r.status_code == 400 and "unknown vertical" in r.text
        # keyword routing picks "other", which this key may not use
        r = await c.post("/v1/generate", json={"prompt": "a grpc service"}, headers=dev)
        assert r.status_code == 403 and "other" in r.text
        assert (await c.post("/v1/generate", json={"prompt": ""}, headers=dev)).status_code == 422
        big = {"prompt": "x", "vertical_id": "fake", "max_budget_usd": 1e6}
        assert (await c.post("/v1/generate", json=big, headers=dev)).status_code == 400
        hook = {"prompt": "x", "vertical_id": "fake", "webhook_url": "ftp://x"}
        assert (await c.post("/v1/generate", json=hook, headers=dev)).status_code == 400
        viewer = await env.key(role="viewer")
        assert (await c.post("/v1/generate", json=req, headers=viewer)).status_code == 403


async def test_quotas(tmp_path: Path, settings: Settings) -> None:
    tiers = {"tiny": TierLimits(rpm=100, rpd=2, concurrent=1)}
    env = Env(tmp_path, settings, [plan(("t1", [])), plan(("t1", []))], tiers=tiers, default_tier="tiny")
    dev = await env.key(tier="tiny")
    req = {"prompt": "build it", "vertical_id": "fake"}
    async with env.client() as c:
        await env.limiter.acquire("acme", "busy", tiers["tiny"])  # the only slot is taken
        r = await c.post("/v1/generate", json=req, headers=dev)
        assert r.status_code == 429 and "concurrent" in r.text and int(r.headers["retry-after"]) > 0
        await env.limiter.release("acme", "busy")

        r = await c.post("/v1/generate", json=req, headers=dev)  # 2nd generation of the day (1st was rejected)
        assert r.status_code == 202
        await _wait(env, r.json()["run_id"])
        r = await c.post("/v1/generate", json=req, headers=dev)
        assert r.status_code == 429 and "rpd" in r.text

    env2 = Env(tmp_path, settings, [], tiers={"t": TierLimits(rpm=2, rpd=9, concurrent=1)}, default_tier="t")
    h = await env2.key(tier="t")
    async with env2.client() as c:
        codes = [(await c.get("/v1/verticals", headers=h)).status_code for _ in range(3)]
        assert codes == [200, 200, 429]


async def test_webhook_on_stop(tmp_path: Path, settings: Settings) -> None:
    env = Env(tmp_path, settings, [plan(("t1", []))])
    dev = await env.key()
    async with env.client() as c:
        req = {"prompt": "x", "vertical_id": "fake", "webhook_url": "http://127.0.0.1:9/hook"}
        run_id = (await c.post("/v1/generate", json=req, headers=dev)).json()["run_id"]
        await _wait(env, run_id)
    assert len(env.webhook_calls) == 1
    url, payload = env.webhook_calls[0]
    assert url == req["webhook_url"] and payload["event"] == "interrupt" and payload["run_id"] == run_id


async def test_webhook_url_validation_and_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    for bad in ("ftp://example.com/x", "http://127.0.0.1/x", "http://10.1.2.3/x", "http://user:pw@example.com/"):
        with pytest.raises(WebhookURLError):
            await validate_webhook_url(bad)
    await validate_webhook_url("http://127.0.0.1/x", allow_private=True)
    await validate_webhook_url("https://8.8.8.8/hook")

    seen: list[httpx.Request] = []
    replies = iter([500, 204])

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(next(replies))

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    settings = Settings(gateway=GatewaySection(allow_private_webhooks=True, webhook_secret="whsec"))
    sender = WebhookSender(settings, backoff=0)
    assert await sender.send("http://127.0.0.1/hook", {"event": "done", "run_id": "r"})
    assert len(seen) == 2
    assert seen[1].headers["x-autogen-signature"] == sign("whsec", seen[1].content)


def test_websocket_auth_and_isolation(tmp_path: Path, settings: Settings) -> None:
    env = Env(tmp_path, settings, [plan(("t1", [])), code("a.txt", "one")])
    with TestClient(env.app) as client:
        dev = client.portal.call(env.key)
        other = client.portal.call(env.key, "beta")
        run_id = client.post("/v1/generate", json={"prompt": "x", "vertical_id": "fake"}, headers=dev).json()["run_id"]
        client.portal.call(env.manager.wait, run_id)
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/v1/ws/runs/{run_id}") as ws:
                ws.receive_json()
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/v1/ws/runs/{run_id}?access_token={other['X-API-Key']}") as ws:
                ws.receive_json()
        with client.websocket_connect(f"/v1/ws/runs/{run_id}?access_token={dev['X-API-Key']}") as ws:
            snap = ws.receive_json()
            assert snap["type"] == "state" and snap["data"]["interrupt_type"] == "plan_review"
            assert (
                client.post(f"/v1/runs/{run_id}/interrupt", json={"action": "approve"}, headers=dev).status_code == 202
            )
            while (ev := ws.receive_json())["type"] not in ("done", "error", "interrupt"):
                pass
            assert ev["type"] == "done"
