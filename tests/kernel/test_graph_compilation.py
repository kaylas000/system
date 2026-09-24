from __future__ import annotations

from kernel.graph import NODES, build_graph
from kernel.state import NodeName

EXPECTED_EDGES = {
    ("__start__", "initialize"),
    ("initialize", "planner"),
    ("initialize", "human_review"),
    ("planner", "get_next_task"),
    ("planner", "human_review"),
    ("get_next_task", "coder"),
    ("get_next_task", "documenter"),
    ("get_next_task", "human_review"),
    ("coder", "verifier"),
    ("coder", "human_review"),
    ("verifier", "fixer"),
    ("verifier", "get_next_task"),
    ("verifier", "human_review"),
    ("fixer", "verifier"),
    ("fixer", "human_review"),
    ("documenter", "packager"),
    ("documenter", "human_review"),
    ("packager", "__end__"),
    ("packager", "human_review"),
}


def test_nodes_match_node_name_enum() -> None:
    assert set(NODES) == {n.value for n in NodeName}


def test_graph_compiles_with_expected_topology() -> None:
    graph = build_graph().get_graph()
    assert set(graph.nodes) == {*NODES, "__start__", "__end__"}
    edges = {(e.source, e.target) for e in graph.edges}
    assert EXPECTED_EDGES <= edges
    human_targets = {t for s, t in edges if s == "human_review"}
    assert human_targets == {*NODES, "__end__"}
    unexpected = {e for e in edges if e[0] != "human_review"} - EXPECTED_EDGES
    assert not unexpected, unexpected


def test_mermaid_render() -> None:
    assert "human_review" in build_graph().get_graph().draw_mermaid()
