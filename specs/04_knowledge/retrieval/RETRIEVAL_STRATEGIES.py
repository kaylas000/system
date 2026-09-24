# specs/04_knowledge/retrieval/RETRIEVAL_STRATEGIES.py
"""
High-Level Retrieval Strategies for different Agent Nodes.
Orchestrates Vector Search, Graph Traversal, Reranking.
"""

from __future__ import annotations
from typing: List, Dict, Any, Optional, Literal
from pydantic import BaseModel
from kernel.state import AgentState
from ..storage.QDRANT_CLIENT import QdrantKB
from ..storage.KUZU_CLIENT import KuzuGraph
from kernel.llm.client import LiteLLMClient

class RetrievalResult(BaseModel):
    chunks: List[Dict] # Unified format: {id, content, metadata, score, source: "vector|graph"}
    strategy: str
    query: str

class RetrievalEngine:
    def __init__(self, vector_db: QdrantKB, graph_db: KuzuGraph, llm: LiteLLMClient):
        self.vector = vector_db
        self.graph = graph_db
        self.llm = llm

    # --- Strategy 1: Coder / Fixer (Hybrid Semantic + Keyword) ---
    
    async def retrieve_for_coding(
        self, 
        state: AgentState, 
        query: str, 
        current_file: str = "", 
        top_k: int = 8
    ) -> RetrievalResult:
        """
        Used by Coder/Fixer nodes.
        Context: Current task, current file, tech stack.
        """
        # 1. Build Filters from State
        filters = self._build_context_filters(state)
        
        # 2. Embed Query
        query_vec = await self._embed(query)
        
        # 3. Vector Search (Hybrid)
        vector_results = await self.vector.hybrid_search(
            query_vector=query_vec,
            query_text=query,
            filters=filters,
            limit=top_k * 2
        )
        
        # 4. Graph Augmentation (If current_file known)
        graph_results = []
        if current_file:
            # Get symbols in current file
            file_symbols = await self.vector.search_by_symbol("", current_file, filters, limit=5)
            for fs in file_symbols:
                sym_name = fs["payload"].get("symbol_name")
                if sym_name:
                    callers = self.graph.query_callers(sym_name, current_file, limit=3)
                    graph_results.extend(callers)
        
        # 5. Merge & Deduplicate
        merged = self._merge_results(vector_results, graph_results)
        
        # 6. Rerank (Cross-Encoder or LLM-based)
        reranked = await self._rerank(query, merged, top_k)
        
        return RetrievalResult(chunks=reranked, strategy="hybrid_coding", query=query)

    # --- Strategy 2: Planner (Architecture & Patterns) ---
    
    async def retrieve_for_planning(
        self, 
        state: AgentState, 
        query: str, 
        top_k: int = 10
    ) -> RetrievalResult:
        """
        Used by Planner.
        Focus: Architecture patterns, configs, high-level modules.
        """
        filters = self._build_context_filters(state)
        filters["chunk_type"] = ["file_summary", "config", "symbol"]
        filters["symbol_type"] = ["class", "interface", "function"] # High level
        
        query_vec = await self._embed(query)
        results = await self.vector.hybrid_search(query_vec, query, filters, limit=top_k)
        
        # Add Graph: Module Dependencies
        # (Requires knowing entry points, skip for brevity)
        
        reranked = await self._rerank(query, results, top_k)
        return RetrievalResult(chunks=reranked, strategy="planning", query=query)

    # --- Strategy 3: Fixer (Error Signature Matching) ---
    
    async def retrieve_for_fixing(
        self, 
        state: AgentState, 
        error_log: str, 
        failed_files: List[str],
        top_k: int = 5
    ) -> RetrievalResult:
        """
        Used by Fixer.
        Query = Error message + Stack trace.
        Filter = Files to fix + Language.
        """
        filters = self._build_context_filters(state)
        filters["file_path"] = failed_files # Qdrant Filter: MatchAny on file_path? 
        # Note: Qdrant doesn't support `MatchAny` on `file_path` easily if not keyword. 
        # Better: Search by error signature in `error_fixes` collection.
        
        # 1. Search Error Fixes Collection (Specialized)
        error_vec = await self._embed(error_log)
        fix_results = await self.vector.hybrid_search(
            error_vec, error_log, 
            filters={"chunk_type": "error_fix"}, 
            limit=top_k
        )
        
        # 2. Get Context of Failed Files (File Summaries)
        file_context = []
        for f in failed_files:
            res = await self.vector.search_by_symbol("", f, filters, limit=1)
            file_context.extend(res)
            
        merged = fix_results + file_context
        reranked = await self._rerank(error_log, merged, top_k)
        return RetrievalResult(chunks=reranked, strategy="fixing", query=error_log)

    # --- Helpers ---
    
    def _build_context_filters(self, state: AgentState) -> Dict[str, Any]:
        vertical_manifest = state.get("vertical_manifest", {})
        tech_stack = vertical_manifest.get("tech_stack", {})
        
        filters = {
            "language": tech_stack.get("language", "typescript"),
            "framework_version": tech_stack.get("framework_version", ""),
        }
        # Add vertical specific tags
        if "tags" in vertical_manifest:
            filters["tags"] = vertical_manifest["tags"]
        return {k: v for k, v in filters.items() if v}

    async def _embed(self, text: str) -> List[float]:
        # Use fast local embedder or API
        # return await self.llm.embed(text) # Assuming client has embed method
        return [0.0] * 768 # Placeholder

    def _merge_results(self, *result_lists: List[Dict]) -> List[Dict]:
        seen = set()
        merged = []
        for rlist in result_lists:
            for r in rlist:
                pid = r["id"]
                if pid not in seen:
                    seen.add(pid)
                    merged.append(r)
        return merged

    async def _rerank(self, query: str, candidates: List[Dict], top_k: int) -> List[Dict]:
        if not candidates: return []
        
        # Option A: Cross-Encoder (Fast, Local) - Best for production
        # scores = cross_encoder.predict([(query, c['payload']['content']) for c in candidates])
        
        # Option B: LLM Rerank (Slow, Accurate) - Use for Planner/Fixer
        # Prompt: "Rate relevance 0-10 for query: ..."
        
        # Option C: Heuristic (Fast, No Model)
        # Boost: exact symbol match, same file, high pattern match.
        for c in candidates:
            score = c.get("score", 0.5)
            payload = c.get("payload", {})
            # Boost exact symbol name match in query
            if payload.get("symbol_name", "").lower() in query.lower():
                score += 0.3
            # Boost same file
            # Boost pattern match (e.g. query "repository" -> pattern "Repository")
            if payload.get("pattern", "").lower() in query.lower():
                score += 0.2
            c["rerank_score"] = min(score, 1.0)
        
        candidates.sort(key=lambda x: x["rerank_score"], reverse=True)
        return candidates[:top_k]
