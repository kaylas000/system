"""
Cost tracking and budget enforcement.

NOTE: ``specs/02_infra/llm_gateway/COST_TRACKER.py`` is listed in the spec tree
but has no content (``specs/MISSING_FILES.md`` #1). This module is AUTHORED BY
THE AGENT, not taken from the spec.

Design:
* The source of truth for a run is ``AgentState.token_usage`` (checkpointed,
  survives restarts). ``check_budget`` is a pure function used by the graph.
* ``CostTracker`` is an in-process aggregator for reporting across runs
  (per run / per model / per day). Phase 6 (``COST_REPORTER``) can persist it.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from threading import Lock
from typing import Any

from ..state import TokenUsage, utcnow


class BudgetExceededError(RuntimeError):
    pass


def check_budget(usage: TokenUsage | None, max_budget_usd: float | None, max_tokens: int | None = None) -> str | None:
    """Return a human-readable reason if the run is over budget, else ``None``."""
    if usage is None:
        return None
    if max_budget_usd is not None and max_budget_usd > 0 and usage.cost_usd > max_budget_usd:
        return f"Budget exceeded: ${usage.cost_usd:.4f} > ${max_budget_usd:.4f}"
    if max_tokens is not None and max_tokens > 0 and usage.total_tokens > max_tokens:
        return f"Token limit exceeded: {usage.total_tokens} > {max_tokens}"
    return None


@dataclass
class _Bucket:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0

    def add(self, usage: TokenUsage) -> None:
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        self.total_tokens += usage.total_tokens
        self.cost_usd = round(self.cost_usd + usage.cost_usd, 6)
        self.calls += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "calls": self.calls,
        }


@dataclass
class CostTracker:
    """Thread-safe in-memory aggregation of LLM usage."""

    _by_run: dict[str, _Bucket] = field(default_factory=lambda: defaultdict(_Bucket))
    _by_model: dict[str, _Bucket] = field(default_factory=lambda: defaultdict(_Bucket))
    _by_day: dict[date, _Bucket] = field(default_factory=lambda: defaultdict(_Bucket))
    _lock: Lock = field(default_factory=Lock)

    def record(self, run_id: str, usage: TokenUsage) -> None:
        with self._lock:
            self._by_run[run_id].add(usage)
            self._by_model[usage.model_name or "unknown"].add(usage)
            self._by_day[utcnow().date()].add(usage)

    def run_total(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            return self._by_run[run_id].as_dict() if run_id in self._by_run else _Bucket().as_dict()

    def ensure_within(self, run_id: str, max_budget_usd: float | None) -> None:
        total = self.run_total(run_id)["cost_usd"]
        if max_budget_usd is not None and max_budget_usd > 0 and total > max_budget_usd:
            raise BudgetExceededError(f"Run {run_id}: ${total:.4f} > ${max_budget_usd:.4f}")

    def report(self) -> dict[str, Any]:
        with self._lock:
            return {
                "runs": {k: v.as_dict() for k, v in self._by_run.items()},
                "models": {k: v.as_dict() for k, v in self._by_model.items()},
                "days": {k.isoformat(): v.as_dict() for k, v in self._by_day.items()},
            }
