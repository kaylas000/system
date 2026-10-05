"""
Rerankers (``specs/04_knowledge/retrieval/RERANKING.py``; rewritten by the agent).

* ``HeuristicReranker`` — default, no model: fused score + exact-symbol / identifier overlap /
  preferred chunk types / language boosts;
* ``CrossEncoderReranker`` — optional (``sentence-transformers``), loaded lazily;
* ``LLMReranker`` — optional, scores candidates 0-10 with one structured LLM call and falls back
  to the heuristic on any error.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from kernel.protocols import ILLMClient, LLMMessage

from ..ingestion.embedding import tokenize
from ..models import SearchHit

logger = logging.getLogger(__name__)

_IDENT_RE = re.compile(r"`([^`]+)`|\b([A-Za-z_][A-Za-z0-9_]{3,})\b")


@runtime_checkable
class IReranker(Protocol):
    async def rerank(
        self, query: str, hits: Sequence[SearchHit], top_k: int, context: dict[str, Any] | None = None
    ) -> list[SearchHit]: ...


def _identifiers(query: str) -> set[str]:
    out = set()
    for a, b in _IDENT_RE.findall(query):
        tok = (a or b).strip().strip("()")
        if tok:
            out.add(tok.lower())
    return out


class HeuristicReranker:
    """``context``: ``prefer_chunk_types`` (list), ``languages`` (list), ``files`` (list of paths)."""

    async def rerank(
        self, query: str, hits: Sequence[SearchHit], top_k: int, context: dict[str, Any] | None = None
    ) -> list[SearchHit]:
        if not hits:
            return []
        ctx = context or {}
        prefer = list(ctx.get("prefer_chunk_types") or [])
        languages = set(ctx.get("languages") or [])
        files = set(ctx.get("files") or [])
        idents = _identifiers(query)
        q_tokens = set(tokenize(query))
        max_score = max(h.score for h in hits) or 1.0
        rescored = []
        for rank, h in enumerate(hits):
            p = h.payload
            score = 0.6 * (h.score / max_score if max_score > 0 else 0.0) + 0.2 / (1 + rank)
            if h.source == "graph":
                score *= 0.8
            name = str(p.get("symbol_name", "")).lower()
            qualified = str(p.get("qualified_name", "")).lower()
            if name and (name in idents or qualified in idents):
                score += 0.5
            elif name and len(name) > 3 and name in query.lower():
                score += 0.3
            content_tokens = set(tokenize(" ".join(str(p.get(k, "")) for k in ("symbol_name", "file_path", "intent"))))
            content_tokens |= set(p.get("tags") or [])
            if q_tokens:
                score += 0.3 * len(q_tokens & content_tokens) / len(q_tokens)
            if prefer and p.get("chunk_type") in prefer:
                score += 0.15 * (len(prefer) - prefer.index(p.get("chunk_type"))) / len(prefer)
            if languages and p.get("language") in languages:
                score += 0.05
            if files and p.get("file_path") in files:
                score += 0.2
            if p.get("enriched"):
                score += 0.02
            rescored.append(h.model_copy(update={"rerank_score": round(score, 4)}))
        rescored.sort(key=lambda h: h.rerank_score or 0.0, reverse=True)
        return rescored[:top_k]


class CrossEncoderReranker:
    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2") -> None:
        self.model_name = model_name
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            from sentence_transformers import CrossEncoder  # type: ignore[import-not-found]

            self._model = CrossEncoder(self.model_name)
        return self._model

    async def rerank(
        self, query: str, hits: Sequence[SearchHit], top_k: int, context: dict[str, Any] | None = None
    ) -> list[SearchHit]:
        if not hits:
            return []
        model = self._load()
        pairs = [(query, h.content[:2000]) for h in hits]
        scores = await asyncio.to_thread(model.predict, pairs)
        rescored = [h.model_copy(update={"rerank_score": float(s)}) for h, s in zip(hits, scores, strict=True)]
        rescored.sort(key=lambda h: h.rerank_score or 0.0, reverse=True)
        return rescored[:top_k]


class _Score(BaseModel):
    index: int
    score: float = Field(ge=0, le=10)


class _Scores(BaseModel):
    scores: list[_Score]


class LLMReranker:
    def __init__(
        self, llm: ILLMClient, model: str, fallback: IReranker | None = None, max_candidates: int = 20
    ) -> None:
        self.llm = llm
        self.model = model
        self.fallback = fallback or HeuristicReranker()
        self.max_candidates = max_candidates

    async def rerank(
        self, query: str, hits: Sequence[SearchHit], top_k: int, context: dict[str, Any] | None = None
    ) -> list[SearchHit]:
        if not hits:
            return []
        cands = list(hits)[: self.max_candidates]
        listing = "\n\n".join(f"[{i}] {h.payload.get('file_path', '')}\n{h.content[:800]}" for i, h in enumerate(cands))
        prompt = (
            f"Query: {query}\n\nRate how useful each candidate is for answering the query (0-10). "
            f"Return a score for every index.\n\n{listing}"
        )
        try:
            resp = await self.llm.achat(
                [LLMMessage(role="user", content=prompt)], model=self.model, response_model=_Scores, temperature=0.0
            )
            parsed = resp.parsed
            if not isinstance(parsed, _Scores):
                raise ValueError("no structured scores")
            by_index = {s.index: s.score for s in parsed.scores}
        except Exception as exc:
            logger.warning("LLM rerank failed (%s), using heuristic", exc)
            return await self.fallback.rerank(query, hits, top_k, context)
        rescored = [h.model_copy(update={"rerank_score": by_index.get(i, 0.0)}) for i, h in enumerate(cands)]
        rescored.sort(key=lambda h: h.rerank_score or 0.0, reverse=True)
        return rescored[:top_k]
