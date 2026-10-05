from __future__ import annotations

from pathlib import Path

import pytest

from kernel.config import Settings
from kernel.graph import build_graph
from kernel.llm import BudgetExceededError
from kernel.llm.budget import BudgetConfig, BudgetManager, InMemoryUsageStore, SqliteUsageStore, UsageRecord
from kernel.llm.cost_report import aggregate, build_report, to_markdown
from kernel.observability import MeteredLLM
from kernel.persistence import memory_checkpointer
from kernel.protocols import GenerateRequest, LLMMessage
from kernel.runner import start_run
from kernel.sandbox import LocalSandbox
from kernel.state import InterruptType, RunStatus, TokenUsage
from tests.kernel.fakes import FakeLLM, FakeVertical, code, plan

MSG = [LLMMessage(role="user", content="hi")]


def _llm(n: int) -> FakeLLM:
    return FakeLLM([plan(("t1", []))] * n)  # every call costs $0.001, 15 tokens


async def test_run_limit_one_cent_raises_budget_exceeded() -> None:
    """DoD: a run with a $0.01 limit -> BudgetExceededError."""
    manager = BudgetManager(BudgetConfig())
    llm = MeteredLLM(_llm(20), manager)
    with manager.run_scope("run_1", {"max_budget_usd": 0.01}):
        with pytest.raises(BudgetExceededError, match="Budget exceeded"):
            for _ in range(20):
                await llm.achat(MSG, model="m")
    assert len(llm.inner.calls) == 10  # stopped exactly at $0.010


async def test_run_scope_counts_spend_already_in_state() -> None:
    manager = BudgetManager()
    state = {"max_budget_usd": 0.5, "token_usage": TokenUsage(cost_usd=0.5, total_tokens=10)}
    with manager.run_scope("r", state):
        with pytest.raises(BudgetExceededError):
            await MeteredLLM(_llm(1), manager).achat(MSG, model="m")


async def test_daily_limit_across_runs_and_alerts(tmp_path: Path) -> None:
    alerts = []
    store = SqliteUsageStore(tmp_path / "usage.sqlite")
    manager = BudgetManager(BudgetConfig(max_cost_usd_per_day=0.003, alert_threshold_pct=0.5), store, [alerts.append])
    llm = MeteredLLM(_llm(10), manager)
    for run in ("a", "b", "c"):
        with manager.run_scope(run, {"vertical_id": "saas_web"}):
            await llm.achat(MSG, model="m")
    with manager.run_scope("d", {}), pytest.raises(BudgetExceededError, match="Daily budget"):
        await llm.achat(MSG, model="m")
    assert [a.scope for a in alerts] == ["day"]  # once, at >= 50%
    # persisted: a new manager on the same DB sees today's spend
    again = BudgetManager(BudgetConfig(max_cost_usd_per_day=0.003), SqliteUsageStore(tmp_path / "usage.sqlite"))
    with pytest.raises(BudgetExceededError):
        await again.before_call("m")
    assert (await again.remaining())["daily_cost_remaining"] == 0.0


async def test_tokens_per_minute_waits_then_fails_when_wait_too_long() -> None:
    now = [0.0]
    manager = BudgetManager(BudgetConfig(max_tokens_per_minute=20, rate_limit_max_wait_s=0.0), clock=lambda: now[0])
    llm = MeteredLLM(_llm(5), manager)
    await llm.achat(MSG, model="m")  # 15 tokens in window
    with pytest.raises(BudgetExceededError, match="rate limit"):
        await manager.before_call("m", estimated_tokens=10)
    now[0] = 61.0  # window expired
    await manager.before_call("m", estimated_tokens=10)


async def test_graph_turns_preflight_budget_error_into_interrupt(tmp_path: Path, settings: Settings) -> None:
    # limit is reached after the coder -> BUDGET_EXCEEDED interrupt; usage is recorded per run
    llm = FakeLLM([plan(("t1", [])), code("a.txt", "BUG"), code("a.txt", "fixed")])
    manager = BudgetManager()
    graph = build_graph(
        FakeVertical(),
        llm=llm,
        sandbox=LocalSandbox(tmp_path / "sb"),
        settings=settings,
        budget=manager,
        checkpointer=memory_checkpointer(),
    )
    _, result = await start_run(graph, GenerateRequest(prompt="x", max_budget_usd=0.0015), settings=settings)
    assert result["interrupt_type"] == InterruptType.BUDGET_EXCEEDED
    assert result["status"] == RunStatus.NEEDS_HUMAN_INPUT
    records = await manager.store.records("0000-00-00", "9999-99-99")
    assert {r.run_id for r in records} == {result["run_id"]}


async def test_cost_report() -> None:
    store = InMemoryUsageStore()
    for run, model, cost in (("r1", "gpt", 0.01), ("r1", "claude", 0.02), ("r2", "gpt", 0.005)):
        await store.add(UsageRecord(run, "saas_web", model, 10, 5, 15, cost))
    report = await build_report(store, days=1)
    assert report["total"]["cost_usd"] == 0.035 and report["total"]["calls"] == 3
    assert list(report["models"]) == ["claude", "gpt"]
    assert list(report["top_runs"]) == ["r1", "r2"]
    md = to_markdown(report)
    assert "**Total:** $0.0350" in md and "| claude | 1 | 15 | 0.0200 |" in md
    assert aggregate([])["total"]["calls"] == 0
