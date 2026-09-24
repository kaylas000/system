"""Wiring of the knowledge base from ``Settings`` (written by the agent)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kernel.config import Settings, get_settings
from kernel.protocols import ILLMClient

from .ingestion.embedding import IEmbedder, build_embedder
from .ingestion.enrichment import EnrichmentPipeline
from .ingestion.pipeline import IngestionConfig, IngestionPipeline
from .retrieval.engine import RetrievalEngine
from .retrieval.reranking import CrossEncoderReranker, HeuristicReranker, IReranker, LLMReranker
from .storage.graph_store import SqliteGraph
from .storage.qdrant_store import QdrantKB


@dataclass
class KnowledgeBase:
    vector: QdrantKB
    graph: SqliteGraph
    embedder: IEmbedder
    engine: RetrievalEngine
    settings: Settings

    async def close(self) -> None:
        await self.vector.close()
        await self.graph.close()


def build_embedder_from_settings(settings: Settings, kind: str | None = None) -> IEmbedder:
    k = settings.knowledge
    api_base = k.embedding_api_base or settings.llm.gateway_url
    api_key = settings.llm.api_key.get_secret_value() if settings.llm.api_key is not None else None
    return build_embedder(kind or k.embedder, k.embedding_model, k.embedding_dim, api_base, api_key)


def build_reranker(settings: Settings, llm: ILLMClient | None) -> IReranker:
    kind = settings.knowledge.reranker
    if kind == "llm" and llm is not None:
        return LLMReranker(llm, settings.knowledge.reranker_model)
    if kind == "cross_encoder":
        return CrossEncoderReranker()
    return HeuristicReranker()


def build_knowledge_base(
    settings: Settings | None = None, llm: ILLMClient | None = None, embedder_kind: str | None = None
) -> KnowledgeBase:
    s = settings or get_settings()
    v = s.vector_db
    embedder = build_embedder_from_settings(s, embedder_kind)
    vector = QdrantKB(
        url=v.qdrant_url,
        api_key=v.api_key.get_secret_value() if v.api_key is not None else None,
        path=None if v.qdrant_url or v.qdrant_path == ":memory:" else v.qdrant_path,
        location=":memory:" if v.qdrant_path == ":memory:" else None,
        collection=v.collection,
        dim=embedder.dim,
    )
    graph = SqliteGraph(s.knowledge.graph_path)
    engine = RetrievalEngine(vector, embedder, graph, build_reranker(s, llm))
    return KnowledgeBase(vector=vector, graph=graph, embedder=embedder, engine=engine, settings=s)


def build_ingestion(
    kb: KnowledgeBase, config: IngestionConfig, llm: ILLMClient | None = None, **enrich_kwargs: Any
) -> IngestionPipeline:
    enricher = None
    if config.enrich and llm is not None:
        enricher = EnrichmentPipeline(llm, kb.settings.knowledge.enrichment_model, **enrich_kwargs)
    return IngestionPipeline(config, vector=kb.vector, embedder=kb.embedder, graph=kb.graph, enricher=enricher)
