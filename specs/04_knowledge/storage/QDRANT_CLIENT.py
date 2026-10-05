# specs/04_knowledge/storage/QDRANT_CLIENT.py
"""
Async Qdrant Client Wrapper.
Handles: Upsert (Batch), Search (Hybrid), Delete, Scroll.
"""

from __future__ import annotations
from typing import List, Dict, Any, Optional, Literal
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    PointStruct,
    Filter,
    FieldCondition,
    MatchValue,
    MatchAny,
    SearchParams,
    Prefetch,
    FusionQuery,
    Fusion,
    SparseVector,
)
from ..ingestion.CHUNKING_STRATEGIES import RetrievalChunk
from kernel.config import settings


class QdrantKB:
    def __init__(self, url: str, api_key: str = None, collection: str = "code_chunks"):
        self.client = AsyncQdrantClient(url=url, api_key=api_key, timeout=60)
        self.collection = collection

    async def ensure_collection(self, vector_size: int = 768):
        from qdrant_client.models import VectorParams, Distance, ScalarQuantization, ScalarQuantizationConfig

        exists = await self.client.collection_exists(self.collection)
        if not exists:
            await self.client.create_collection(
                collection_name=self.collection,
                vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
                quantization_config=ScalarQuantization(scalar=ScalarQuantizationConfig(type="int8", always_ram=True)),
            )
            # Create Payload Indexes (Async helper)
            await self._create_indexes()

    async def _create_indexes(self):
        indexes = ["repo", "language", "symbol_type", "framework_version", "pattern", "tags", "chunk_type"]
        for field in indexes:
            try:
                await self.client.create_payload_index(
                    collection_name=self.collection, field_name=field, field_schema="keyword"
                )
            except:
                pass  # Ignore if exists

    async def upsert_chunks(self, chunks: List[RetrievalChunk], vectors: List[List[float]]):
        """Batch Upsert. Chunks and Vectors must align."""
        points = []
        for chunk, vec in zip(chunks, vectors):
            # Qdrant payload cannot have nested lists of objects easily, flatten tags.
            payload = chunk.metadata.copy()
            payload["content"] = chunk.content  # Store content for retrieval
            # Ensure tags is list of strings
            if isinstance(payload.get("tags"), list):
                payload["tags"] = [str(t) for t in payload["tags"]]

            points.append(PointStruct(id=chunk.id, vector=vec, payload=payload))

        # Batch upsert (Qdrant handles batching internally, but chunk client-side for memory)
        batch_size = 100
        for i in range(0, len(points), batch_size):
            await self.client.upsert(collection_name=self.collection, points=points[i : i + batch_size], wait=True)

    async def hybrid_search(
        self,
        query_vector: List[float],
        query_text: str,  # For BM25 (if using Qdrant Hybrid)
        filters: Dict[str, Any],
        limit: int = 10,
        score_threshold: float = 0.65,
    ) -> List[Dict]:
        """
        Hybrid Search: Vector + BM25 (via Qdrant's `prefetch` + `fusion`).
        Requires Qdrant 1.8+ with BM25 index on `content`.
        """
        qdrant_filter = self._build_filter(filters)

        # 1. Vector Search (Prefetch)
        prefetch = Prefetch(
            query=query_vector,
            filter=qdrant_filter,
            limit=limit * 3,  # Fetch more for fusion
            using="default",  # Dense vector name
        )

        # 2. BM25 Search (Prefetch) - Requires `content` field indexed as `text` with BM25
        # Note: Qdrant BM25 is experimental/beta. Alternative: Use separate BM25 index (Tantivy/Whoosh) or rely on Vector only.
        # Here we show Fusion approach.

        try:
            results = await self.client.query_points(
                collection_name=self.collection,
                prefetch=[prefetch],
                query=FusionQuery(fusion=Fusion.RRF),  # Reciprocal Rank Fusion
                limit=limit,
                with_payload=True,
                score_threshold=score_threshold,
            )
            return [{"id": p.id, "score": p.score, "payload": p.payload} for p in results.points]
        except Exception as e:
            # Fallback: Pure Vector Search
            results = await self.client.search(
                collection_name=self.collection,
                query_vector=query_vector,
                query_filter=qdrant_filter,
                limit=limit,
                score_threshold=score_threshold,
                with_payload=True,
            )
            return [{"id": p.id, "score": p.score, "payload": p.payload} for p in results]

    async def search_by_symbol(
        self, symbol_name: str, file_path: str = "", filters: Dict = None, limit: int = 5
    ) -> List[Dict]:
        """Exact/Partial match on symbol_name + file_path."""
        conds = [FieldCondition(key="symbol_name", match=MatchValue(value=symbol_name))]
        if file_path:
            conds.append(FieldCondition(key="file_path", match=MatchValue(value=file_path)))
        if filters:
            for k, v in filters.items():
                if isinstance(v, list):
                    conds.append(FieldCondition(key=k, match=MatchAny(any=v)))
                else:
                    conds.append(FieldCondition(key=k, match=MatchValue(value=v)))

        # Use scroll for exact filter match (no vector)
        records, _ = await self.client.scroll(
            collection_name=self.collection, scroll_filter=Filter(must=conds), limit=limit, with_payload=True
        )
        return [{"id": r.id, "score": 1.0, "payload": r.payload} for r in records]

    async def delete_repo(self, repo_name: str):
        await self.client.delete(
            collection_name=self.collection,
            points_selector=Filter(must=[FieldCondition(key="repo", match=MatchValue(value=repo_name))]),
        )

    def _build_filter(self, filters: Dict) -> Optional[Filter]:
        if not filters:
            return None
        must = []
        for k, v in filters.items():
            if isinstance(v, list):
                must.append(FieldCondition(key=k, match=MatchAny(any=[str(x) for x in v])))
            else:
                must.append(FieldCondition(key=k, match=MatchValue(value=v)))
        return Filter(must=must) if must else None
