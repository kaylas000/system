"""
Postgres checkpointer + HITL across a service restart. Runs only with a database:

    AUTOGEN_TEST_POSTGRES_DSN=postgresql://postgres:postgres@localhost:5432/test pytest tests/ops -k postgres
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from kernel.config import DatabaseSection, HITLSection, Settings
from kernel.graph import build_graph
from kernel.persistence import open_checkpointer
from kernel.protocols import GenerateRequest
from kernel.sandbox import LocalSandbox
from kernel.service import RunManager
from tests.kernel.fakes import FakeLLM, FakeVertical, code, plan

DSN = os.environ.get("AUTOGEN_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="AUTOGEN_TEST_POSTGRES_DSN not set")


async def test_interrupt_survives_restart(tmp_path: Path, settings: Settings) -> None:
    pytest.importorskip("langgraph.checkpoint.postgres")
    settings = settings.model_copy(
        update={"hitl": HITLSection(plan_review=True), "database": DatabaseSection(postgres_dsn=DSN)}
    )
    llm = FakeLLM([plan(("t1", [])), code("a.txt", "one")])
    vertical = FakeVertical()
    sandbox = LocalSandbox(tmp_path / "sb")

    async with open_checkpointer(settings) as cp:  # "process 1": run until plan_review
        m1 = RunManager(
            build_graph(None, checkpointer=cp), lambda _v: vertical, settings, llm_client=llm, sandbox=sandbox
        )
        run_id = await m1.start(GenerateRequest(prompt="x", vertical_id="fake"))
        await asyncio.wait_for(m1.wait(run_id), 30)
        assert (await m1.pending_interrupt(run_id) or {}).get("interrupt_type") == "plan_review"

    async with open_checkpointer(settings) as cp:  # "process 2": new connection, approve
        m2 = RunManager(
            build_graph(None, checkpointer=cp), lambda _v: vertical, settings, llm_client=llm, sandbox=sandbox
        )
        assert (await m2.status(run_id))["interrupt_type"] == "plan_review"
        await m2.resume(run_id, {"action": "approve"})
        await asyncio.wait_for(m2.wait(run_id), 30)
        assert (await m2.status(run_id))["status"] == "completed"
