"""Knowledge base wired into the kernel graph: planner/coder/fixer prompts get ``metadata.rag_context``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("tree_sitter")
pytest.importorskip("qdrant_client")

from kernel.config import Settings
from kernel.graph import build_graph
from kernel.knowledge.ingestion.embedding import HashingEmbedder
from kernel.knowledge.ingestion.pipeline import IngestionConfig, IngestionPipeline
from kernel.knowledge.retrieval.engine import RetrievalEngine
from kernel.knowledge.storage.graph_store import SqliteGraph
from kernel.knowledge.storage.qdrant_store import QdrantKB
from kernel.persistence import memory_checkpointer
from kernel.protocols import GenerateRequest
from kernel.runner import start_run
from kernel.sandbox import LocalSandbox
from kernel.state import AgentState, RunStatus, Task
from tests.kernel.fakes import FakeLLM, FakeVertical, code, plan


class RagVertical(FakeVertical):
    @staticmethod
    def _rag(state: AgentState) -> str:
        return str((state.get("metadata") or {}).get("rag_context", ""))

    def get_planner_prompt(self, state: AgentState) -> str:
        return "planner\n" + self._rag(state)

    def get_coder_prompt(self, state: AgentState, task: Task) -> str:
        return "coder\n" + self._rag(state)

    def get_fixer_prompt(self, state: AgentState, task: Task) -> str:
        return "fixer\n" + self._rag(state)


class BrokenRetriever:
    async def retrieve_for_planning(self, *a: Any, **kw: Any) -> Any:
        raise ConnectionError("qdrant down")

    retrieve_for_coding = retrieve_for_fixing = retrieve_for_planning


@pytest.fixture
async def engine(sample_repo, tmp_path):
    vector = QdrantKB(location=":memory:", dim=128)
    graph = SqliteGraph(tmp_path / "g.sqlite")
    embedder = HashingEmbedder(128)
    await IngestionPipeline(
        IngestionConfig(source=str(sample_repo), repo_name="sample", enrich=False), vector, embedder, graph
    ).run()
    yield RetrievalEngine(vector, embedder, graph)
    await vector.close()
    await graph.close()


def _script() -> list[Any]:
    return [
        plan(("t1", [])),
        code("src/stream.ts", "BUG"),  # coder -> NoBugGate fails
        code("src/stream.ts", "export const ok = 1"),  # fixer
    ]


async def _run(tmp_path: Path, settings: Settings, retriever: Any) -> tuple[FakeLLM, dict[str, Any]]:
    llm = FakeLLM(_script())
    graph = build_graph(
        RagVertical(),
        llm=llm,
        sandbox=LocalSandbox(tmp_path / "sb"),
        settings=settings,
        retriever=retriever,
        checkpointer=memory_checkpointer(),
    )
    request = GenerateRequest(
        prompt="Stream a server-rendered response with renderToReadableStream", vertical_id="fake"
    )
    _, result = await start_run(graph, request, settings=settings)
    return llm, result


async def test_rag_context_reaches_planner_coder_fixer(tmp_path: Path, settings: Settings, engine) -> None:
    llm, result = await _run(tmp_path, settings, engine)
    assert result["status"] == RunStatus.COMPLETED
    systems = [c["messages"][0].content for c in llm.calls]
    assert [s.split("\n", 1)[0] for s in systems] == ["planner", "coder", "fixer"]
    for s in systems:
        assert "## Relevant knowledge" in s
    assert "streamResponse" in systems[0]
    assert "rag_context" not in (result.get("metadata") or {})  # prompt-only view, not checkpointed
    assert any("rag " in line for line in result["logs"])


async def test_broken_retriever_does_not_fail_run(tmp_path: Path, settings: Settings) -> None:
    llm, result = await _run(tmp_path, settings, BrokenRetriever())
    assert result["status"] == RunStatus.COMPLETED
    assert all("Relevant knowledge" not in c["messages"][0].content for c in llm.calls)


async def test_no_retriever_keeps_prompts_unchanged(tmp_path: Path, settings: Settings) -> None:
    llm, result = await _run(tmp_path, settings, None)
    assert result["status"] == RunStatus.COMPLETED
    assert llm.calls[0]["messages"][0].content == "planner\n"
