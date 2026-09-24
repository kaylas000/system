from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter")

from kernel.knowledge.ingestion.chunking import CodeChunker, RepoInfo
from kernel.knowledge.ingestion.parsers import MultiLanguageParser
from kernel.knowledge.storage.graph_store import IGraphStore, SqliteGraph


@pytest.fixture
async def graph(sample_files, tmp_path):
    parser = MultiLanguageParser()
    parsed = [parser.parse_file(p, c) for p, c in sample_files.items() if parser.supports(p)]
    res = CodeChunker(RepoInfo("sample")).build(parsed, sample_files)
    g = SqliteGraph(tmp_path / "graph.sqlite")
    await g.replace_repo("sample", res.nodes, res.edges)
    yield g
    await g.close()


async def test_callers_callees_and_filters(graph):
    assert isinstance(graph, IGraphStore)
    callers = await graph.query_callers("getUser")
    assert {(c["name"], c["file_path"]) for c in callers} == {
        ("updateUserName", "src/server/users.ts"),
        ("GET", "src/app/api/users/route.ts"),
        ("ProfilePage", "src/app/profile/page.tsx"),
        ("find", "src/lib/repo.ts"),
    }
    assert callers[0]["type"] in {"function", "component", "method"}
    assert await graph.query_callers("getUser", file_path="src/other.ts") == []
    assert await graph.query_callers("getUser", repo="nope") == []
    callees = await graph.query_callees("ProfilePage")
    assert {c["name"] for c in callees} == {"getUser", "Avatar"}
    assert {c["name"] for c in await graph.query_callees("Seeder.run")} == {"build"}


async def test_dependencies_implementations_impact(graph):
    deps = await graph.query_dependencies("src/app/profile/page.tsx")
    by_rel = {(d["relation"], d["name"]) for d in deps}
    assert ("IMPORTS_FILE", "Avatar.tsx") in by_rel
    assert ("IMPORTS_FILE", "react") in by_rel
    assert ("IMPORTS", "getUser") in by_rel
    assert ("CALLS", "getUser") in by_rel

    impls = await graph.query_implementations("IRepo")
    assert [(i["name"], i["relation"]) for i in impls] == [("UserRepo", "IMPLEMENTS")]

    impact = await graph.impact_analysis("getUser", depth=3)
    names = {i["name"]: i["depth"] for i in impact}
    assert names["GET"] == 1 and names["find"] == 1
    assert "getUser" not in names


async def test_neighbors_stats_and_replace(graph):
    get_user = "sample#src/server/users.ts#getUser#13"
    neigh = await graph.neighbors([get_user], limit=10)
    assert {n["relation"] for n in neigh} == {"in:CALLS"}
    assert len(neigh) == 4

    stats = await graph.stats("sample")
    assert stats["nodes:File"] == 9
    assert stats["edges:CALLS"] >= 6
    assert (await graph.stats()).get("nodes:Module", 0) >= 3

    await graph.delete_repo("sample")
    assert await graph.query_callers("getUser") == []
    assert await graph.stats() == {}
