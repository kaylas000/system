# specs/04_knowledge/ingestion/ENRICHMENT.py
"""
LLM-based Enrichment of Code Chunks.
Runs as a BATCH job (async, concurrent) after parsing.
Adds semantic metadata: Intent, Pattern, Complexity, Issues.
"""

from __future__ import annotations
import asyncio
import json
from typing: List, Dict, Any
from pydantic import BaseModel, Field
from .CHUNKING_STRATEGIES import RetrievalChunk
from kernel.llm.client import LiteLLMClient # Our LLM Client
from kernel.config import settings

# --- Enrichment Schema ---

class EnrichedMetadata(BaseModel):
    intent: str = Field(..., description="One sentence: What does this code do?")
    pattern: str = Field(..., description="Design Pattern: Repository, Factory, Hook, HOC, Middleware, Controller, Service, Utility, Config, Test, Migration, Script, Other")
    complexity: str = Field(..., description="O(1), O(n), DB Query, External API, Heavy Compute, Recursive")
    side_effects: List[str] = Field(default_factory=list, description="DB Write, Cache Invalidation, Email Send, File IO, Network Request, State Mutation, None")
    dependencies_explained: List[str] = Field(default_factory=list, description="Human readable: 'UserRepository: DB Access', 'Redis: Cache'")
    usage_example: Optional[str] = Field(None, description="Minimal usage snippet if inferable")
    potential_issues: List[str] = Field(default_factory=list, description="N+1, No Timeout, SQL Injection, Race Condition, Memory Leak, None")
    tags: List[str] = Field(default_factory=list, description="auth, database, cache, api, ui, security, performance, testing")

ENRICHMENT_PROMPT = """
You are a Senior Software Architect analyzing a code snippet for a Knowledge Base.
Analyze the following code and return STRICT JSON matching the schema.

CODE:
```{language}
{code}
```

CONTEXT:
- File: {file_path}
- Symbol: {symbol_name} ({symbol_type})
- Repo: {repo_name} (Stack: {framework_version})

RETURN JSON:
{{
  "intent": "string",
  "pattern": "Repository | Factory | Builder | Hook | HOC | Middleware | Controller | Service | Utility | Config | Test | Migration | Script | Other",
  "complexity": "O(1) | O(n) | DB Query | External API | Heavy Compute | Recursive",
  "side_effects": ["DB Write", "Cache Invalidation", "Email Send", "File IO", "Network Request", "State Mutation", "None"],
  "dependencies_explained": ["DepName: Purpose"],
  "usage_example": "code snippet or null",
  "potential_issues": ["N+1 problem", "No timeout on HTTP", "SQL Injection risk", "Race condition", "Memory leak", "None"],
  "tags": ["auth", "database", "cache", "api", "ui", "security", "performance", "testing"]
}}
"""

class EnrichmentPipeline:
    def __init__(self, llm_client: LiteLLMClient, batch_size: int = 20, concurrency: int = 5):
        self.llm = llm_client
        self.batch_size = batch_size
        self.semaphore = asyncio.Semaphore(concurrency)
        self.model = settings.llm.enrichment_model # e.g. "router/enricher" -> Haiku/4o-mini

    async def enrich_chunks(self, chunks: List[RetrievalChunk]) -> List[RetrievalChunk]:
        """Process chunks in batches."""
        # Filter chunks that need enrichment (skip config/file_summary if desired)
        to_enrich = [c for c in chunks if c.metadata.get("chunk_type") == "symbol"]
        
        for i in range(0, len(to_enrich), self.batch_size):
            batch = to_enrich[i:i+self.batch_size]
            await self._enrich_batch(batch)
            
        return chunks

    async def _enrich_batch(self, batch: List[RetrievalChunk]):
        tasks = [self._enrich_one(chunk) for chunk in batch]
        await asyncio.gather(*tasks)

    async def _enrich_one(self, chunk: RetrievalChunk):
        async with self.semaphore:
            meta = chunk.metadata
            prompt = ENRICHMENT_PROMPT.format(
                language=meta.get("language", "text"),
                code=chunk.content[:3000], # Limit context
                file_path=meta.get("file_path", ""),
                symbol_name=meta.get("symbol_name", ""),
                symbol_type=meta.get("symbol_type", ""),
                repo_name=meta.get("repo", ""),
                framework_version=meta.get("framework_version", "")
            )
            
            try:
                # Use structured output
                result = await self.llm.achat(
                    messages=[{"role": "user", "content": prompt}],
                    model=self.model,
                    response_model=EnrichedMetadata,
                    temperature=0.0
                )
                # Update chunk metadata
                chunk.metadata.update(result.model_dump())
                chunk.content = self._rebuild_content(chunk) # Rebuild embed text with new metadata
            except Exception as e:
                print(f"[Enrichment] Failed for {chunk.id}: {e}")
                # Set defaults
                chunk.metadata.update(EnrichedMetadata(intent="Unknown", pattern="Other", complexity="Unknown", side_effects=["None"]).model_dump())

    def _rebuild_content(self, chunk: RetrievalChunk) -> str:
        """Rebuild embedding text with enriched metadata for better retrieval."""
        m = chunk.metadata
        header = f"# {m.get('symbol_name', '')} ({m.get('symbol_type', '')})\n"
        header += f"# Intent: {m.get('intent', '')}\n"
        header += f"# Pattern: {m.get('pattern', '')}\n"
        header += f"# Tags: {', '.join(m.get('tags', []))}\n"
        header += f"# File: {m.get('file_path', '')}\n"
        # Keep original code but maybe trimmed
        code_part = chunk.content.split("\n# File:")[0] if "# File:" in chunk.content else chunk.content
        return header + code_part
