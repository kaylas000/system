# specs/02_infra/tools/RAG_TOOL.py
"""
Retrieval-Augmented Generation Tool.
Interfaces with Vector DB (Qdrant) and Graph DB (Kuzu).
Implements Hybrid Search (Vector + BM25) + Graph Traversal.
"""

from __future__ import annotations
from typing import Dict, Any, List, Optional, Literal
from pydantic import BaseModel, Field
from kernel.protocols import ITool, ToolResult, IRAGTool
from kernel.state import AgentState


# --- Config ---
class RagConfig(BaseModel):
    qdrant_url: str
    qdrant_api_key: Optional[str] = None
    default_collection: str = "code_chunks"
    default_limit: int = 10
    score_threshold: float = 0.65


# --- Data Models ---


class RetrievedChunk(BaseModel):
    id: str
    content: str
    score: float
    metadata: Dict[str, Any] = Field(default_factory=dict)
    # Enriched fields
    intent: Optional[str] = None
    pattern: Optional[str] = None
    symbol_name: Optional[str] = None
    file_path: Optional[str] = None
    language: Optional[str] = None


class RagResult(BaseModel):
    chunks: List[RetrievedChunk]
    query: str
    total_found: int


RAG_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["retrieve", "retrieve_by_symbol", "retrieve_by_file", "graph_neighbors"]},
        "query": {"type": "string", "description": "Natural language query or symbol name"},
        "filters": {"type": "object", "description": "Metadata filters (language, framework, repo, tag)"},
        "top_k": {"type": "integer", "default": 10},
        "symbol_name": {"type": "string"},
        "file_path": {"type": "string"},
        "depth": {"type": "integer", "default": 1, "description": "Graph traversal depth"},
    },
    "required": ["action"],
}

# --- Implementation ---


class RagTool(IRAGTool):
    name = "rag"
    description = "Retrieve relevant code examples, patterns, docs, and architecture context from Knowledge Base."
    parameters_json_schema = RAG_TOOL_SCHEMA

    def __init__(self, config: RagConfig):
        self.config = config
        self._qdrant_client = None  # Lazy init
        self._kuzu_conn = None  # Lazy init

    def _get_qdrant(self):
        if not self._qdrant_client:
            from qdrant_client import AsyncQdrantClient

            self._qdrant_client = AsyncQdrantClient(url=self.config.qdrant_url, api_key=self.config.qdrant_api_key)
        return self._qdrant_client

    def _get_kuzu(self):
        if not self._kuzu_conn:
            import kuzu

            db = kuzu.Database("./kuzu_db")  # Or remote
            self._kuzu_conn = kuzu.Connection(db)
        return self._kuzu_conn

    async def execute(self, sandbox_id: str, args: Dict[str, Any], state: AgentState) -> ToolResult:
        action = args["action"]

        try:
            if action == "retrieve":
                result = await self._retrieve(args)
            elif action == "retrieve_by_symbol":
                result = await self._retrieve_by_symbol(args)
            elif action == "retrieve_by_file":
                result = await self._retrieve_by_file(args)
            elif action == "graph_neighbors":
                result = await self._graph_neighbors(args)
            else:
                return ToolResult(success=False, error=f"Unknown action: {action}")

            return ToolResult(success=True, data=result.model_dump())
        except Exception as e:
            return ToolResult(success=False, error=f"RAG Error: {str(e)}")

    async def _retrieve(self, args: Dict) -> RagResult:
        query = args["query"]
        top_k = args.get("top_k", self.config.default_limit)
        filters = args.get("filters", {})

        # 1. Embed Query (Use fast local model or API)
        # For spec, assume `embed(query)` returns vector
        vector = await self._embed(query)

        # 2. Hybrid Search (Vector + BM25)
        # Qdrant supports hybrid via `prefetch` (Vector) + `query` (BM25) or Fusion API
        # Simplified: Vector search with filters
        client = self._get_qdrant()

        # Build Filter
        from qdrant_client.models import Filter, FieldCondition, MatchValue, MatchAny

        must = []
        for k, v in filters.items():
            if isinstance(v, list):
                must.append(FieldCondition(key=k, match=MatchAny(any=v)))
            else:
                must.append(FieldCondition(key=k, match=MatchValue(value=v)))

        # Add vertical filter automatically from state context if available
        # vertical_id = state.get("vertical_id") -> filters["vertical_id"] = vertical_id

        search_result = await client.query_points(
            collection_name=self.config.default_collection,
            query=vector,
            query_filter=Filter(must=must) if must else None,
            limit=top_k,
            with_payload=True,
            score_threshold=self.config.score_threshold,
        )

        chunks = [
            RetrievedChunk(id=hit.id, content=hit.payload.pop("content", ""), score=hit.score, metadata=hit.payload)
            for hit in search_result.points
        ]
        return RagResult(chunks=chunks, query=query, total_found=len(chunks))

    async def _retrieve_by_symbol(self, args: Dict) -> RagResult:
        """Exact match on symbol_name + file_path context."""
        symbol = args["symbol_name"]
        file_path = args.get("file_path", "")
        client = self._get_qdrant()

        res = await client.query_points(
            collection_name=self.config.default_collection,
            query_filter=Filter(
                must=[
                    FieldCondition(key="symbol_name", match=MatchValue(value=symbol)),
                    FieldCondition(key="file_path", match=MatchValue(value=file_path)),  # Optional
                ]
            ),
            limit=5,
            with_payload=True,
        )
        return RagResult(chunks=[...], query=symbol, total_found=len(res.points))

    async def _retrieve_by_file(self, args: Dict) -> RagResult:
        """Get ALL chunks for a specific file (for context loading)."""
        file_path = args["file_path"]
        client = self._get_qdrant()
        res = await client.query_points(
            collection_name=self.config.default_collection,
            query_filter=Filter(must=[FieldCondition(key="file_path", match=MatchValue(value=file_path))]),
            limit=100,  # Get all chunks for file
            with_payload=True,
        )
        # Sort by line number
        chunks = sorted([...], key=lambda c: c.metadata.get("start_line", 0))
        return RagResult(chunks=chunks, query=file_path, total_found=len(chunks))

    async def _graph_neighbors(self, args: Dict) -> RagResult:
        """Traverse Code Graph (Callers/Callees/Imports)."""
        symbol = args["symbol_name"]
        file_path = args.get("file_path")
        depth = args.get("depth", 1)

        conn = self._get_kuzu()
        # Cypher query for Kuzu
        # MATCH (n:Function {name: $symbol, file: $file}) -[:CALLS*1..$depth]-> (m) RETURN m
        query = """
        MATCH (n {name: $symbol, file_path: $file}) -[r:CALLS|IMPORTS|INHERITS*1..$depth]-> (m)
        RETURN m.name, m.type, m.file_path, m.code, m.intent
        LIMIT 50
        """
        result = conn.execute(query, {"symbol": symbol, "file": file_path, "depth": depth})

        chunks = []
        while result.has_next():
            row = result.get_next()
            chunks.append(
                RetrievedChunk(
                    id=f"{row[2]}:{row[0]}",
                    content=row[3] or "",
                    score=1.0,
                    metadata={"symbol_name": row[0], "symbol_type": row[1], "file_path": row[2]},
                    intent=row[4],
                )
            )
        return RagResult(chunks=chunks, query=f"graph:{symbol}", total_found=len(chunks))

    async def _embed(self, text: str) -> List[float]:
        # Call Embedding Model (Local SentenceTransformer or API)
        # Use `nomic-embed-text` or `text-embedding-3-small`
        # Cache embeddings for repeated queries in same run.
        return [0.0] * 768  # Placeholder
