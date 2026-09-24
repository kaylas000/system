# specs/06_ops/cost_control/BUDGET_MANAGER.py
"""
Budget Enforcement & Cost Tracking.
Integrates with LangGraph Checkpointer to stop runs exceeding budget.
"""

from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from typing: Dict, Optional
from contextlib import asynccontextmanager
from kernel.state import AgentState, TokenUsage
from kernel.config import settings

@dataclass
class BudgetConfig:
    max_cost_usd_per_run: float = 5.0
    max_tokens_per_run: int = 2_000_000
    max_cost_usd_per_day: float = 100.0
    max_tokens_per_minute: int = 500_000 # Rate limit
    alert_threshold_pct: float = 0.8 # Alert at 80%

@dataclass
class BudgetState:
    current_run_cost: float = 0.0
    current_run_tokens: int = 0
    daily_cost: float = 0.0
    daily_tokens: int = 0
    minute_tokens: int = 0
    last_minute_reset: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

class BudgetManager:
    def __init__(self, config: BudgetConfig = None):
        self.config = config or BudgetConfig()
        self.state = BudgetState()
        self._alerts_sent: set = set()

    @asynccontextmanager
    async def check_budget(self, state: AgentState, estimated_cost: float = 0.0, estimated_tokens: int = 0):
        """Context manager to check budget before and after node execution."""
        async with self.state._lock:
            await self._reset_minute_window()
            
            # Pre-flight Check
            projected_cost = self.state.current_run_cost + estimated_cost
            projected_tokens = self.state.current_run_tokens + estimated_tokens
            
            if projected_cost > self.config.max_cost_usd_per_run:
                raise BudgetExceededError(f"Run cost limit exceeded: ${projected_cost:.4f} > ${self.config.max_cost_usd_per_run}")
            if projected_tokens > self.config.max_tokens_per_run:
                raise BudgetExceededError(f"Run token limit exceeded: {projected_tokens} > {self.config.max_tokens_per_run}")
            if self.state.minute_tokens + estimated_tokens > self.config.max_tokens_per_minute:
                # Wait for rate limit window
                wait_time = 60 - (asyncio.get_event_loop().time() - self.state.last_minute_reset)
                if wait_time > 0:
                    await asyncio.sleep(wait_time)
                    await self._reset_minute_window()
            
            # Reserve budget
            self.state.current_run_cost += estimated_cost
            self.state.current_run_tokens += estimated_tokens
            self.state.minute_tokens += estimated_tokens
            
        try:
            yield
        finally:
            # Actual usage is recorded via `record_actual_usage` callback from LLM Client
            pass

    async def record_actual_usage(self, usage: TokenUsage, vertical: str):
        """Called by LLM Client after successful call."""
        async with self.state._lock:
            self.state.current_run_cost += usage.cost_usd
            self.state.current_run_tokens += usage.total_tokens
            self.state.daily_cost += usage.cost_usd
            self.state.daily_tokens += usage.total_tokens
            self.state.minute_tokens += usage.total_tokens
            
            await self._check_alerts(vertical)

    async def _reset_minute_window(self):
        import time
        now = time.time()
        if now - self.state.last_minute_reset > 60:
            self.state.minute_tokens = 0
            self.state.last_minute_reset = now

    async def _check_alerts(self, vertical: str):
        run_pct = self.state.current_run_cost / self.config.max_cost_usd_per_run
        daily_pct = self.state.daily_cost / self.config.max_cost_usd_per_day
        
        for pct, limit_type in [(run_pct, "run"), (daily_pct, "daily")]:
            if pct >= self.config.alert_threshold_pct and f"{vertical}_{limit_type}" not in self._alerts_sent:
                self._alerts_sent.add(f"{vertical}_{limit_type}")
                # Fire Alert (Webhook, Slack, PagerDuty)
                print(f"🚨 BUDGET ALERT: {vertical} {limit_type} budget at {pct*100:.0f}%")

    def get_remaining_budget(self) -> Dict[str, float]:
        return {
            "run_cost_remaining": max(0, self.config.max_cost_usd_per_run - self.state.current_run_cost),
            "run_tokens_remaining": max(0, self.config.max_tokens_per_run - self.state.current_run_tokens),
            "daily_cost_remaining": max(0, self.config.max_cost_usd_per_day - self.state.daily_cost),
        }

    def reset_run(self):
        """Call at start of new run."""
        self.state.current_run_cost = 0.0
        self.state.current_run_tokens = 0.0

class BudgetExceededError(Exception):
    pass
