"""DoD integration test of specs/04_knowledge (README §6): ingest sample repo -> Qdrant + graph -> retrieve."""

from __future__ import annotations

import re
import shutil
from typing import Any

import pytest

pytest.importorskip("tree_sitter")
pytest.importorskip("qdrant_client")

from kernel.knowledge.ingestion.embedding import HashingEmbedder
from kernel.knowledge.ingestion.enrichment import EnrichedItem, EnrichmentBatch, EnrichmentPipeline
from kernel.knowledge.ingestion.pipeline import IngestionConfig, IngestionPipeline, repo_name_from
from kernel.knowledge.retrieval.engine import RetrievalEngine, error_query, format_context
from kernel.knowledge.retrieval.reranking import LLMReranker
from kernel.knowledge.storage.graph_store import SqliteGraph
from kernel.knowledge.storage.qdrant_store import QdrantKB
from kernel.protocols import LLMResponse


class EnrichLLM:
    """Fake LLM: enriches every snippet in the prompt deterministically."""

    def __init__(self, fail_first: bool = False) -> None:
        self.calls = 0
        self.fail_first = fail_first

    async def achat(self, messages, model, response_model=None, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise RuntimeError("provider down")
        prompt = messages[-1].content
        items = []
        for cid, body in re.findall(r"### id: (\S+)\n(.*?)```\n(?=\n### id:|\Z)", prompt + "\n", flags=re.S):
            streaming = "renderToReadableStream" in body
            items.append(
                EnrichedItem(
                    id=cid,
                    intent="Streams a React tree as an HTTP response"
                    if streaming
                    else f"Implements {cid.split('#')[2]}",
                    pattern="Adapter" if streaming else "Service",
                    tags=["Streaming", "ssr"] if streaming else ["backend"],
                )
            )
        return LLMResponse(content="", model=model, parsed=response_model(items=items) if response_model else None)

    async def astream_chat(self, messages, model, **kwargs):  # pragma: no cover
        yield ""

    def estimate_tokens(self, messages, model) -> int:  # pragma: no cover
        return 0


@pytest.fixture
async def kb(tmp_path):
    vector = QdrantKB(location=":memory:", collection="code_chunks", dim=256)
    graph = SqliteGraph(tmp_path / "g.sqlite")
    yield vector, graph
    await vector.close()
    await graph.close()


async def _ingest(vector, graph, source, llm=None, **cfg):
    embedder = HashingEmbedder(256)
    enricher = EnrichmentPipeline(llm, model="fake/enricher", items_per_call=4) if llm else None
    pipe = IngestionPipeline(
        IngestionConfig(source=str(source), repo_name="sample", **cfg), vector, embedder, graph, enricher
    )
    return await pipe.run(), embedder


async def test_ingest_enrich_retrieve_and_graph(kb, sample_repo):
    vector, graph = kb
    llm = EnrichLLM()
    stats, embedder = await _ingest(vector, graph, sample_repo, llm)
    assert stats["framework_version"] == "nextjs@14.2.35"
    assert stats["vectors"] == stats["chunks_total"] > 30
    assert stats["enrichment"]["enriched"] >= 8 and stats["enrichment"]["failed"] == 0
    assert llm.calls == -(-stats["enrichment"]["enriched"] // 4)
    assert ".gitignore" not in {h.payload["file_path"] for h in await vector.scroll({"repo": "sample"}, limit=500)}

    # Qdrant: enriched payload
    hits = await vector.scroll({"repo": "sample", "symbol_name": "streamResponse"})
    payload = hits[0].payload
    assert payload["intent"].startswith("Streams a React tree")
    assert payload["pattern"] == "Adapter"
    assert payload["tags"] == ["ssr", "streaming"]
    assert "# Intent: Streams a React tree" in payload["content"]
    assert payload["file_hash"]

    # Graph: Symbol/File nodes and CALLS/IMPORTS edges
    gstats = await graph.stats("sample")
    assert gstats["nodes:Symbol"] > 15 and gstats["nodes:File"] == 9
    assert gstats["edges:CALLS"] > 0 and gstats["edges:IMPORTS"] > 0
    callers = {c["name"] for c in await graph.query_callers("getUser")}
    assert callers == {"updateUserName", "GET", "ProfilePage", "find"}

    # Retrieval
    engine = RetrievalEngine(vector, embedder, graph)
    res = await engine.retrieve_for_coding("How to stream response in Next.js?")
    assert "renderToReadableStream" in res.hits[0].content
    assert "streamResponse" in [h.payload["symbol_name"] for h in res.hits[:2]]
    assert (res.hits[0].rerank_score or 0) > (res.hits[-1].rerank_score or 0)

    coding = await engine.retrieve_for_coding("update the user name in getUser", top_k=6)
    ids = coding.ids()
    assert "sample#src/server/users.ts#getUser#13" in ids[:2]
    assert any(h.source == "graph" for h in coding.hits) or len(ids) == 6

    plan = await engine.retrieve_for_planning("database schema and user model", top_k=5)
    assert any(h.payload["chunk_type"] in {"config", "file_summary", "doc"} for h in plan.hits)
    assert all(h.payload.get("symbol_type") not in {"constant", "method"} for h in plan.hits)

    fix = await engine.retrieve_for_fixing(
        "src/app/page.tsx(3,10): error TS2305: Module '\"@/server/users\"' has no exported member 'getUserById'.",
        failed_files=["src/app/page.tsx"],
    )
    assert any("users.ts" in h.payload["file_path"] for h in fix.hits)

    ctx = format_context(res, max_chars=1500)
    assert ctx.startswith("## Relevant knowledge") and "streamResponse" in ctx and len(ctx) <= 1600

    # repo scoping from manifest: unknown collections -> whole KB; known -> filter
    state = {"vertical_manifest": {"rag_collections": ["nextjs_docs_v14"], "tech_stack": {"language": "typescript@5"}}}
    assert (await engine.retrieve_for_coding("stream", state)).hits
    assert await engine.scope_filters(["sample", "missing"]) == {"repo": ["sample"]}


async def test_incremental_reingest_only_changed_files(kb, sample_repo, tmp_path):
    vector, graph = kb
    repo = tmp_path / "repo"
    shutil.copytree(sample_repo, repo)
    first, _ = await _ingest(vector, graph, repo, EnrichLLM())
    assert first["changed_files"] == first["files"]

    again, _ = await _ingest(vector, graph, repo, EnrichLLM())
    assert again["changed_files"] == 0 and again["vectors"] == 0
    assert await vector.count({"repo": "sample"}) == first["chunks_total"]

    users = repo / "src/server/users.ts"
    users.write_text(
        users.read_text().replace("export async function updateUserName", "export async function renameUser")
    )
    (repo / "scripts/seed.py").unlink()
    llm = EnrichLLM()
    third, _ = await _ingest(vector, graph, repo, llm)
    assert third["changed_files"] == 1 and third["removed_files"] == 1
    assert llm.calls >= 1
    names = {h.payload.get("symbol_name") for h in await vector.scroll({"repo": "sample"}, limit=500)}
    assert "renameUser" in names and "updateUserName" not in names and "Seeder" not in names
    assert await vector.count({"repo": "sample"}) == third["chunks_total"]
    assert "renameUser" in {c["name"] for c in await graph.query_callers("getUser")}


async def test_enrichment_failure_is_not_fatal(kb, sample_repo):
    vector, graph = kb
    stats, _ = await _ingest(vector, graph, sample_repo, EnrichLLM(fail_first=True))
    assert stats["enrichment"]["failed"] > 0
    assert stats["vectors"] == stats["chunks_total"]
    failed = [
        h
        for h in await vector.scroll({"repo": "sample", "chunk_type": "symbol"}, limit=500)
        if h.payload.get("enriched") is False
    ]
    assert failed and all(h.payload["intent"] == "" for h in failed)


async def test_llm_reranker_and_fallback(kb, sample_repo):
    vector, graph = kb
    _, embedder = await _ingest(vector, graph, sample_repo, None, enrich=False)
    hits = (await RetrievalEngine(vector, embedder).search("prisma user", top_k=5)).hits

    class ScoreLLM(EnrichLLM):
        async def achat(self, messages, model, response_model=None, **kw):
            scores = [{"index": i, "score": float(i)} for i in range(len(hits))]
            return LLMResponse(content="", model=model, parsed=response_model.model_validate({"scores": scores}))

    ranked = await LLMReranker(ScoreLLM(), "fake").rerank("q", hits, 3)
    assert [h.id for h in ranked] == [hits[-1].id, hits[-2].id, hits[-3].id]
    fallback = await LLMReranker(EnrichLLM(fail_first=True), "fake").rerank("prisma user", hits, 2)
    assert len(fallback) == 2


def test_helpers():
    assert repo_name_from("https://github.com/vercel/next.js.git") == "vercel_next_js"
    assert repo_name_from("git@github.com:trpc/trpc.git") == "trpc_trpc"
    assert repo_name_from("/tmp/my-app/") == "my-app"
    q = error_query("noise\nsrc/a.ts(1,2): error TS2339: Property 'fooBar' does not exist on type 'User'.\nmore noise")
    assert "TS2339" in q and "fooBar" in q and "noise" not in q.splitlines()[0]
    _ = EnrichmentBatch(items=[])
