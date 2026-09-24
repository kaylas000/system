"""
Vertical router (``specs/07_gateway/router/CLASSIFIER.py``; rewritten by the agent).

Order: explicit ``vertical_id`` -> keyword rules -> capability match on ``tech_stack_hints`` -> LLM
classifier -> fallback (``kernel.default_vertical``). Only verticals that are actually loaded can win.

Rules come from each manifest's ``routing.keywords`` plus the spec's built-in patterns for known
vertical ids. A rule decision needs a clear winner (more distinct keyword hits than the runner-up);
ties go to the next stage. When several verticals match strongly, ``candidates`` lists them — the
gateway may offer a composition (``kernel.gateway.composer``).

Spec defects fixed (ISSUES G-01, G-xx): ``method="fallback"`` outside the Literal; ``from typing:``;
an unknown explicit vertical was silently ignored (now an error); ``_match_rules`` counted patterns, not
hits, so every match scored 1 (ties decided by dict order); ``await loader.discover()`` on a sync method;
free-text LLM answer parsed with ``.strip().lower()`` (now structured output).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..protocols import ILLMClient, LLMMessage, VerticalManifest

logger = logging.getLogger(__name__)

Method = Literal["explicit", "rule", "capability", "llm", "fallback"]

# From the spec (CLASSIFIER.py). Used only for verticals with these ids that are actually installed.
BUILTIN_KEYWORDS: dict[str, list[str]] = {
    "saas_web": [
        "next.js",
        "nextjs",
        "react",
        "vercel",
        "saas",
        "dashboard",
        "admin panel",
        "landing",
        "marketing",
        "stripe",
        "nextauth",
        "shadcn",
        "tailwind",
    ],
    "py_fastapi_microsvc": [
        "fastapi",
        "pydantic",
        "sqlalchemy",
        "alembic",
        "microservice",
        "api only",
        "backend",
        "python",
    ],
    "go_microsvc": ["golang", "grpc", "protobuf", "high load", "cli tool", "cobra", "viper"],
    "iac_terraform": [
        "terraform",
        "infrastructure",
        "aws",
        "azure",
        "gcp",
        "kubernetes",
        "helm",
        "vpc",
        "rds",
        "iam",
        "cloud",
    ],
    "data_pipeline": ["airflow", "dbt", "spark", "pandas", "etl", "data pipeline", "mlops"],
}


class UnknownVerticalError(LookupError):
    pass


class RoutingDecision(BaseModel):
    vertical_id: str
    confidence: float
    method: Method
    reasoning: str
    scores: dict[str, float] = Field(default_factory=dict)
    candidates: list[str] = Field(default_factory=list)  # >1 -> the request spans several verticals
    suggested_tech_stack: dict[str, str] = Field(default_factory=dict)


class ClassifierOutput(BaseModel):
    vertical_id: str
    confidence: float = Field(ge=0, le=1)
    reasoning: str = ""
    other_verticals: list[str] = Field(default_factory=list)  # also needed for a multi-part system


def _pattern(keyword: str) -> re.Pattern[str]:
    # word boundaries that also work for "next.js", "auth.js", "c++"-like tokens
    return re.compile(rf"(?<![\w.-]){re.escape(keyword.lower())}(?![\w-])", re.I)


class VerticalRouter:
    def __init__(
        self,
        manifests: Mapping[str, VerticalManifest] | Callable[[], Mapping[str, VerticalManifest]],
        llm: ILLMClient | None = None,
        *,
        default_vertical: str = "saas_web",
        classifier_model: str = "router/classifier",
        extra_keywords: Mapping[str, list[str]] | None = None,
    ) -> None:
        self._source = manifests
        self.llm = llm
        self.default_vertical = default_vertical
        self.classifier_model = classifier_model
        self.extra_keywords = dict(extra_keywords or {})
        self._manifests: dict[str, VerticalManifest] = {}
        self._rules: dict[str, list[tuple[str, re.Pattern[str]]]] = {}
        self.refresh()

    # --- setup -------------------------------------------------------------------------------
    def refresh(self) -> None:
        src = self._source() if callable(self._source) else self._source
        self._manifests = dict(src)
        self._rules = {}
        for vid, manifest in self._manifests.items():
            routing = (manifest.model_extra or {}).get("routing") or {}
            words = [*BUILTIN_KEYWORDS.get(vid, []), *routing.get("keywords", []), *self.extra_keywords.get(vid, [])]
            uniq = list(dict.fromkeys(w.strip().lower() for w in words if str(w).strip()))
            self._rules[vid] = [(w, _pattern(w)) for w in uniq]

    @property
    def verticals(self) -> dict[str, VerticalManifest]:
        return dict(self._manifests)

    # --- routing -----------------------------------------------------------------------------
    async def route(
        self, prompt: str, explicit_vertical: str | None = None, tech_hints: Mapping[str, str] | None = None
    ) -> RoutingDecision:
        hints = dict(tech_hints or {})
        if explicit_vertical:
            if explicit_vertical not in self._manifests:
                available = sorted(self._manifests)
                raise UnknownVerticalError(f"unknown vertical {explicit_vertical!r}; available: {available}")
            return RoutingDecision(
                vertical_id=explicit_vertical, confidence=1.0, method="explicit", reasoning="explicitly selected"
            )
        if not self._manifests:
            raise UnknownVerticalError("no verticals installed")

        rule_scores, hits = self.rule_scores(prompt)
        candidates = [v for v, s in sorted(rule_scores.items(), key=lambda kv: -kv[1]) if s >= 2]
        ranked = sorted(rule_scores.items(), key=lambda kv: -kv[1])
        if ranked and (len(ranked) == 1 or ranked[0][1] > ranked[1][1]):
            vid, score = ranked[0]
            return RoutingDecision(
                vertical_id=vid,
                confidence=min(0.95, 0.6 + 0.1 * score),
                method="rule",
                reasoning=f"keywords: {', '.join(hits[vid])}",
                scores=rule_scores,
                candidates=candidates,
            )

        cap_scores = self.capability_scores(hints)
        cap_ranked = sorted(cap_scores.items(), key=lambda kv: -kv[1])
        if cap_ranked and (len(cap_ranked) == 1 or cap_ranked[0][1] > cap_ranked[1][1]):
            vid = cap_ranked[0][0]
            return RoutingDecision(
                vertical_id=vid,
                confidence=0.9,
                method="capability",
                reasoning=f"tech_stack_hints match {vid}",
                scores=cap_scores,
                candidates=candidates,
            )

        decision = await self._llm_classify(prompt, hints)
        if decision is not None:
            decision.candidates = decision.candidates or candidates
            return decision
        fallback = self.default_vertical if self.default_vertical in self._manifests else sorted(self._manifests)[0]
        return RoutingDecision(
            vertical_id=fallback,
            confidence=0.3,
            method="fallback",
            reasoning="no rule/capability match and no LLM decision",
            scores=rule_scores,
            candidates=candidates,
        )

    def rule_scores(self, prompt: str) -> tuple[dict[str, float], dict[str, list[str]]]:
        scores: dict[str, float] = {}
        hits: dict[str, list[str]] = {}
        for vid, rules in self._rules.items():
            found = [w for w, pat in rules if pat.search(prompt)]
            if found:
                scores[vid] = float(len(found))
                hits[vid] = found
        return scores, hits

    def capability_scores(self, hints: Mapping[str, str]) -> dict[str, float]:
        scores: dict[str, float] = {}
        for vid, manifest in self._manifests.items():
            stack = {k: str(v).lower() for k, v in manifest.tech_stack.items()}
            score = 0.0
            for key, value in hints.items():
                val = str(value).lower().strip()
                if not val:
                    continue
                if key in stack and (stack[key] == val or stack[key].startswith(val) or val in stack[key]):
                    score += 2
                elif any(val in s for s in stack.values()):
                    score += 1
            if score:
                scores[vid] = score
        return scores

    async def _llm_classify(self, prompt: str, hints: Mapping[str, str]) -> RoutingDecision | None:
        if self.llm is None:
            return None
        catalog = "\n".join(
            f"- {m.id}: {m.name}. {m.description.strip()} Stack: {m.tech_stack}" for m in self._manifests.values()
        )
        system = (
            "You route software generation requests to a vertical (a code generator for one kind of project).\n"
            f"Available verticals:\n{catalog}\n\n"
            "Pick the single best vertical_id from the list. If the request clearly needs several parts "
            "(e.g. web frontend + separate backend service + infrastructure), put the other needed vertical ids "
            "in other_verticals. Never invent ids."
        )
        user = f"Request:\n{prompt[:8000]}\n\nTech hints: {dict(hints)}"
        try:
            resp = await self.llm.achat(
                [LLMMessage(role="system", content=system), LLMMessage(role="user", content=user)],
                model=self.classifier_model,
                response_model=ClassifierOutput,
                max_tokens=300,
            )
            parsed = resp.parsed
            out = parsed if isinstance(parsed, ClassifierOutput) else ClassifierOutput.model_validate_json(resp.content)
        except Exception as exc:  # classifier is best effort
            logger.warning("LLM classifier failed: %s", exc)
            return None
        if out.vertical_id not in self._manifests:
            logger.warning("LLM classifier returned unknown vertical %r", out.vertical_id)
            return None
        others = [v for v in out.other_verticals if v in self._manifests and v != out.vertical_id]
        return RoutingDecision(
            vertical_id=out.vertical_id,
            confidence=out.confidence,
            method="llm",
            reasoning=out.reasoning or "LLM classification",
            candidates=[out.vertical_id, *others] if others else [],
        )


def manifest_summary(m: VerticalManifest) -> dict[str, Any]:
    """``VerticalInfo`` of the OpenAPI spec."""
    return {
        "id": m.id,
        "name": m.name,
        "description": m.description.strip(),
        "version": m.version,
        "tech_stack": dict(m.tech_stack),
        "key_skills": list(m.key_skills),
    }
