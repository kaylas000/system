from __future__ import annotations

import asyncio
import io
import re
import tarfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel

from kernel.config import Settings, TierLimits
from kernel.gateway.composer import (
    CompositionPlan,
    ContractFile,
    PlanError,
    SubProjectPlan,
    render_glue,
    topo_levels,
    validate_plan,
)
from kernel.graph.nodes.coder import CodeChanges
from kernel.graph.nodes.planner import PlannerOutput
from kernel.protocols import LLMMessage, LLMResponse
from tests.gateway.test_api import Env
from tests.kernel.fakes import code, plan

OPENAPI = "openapi: 3.1.0\ninfo: {title: billing, version: '1'}\npaths: {}\n"
REFINED = OPENAPI + "# refined by api\n"


def _plan(**over: Any) -> CompositionPlan:
    data: dict[str, Any] = {
        "project_name": "Billing Suite",
        "summary": "Web app with a billing API.",
        "shared_contracts": [{"path": "shared/openapi.yaml", "kind": "openapi", "content": OPENAPI}],
        "sub_projects": [
            {
                "name": "api",
                "vertical_id": "fake",
                "role": "api",
                "prompt": "Billing API",
                "provides": ["shared/openapi.yaml"],
                "port": 8000,
            },
            {
                "name": "web",
                "vertical_id": "other",
                "role": "frontend",
                "prompt": "Web UI",
                "contracts": ["shared/openapi.yaml"],
                "port": 3000,
            },
            {
                "name": "infra",
                "vertical_id": "fake",
                "role": "infra",
                "prompt": "Terraform",
                "depends_on": ["api", "web"],
            },
        ],
        "integration_tests": ["web calls api /health"],
    }
    data.update(over)
    return CompositionPlan.model_validate(data)


class SmartLLM:
    """Answers by response model; sub-project identified from the prompt (planner) or task id (coder)."""

    def __init__(self, composition: CompositionPlan | None = None, fail: set[str] | None = None) -> None:
        self.composition = composition
        self.fail = fail or set()
        self.calls: list[dict[str, Any]] = []

    async def achat(
        self, messages: list[LLMMessage], model: str, response_model: type[BaseModel] | None = None, **kw: Any
    ) -> LLMResponse:
        text = "\n".join(m.content for m in messages)
        self.calls.append({"model": model, "text": text, "response_model": response_model})
        out: BaseModel
        if response_model is CompositionPlan:
            assert self.composition is not None
            out = self.composition
        elif response_model is PlannerOutput:
            name = re.search(r"sub-project `([a-z0-9-]+)`", text)
            assert name, text[:500]
            if name.group(1) in self.fail:
                raise RuntimeError(f"planner exploded for {name.group(1)}")
            out = plan((f"t_{name.group(1).replace('-', '_')}", []))
        elif response_model is CodeChanges:
            name = re.search(r"coder for t_([a-z0-9_]+)", text)
            assert name, text[:500]
            sub = name.group(1)
            out = code(f"{sub}.txt", f"hello from {sub}")
            if sub == "api":
                out.file_changes.extend(code("Dockerfile", "FROM scratch\n").file_changes)
                out.file_changes.extend(code("shared/openapi.yaml", REFINED).file_changes)
        else:
            raise AssertionError(f"unexpected response model {response_model}")
        return LLMResponse(content=out.model_dump_json(), parsed=out, model=model, usage={"total_tokens": 1})

    async def astream_chat(self, messages: list[LLMMessage], model: str, **kw: Any):  # type: ignore[no-untyped-def]
        yield ""

    def estimate_tokens(self, messages: list[LLMMessage], model: str) -> int:
        return 1


def _planner_text(llm: SmartLLM, name: str) -> str:
    return next(c["text"] for c in llm.calls if c["response_model"] is PlannerOutput and f"`{name}`" in c["text"])


# --- unit ----------------------------------------------------------------------------------------
def test_topo_levels_and_validation() -> None:
    p = _plan()
    assert topo_levels(p.sub_projects) == [["api", "web"], ["infra"]]
    installed = {"fake": 1, "other": 1}
    validate_plan(p, installed, lambda v: True)

    cyc = [SubProjectPlan(name="a", vertical_id="fake", prompt="x", depends_on=["b"]),
           SubProjectPlan(name="b", vertical_id="fake", prompt="x", depends_on=["a"])]  # fmt: skip
    with pytest.raises(PlanError, match="cycle"):
        topo_levels(cyc)
    with pytest.raises(PlanError, match="unknown depends_on"):
        topo_levels([SubProjectPlan(name="a", vertical_id="fake", prompt="x", depends_on=["zzz"])])
    with pytest.raises(PlanError, match="not installed"):
        validate_plan(p, {"fake": 1}, lambda v: True)
    with pytest.raises(PlanError, match="no access"):
        validate_plan(p, installed, lambda v: v == "fake")
    dup = _plan(sub_projects=[p.sub_projects[0].model_dump(), p.sub_projects[0].model_dump()])
    with pytest.raises(PlanError, match="duplicate"):
        validate_plan(dup, installed, lambda v: True)
    bad_ref = _plan(sub_projects=[{**p.sub_projects[1].model_dump(), "contracts": ["shared/nope.sql"]}])
    with pytest.raises(PlanError, match="unknown contract"):
        validate_plan(bad_ref, installed, lambda v: True)
    for path in ("../etc/passwd", "/shared/x", "shared", "other/x.yaml"):
        with pytest.raises(ValueError):
            ContractFile(path=path, content="x")
    with pytest.raises(ValueError):
        SubProjectPlan(name="Bad Name", vertical_id="fake", prompt="x")


def test_render_glue() -> None:
    p = _plan()
    state = {
        "composition_id": "cmp_1",
        "sub_projects": {s.name: {"status": "completed"} for s in p.sub_projects},
        "contracts": {"shared/openapi.yaml": OPENAPI},
    }
    files = render_glue(p, state, {"api", "web"})
    compose = yaml.safe_load(files["docker-compose.yml"])
    assert compose["services"]["api"] == {"build": "./api", "ports": ["8000:8000"], "environment": {"PORT": "8000"}}
    assert set(compose["services"]) == {"api", "web"}
    assert "| `infra/` | fake | infra |" in files["README.md"] and "- [ ] web calls api /health" in files["README.md"]
    assert "api --> infra" in files["ARCHITECTURE.md"]
    assert files["Makefile"].startswith(".PHONY: up down logs ps")
    no_docker = render_glue(p, state, set())
    assert "docker-compose.yml" not in no_docker and "help:" in no_docker["Makefile"]


# --- API -----------------------------------------------------------------------------------------
async def test_composition_end_to_end(tmp_path: Path, settings: Settings) -> None:
    llm = SmartLLM(composition=_plan())
    env = Env(tmp_path, settings, [], llm=llm, plan_review=False)
    dev = await env.key()
    composer = env.services.composer
    async with env.client() as c:
        dry = await c.post("/v1/compositions", json={"prompt": "billing suite", "dry_run": True}, headers=dev)
        assert dry.status_code == 200, dry.text
        assert [s["name"] for s in dry.json()["plan"]["sub_projects"]] == ["api", "web", "infra"]
        system = llm.calls[0]["text"]
        assert "- fake:" in system and "- other:" in system and llm.calls[0]["model"] == "router/planner"

        r = await c.post(
            "/v1/compositions", json={"prompt": "billing suite", "plan": _plan().model_dump()}, headers=dev
        )
        assert r.status_code == 202, r.text
        cid = r.json()["composition_id"]
        assert r.headers["location"].endswith(f"/v1/compositions/{cid}")
        assert r.json()["levels"] == [["api", "web"], ["infra"]]
        assert len(llm.calls) == 1  # ready plan: no composer LLM call
        await asyncio.wait_for(composer.wait(cid), 20)

        view = (await c.get(f"/v1/compositions/{cid}", headers=dev)).json()
        assert view["status"] == "completed", view
        assert {n: s["status"] for n, s in view["sub_projects"].items()} == dict.fromkeys(
            ["api", "web", "infra"], "completed"
        )
        children = (await c.get("/v1/runs", params={"parent_id": cid}, headers=dev)).json()
        assert sorted(x["run_id"] for x in children) == sorted(s["run_id"] for s in view["sub_projects"].values())
        assert await env.limiter.active("acme") == 0

        # contracts: web (same level) saw the original, infra (next level) the one refined by api
        assert "shared/openapi.yaml" in _planner_text(llm, "web") and "refined by api" not in _planner_text(llm, "web")
        assert "refined by api" in _planner_text(llm, "infra")

        art = await c.get(f"/v1/compositions/{cid}/artifact", headers=dev)
        assert art.status_code == 200
        with tarfile.open(fileobj=io.BytesIO(art.content), mode="r:gz") as tar:
            names = set(tar.getnames())
            assert {
                "billing-suite/api/api.txt",
                "billing-suite/web/web.txt",
                "billing-suite/infra/infra.txt",
                "billing-suite/README.md",
                "billing-suite/ARCHITECTURE.md",
                "billing-suite/Makefile",
                "billing-suite/docker-compose.yml",
                "billing-suite/shared/openapi.yaml",
            } <= names
            shared = tar.extractfile("billing-suite/shared/openapi.yaml")
            assert shared is not None and shared.read().decode() == REFINED
            compose = tar.extractfile("billing-suite/docker-compose.yml")
            assert compose is not None and list(yaml.safe_load(compose.read())["services"]) == ["api"]

        ev = (await c.get(f"/v1/compositions/{cid}/events", headers=dev)).text
        assert "event: state" in ev and '"status": "completed"' in ev
        child = view["sub_projects"]["api"]["run_id"]
        assert (await c.get(f"/v1/runs/{child}/artifact", headers=dev)).status_code == 200

        stranger = await env.key(tenant="beta", role="admin")
        assert (await c.get(f"/v1/compositions/{cid}", headers=stranger)).status_code == 404
        assert (await c.get(f"/v1/compositions/{cid}/artifact", headers=stranger)).status_code == 404
        assert (await c.delete(f"/v1/compositions/{cid}", headers=dev)).status_code == 409


async def test_composition_rejects_bad_plans(tmp_path: Path, settings: Settings) -> None:
    env = Env(tmp_path, settings, [], llm=SmartLLM(), plan_review=False)
    limited = await env.key(vertical_access=["fake"])
    async with env.client() as c:
        body = {"prompt": "x", "plan": _plan().model_dump()}
        r = await c.post("/v1/compositions", json=body, headers=limited)
        assert r.status_code == 400 and "no access" in r.text
        cyclic = _plan().model_dump()
        cyclic["sub_projects"][0]["depends_on"] = ["infra"]
        r = await c.post("/v1/compositions", json={"prompt": "x", "plan": cyclic}, headers=await env.key())
        assert r.status_code == 400 and "cycle" in r.text
        viewer = await env.key(role="viewer")
        assert (await c.post("/v1/compositions", json=body, headers=viewer)).status_code == 403


async def test_composition_waits_for_human_decisions(tmp_path: Path, settings: Settings) -> None:
    one = _plan(
        sub_projects=[{"name": "api", "vertical_id": "fake", "prompt": "API", "provides": ["shared/openapi.yaml"]}]
    )
    llm = SmartLLM()
    env = Env(tmp_path, settings, [], llm=llm, plan_review=True)
    dev = await env.key()
    composer = env.services.composer
    async with env.client() as c:
        cid = (await c.post("/v1/compositions", json={"prompt": "x", "plan": one.model_dump()}, headers=dev)).json()[
            "composition_id"
        ]
        for _ in range(200):
            view = (await c.get(f"/v1/compositions/{cid}", headers=dev)).json()
            if view["status"] == "waiting_human":
                break
            await asyncio.sleep(0.02)
        assert view["status"] == "waiting_human", view
        assert view["sub_projects"]["api"]["interrupt_type"] == "plan_review"
        child = view["sub_projects"]["api"]["run_id"]
        r = await c.post(f"/v1/runs/{child}/interrupt", json={"action": "approve"}, headers=dev)
        assert r.status_code == 202
        await asyncio.wait_for(composer.wait(cid), 20)
        assert (await c.get(f"/v1/compositions/{cid}", headers=dev)).json()["status"] == "completed"


async def test_composition_fails_when_a_sub_project_fails(tmp_path: Path, settings: Settings) -> None:
    webhooks: list[dict[str, Any]] = []
    env = Env(tmp_path, settings, [], llm=SmartLLM(fail={"web"}), plan_review=False)
    env.services.composer.webhook = lambda url, payload: webhooks.append(payload)
    dev = await env.key()
    async with env.client() as c:
        body = {"prompt": "x", "plan": _plan().model_dump(), "webhook_url": "http://127.0.0.1:1/h"}
        cid = (await c.post("/v1/compositions", json=body, headers=dev)).json()["composition_id"]
        # a node error pauses the sub-project for a human (kernel HITL) -> the human aborts it
        for _ in range(300):
            view = (await c.get(f"/v1/compositions/{cid}", headers=dev)).json()
            if view["sub_projects"]["web"].get("interrupt_type") == "node_error":
                break
            await asyncio.sleep(0.02)
        assert view["status"] == "waiting_human", view
        child = view["sub_projects"]["web"]["run_id"]
        r = await c.post(f"/v1/runs/{child}/interrupt", json={"action": "abort"}, headers=dev)
        assert r.status_code == 202, r.text
        await asyncio.wait_for(env.services.composer.wait(cid), 20)
        view = (await c.get(f"/v1/compositions/{cid}", headers=dev)).json()
        assert view["status"] == "failed" and "web" in view["error"]
        assert view["sub_projects"]["infra"]["status"] == "pending"  # next level never started
        assert (await c.get(f"/v1/compositions/{cid}/artifact", headers=dev)).status_code == 404
    assert webhooks and webhooks[-1]["event"] == "failed" and webhooks[-1]["composition_id"] == cid


async def test_composition_queues_for_slots_and_cancels(tmp_path: Path, settings: Settings) -> None:
    tiers = {"one": TierLimits(rpm=1000, rpd=100, concurrent=1)}
    env = Env(tmp_path, settings, [], llm=SmartLLM(), plan_review=False, tiers=tiers, default_tier="one")
    env.services.composer.slot_poll_seconds = 0.01
    dev = await env.key(tier="one")
    await env.limiter.acquire("acme", "someone-else", tiers["one"])
    async with env.client() as c:
        cid = (
            await c.post("/v1/compositions", json={"prompt": "x", "plan": _plan().model_dump()}, headers=dev)
        ).json()["composition_id"]
        await asyncio.sleep(0.1)
        view = (await c.get(f"/v1/compositions/{cid}", headers=dev)).json()
        assert view["status"] == "running" and view["sub_projects"]["api"]["status"] == "queued"
        assert (await c.delete(f"/v1/compositions/{cid}", headers=dev)).status_code == 200
        assert (await c.get(f"/v1/compositions/{cid}", headers=dev)).json()["status"] == "cancelled"


async def test_recover_marks_orphans_failed(tmp_path: Path, settings: Settings) -> None:
    from kernel.gateway.store import RunRecord

    env = Env(tmp_path, settings, [], llm=SmartLLM(), plan_review=False)
    await env.store.add_run(
        RunRecord(run_id="cmp_old", tenant_id="acme", user_id="u", vertical_id="composition", kind="composition")
    )
    await env.store.save_composition("cmp_old", {"x": 1}, {"status": "running"})
    assert await env.services.composer.recover() == 1
    found = await env.store.get_composition("cmp_old")
    assert found is not None and found[1]["status"] == "failed"
