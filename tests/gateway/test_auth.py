from __future__ import annotations

import time
from typing import Any

import fakeredis
import jwt
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from kernel.config import GatewaySection, Settings, TierLimits
from kernel.gateway.auth import (
    GatewayAuth,
    InMemoryRateLimiter,
    QuotaExceeded,
    RedisRateLimiter,
    issue_jwt,
    require_role,
)
from kernel.gateway.store import GatewayStore, Principal, RunRecord, hash_key

SECRET = "test-secret-test-secret-test-secret-42"


def _settings(**gw: Any) -> Settings:
    base: dict[str, Any] = {"auth_enabled": True, "jwt_secret": SECRET}
    base.update(gw)
    return Settings(gateway=GatewaySection(**base))


def _request(headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": raw, "query_string": b""})


@pytest.fixture
def store() -> GatewayStore:
    return GatewayStore(":memory:")


async def test_api_key_roundtrip_and_revoke(store: GatewayStore) -> None:
    key = await store.create_api_key(Principal(user_id="alice", tenant_id="acme", roles=["admin"], tier="free"))
    assert key.startswith("agk_acme_")
    listed = await store.list_api_keys("acme")
    assert len(listed) == 1 and key.startswith(listed[0].key_prefix) and len(listed[0].key_prefix) < len(key)
    # only the full hash is stored
    rows = store._conn.execute("SELECT key_hash FROM api_keys").fetchall()
    assert rows == [(hash_key(key),)] and len(rows[0][0]) == 64

    auth = GatewayAuth(_settings(), store, InMemoryRateLimiter())
    p = await auth.authenticate(_request({"X-API-Key": key}))
    assert (p.user_id, p.tenant_id, p.roles, p.tier) == ("alice", "acme", ["admin"], "free")
    p2 = await auth.authenticate(_request({"Authorization": f"Bearer {key}"}))
    assert p2.user_id == "alice"

    assert await store.revoke_api_key(listed[0].key_prefix) == 1
    with pytest.raises(HTTPException) as ei:
        await auth.authenticate(_request({"X-API-Key": key}))
    assert ei.value.status_code == 401


async def test_missing_and_bad_credentials(store: GatewayStore) -> None:
    auth = GatewayAuth(_settings(), store, InMemoryRateLimiter())
    for headers in ({}, {"X-API-Key": "agk_x_nope"}, {"Authorization": "Bearer not-a-jwt"}):
        with pytest.raises(HTTPException) as ei:
            await auth.authenticate(_request(headers))
        assert ei.value.status_code == 401


async def test_jwt(store: GatewayStore) -> None:
    settings = _settings()
    auth = GatewayAuth(settings, store, InMemoryRateLimiter())
    tok = issue_jwt(settings, Principal(user_id="bob", tenant_id="acme", roles=["viewer"], tier="enterprise"))
    p = await auth.authenticate(_request({"Authorization": f"Bearer {tok}"}))
    assert (p.user_id, p.tenant_id, p.roles, p.tier, p.auth_method) == ("bob", "acme", ["viewer"], "enterprise", "jwt")

    expired = jwt.encode({"sub": "bob", "exp": int(time.time()) - 10, "iss": "autogen-platform"}, SECRET)
    wrong_key = jwt.encode({"sub": "bob", "exp": int(time.time()) + 60, "iss": "autogen-platform"}, "x" * 40)
    no_exp = jwt.encode({"sub": "bob", "iss": "autogen-platform"}, SECRET)
    wrong_iss = jwt.encode({"sub": "bob", "exp": int(time.time()) + 60, "iss": "evil"}, SECRET)
    for bad in (expired, wrong_key, no_exp, wrong_iss):
        with pytest.raises(HTTPException) as ei:
            await auth.authenticate(_request({"Authorization": f"Bearer {bad}"}))
        assert ei.value.status_code == 401

    # unknown roles are dropped; no valid role -> viewer
    odd = jwt.encode({"sub": "eve", "exp": int(time.time()) + 60, "iss": "autogen-platform", "roles": ["root"]}, SECRET)
    assert (await auth.authenticate(_request({"Authorization": f"Bearer {odd}"}))).roles == ["viewer"]


async def test_hs_tokens_rejected_without_secret(store: GatewayStore) -> None:
    auth = GatewayAuth(_settings(jwt_secret=None), store, InMemoryRateLimiter())
    tok = jwt.encode({"sub": "bob", "exp": int(time.time()) + 60, "iss": "autogen-platform"}, SECRET)
    with pytest.raises(HTTPException):
        await auth.authenticate(_request({"Authorization": f"Bearer {tok}"}))


async def test_auth_disabled_is_anonymous_admin(store: GatewayStore) -> None:
    auth = GatewayAuth(_settings(auth_enabled=False), store, InMemoryRateLimiter())
    p = await auth.principal(_request({}))
    assert p.auth_method == "anonymous" and p.has_role("admin")


def test_rbac_order() -> None:
    viewer = Principal(user_id="v", roles=["viewer"])
    dev = Principal(user_id="d", roles=["developer"], vertical_access=["saas_web"])
    assert viewer.has_role("viewer") and not viewer.has_role("developer")
    assert dev.has_role("viewer") and dev.has_role("developer") and not dev.has_role("admin")
    assert dev.can_use_vertical("saas_web") and not dev.can_use_vertical("go_microsvc")
    with pytest.raises(HTTPException) as ei:
        require_role(viewer, "developer")
    assert ei.value.status_code == 403


async def test_rpm_via_principal_dependency(store: GatewayStore) -> None:
    settings = _settings(tiers={"free": TierLimits(rpm=2, rpd=10, concurrent=1)}, default_tier="free")
    auth = GatewayAuth(settings, store, InMemoryRateLimiter())
    key = await store.create_api_key(Principal(user_id="a", tenant_id="t", tier="free"))
    await auth.principal(_request({"X-API-Key": key}))
    await auth.principal(_request({"X-API-Key": key}))
    with pytest.raises(HTTPException) as ei:
        await auth.principal(_request({"X-API-Key": key}))
    assert ei.value.status_code == 429 and int(ei.value.headers["Retry-After"]) > 0


def _limiters() -> list[Any]:
    return [
        InMemoryRateLimiter(),
        RedisRateLimiter.from_client(fakeredis.FakeAsyncRedis(decode_responses=True)),
    ]


@pytest.mark.parametrize("limiter", _limiters(), ids=["memory", "redis"])
async def test_limiter_quotas(limiter: Any) -> None:
    lim = TierLimits(rpm=3, rpd=2, concurrent=2)
    for _ in range(3):
        await limiter.hit("t1", lim)
    with pytest.raises(QuotaExceeded) as ei:
        await limiter.hit("t1", lim)
    assert ei.value.limit == "rpm"
    await limiter.hit("t2", lim)  # other tenant unaffected

    await limiter.count_generation("t1", lim)
    await limiter.count_generation("t1", lim)
    with pytest.raises(QuotaExceeded) as ei:
        await limiter.count_generation("t1", lim)
    assert ei.value.limit == "rpd"

    await limiter.acquire("t1", "r1", lim)
    await limiter.acquire("t1", "r1", lim)  # idempotent
    await limiter.acquire("t1", "r2", lim)
    with pytest.raises(QuotaExceeded) as ei:
        await limiter.acquire("t1", "r3", lim)
    assert ei.value.limit == "concurrent"
    assert await limiter.active("t1") == 2
    await limiter.release("t1", "r1")
    await limiter.acquire("t1", "r3", lim)
    assert await limiter.active("t1") == 2


async def test_run_registry_and_idempotency(store: GatewayStore) -> None:
    r1 = await store.add_run(RunRecord(run_id="r1", tenant_id="acme", user_id="a", vertical_id="saas_web"))
    assert r1.run_id == "r1"
    first = await store.add_run(
        RunRecord(
            run_id="r2", tenant_id="acme", user_id="a", vertical_id="saas_web", idempotency_key="k", routing={"m": 1}
        )
    )
    again = await store.add_run(
        RunRecord(run_id="r3", tenant_id="acme", user_id="a", vertical_id="saas_web", idempotency_key="k")
    )
    assert first.run_id == again.run_id == "r2" and again.routing == {"m": 1}
    other_tenant = await store.add_run(
        RunRecord(run_id="r4", tenant_id="beta", user_id="b", vertical_id="saas_web", idempotency_key="k")
    )
    assert other_tenant.run_id == "r4"
    await store.add_run(RunRecord(run_id="c1-a", tenant_id="acme", user_id="a", vertical_id="x", parent_id="c1"))

    assert (await store.list_runs("acme"))[0].run_id == "c1-a"
    assert {r.run_id for r in await store.list_runs("acme")} == {"r1", "r2", "c1-a"}
    assert len(await store.list_runs(None)) == 4
    assert [r.run_id for r in await store.list_runs("acme", parent_id="c1")] == ["c1-a"]
    assert (await store.get_run("r4")) is not None and (await store.get_run("nope")) is None

    await store.save_composition("c1", {"subprojects": []}, {"status": "running"})
    await store.save_composition("c1", {"subprojects": [1]}, {"status": "done"})
    assert await store.get_composition("c1") == ({"subprojects": [1]}, {"status": "done"})
