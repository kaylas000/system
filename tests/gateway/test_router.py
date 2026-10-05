from __future__ import annotations

from pathlib import Path

import pytest

from kernel.gateway.router import ClassifierOutput, UnknownVerticalError, VerticalRouter, manifest_summary
from kernel.protocols import VerticalManifest
from kernel.skills import VerticalLoader
from tests.kernel.fakes import FakeLLM

ROOT = Path(__file__).resolve().parents[2]


def manifests() -> dict[str, VerticalManifest]:
    return {
        "saas_web": VerticalManifest(
            id="saas_web", name="SaaS", version="1", tech_stack={"framework": "nextjs@14.2", "orm": "prisma@5"}
        ),
        "py_fastapi_microsvc": VerticalManifest(
            id="py_fastapi_microsvc", name="FastAPI", version="1", tech_stack={"framework": "fastapi@0.115"}
        ),
        "iac_terraform": VerticalManifest(
            id="iac_terraform",
            name="Terraform",
            version="1",
            tech_stack={"iac": "terraform@1.9"},
            routing={"keywords": ["ecs fargate"]},
        ),
    }


async def test_explicit_and_unknown_explicit() -> None:
    r = VerticalRouter(manifests())
    d = await r.route("anything", explicit_vertical="iac_terraform")
    assert (d.vertical_id, d.method, d.confidence) == ("iac_terraform", "explicit", 1.0)
    with pytest.raises(UnknownVerticalError):
        await r.route("x", explicit_vertical="nope")


async def test_rules_pick_clear_winner_and_report_candidates() -> None:
    r = VerticalRouter(manifests())
    d = await r.route("A Next.js SaaS dashboard with Stripe billing")
    assert d.vertical_id == "saas_web" and d.method == "rule"
    assert "next.js" in d.reasoning
    d = await r.route(
        "Next.js SaaS dashboard + FastAPI microservice backend in python, Terraform on AWS with ECS Fargate"
    )
    assert set(d.candidates) == {"saas_web", "py_fastapi_microsvc", "iac_terraform"}


async def test_keyword_boundaries() -> None:
    r = VerticalRouter(manifests())
    scores, _ = r.rule_scores("reactivity of a cloudless awsome app")
    assert scores == {}


async def test_tie_goes_to_capabilities_then_llm_then_fallback() -> None:
    r = VerticalRouter(manifests())
    # one keyword each -> tie; hints decide
    d = await r.route("react frontend with a python service", tech_hints={"framework": "fastapi"})
    assert d.vertical_id == "py_fastapi_microsvc" and d.method == "capability"

    llm = FakeLLM(
        [ClassifierOutput(vertical_id="iac_terraform", confidence=0.8, other_verticals=["saas_web", "bogus"])]
    )
    d = await VerticalRouter(manifests(), llm).route("provision everything I need")
    assert d.method == "llm" and d.vertical_id == "iac_terraform" and d.candidates == ["iac_terraform", "saas_web"]
    assert llm.calls[0]["model"] == "router/classifier"

    bad = FakeLLM([ClassifierOutput(vertical_id="invented", confidence=0.9)])
    d = await VerticalRouter(manifests(), bad).route("something vague")
    assert d.method == "fallback" and d.vertical_id == "saas_web"

    failing = FakeLLM([RuntimeError("provider down")])
    d = await VerticalRouter(manifests(), failing, default_vertical="iac_terraform").route("vague")
    assert (d.method, d.vertical_id) == ("fallback", "iac_terraform")


async def test_only_installed_verticals_and_real_manifest() -> None:
    loader = VerticalLoader(ROOT / "verticals")
    r = VerticalRouter(loader.discover)
    assert set(r.verticals) == {"saas_web"}
    d = await r.route("Terraform for AWS")  # iac_terraform is not installed -> no rule match
    assert d.method == "fallback" and d.vertical_id == "saas_web"
    d = await r.route("Simple blog with auth built on tRPC and Prisma")
    assert d.method == "rule" and d.vertical_id == "saas_web"
    info = manifest_summary(r.verticals["saas_web"])
    assert info["id"] == "saas_web" and "framework" in info["tech_stack"] and info["key_skills"]
