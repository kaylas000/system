# specs/04_knowledge/retrieval/RERANKING.py
"""
Cross-Encoder Reranking & LLM Judge Reranking.
"""

from __future__ import annotations
from typing: List, Dict, Any
import numpy as np

class Reranker:
    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"):
        # Lazy load heavy model
        self._model = None
        self.model_name = model_name

    def _load(self):
        if self._model is None:
            from sentence_transformers import CrossEncoder
            self._model = CrossEncoder(self.model_name, max_length=512)

    def rerank(self, query: str, candidates: List[Dict], top_k: int) -> List[Dict]:
        self._load()
        pairs = [(query, c["payload"].get("content", "")[:500]) for c in candidates]
        scores = self._model.predict(pairs, show_progress_bar=False)
        
        for c, score in zip(candidates, scores):
            c["rerank_score"] = float(score)
        
        candidates.sort(key=lambda x: x["rerank_score"], reverse=True)
        return candidates[:top_k]

class LLMReranker:
    """LLM-as-a-Judge Reranker (High accuracy, High latency). Use for Planner/Fixer."""
    def __init__(self, llm_client, model: str = "router/reranker"):
        self.llm = llm_client
        self.model = model

    async def rerank(self, query: str, candidates: List[Dict], top_k: int) -> List[Dict]:
        if not candidates: return []
        
        # Batch prompt for efficiency
        prompt = f"""Rate the relevance of each code snippet to the QUERY on a scale 0-10.
QUERY: {query}

SNIPPETS:
"""
        for i, c in enumerate(candidates):
            content = c["payload"].get("content", "")[:800]
            prompt += f"\n--- SNIPPET {i+1} (ID: {c['id']}) ---\n{content}\n"

        prompt += """
\nReturn JSON array: [{"id": "snippet_id", "score": 0-10, "reason": "..."}, ...]
"""
        try:
            # Use structured output for list of scores
            from pydantic import BaseModel
            class Score(BaseModel):
                id: str
                score: float
                reason: str
            
            class Scores(BaseModel):
                scores: List[Score]
            
            result = await self.llm.achat(
                messages=[{"role": "user", "content": prompt}],
                model=self.model,
                response_model=Scores,
                temperature=0.0
            )
            
            score_map = {s.id: s.score for s in result.scores}
            for c in candidates:
                c["rerank_score"] = score_map.get(c["id"], 0.0)
            
            candidates.sort(key=lambda x: x["rerank_score"], reverse=True)
            return candidates[:top_k]
        except Exception as e:
            print(f"[LLM Rerank] Failed: {e}. Falling back to vector scores.")
            return candidates[:top_k]
