"""
Retrieval engine (``specs/04_knowledge/retrieval/RETRIEVAL_STRATEGIES.py``; rewritten by the agent).

Strategies:
* ``coding``   — hybrid search over symbols/docs/configs + graph expansion (callees/callers of
  the top symbol hits) + rerank;
* ``planning`` — file summaries, configs, docs and high-level symbols (classes, interfaces,
  components, route handlers);
* ``fixing``   — error text (identifiers, TS/ESLint codes) against ``error_fix`` chunks first,
  then code/docs mentioning the failing identifiers.

Repository scoping: the vertical manifest's ``rag_collections`` are treated as repo names in the
knowledge base. Only repos that are actually indexed are used as a filter; when none are indexed
the search runs over the whole base (ISSUES KN-09: the spec filtered by
``tech_stack.language == "typescript@5"`` which never matches payload ``language: "typescript"``).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, Field

from ..ingestion.embedding import IEmbedder
from ..models import SearchHit
from ..storage.graph_store import IGraphStore
from ..storage.qdrant_store import QdrantKB
from .reranking import HeuristicReranker, IReranker

logger = logging.getLogger(__name__)

_LANGUAGE_FAMILIES = {
    "typescript": ["typescript", "tsx", "javascript"],
    "javascript": ["javascript", "tsx", "typescript"],
    "python": ["python"],
    "go": ["go"],
}
_ERROR_CODE_RE = re.compile(r"\b(TS\d{4}|E\d{3,4}|[a-z-]+/[a-z-]+)\b")
_QUOTED_RE = re.compile(r"['\"`]([A-Za-z_$][\w$.]{2,})['\"`]")


class RetrievalResult(BaseModel):
    strategy: str
    query: str
    hits: list[SearchHit] = Field(default_factory=list)

    def ids(self) -> list[str]:
        return [h.id for h in self.hits]


def languages_from_manifest(manifest: Mapping[str, Any] | None) -> list[str]:
    lang = str(((manifest or {}).get("tech_stack") or {}).get("language", "")).split("@")[0].strip().lower()
    return _LANGUAGE_FAMILIES.get(lang, [lang] if lang else [])


class RetrievalEngine:
    def __init__(
        self,
        vector: QdrantKB,
        embedder: IEmbedder,
        graph: IGraphStore | None = None,
        reranker: IReranker | None = None,
        graph_expansion: int = 4,
    ) -> None:
        self.vector = vector
        self.embedder = embedder
        self.graph = graph
        self.reranker = reranker or HeuristicReranker()
        self.graph_expansion = graph_expansion
        self._indexed_repos: set[str] | None = None

    # --- scoping -----------------------------------------------------------------
    async def indexed_repos(self, refresh: bool = False) -> set[str]:
        if self._indexed_repos is None or refresh:
            hits = await self.vector.scroll({"chunk_type": "file_summary"}, limit=1_000_000, fields=["repo"])
            self._indexed_repos = {str(h.payload.get("repo")) for h in hits if h.payload.get("repo")}
        return self._indexed_repos

    async def scope_filters(self, repos: Sequence[str] | None) -> dict[str, Any]:
        if not repos:
            return {}
        available = await self.indexed_repos()
        wanted = [r for r in repos if r in available]
        return {"repo": wanted} if wanted else {}

    def _state_scope(self, state: Mapping[str, Any] | None) -> tuple[list[str], list[str]]:
        manifest = (state or {}).get("vertical_manifest") or {}
        repos = [str(r) for r in (manifest.get("rag_collections") or [])]
        return repos, languages_from_manifest(manifest)

    # --- core --------------------------------------------------------------------
    async def search(
        self,
        query: str,
        filters: dict[str, Any] | None = None,
        top_k: int = 8,
        prefetch: int = 40,
        expand_graph: bool = False,
        rerank_context: dict[str, Any] | None = None,
        strategy: str = "search",
    ) -> RetrievalResult:
        query = query.strip()
        if not query:
            return RetrievalResult(strategy=strategy, query=query)
        vec = (await self.embedder.embed([query]))[0]
        hits = await self.vector.hybrid_search(
            vec, query, filters=filters, limit=max(top_k * 3, 10), prefetch_limit=prefetch
        )
        if expand_graph and self.graph is not None and hits:
            hits = hits + await self._graph_expand(hits, filters)
        hits = _dedupe(hits)
        ranked = await self.reranker.rerank(query, hits, top_k, rerank_context)
        return RetrievalResult(strategy=strategy, query=query, hits=ranked)

    async def _graph_expand(self, hits: list[SearchHit], filters: dict[str, Any] | None) -> list[SearchHit]:
        assert self.graph is not None
        seeds = [h for h in hits[:3] if h.payload.get("chunk_type") == "symbol"]
        if not seeds:
            return []
        neighbours = await self.graph.neighbors([h.id for h in seeds], limit=self.graph_expansion * len(seeds))
        ids = [n["id"] for n in neighbours if n.get("label") == "Symbol"]
        extra = await self.vector.get(ids)
        allowed_repos = set((filters or {}).get("repo") or [])
        base = min(h.score for h in seeds)
        out = []
        for h in extra:
            if allowed_repos and h.payload.get("repo") not in allowed_repos:
                continue
            out.append(h.model_copy(update={"score": base * 0.5, "source": "graph"}))
        return out

    # --- strategies --------------------------------------------------------------
    async def retrieve_for_coding(
        self,
        query: str,
        state: Mapping[str, Any] | None = None,
        current_files: Sequence[str] = (),
        top_k: int = 8,
    ) -> RetrievalResult:
        repos, languages = self._state_scope(state)
        filters = await self.scope_filters(repos)
        filters["chunk_type"] = ["symbol", "doc", "config", "pattern"]
        return await self.search(
            query,
            filters,
            top_k=top_k,
            expand_graph=True,
            strategy="coding",
            rerank_context={
                "prefer_chunk_types": ["symbol", "pattern", "doc", "config"],
                "languages": languages,
                "files": list(current_files),
            },
        )

    async def retrieve_for_planning(
        self, query: str, state: Mapping[str, Any] | None = None, top_k: int = 10
    ) -> RetrievalResult:
        repos, languages = self._state_scope(state)
        filters = await self.scope_filters(repos)
        filters["chunk_type"] = ["file_summary", "config", "doc", "symbol", "pattern"]
        filters["exclude"] = {"symbol_type": ["constant", "variable", "method", "type_alias"]}
        return await self.search(
            query,
            filters,
            top_k=top_k,
            strategy="planning",
            rerank_context={
                "prefer_chunk_types": ["doc", "file_summary", "pattern", "config", "symbol"],
                "languages": languages,
            },
        )

    async def retrieve_for_fixing(
        self,
        error_log: str,
        state: Mapping[str, Any] | None = None,
        failed_files: Sequence[str] = (),
        top_k: int = 6,
    ) -> RetrievalResult:
        repos, languages = self._state_scope(state)
        query = error_query(error_log)
        scope = await self.scope_filters(repos)
        fixes = await self.search(query, {"chunk_type": "error_fix"}, top_k=max(2, top_k // 2), strategy="fixing")
        code = await self.search(
            query,
            {**scope, "chunk_type": ["symbol", "doc", "config", "pattern"]},
            top_k=top_k,
            strategy="fixing",
            rerank_context={
                "prefer_chunk_types": ["symbol", "doc"],
                "languages": languages,
                "files": list(failed_files),
            },
        )
        merged = _dedupe(fixes.hits + code.hits)[:top_k]
        return RetrievalResult(strategy="fixing", query=query, hits=merged)

    # --- graph passthrough ---------------------------------------------------------
    async def callers(self, name: str, repo: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        return await self.graph.query_callers(name, repo=repo, limit=limit) if self.graph is not None else []


def error_query(error_log: str, max_chars: int = 1200) -> str:
    """Condense a gate log into a search query: error lines, codes, quoted identifiers."""
    lines = [ln.strip() for ln in error_log.splitlines() if ln.strip()]
    important = [
        ln for ln in lines if re.search(r"error|Error|failed|Cannot|not assignable|does not exist|undefined", ln)
    ]
    picked = (important or lines)[:15]
    extras = sorted(set(_ERROR_CODE_RE.findall(error_log)) | set(_QUOTED_RE.findall(error_log)))
    text = "\n".join(picked)
    if extras:
        text += "\n" + " ".join(extras[:30])
    return text[:max_chars]


def _dedupe(hits: Sequence[SearchHit]) -> list[SearchHit]:
    seen: set[str] = set()
    out = []
    for h in hits:
        if h.id not in seen:
            seen.add(h.id)
            out.append(h)
    return out


def format_context(result: RetrievalResult, max_chars: int = 6000, title: str = "Relevant knowledge") -> str:
    """Markdown block for prompts (``metadata.rag_context``)."""
    if not result.hits:
        return ""
    parts = [f"## {title}"]
    used = len(parts[0])
    for h in result.hits:
        p = h.payload
        loc = str(p.get("file_path", ""))
        if p.get("start_line") and p.get("end_line"):
            loc += f":{p['start_line']}-{p['end_line']}"
        name = p.get("qualified_name") or p.get("symbol_name", "")
        head = f"### {loc} — {name} ({p.get('symbol_type') or p.get('chunk_type')})"
        if p.get("repo"):
            head += f" [{p['repo']}]"
        body = h.content
        block = (
            f"{head}\n```{p.get('language', '')}\n{body}\n```" if p.get("chunk_type") != "doc" else f"{head}\n{body}"
        )
        if used + len(block) > max_chars:
            room = max_chars - used - len(head) - 20
            if room < 200:
                break
            block = f"{head}\n```\n{body[:room]}\n```"
        parts.append(block)
        used += len(block) + 2
    return "\n\n".join(parts)
