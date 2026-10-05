# specs/07_gateway/router/CLASSIFIER.py
"""
Intent Classifier & Vertical Router.
Routes incoming prompt to the best Vertical.
Supports: Rule-based (fast), LLM-based (smart), Capability Matching (precise).
"""

from __future__ import annotations
import re
from typing: Dict, List, Optional, Literal
from pydantic import BaseModel, Field
from kernel.llm.client import LiteLLMClient
from kernel.vertical.loader import VerticalLoader
from kernel.protocols import VerticalManifest

class RoutingDecision(BaseModel):
    vertical_id: str
    confidence: float
    method: Literal["rule", "llm", "capability", "explicit"]
    reasoning: str
    suggested_tech_stack: Dict[str, str] = Field(default_factory=dict)

class VerticalRouter:
    def __init__(self, vertical_loader: VerticalLoader, llm_client: LiteLLMClient):
        self.loader = vertical_loader
        self.llm = llm_client
        self._verticals: Dict[str, VerticalManifest] = {}
        self._rule_patterns = self._compile_rules()

    def _compile_rules(self) -> Dict[str, List[re.Pattern]]:
        """Keyword/Regex rules for instant routing (0 LLM cost)."""
        return {
            "saas_web": [
                re.compile(r"\b(next\.?js|react|vercel|saas|dashboard|admin panel|landing|marketing|stripe|nextauth|shadcn|tailwind)\b", re.I),
            ],
            "py_fastapi_microsvc": [
                re.compile(r"\b(fastapi|pydantic|sqlalchemy|alembic|postgres|async|microservice|api only|backend|python)\b", re.I),
            ],
            "go_microsvc": [
                re.compile(r"\b(go|golang|grpc|protobuf|high load|cli tool|cobra|viper)\b", re.I),
            ],
            "iac_terraform": [
                re.compile(r"\b(terraform|infrastructure|aws|azure|gcp|kubernetes|helm|vpc|rds|iam|cloud)\b", re.I),
            ],
            "data_pipeline": [
                re.compile(r"\b(airflow|dbt|spark|pandas|etl|data pipeline|mlops|training|model)\b", re.I),
            ],
        }

    async def load_verticals(self):
        """Cache manifests for capability matching."""
        self._verticals = await self.loader.discover()

    async def route(self, prompt: str, explicit_vertical: Optional[str] = None, tech_hints: Dict = None) -> RoutingDecision:
        tech_hints = tech_hints or {}

        # 1. Explicit Override
        if explicit_vertical and explicit_vertical in self._verticals:
            return RoutingDecision(vertical_id=explicit_vertical, confidence=1.0, method="explicit", reasoning="User explicitly selected vertical.")

        # 2. Rule-Based (Fast, Cheap)
        rule_match = self._match_rules(prompt)
        if rule_match:
            return RoutingDecision(vertical_id=rule_match, confidence=0.85, method="rule", reasoning=f"Matched keywords for {rule_match}.")

        # 3. Capability Matching (Structured Hints)
        cap_match = self._match_capabilities(tech_hints)
        if cap_match:
            return RoutingDecision(vertical_id=cap_match, confidence=0.9, method="capability", reasoning=f"Tech stack hints match {cap_match} capabilities.")

        # 4. LLM Classifier (Smart, Costly)
        return await self._llm_classify(prompt, tech_hints)

    def _match_rules(self, prompt: str) -> Optional[str]:
        scores = {}
        for vid, patterns in self._rule_patterns.items():
            for pat in patterns:
                if pat.search(prompt):
                    scores[vid] = scores.get(vid, 0) + 1
        if scores:
            return max(scores, key=scores.get)
        return None

    def _match_capabilities(self, hints: Dict) -> Optional[str]:
        """Match tech_stack_hints to vertical provides."""
        scores = {}
        for vid, manifest in self._verticals.items():
            score = 0
            stack = manifest.tech_stack
            for k, v in hints.items():
                if stack.get(k) == v: score += 2
                elif v in str(stack.values()): score += 1
            if score > 0: scores[vid] = score
        if scores: return max(scores, key=scores.get)
        return None

    async def _llm_classify(self, prompt: str, hints: Dict) -> RoutingDecision:
        descriptions = "\n".join([f"- **{v.id}**: {v.description} (Stack: {v.tech_stack})" for v in self._verticals.values()])
        
        system = f"""You are a Platform Router. Classify the user request into the BEST matching vertical.
Available Verticals:
{descriptions}

Rules:
1. If request mentions specific tech (Next.js, FastAPI, Terraform) -> Match that vertical.
2. If generic "web app" -> saas_web.
3. If generic "api/backend" -> py_fastapi_microsvc.
4. If "infrastructure/cloud" -> iac_terraform.
5. Return ONLY the vertical_id."""
        
        user = f"Request: {prompt}\nHints: {hints}"
        
        try:
            resp = await self.llm.achat(
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                model="router/classifier",
                temperature=0.0,
                max_tokens=20
            )
            vid = resp.content.strip().lower()
            if vid in self._verticals:
                return RoutingDecision(vertical_id=vid, confidence=0.95, method="llm", reasoning="LLM classification.")
        except Exception:
            pass
        
        # Fallback
        return RoutingDecision(vertical_id="saas_web", confidence=0.5, method="fallback", reasoning="Default fallback.")
