"""
Gateway persistence (``specs/07_gateway/auth/API_KEYS.py`` — no content in the spec; written by the agent).

SQLite (single node): API keys (only SHA-256 hashes are stored), the run registry (owner tenant/user,
vertical, parent composition, idempotency key) and compositions. For several gateway replicas point
``gateway.db_path`` at shared storage or replace ``GatewayStore`` (ISSUES O-15).

API key format: ``agk_<tenant>_<32 random url-safe chars>``; the plaintext is shown once at creation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROLES = ("viewer", "developer", "admin")


@dataclass
class Principal:
    user_id: str
    tenant_id: str = "default"
    roles: list[str] = field(default_factory=lambda: ["developer"])
    vertical_access: list[str] = field(default_factory=lambda: ["*"])
    tier: str = "pro"
    auth_method: str = "api_key"  # api_key | jwt | anonymous

    def has_role(self, role: str) -> bool:
        """Roles are ordered: admin > developer > viewer."""
        rank = {r: i for i, r in enumerate(ROLES)}
        need = rank.get(role, len(ROLES))
        return any(rank.get(r, -1) >= need for r in self.roles)

    def can_use_vertical(self, vertical_id: str) -> bool:
        return "*" in self.vertical_access or vertical_id in self.vertical_access


@dataclass
class ApiKeyRecord:
    key_prefix: str
    name: str
    principal: Principal
    created_at: float
    revoked_at: float | None = None
    last_used_at: float | None = None


@dataclass
class RunRecord:
    run_id: str
    tenant_id: str
    user_id: str
    vertical_id: str
    kind: str = "run"  # run | composition
    parent_id: str | None = None
    idempotency_key: str | None = None
    request_hash: str | None = None
    webhook_url: str | None = None
    created_at: float = field(default_factory=time.time)
    routing: dict[str, Any] = field(default_factory=dict)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_api_key(tenant_id: str) -> tuple[str, str]:
    """(key, display prefix). The prefix identifies the key in listings/revocation; the rest is secret."""
    safe_tenant = "".join(ch for ch in tenant_id if ch.isalnum())[:24] or "t"
    head = f"agk_{safe_tenant}_"
    key = head + secrets.token_urlsafe(24)
    return key, key[: len(head) + 8]


class GatewayStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS api_keys (
                    key_hash TEXT PRIMARY KEY, key_prefix TEXT NOT NULL, name TEXT NOT NULL,
                    tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, roles TEXT NOT NULL,
                    vertical_access TEXT NOT NULL, tier TEXT NOT NULL,
                    created_at REAL NOT NULL, revoked_at REAL, last_used_at REAL);
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    vertical_id TEXT NOT NULL, kind TEXT NOT NULL, parent_id TEXT,
                    idempotency_key TEXT, request_hash TEXT, webhook_url TEXT,
                    created_at REAL NOT NULL, routing TEXT NOT NULL DEFAULT '{}');
                CREATE UNIQUE INDEX IF NOT EXISTS runs_idem ON runs(tenant_id, idempotency_key)
                    WHERE idempotency_key IS NOT NULL;
                CREATE INDEX IF NOT EXISTS runs_tenant ON runs(tenant_id, created_at);
                CREATE INDEX IF NOT EXISTS runs_parent ON runs(parent_id);
                CREATE TABLE IF NOT EXISTS compositions (
                    composition_id TEXT PRIMARY KEY, plan TEXT NOT NULL, state TEXT NOT NULL,
                    updated_at REAL NOT NULL);
                """
            )

    async def _run(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        def locked() -> Any:
            with self._lock:
                return fn(self._conn)

        return await asyncio.to_thread(locked)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- API keys ------------------------------------------------------------------------------
    async def create_api_key(self, principal: Principal, name: str = "") -> str:
        key, prefix = new_api_key(principal.tenant_id)

        def ins(c: sqlite3.Connection) -> None:
            with c:
                c.execute(
                    "INSERT INTO api_keys VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL)",
                    (
                        hash_key(key),
                        prefix,
                        name or principal.user_id,
                        principal.tenant_id,
                        principal.user_id,
                        json.dumps(principal.roles),
                        json.dumps(principal.vertical_access),
                        principal.tier,
                        time.time(),
                    ),
                )

        await self._run(ins)
        return key

    async def principal_for_key(self, key: str) -> Principal | None:
        h = hash_key(key)

        def get(c: sqlite3.Connection) -> Any:
            row = c.execute(
                "SELECT tenant_id, user_id, roles, vertical_access, tier FROM api_keys "
                "WHERE key_hash = ? AND revoked_at IS NULL",
                (h,),
            ).fetchone()
            if row is not None:
                with c:
                    c.execute("UPDATE api_keys SET last_used_at = ? WHERE key_hash = ?", (time.time(), h))
            return row

        row = await self._run(get)
        if row is None:
            return None
        return Principal(
            user_id=row[1],
            tenant_id=row[0],
            roles=json.loads(row[2]),
            vertical_access=json.loads(row[3]),
            tier=row[4],
            auth_method="api_key",
        )

    async def list_api_keys(self, tenant_id: str | None = None) -> list[ApiKeyRecord]:
        sql = (
            "SELECT key_prefix, name, tenant_id, user_id, roles, vertical_access, tier, created_at, revoked_at, "
            "last_used_at FROM api_keys"
        )
        args: tuple[Any, ...] = ()
        if tenant_id is not None:
            sql += " WHERE tenant_id = ?"
            args = (tenant_id,)
        rows = await self._run(lambda c: c.execute(sql + " ORDER BY created_at", args).fetchall())
        return [
            ApiKeyRecord(
                key_prefix=r[0],
                name=r[1],
                principal=Principal(
                    user_id=r[3], tenant_id=r[2], roles=json.loads(r[4]), vertical_access=json.loads(r[5]), tier=r[6]
                ),
                created_at=r[7],
                revoked_at=r[8],
                last_used_at=r[9],
            )
            for r in rows
        ]

    async def revoke_api_key(self, key_prefix: str) -> int:
        def upd(c: sqlite3.Connection) -> int:
            with c:
                cur = c.execute(
                    "UPDATE api_keys SET revoked_at = ? WHERE key_prefix = ? AND revoked_at IS NULL",
                    (time.time(), key_prefix),
                )
                return int(cur.rowcount)

        result: int = await self._run(upd)
        return result

    # --- run registry --------------------------------------------------------------------------
    async def add_run(self, rec: RunRecord) -> RunRecord:
        """Insert; with an idempotency key already used by the tenant, return the existing record."""

        def ins(c: sqlite3.Connection) -> Any:
            try:
                with c:
                    c.execute(
                        "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            rec.run_id,
                            rec.tenant_id,
                            rec.user_id,
                            rec.vertical_id,
                            rec.kind,
                            rec.parent_id,
                            rec.idempotency_key,
                            rec.request_hash,
                            rec.webhook_url,
                            rec.created_at,
                            json.dumps(rec.routing),
                        ),
                    )
                return None
            except sqlite3.IntegrityError:
                if rec.idempotency_key is None:
                    raise
                return c.execute(
                    f"SELECT {_RUN_COLS} FROM runs WHERE tenant_id = ? AND idempotency_key = ?",
                    (rec.tenant_id, rec.idempotency_key),
                ).fetchone()

        existing = await self._run(ins)
        return _run_from_row(existing) if existing is not None else rec

    async def get_run(self, run_id: str) -> RunRecord | None:
        sql = f"SELECT {_RUN_COLS} FROM runs WHERE run_id = ?"
        row = await self._run(lambda c: c.execute(sql, (run_id,)).fetchone())
        return _run_from_row(row) if row is not None else None

    async def get_run_by_idempotency_key(self, tenant_id: str, key: str) -> RunRecord | None:
        sql = f"SELECT {_RUN_COLS} FROM runs WHERE tenant_id = ? AND idempotency_key = ?"
        row = await self._run(lambda c: c.execute(sql, (tenant_id, key)).fetchone())
        return _run_from_row(row) if row is not None else None

    async def list_runs(
        self, tenant_id: str | None, limit: int = 50, offset: int = 0, parent_id: str | None = None
    ) -> list[RunRecord]:
        where, args = [], []
        if tenant_id is not None:
            where.append("tenant_id = ?")
            args.append(tenant_id)
        if parent_id is not None:
            where.append("parent_id = ?")
            args.append(parent_id)
        sql = f"SELECT {_RUN_COLS} FROM runs" + (f" WHERE {' AND '.join(where)}" if where else "")
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?"
        rows = await self._run(lambda c: c.execute(sql, (*args, limit, offset)).fetchall())
        return [_run_from_row(r) for r in rows]

    async def delete_run(self, run_id: str) -> None:
        def rm(c: sqlite3.Connection) -> None:
            with c:
                c.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))

        await self._run(rm)

    # --- compositions --------------------------------------------------------------------------
    async def save_composition(self, composition_id: str, plan: dict[str, Any], state: dict[str, Any]) -> None:
        def up(c: sqlite3.Connection) -> None:
            with c:
                c.execute(
                    "INSERT INTO compositions VALUES (?,?,?,?) ON CONFLICT(composition_id) DO UPDATE SET "
                    "plan = excluded.plan, state = excluded.state, updated_at = excluded.updated_at",
                    (composition_id, json.dumps(plan), json.dumps(state), time.time()),
                )

        await self._run(up)

    async def get_composition(self, composition_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        row = await self._run(
            lambda c: c.execute(
                "SELECT plan, state FROM compositions WHERE composition_id = ?", (composition_id,)
            ).fetchone()
        )
        return (json.loads(row[0]), json.loads(row[1])) if row else None


_RUN_COLS = (
    "run_id, tenant_id, user_id, vertical_id, kind, parent_id, idempotency_key, request_hash, webhook_url, "
    "created_at, routing"
)


def _run_from_row(r: Any) -> RunRecord:
    return RunRecord(
        run_id=r[0],
        tenant_id=r[1],
        user_id=r[2],
        vertical_id=r[3],
        kind=r[4],
        parent_id=r[5],
        idempotency_key=r[6],
        request_hash=r[7],
        webhook_url=r[8],
        created_at=r[9],
        routing=json.loads(r[10] or "{}"),
    )
