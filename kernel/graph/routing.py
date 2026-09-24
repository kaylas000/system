"""
Conditional edge logic (``specs/01_kernel/graph_topology.md`` §3).

Routers are PURE functions: they only read the state. In the spec reference
implementation routers also wrote to ``state`` (``state["error"] = ...``);
LangGraph discards such writes, so all state changes live in the nodes
(see ``specs/ISSUES.md`` K-11).
"""

from __future__ import annotations

from typing import Literal

from ..state import AgentState, InterruptType, VerificationGateStatus

HUMAN = "human_review"
END = "__end__"

HumanTarget = Literal[
    "initialize",
    "planner",
    "get_next_task",
    "coder",
    "verifier",
    "fixer",
    "documenter",
    "packager",
    "human_review",
    "__end__",
]
HUMAN_TARGETS: tuple[str, ...] = HumanTarget.__args__  # type: ignore[attr-defined]


def route_after_initialize(state: AgentState) -> Literal["planner", "human_review"]:
    """Check if vertical & sandbox were set up correctly."""
    return "human_review" if state.get("error") else "planner"


def route_after_planner(state: AgentState) -> Literal["get_next_task", "human_review"]:
    """Planner output is validated inside the node; errors or plan review go to a human."""
    if state.get("error") or state.get("interrupt_type") == InterruptType.PLAN_REVIEW:
        return "human_review"
    return "get_next_task"


def route_get_next_task(state: AgentState) -> Literal["coder", "documenter", "human_review"]:
    """``get_next_task`` sets ``current_task_id`` (or an error on deadlock)."""
    if state.get("error"):
        return "human_review"
    return "coder" if state.get("current_task_id") else "documenter"


def route_after_coder(state: AgentState) -> Literal["verifier", "human_review"]:
    return "human_review" if state.get("error") else "verifier"


def route_after_verifier(state: AgentState) -> Literal["fixer", "get_next_task", "human_review"]:
    """The core control-flow decision (infra errors / retry budget are decided in the node)."""
    if state.get("error") or not state.get("current_task_id"):
        return "human_review"
    results = state.get("current_gate_results", [])
    if any(r.status == VerificationGateStatus.FAILED for r in results):
        return "fixer"
    return "get_next_task"


def route_after_fixer(state: AgentState) -> Literal["verifier", "human_review"]:
    return "human_review" if state.get("error") else "verifier"


def route_after_documenter(state: AgentState) -> Literal["packager", "human_review"]:
    return "human_review" if state.get("error") else "packager"


def route_after_packager(state: AgentState) -> Literal["__end__", "human_review"]:
    return "human_review" if state.get("error") else "__end__"


def route_after_human_review(state: AgentState) -> HumanTarget:
    target = state.get("next_after_human") or END
    if target not in HUMAN_TARGETS:
        return "human_review"
    return target  # type: ignore[return-value]
