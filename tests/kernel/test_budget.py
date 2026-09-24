"""Budget enforcement (BUDGET_EXCEEDED interrupt) and CostTracker."""

from __future__ import annotations

from pathlib import Path

import pytest

from kernel.config import Settings
from kernel.llm import BudgetExceededError, CostTracker, check_budget
from kernel.protocols import GenerateRequest
from kernel.runner import start_run
from kernel.state import RunStatus, TokenUsage

from .fakes import code, plan
from .test_e2e_fake import Harness


def test_check_budget() -> None:
    u = TokenUsage(total_tokens=100, cost_usd=0.5)
    assert check_budget(u, None) is None
    assert check_budget(u, 1.0) is None
    assert "Budget exceeded" in (check_budget(u, 0.4) or "")
    assert "Token limit" in (check_budget(u, None, 50) or "")
    assert check_budget(None, 0.1) is None


def test_cost_tracker() -> None:
    ct = CostTracker()
    ct.record("r1", TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15, cost_usd=0.2, model_name="m"))
    ct.record("r1", TokenUsage(total_tokens=5, cost_usd=0.2, model_name="m"))
    assert ct.run_total("r1")["calls"] == 2
    assert ct.run_total("r1")["cost_usd"] == pytest.approx(0.4)
    assert ct.run_total("missing")["calls"] == 0
    assert ct.report()["models"]["m"]["total_tokens"] == 20
    ct.ensure_within("r1", 1.0)
    with pytest.raises(BudgetExceededError):
        ct.ensure_within("r1", 0.3)


async def test_budget_exceeded_then_raise_limit(tmp_path: Path, settings: Settings) -> None:
    # FakeLLM costs 0.001 per call; budget allows exactly 2 calls.
    h = Harness(tmp_path, settings, [plan(("t1", []), ("t2", ["t1"])), code("a.txt", "a"), code("b.txt", "b")])
    req = GenerateRequest(prompt="Build a demo", vertical_id="fake", max_budget_usd=0.0015)
    h.run_id, result = await start_run(h.graph, req, settings=settings)

    assert result["status"] == RunStatus.NEEDS_HUMAN_INPUT
    intr = await h.interrupt()
    assert intr["interrupt_type"] == "budget_exceeded"
    assert intr["payload"]["failed_node"] == "coder"
    assert intr["actions"] == ["approve", "abort"]

    # approve without a new limit is rejected -> still waiting
    result = await h.resume({"action": "approve"})
    assert (await h.interrupt())["interrupt_type"] == "budget_exceeded"

    result = await h.resume({"action": "approve", "edited_data": {"max_budget_usd": 1.0}})
    assert result["status"] == RunStatus.COMPLETED, result.get("error")
    assert result["max_budget_usd"] == 1.0
    assert result["token_usage"].cost_usd == pytest.approx(0.003)
    assert [c["model"] for c in h.llm.calls] == ["router/planner", "router/coder", "router/coder"]


async def test_token_limit_override(tmp_path: Path, settings: Settings) -> None:
    settings = settings.model_copy(update={"llm": settings.llm.model_copy(update={"max_tokens_per_run": 20})})
    h = Harness(tmp_path, settings, [plan(("t1", [])), code("a.txt", "a")])
    await h.start()
    intr = await h.interrupt()
    assert intr["interrupt_type"] == "budget_exceeded" and intr["payload"]["failed_node"] == "coder"
    result = await h.resume({"action": "approve", "edited_data": {"max_tokens_per_run": 1000}})
    assert result["status"] == RunStatus.COMPLETED, result.get("error")
