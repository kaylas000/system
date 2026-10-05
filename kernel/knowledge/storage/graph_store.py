"""
Code graph store (``specs/04_knowledge/storage/GRAPH_DB_SCHEMA.md`` / ``KUZU_CLIENT.py``;
written by the agent) with traversal queries (``MISSING_FILES.md`` #9 ``GRAPH_TRAVERSAL.py``).

The spec targets Kuzu. The Kuzu project was archived on 2025-10-10 (read-only, no further
releases; ISSUES KN-03), so the default backend is SQLite (stdlib, embedded, file or ``:memory:``)
with the same labels/relations as the spec schema and recursive CTEs for multi-hop queries.
The ``IGraphStore`` protocol keeps the door open for Neo4j/FalkorDB later.

Nodes: Repo, File, Symbol, Config, Module. Edges: CONTAINS, IMPORTS (File->Symbol),
IMPORTS_FILE (File->File|Module), CALLS, INHERITS, IMPLEMENTS (Symbol->Symbol).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol, TypeVar, runtime_checkable

from ..models import GraphEdge, GraphNode

T = TypeVar("T")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    name TEXT NOT NULL,
    repo TEXT NOT NULL DEFAULT '',
    file_path TEXT NOT NULL DEFAULT '',
    props TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS nodes_name ON nodes(name, label);
CREATE INDEX IF NOT EXISTS nodes_repo ON nodes(repo, file_path);
CREATE TABLE IF NOT EXISTS edges (
    src TEXT NOT NULL,
    rel TEXT NOT NULL,
    dst TEXT NOT NULL,
    repo TEXT NOT NULL DEFAULT '',
    props TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (src, rel, dst)
);
CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst, rel);
CREATE INDEX IF NOT EXISTS edges_repo ON edges(repo);
"""

SYMBOL_EDGES = ("CALLS", "INHERITS", "IMPLEMENTS")


@runtime_checkable
class IGraphStore(Protocol):
    async def replace_repo(self, repo: str, nodes: Sequence[GraphNode], edges: Sequence[GraphEdge]) -> None: ...
    async def delete_repo(self, repo: str) -> None: ...
    async def query_callers(
        self, name: str, file_path: str | None = None, repo: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]: ...
    async def query_callees(
        self, name: str, file_path: str | None = None, repo: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]: ...
    async def query_dependencies(self, file_path: str, repo: str | None = None) -> list[dict[str, Any]]: ...
    async def query_implementations(self, name: str, repo: str | None = None) -> list[dict[str, Any]]: ...
    async def impact_analysis(self, name: str, repo: str | None = None, depth: int = 3) -> list[dict[str, Any]]: ...
    async def neighbors(self, node_ids: Sequence[str], limit: int = 20) -> list[dict[str, Any]]: ...
    async def stats(self, repo: str | None = None) -> dict[str, int]: ...
    async def close(self) -> None: ...


class SqliteGraph:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            if str(path) != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")

    async def _run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        def locked() -> T:
            with self._lock:
                return fn(self._conn)

        return await asyncio.to_thread(locked)

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        props = json.loads(d.pop("props", "{}") or "{}")
        d.update({k: v for k, v in props.items() if k not in d})
        return d

    # --- write -------------------------------------------------------------------
    async def replace_repo(self, repo: str, nodes: Sequence[GraphNode], edges: Sequence[GraphEdge]) -> None:
        """Atomically replace the whole graph of ``repo`` (graph rebuild is cheap; keeps cross-file edges exact)."""

        def tx(conn: sqlite3.Connection) -> None:
            with conn:
                conn.execute("DELETE FROM edges WHERE repo = ?", (repo,))
                conn.execute("DELETE FROM nodes WHERE repo = ?", (repo,))
                conn.executemany(
                    "INSERT OR REPLACE INTO nodes(id, label, name, repo, file_path, props) VALUES (?,?,?,?,?,?)",
                    [(n.id, n.label, n.name, n.repo, n.file_path, json.dumps(n.props)) for n in nodes],
                )
                conn.executemany(
                    "INSERT OR REPLACE INTO edges(src, rel, dst, repo, props) VALUES (?,?,?,?,?)",
                    [(e.src, e.rel, e.dst, repo, json.dumps(e.props)) for e in edges],
                )
                # shared Module nodes (repo='') are kept only while referenced
                conn.execute(
                    "DELETE FROM nodes WHERE repo = '' AND label = 'Module' AND id NOT IN (SELECT dst FROM edges)"
                )

        await self._run(tx)

    async def delete_repo(self, repo: str) -> None:
        await self.replace_repo(repo, [], [])

    # --- queries -----------------------------------------------------------------
    def _symbol_where(self, name: str, file_path: str | None, repo: str | None, alias: str) -> tuple[str, list[Any]]:
        where = [
            f"{alias}.label = 'Symbol'",
            f"({alias}.name = ? OR json_extract({alias}.props, '$.parent') || '.' || {alias}.name = ?)",
        ]
        args: list[Any] = [name, name]
        if file_path:
            where.append(f"{alias}.file_path = ?")
            args.append(file_path)
        if repo:
            where.append(f"{alias}.repo = ?")
            args.append(repo)
        return " AND ".join(where), args

    async def query_callers(
        self, name: str, file_path: str | None = None, repo: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Who calls ``name`` (reverse call graph)."""
        where, args = self._symbol_where(name, file_path, repo, "callee")
        sql = f"""
            SELECT DISTINCT caller.* FROM edges e
            JOIN nodes callee ON callee.id = e.dst
            JOIN nodes caller ON caller.id = e.src
            WHERE e.rel = 'CALLS' AND {where}
            ORDER BY caller.file_path, caller.name LIMIT ?"""
        return await self._run(lambda c: [self._row(r) for r in c.execute(sql, [*args, limit])])

    async def query_callees(
        self, name: str, file_path: str | None = None, repo: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        where, args = self._symbol_where(name, file_path, repo, "caller")
        sql = f"""
            SELECT DISTINCT callee.* FROM edges e
            JOIN nodes caller ON caller.id = e.src
            JOIN nodes callee ON callee.id = e.dst
            WHERE e.rel = 'CALLS' AND {where}
            ORDER BY callee.file_path, callee.name LIMIT ?"""
        return await self._run(lambda c: [self._row(r) for r in c.execute(sql, [*args, limit])])

    async def query_dependencies(self, file_path: str, repo: str | None = None) -> list[dict[str, Any]]:
        """Files/modules imported by ``file_path`` plus imported and called symbols (with ``relation``)."""
        repo_clause = "AND f.repo = ?" if repo else ""
        args: list[Any] = [file_path, *([repo] if repo else [])]
        sql = f"""
            SELECT DISTINCT d.*, e.rel AS relation FROM nodes f
            JOIN edges e ON e.src = f.id AND e.rel IN ('IMPORTS', 'IMPORTS_FILE')
            JOIN nodes d ON d.id = e.dst
            WHERE f.label = 'File' AND f.file_path = ? {repo_clause}
            UNION
            SELECT DISTINCT d.*, 'CALLS' AS relation FROM nodes f
            JOIN edges c ON c.src = f.id AND c.rel = 'CONTAINS'
            JOIN edges e ON e.src = c.dst AND e.rel = 'CALLS'
            JOIN nodes d ON d.id = e.dst
            WHERE f.label = 'File' AND f.file_path = ? {repo_clause} AND d.file_path != f.file_path
            ORDER BY relation, file_path, name"""
        return await self._run(lambda c: [self._row(r) for r in c.execute(sql, args + args)])

    async def query_implementations(self, name: str, repo: str | None = None) -> list[dict[str, Any]]:
        where, args = self._symbol_where(name, None, repo, "iface")
        sql = f"""
            SELECT DISTINCT impl.*, e.rel AS relation FROM edges e
            JOIN nodes iface ON iface.id = e.dst
            JOIN nodes impl ON impl.id = e.src
            WHERE e.rel IN ('IMPLEMENTS', 'INHERITS') AND {where}
            ORDER BY impl.file_path, impl.name"""
        return await self._run(lambda c: [self._row(r) for r in c.execute(sql, args)])

    async def impact_analysis(self, name: str, repo: str | None = None, depth: int = 3) -> list[dict[str, Any]]:
        """Symbols transitively depending on ``name`` via CALLS/INHERITS/IMPLEMENTS (up to ``depth`` hops)."""
        where, args = self._symbol_where(name, None, repo, "n")
        rels = ",".join(f"'{r}'" for r in SYMBOL_EDGES)
        sql = f"""
            WITH RECURSIVE affected(id, depth) AS (
                SELECT n.id, 0 FROM nodes n WHERE {where}
                UNION
                SELECT e.src, a.depth + 1 FROM edges e JOIN affected a ON e.dst = a.id
                WHERE e.rel IN ({rels}) AND a.depth < ?
            )
            SELECT n.*, MIN(a.depth) AS depth FROM affected a JOIN nodes n ON n.id = a.id
            WHERE a.depth > 0 GROUP BY n.id ORDER BY depth, n.file_path, n.name"""
        return await self._run(lambda c: [self._row(r) for r in c.execute(sql, [*args, depth])])

    async def neighbors(self, node_ids: Sequence[str], limit: int = 20) -> list[dict[str, Any]]:
        """Direct symbol neighbours (callees first, then callers / base classes) of the given symbols."""
        if not node_ids:
            return []
        marks = ",".join("?" * len(node_ids))
        rels = ",".join(f"'{r}'" for r in SYMBOL_EDGES)
        sql = f"""
            SELECT n.*, 'out:' || e.rel AS relation, e.src AS via FROM edges e JOIN nodes n ON n.id = e.dst
            WHERE e.src IN ({marks}) AND e.rel IN ({rels}) AND e.dst NOT IN ({marks})
            UNION ALL
            SELECT n.*, 'in:' || e.rel AS relation, e.dst AS via FROM edges e JOIN nodes n ON n.id = e.src
            WHERE e.dst IN ({marks}) AND e.rel IN ({rels}) AND e.src NOT IN ({marks})
            LIMIT ?"""
        ids = list(node_ids)
        rows = await self._run(lambda c: [self._row(r) for r in c.execute(sql, ids * 4 + [limit * 3])])
        seen: set[str] = set()
        out = []
        for r in rows:
            if r["id"] not in seen:
                seen.add(r["id"])
                out.append(r)
        return out[:limit]

    async def find_symbols(self, name: str, repo: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        where, args = self._symbol_where(name, None, repo, "n")
        sql = f"SELECT n.* FROM nodes n WHERE {where} ORDER BY n.file_path LIMIT ?"
        return await self._run(lambda c: [self._row(r) for r in c.execute(sql, [*args, limit])])

    async def stats(self, repo: str | None = None) -> dict[str, int]:
        def q(conn: sqlite3.Connection) -> dict[str, int]:
            out: dict[str, int] = {}
            clause, args = ("WHERE repo = ?", [repo]) if repo else ("", [])
            for label, cnt in conn.execute(f"SELECT label, COUNT(*) FROM nodes {clause} GROUP BY label", args):
                out[f"nodes:{label}"] = cnt
            for rel, cnt in conn.execute(f"SELECT rel, COUNT(*) FROM edges {clause} GROUP BY rel", args):
                out[f"edges:{rel}"] = cnt
            return out

        return await self._run(q)

    async def close(self) -> None:
        await self._run(lambda c: c.close())
