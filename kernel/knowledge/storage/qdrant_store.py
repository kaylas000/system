"""
Qdrant vector store (``specs/04_knowledge/storage/QDRANT_CLIENT.py`` + ``VECTOR_DB_SCHEMA.md``;
rewritten by the agent) with hybrid search (``MISSING_FILES.md`` #8 ``HYBRID_SEARCH.py``).

* one collection (``code_chunks``) with a named dense vector ``dense`` (Cosine, HNSW m=16 /
  ef_construct=100, int8 scalar quantisation) and a sparse vector ``bm25`` (``Modifier.IDF``);
* hybrid search = server-side ``query_points`` with two prefetches (dense + sparse) fused by RRF;
* point ids are ``uuid5(chunk_id)`` — Qdrant accepts only UUIDs / unsigned ints, the spec used raw
  strings (ISSUES KN-06); the readable id is kept in ``payload.chunk_id``;
* ``location=":memory:"`` or ``path=...`` runs Qdrant in-process (tests, local dev), ``url=...``
  talks to a server (production).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Any

from ..ingestion.embedding import sparse_doc_vector, sparse_query_vector
from ..models import RetrievalChunk, SearchHit

logger = logging.getLogger(__name__)

DENSE = "dense"
SPARSE = "bm25"
KEYWORD_INDEXES = (
    "repo",
    "language",
    "symbol_type",
    "framework_version",
    "pattern",
    "tags",
    "file_path",
    "path_prefixes",
    "symbol_name",
    "chunk_type",
)


def point_id(chunk_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, chunk_id))


def path_prefixes(path: str) -> list[str]:
    """``src/app/page.tsx`` -> ``["src", "src/app"]`` (keyword index => cheap prefix filters)."""
    parts = PurePosixPath(path).parts[:-1]
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


class QdrantKB:
    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        path: str | None = None,
        location: str | None = None,
        collection: str = "code_chunks",
        dim: int = 768,
        upsert_batch: int = 128,
    ) -> None:
        from qdrant_client import AsyncQdrantClient

        if url:
            self.client = AsyncQdrantClient(url=url, api_key=api_key)
            self.local = False
        elif path:
            self.client = AsyncQdrantClient(path=path)
            self.local = True
        else:
            self.client = AsyncQdrantClient(location=location or ":memory:")
            self.local = True
        self.collection = collection
        self.dim = dim
        self.upsert_batch = upsert_batch
        self._ready = False

    async def ensure_collection(self) -> None:
        if self._ready:
            return
        from qdrant_client import models as m

        if not await self.client.collection_exists(self.collection):
            await self.client.create_collection(
                collection_name=self.collection,
                vectors_config={
                    DENSE: m.VectorParams(
                        size=self.dim,
                        distance=m.Distance.COSINE,
                        hnsw_config=m.HnswConfigDiff(m=16, ef_construct=100, full_scan_threshold=10_000),
                        quantization_config=m.ScalarQuantization(
                            scalar=m.ScalarQuantizationConfig(type=m.ScalarType.INT8, always_ram=True)
                        ),
                    )
                },
                sparse_vectors_config={SPARSE: m.SparseVectorParams(modifier=m.Modifier.IDF)},
            )
            if not self.local:  # local mode ignores payload indexes (and warns)
                for field in KEYWORD_INDEXES:
                    await self.client.create_payload_index(self.collection, field, m.PayloadSchemaType.KEYWORD)
        else:
            info = await self.client.get_collection(self.collection)
            vectors = info.config.params.vectors
            size = vectors[DENSE].size if isinstance(vectors, dict) and DENSE in vectors else None
            if size is not None and size != self.dim:
                raise ValueError(
                    f"collection {self.collection!r} has dense size {size}, embedder produces {self.dim}; "
                    "use another collection or reindex"
                )
        self._ready = True

    # --- write -------------------------------------------------------------------
    async def upsert(self, chunks: Sequence[RetrievalChunk], vectors: Sequence[list[float]]) -> int:
        from qdrant_client import models as m

        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors length mismatch")
        await self.ensure_collection()
        points = []
        for chunk, vec in zip(chunks, vectors, strict=True):
            idx, vals = sparse_doc_vector(chunk.content)
            payload = dict(chunk.metadata)
            payload["chunk_id"] = chunk.id
            payload["content"] = chunk.content
            if payload.get("file_path"):
                payload["path_prefixes"] = path_prefixes(str(payload["file_path"]))
            points.append(
                m.PointStruct(
                    id=point_id(chunk.id),
                    vector={DENSE: list(vec), SPARSE: m.SparseVector(indices=idx, values=vals)},
                    payload=payload,
                )
            )
        for i in range(0, len(points), self.upsert_batch):
            await self.client.upsert(self.collection, points=points[i : i + self.upsert_batch], wait=True)
        return len(points)

    async def delete(self, repo: str, file_paths: Sequence[str] | None = None) -> None:
        """Delete all points of ``repo`` (or only of the given files)."""
        from qdrant_client import models as m

        if file_paths is not None and not file_paths:
            return
        await self.ensure_collection()
        filt = self.build_filter({"repo": repo, **({"file_path": list(file_paths)} if file_paths else {})})
        await self.client.delete(self.collection, points_selector=m.FilterSelector(filter=filt), wait=True)

    # --- read --------------------------------------------------------------------
    @staticmethod
    def build_filter(filters: dict[str, Any] | None) -> Any:
        """``{"repo": "x", "language": ["ts", "tsx"], "path_prefix": "src/app", "exclude": {...}}`` -> ``Filter``."""
        from qdrant_client import models as m

        if not filters:
            return None
        must: list[Any] = []
        must_not: list[Any] = []

        def cond(key: str, value: Any) -> Any:
            if key == "path_prefix":
                key = "path_prefixes"
            if isinstance(value, list | tuple | set):
                return m.FieldCondition(key=key, match=m.MatchAny(any=list(value)))
            return m.FieldCondition(key=key, match=m.MatchValue(value=value))

        for key, value in filters.items():
            if value is None or value == [] or value == "":
                continue
            if key == "exclude":
                must_not.extend(cond(k, v) for k, v in value.items() if v not in (None, [], ""))
            else:
                must.append(cond(key, value))
        if not must and not must_not:
            return None
        return m.Filter(must=must or None, must_not=must_not or None)

    @staticmethod
    def _hit(point: Any, source: str = "vector") -> SearchHit:
        payload = dict(point.payload or {})
        return SearchHit(
            id=str(payload.get("chunk_id", point.id)),
            score=float(getattr(point, "score", 0.0) or 0.0),
            payload=payload,
            source=source,
        )

    async def hybrid_search(
        self,
        query_vector: list[float] | None,
        query_text: str,
        filters: dict[str, Any] | None = None,
        limit: int = 10,
        prefetch_limit: int = 50,
    ) -> list[SearchHit]:
        """Dense + BM25 fused with RRF. Either side may be absent (``query_vector=None`` => sparse only)."""
        from qdrant_client import models as m

        await self.ensure_collection()
        filt = self.build_filter(filters)
        prefetch = []
        if query_vector is not None:
            prefetch.append(m.Prefetch(query=query_vector, using=DENSE, limit=prefetch_limit, filter=filt))
        idx, vals = sparse_query_vector(query_text)
        if idx:
            prefetch.append(
                m.Prefetch(
                    query=m.SparseVector(indices=idx, values=vals), using=SPARSE, limit=prefetch_limit, filter=filt
                )
            )
        if not prefetch:
            return []
        if len(prefetch) == 1:
            p = prefetch[0]
            res = await self.client.query_points(
                self.collection, query=p.query, using=p.using, query_filter=filt, limit=limit, with_payload=True
            )
        else:
            res = await self.client.query_points(
                self.collection,
                prefetch=prefetch,
                query=m.FusionQuery(fusion=m.Fusion.RRF),
                query_filter=filt,
                limit=limit,
                with_payload=True,
            )
        return [self._hit(p) for p in res.points]

    async def scroll(
        self, filters: dict[str, Any] | None = None, limit: int = 100, fields: list[str] | None = None
    ) -> list[SearchHit]:
        await self.ensure_collection()
        out: list[SearchHit] = []
        offset = None
        while len(out) < limit:
            points, offset = await self.client.scroll(
                self.collection,
                scroll_filter=self.build_filter(filters),
                limit=min(256, limit - len(out)),
                offset=offset,
                with_payload=fields if fields is not None else True,
            )
            out.extend(self._hit(p, source="symbol") for p in points)
            if offset is None:
                break
        return out

    async def get(self, chunk_ids: Sequence[str]) -> list[SearchHit]:
        await self.ensure_collection()
        if not chunk_ids:
            return []
        points = await self.client.retrieve(self.collection, ids=[point_id(c) for c in chunk_ids], with_payload=True)
        hits = [self._hit(p, source="graph") for p in points]
        order = {c: i for i, c in enumerate(chunk_ids)}
        return sorted(hits, key=lambda h: order.get(h.id, len(order)))

    async def count(self, filters: dict[str, Any] | None = None) -> int:
        await self.ensure_collection()
        res = await self.client.count(self.collection, count_filter=self.build_filter(filters), exact=True)
        return int(res.count)

    async def file_hashes(self, repo: str) -> dict[str, str]:
        """``{file_path: content_hash}`` of indexed files (for incremental ingestion)."""
        hits = await self.scroll({"repo": repo}, limit=10_000_000, fields=["file_path", "file_hash", "chunk_id"])
        return {
            str(h.payload["file_path"]): str(h.payload.get("file_hash", "")) for h in hits if h.payload.get("file_path")
        }

    async def close(self) -> None:
        await self.client.close()
