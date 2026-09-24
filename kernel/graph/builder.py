"""
Graph assembly (``specs/01_kernel/graph_topology.md`` §1-§2).

Every node except ``human_review`` is wrapped so that an unexpected exception
turns into a ``NODE_ERROR`` interrupt instead of crashing the run; the human
can then ``retry`` (re-run the failed node), ``skip_gate`` or ``abort``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from dataclasses import replace
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from ..config import Settings
from ..llm.cost_tracker import BudgetExceededError, check_budget
from ..observability import MeteredLLM, MeteredSandbox, observe_node, record_node_error, record_node_result
from ..protocols import ILLMClient, ISandbox, IVertical
from ..state import AgentState, InterruptType, RunStatus
from . import routing
from .deps import KernelDeps, resolve_deps
from .nodes import (
    coder_node,
    documenter_node,
    fixer_node,
    get_next_task_node,
    human_review_node,
    initialize_node,
    packager_node,
    planner_node,
    verifier_node,
)
from .nodes._common import log

NodeFn = Callable[[AgentState, KernelDeps], Awaitable[dict[str, Any]]]
GraphNode = Callable[[AgentState, RunnableConfig], Awaitable[dict[str, Any]]]

NODES: dict[str, NodeFn] = {
    "initialize": initialize_node,
    "planner": planner_node,
    "get_next_task": get_next_task_node,
    "coder": coder_node,
    "verifier": verifier_node,
    "fixer": fixer_node,
    "documenter": documenter_node,
    "packager": packager_node,
    "human_review": human_review_node,
}


def _instrument(deps: KernelDeps) -> KernelDeps:
    """Wrap LLM / sandbox with metrics, tracing and the optional budget manager (once per node call)."""
    llm = deps.llm if isinstance(deps.llm, MeteredLLM) else MeteredLLM(deps.llm, deps.budget)
    sandbox = deps.sandbox if isinstance(deps.sandbox, MeteredSandbox) else MeteredSandbox(deps.sandbox)
    return replace(deps, llm=llm, sandbox=sandbox)


def _vertical_id(state: AgentState, deps: KernelDeps) -> str:
    try:
        return str(state.get("vertical_id") or deps.vertical.manifest.id)
    except Exception:
        return "unknown"


def _wrap(name: str, fn: NodeFn, defaults: dict[str, Any], *, catch: bool = True) -> GraphNode:
    async def node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        deps = _instrument(resolve_deps(config, defaults))
        vertical = _vertical_id(state, deps)
        run_id = str(state.get("run_id", ""))
        with observe_node(name, run_id, vertical):
            if not catch:
                return await fn(state, deps)
            budget_scope = deps.budget.run_scope(run_id, state) if deps.budget is not None else nullcontext()
            try:
                with budget_scope:
                    result = await fn(state, deps)
            except GraphBubbleUp:  # interrupts / parent commands must propagate
                raise
            except BudgetExceededError as exc:  # pre-flight check of the budget manager (per call)
                result = _budget_interrupt(name, {}, str(exc))
            except Exception as exc:
                record_node_error(name, vertical)
                result = {
                    "error": f"{name} failed: {type(exc).__name__}: {exc}",
                    "interrupt_type": InterruptType.NODE_ERROR,
                    "status": RunStatus.NEEDS_HUMAN_INPUT,
                    "failed_node": name,
                    "logs": [log(name, f"ERROR {type(exc).__name__}: {exc}")],
                }
            else:
                result = _enforce_budget(name, state, result, deps)
            record_node_result(name, state, result, vertical)
            return result

    node.__name__ = f"{name}_node"
    return node


def _budget_interrupt(name: str, result: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        **result,
        "error": reason,
        "interrupt_type": InterruptType.BUDGET_EXCEEDED,
        "status": RunStatus.NEEDS_HUMAN_INPUT,
        "failed_node": name,
        "logs": [*result.get("logs", []), log(name, reason)],
    }


def _enforce_budget(name: str, state: AgentState, result: dict[str, Any], deps: KernelDeps) -> dict[str, Any]:
    """Pause the run (BUDGET_EXCEEDED) after a node if cost/token limits are exceeded."""
    if result.get("error") or result.get("interrupt_type") or name == "packager":  # own interrupt wins
        return result
    usage = result.get("token_usage") or state.get("token_usage")
    max_tokens = (state.get("metadata") or {}).get("max_tokens_per_run") or deps.settings.llm.max_tokens_per_run
    reason = check_budget(usage, state.get("max_budget_usd"), max_tokens)
    if reason is None:
        return result
    return _budget_interrupt(name, result, reason)


def build_graph(
    vertical: IVertical | None = None,
    *,
    llm: ILLMClient | None = None,
    sandbox: ISandbox | None = None,
    settings: Settings | None = None,
    tool_registry: Any | None = None,
    retriever: Any | None = None,
    budget: Any | None = None,
    checkpointer: Any | None = None,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """Build and compile the kernel graph.

    Dependencies given here are defaults; each can be overridden per run via
    ``config["configurable"]`` (``vertical``, ``llm_client``, ``sandbox``, ``settings``, ``tool_registry``,
    ``retriever`` — optional knowledge-base ``RetrievalEngine`` that fills ``metadata.rag_context``,
    ``budget`` — optional ``kernel.llm.budget.BudgetManager`` (daily limits, tokens/min, pre-flight checks)).
    """
    defaults: dict[str, Any] = {
        "vertical": vertical,
        "llm_client": llm,
        "sandbox": sandbox,
        "settings": settings,
        "tool_registry": tool_registry,
        "retriever": retriever,
        "budget": budget,
    }
    g: StateGraph[Any, Any, Any, Any] = StateGraph(AgentState)
    for name, fn in NODES.items():
        g.add_node(name, _wrap(name, fn, defaults, catch=name != "human_review"))  # type: ignore[call-overload]

    g.add_edge(START, "initialize")
    g.add_conditional_edges("initialize", routing.route_after_initialize)
    g.add_conditional_edges("planner", routing.route_after_planner)
    g.add_conditional_edges("get_next_task", routing.route_get_next_task)
    g.add_conditional_edges("coder", routing.route_after_coder)
    g.add_conditional_edges("verifier", routing.route_after_verifier)
    g.add_conditional_edges("fixer", routing.route_after_fixer)
    g.add_conditional_edges("documenter", routing.route_after_documenter)
    g.add_conditional_edges("packager", routing.route_after_packager, {"__end__": END, "human_review": "human_review"})
    human_map: dict[str, str] = {t: t for t in routing.HUMAN_TARGETS}
    human_map["__end__"] = END
    g.add_conditional_edges("human_review", routing.route_after_human_review, human_map)  # type: ignore[arg-type]
    return g.compile(checkpointer=checkpointer)
