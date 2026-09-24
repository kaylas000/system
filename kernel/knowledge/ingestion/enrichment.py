"""
LLM enrichment of symbol chunks (``specs/04_knowledge/ingestion/ENRICHMENT.py``; rewritten by the agent).

Differences from the spec (ISSUES KN-07):
* several chunks per LLM call (``items_per_call``) — ~5x fewer requests for the same tokens;
* goes through ``ILLMClient`` (budget / cost tracking of the kernel) with structured output;
* on failure the chunk is marked ``enriched=False`` instead of being filled with fake
  ``"Unknown"`` values that would pollute filters;
* trivial symbols (short constants / type aliases) are skipped.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from pydantic import BaseModel, Field

from kernel.protocols import ILLMClient, LLMMessage

from ..models import RetrievalChunk
from .chunking import chunk_body, render_symbol_content

logger = logging.getLogger(__name__)

PATTERNS = (
    "Repository, Factory, Builder, Hook, HOC, Component, Middleware, Controller, Route Handler, Service, "
    "Utility, Adapter, Config, Schema, Test, Migration, Script, Other"
)
ENRICHABLE_TYPES = {"function", "method", "class", "component", "hook", "interface", "struct", "enum"}


class EnrichedItem(BaseModel):
    id: str = Field(description="The chunk id exactly as given")
    intent: str = Field(description="One sentence: what does this code do and why")
    pattern: str = Field(description=f"One of: {PATTERNS}")
    complexity: str = Field(default="", description="O(1) | O(n) | DB Query | External API | Heavy Compute | Recursive")
    side_effects: list[str] = Field(default_factory=list, description="DB Write, File IO, Network Request, ...")
    dependencies_explained: list[str] = Field(default_factory=list, description="'Name: purpose'")
    usage_example: str | None = Field(default=None, description="Minimal usage snippet if inferable")
    potential_issues: list[str] = Field(default_factory=list, description="N+1, No timeout, Race condition, ...")
    tags: list[str] = Field(
        default_factory=list, description="lowercase topic tags: auth, database, streaming, ui, ..."
    )


class EnrichmentBatch(BaseModel):
    items: list[EnrichedItem]


SYSTEM_PROMPT = (
    "You are a senior software architect building a code knowledge base. For every code snippet return "
    "semantic metadata. Be concrete and short. Tags are lowercase single words or kebab-case. "
    "Return one item per snippet, copying its id exactly."
)


def _snippet(chunk: RetrievalChunk, max_chars: int) -> str:
    m = chunk.metadata
    return (
        f"### id: {chunk.id}\n"
        f"File: {m.get('file_path')} | Symbol: {m.get('qualified_name') or m.get('symbol_name')} "
        f"({m.get('symbol_type')}) | Repo: {m.get('repo')} | Stack: {m.get('framework_version') or 'n/a'}\n"
        f"```{m.get('language', '')}\n{chunk_body(chunk.content)[:max_chars]}\n```"
    )


class EnrichmentPipeline:
    def __init__(
        self,
        llm: ILLMClient,
        model: str,
        items_per_call: int = 5,
        concurrency: int = 4,
        max_code_chars: int = 2500,
        min_lines: int = 3,
    ) -> None:
        self.llm = llm
        self.model = model
        self.items_per_call = max(1, items_per_call)
        self.semaphore = asyncio.Semaphore(concurrency)
        self.max_code_chars = max_code_chars
        self.min_lines = min_lines
        self.stats = {"enriched": 0, "failed": 0, "skipped": 0, "calls": 0}

    def needs_enrichment(self, chunk: RetrievalChunk) -> bool:
        m = chunk.metadata
        if m.get("chunk_type") != "symbol" or m.get("intent"):
            return False
        lines = int(m.get("end_line", 0)) - int(m.get("start_line", 0)) + 1
        return m.get("symbol_type") in ENRICHABLE_TYPES and lines >= self.min_lines

    async def enrich_chunks(self, chunks: Sequence[RetrievalChunk]) -> list[RetrievalChunk]:
        todo = [c for c in chunks if self.needs_enrichment(c)]
        self.stats["skipped"] += len(chunks) - len(todo)
        batches = [todo[i : i + self.items_per_call] for i in range(0, len(todo), self.items_per_call)]
        await asyncio.gather(*(self._enrich_batch(b) for b in batches))
        return list(chunks)

    async def _enrich_batch(self, batch: list[RetrievalChunk]) -> None:
        prompt = "Analyse these code snippets:\n\n" + "\n\n".join(_snippet(c, self.max_code_chars) for c in batch)
        async with self.semaphore:
            self.stats["calls"] += 1
            try:
                resp = await self.llm.achat(
                    [LLMMessage(role="system", content=SYSTEM_PROMPT), LLMMessage(role="user", content=prompt)],
                    model=self.model,
                    response_model=EnrichmentBatch,
                    temperature=0.0,
                )
                parsed = resp.parsed
                items = parsed.items if isinstance(parsed, EnrichmentBatch) else []
            except Exception as exc:  # enrichment is best-effort; ingestion continues
                logger.warning("enrichment failed for %d chunks: %s", len(batch), exc)
                items = []
        by_id = {i.id: i for i in items}
        if len(batch) == 1 and len(items) == 1 and items[0].id not in {c.id for c in batch}:
            by_id = {batch[0].id: items[0]}  # model mangled the id of a single item
        for chunk in batch:
            item = by_id.get(chunk.id)
            if item is None:
                chunk.metadata["enriched"] = False
                self.stats["failed"] += 1
                continue
            apply_enrichment(chunk, item)
            self.stats["enriched"] += 1


def apply_enrichment(chunk: RetrievalChunk, item: EnrichedItem) -> None:
    data = item.model_dump(exclude={"id"})
    data["tags"] = sorted({t.strip().lower() for t in item.tags if t.strip()})
    data["pattern"] = item.pattern.strip()
    chunk.metadata.update(data)
    chunk.metadata["enriched"] = True
    chunk.content = render_symbol_content(chunk.metadata, chunk_body(chunk.content))
