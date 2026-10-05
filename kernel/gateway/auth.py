"""
Authentication, RBAC and quotas (``specs/07_gateway/auth/AUTH_MANAGER.py``, fixes: ISSUES G-05).

* API keys ``agk_...`` (``X-API-Key`` or ``Authorization: Bearer agk_...``) — looked up by full SHA-256.
* JWT (``Authorization: Bearer <jwt>``): HS256 secret, PEM public key or JWKS URL (Auth0/Clerk/Keycloak).
  Claims: ``sub`` → user, ``tenant_id``/``org_id`` → tenant, ``roles``, ``tier``, ``vertical_access``.
* RBAC: viewer (read) < developer (generate, HITL decisions) < admin (all runs of the tenant, keys).
* Quotas per tenant tier: rpm (every authenticated call), rpd (generations), concurrent (executing runs;
  a slot is taken when a run starts/resumes and released when it stops — done, error or HITL pause).
* ``X-Tenant-ID`` (optional) must match the tenant of the credentials. WebSocket clients that can not set
  headers may pass ``?access_token=``.
* ``gateway.auth_enabled=false`` — local development: every request is an anonymous admin.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import defaultdict
from typing import Any, Protocol

import jwt
from fastapi import HTTPException
from starlette.requests import HTTPConnection

from kernel.config import Settings, TierLimits
from kernel.gateway.store import ROLES, GatewayStore, Principal


class QuotaExceeded(Exception):
    def __init__(self, limit: str, retry_after: int) -> None:
        super().__init__(f"quota exceeded: {limit}")
        self.limit = limit
        self.retry_after = retry_after


# --- rate limiting ---------------------------------------------------------------------------------
class RateLimiter(Protocol):
    async def hit(self, tenant_id: str, limits: TierLimits) -> None:
        """Count one API request; raise QuotaExceeded when rpm is exhausted."""

    async def count_generation(self, tenant_id: str, limits: TierLimits) -> None:
        """Count one generation; raise QuotaExceeded when rpd is exhausted."""

    async def acquire(self, tenant_id: str, run_id: str, limits: TierLimits) -> None:
        """Take a concurrent-run slot (idempotent per run_id); raise QuotaExceeded when none is free."""

    async def release(self, tenant_id: str, run_id: str) -> None: ...

    async def active(self, tenant_id: str) -> int: ...


def _minute(now: float) -> int:
    return int(now // 60)


def _day(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now))


def _seconds_to_next_day(now: float) -> int:
    return int(86400 - (now % 86400)) + 1


class InMemoryRateLimiter:
    """Single process. Fixed windows: per UTC minute (rpm) and per UTC day (rpd)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._minute: dict[tuple[str, int], int] = defaultdict(int)
        self._day: dict[tuple[str, str], int] = defaultdict(int)
        self._running: dict[str, set[str]] = defaultdict(set)

    async def hit(self, tenant_id: str, limits: TierLimits) -> None:
        now = time.time()
        key = (tenant_id, _minute(now))
        with self._lock:
            for k in [k for k in self._minute if k[1] < key[1]]:
                del self._minute[k]
            if self._minute[key] >= limits.rpm:
                raise QuotaExceeded("rpm", int(60 - now % 60) + 1)
            self._minute[key] += 1

    async def count_generation(self, tenant_id: str, limits: TierLimits) -> None:
        now = time.time()
        key = (tenant_id, _day(now))
        with self._lock:
            for k in [k for k in self._day if k[1] < key[1]]:
                del self._day[k]
            if self._day[key] >= limits.rpd:
                raise QuotaExceeded("rpd", _seconds_to_next_day(now))
            self._day[key] += 1

    async def acquire(self, tenant_id: str, run_id: str, limits: TierLimits) -> None:
        with self._lock:
            running = self._running[tenant_id]
            if run_id in running:
                return
            if len(running) >= limits.concurrent:
                raise QuotaExceeded("concurrent", 30)
            running.add(run_id)

    async def release(self, tenant_id: str, run_id: str) -> None:
        with self._lock:
            self._running[tenant_id].discard(run_id)

    async def active(self, tenant_id: str) -> int:
        with self._lock:
            return len(self._running[tenant_id])


class RedisRateLimiter:
    """Shared across replicas. Concurrent slots are a sorted set scored by expiry (crash safety)."""

    SLOT_TTL = 6 * 3600

    def __init__(self, url: str, prefix: str = "autogen:rl") -> None:
        import redis.asyncio as aioredis

        self._redis: Any = aioredis.from_url(url, decode_responses=True)
        self._prefix = prefix

    @classmethod
    def from_client(cls, client: Any, prefix: str = "autogen:rl") -> RedisRateLimiter:
        self = cls.__new__(cls)
        self._redis = client
        self._prefix = prefix
        return self

    async def _incr(self, key: str, ttl: int) -> int:
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, ttl)
            res = await pipe.execute()
        return int(res[0])

    async def hit(self, tenant_id: str, limits: TierLimits) -> None:
        now = time.time()
        n = await self._incr(f"{self._prefix}:m:{tenant_id}:{_minute(now)}", 120)
        if n > limits.rpm:
            raise QuotaExceeded("rpm", int(60 - now % 60) + 1)

    async def count_generation(self, tenant_id: str, limits: TierLimits) -> None:
        now = time.time()
        key = f"{self._prefix}:d:{tenant_id}:{_day(now)}"
        n = await self._incr(key, 2 * 86400)
        if n > limits.rpd:
            await self._redis.decr(key)
            raise QuotaExceeded("rpd", _seconds_to_next_day(now))

    async def acquire(self, tenant_id: str, run_id: str, limits: TierLimits) -> None:
        key = f"{self._prefix}:c:{tenant_id}"
        now = time.time()
        await self._redis.zremrangebyscore(key, "-inf", now)
        if await self._redis.zscore(key, run_id) is not None:
            await self._redis.zadd(key, {run_id: now + self.SLOT_TTL})
            return
        await self._redis.zadd(key, {run_id: now + self.SLOT_TTL})
        if int(await self._redis.zcard(key)) > limits.concurrent:  # optimistic: never over-admits
            await self._redis.zrem(key, run_id)
            raise QuotaExceeded("concurrent", 30)

    async def release(self, tenant_id: str, run_id: str) -> None:
        await self._redis.zrem(f"{self._prefix}:c:{tenant_id}", run_id)

    async def active(self, tenant_id: str) -> int:
        key = f"{self._prefix}:c:{tenant_id}"
        await self._redis.zremrangebyscore(key, "-inf", time.time())
        return int(await self._redis.zcard(key))


def build_rate_limiter(settings: Settings) -> RateLimiter:
    if settings.gateway.redis_url:
        return RedisRateLimiter(settings.gateway.redis_url)
    return InMemoryRateLimiter()


# --- authentication --------------------------------------------------------------------------------
class GatewayAuth:
    def __init__(self, settings: Settings, store: GatewayStore, limiter: RateLimiter) -> None:
        self.settings = settings
        self.cfg = settings.gateway
        self.store = store
        self.limiter = limiter
        self._jwks: Any = None
        if self.cfg.jwks_url:
            self._jwks = jwt.PyJWKClient(self.cfg.jwks_url, cache_keys=True)

    def limits(self, principal: Principal) -> TierLimits:
        tiers = self.cfg.tiers
        return (
            tiers.get(principal.tier) or tiers.get(self.cfg.default_tier) or TierLimits(rpm=60, rpd=1000, concurrent=5)
        )

    async def authenticate(self, request: HTTPConnection) -> Principal:
        if not self.cfg.auth_enabled:
            return Principal(
                user_id="anonymous", tenant_id="default", roles=["admin"], tier=self.cfg.default_tier,
                auth_method="anonymous",
            )  # fmt: skip
        token = request.headers.get("x-api-key")
        auth = request.headers.get("authorization", "")
        if not token and auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        if not token and request.scope.get("type") == "websocket":  # browsers can not set WS headers
            token = request.query_params.get("access_token")
        if not token:
            raise HTTPException(401, "missing credentials: X-API-Key or Authorization: Bearer",
                                headers={"WWW-Authenticate": "Bearer"})  # fmt: skip
        if token.startswith("agk_"):
            principal = await self.store.principal_for_key(token)
            if principal is None:
                raise HTTPException(401, "invalid or revoked API key")
            return principal
        return await self._from_jwt(token)

    async def _from_jwt(self, token: str) -> Principal:
        try:
            key = await self._jwt_key(token)
            claims = jwt.decode(
                token,
                key,
                algorithms=self.cfg.jwt_algorithms,
                issuer=self.cfg.jwt_issuer,
                audience=self.cfg.jwt_audience,
                options={"require": ["exp", "sub"], "verify_aud": self.cfg.jwt_audience is not None},
            )
        except jwt.PyJWTError as exc:
            raise HTTPException(401, f"invalid token: {exc}", headers={"WWW-Authenticate": "Bearer"}) from exc
        roles = claims.get("roles") or ["developer"]
        if isinstance(roles, str):
            roles = [roles]
        roles = [r for r in roles if r in ROLES] or ["viewer"]
        access = claims.get("vertical_access") or ["*"]
        return Principal(
            user_id=str(claims["sub"]),
            tenant_id=str(claims.get("tenant_id") or claims.get("org_id") or claims["sub"]),
            roles=roles,
            vertical_access=list(access),
            tier=str(claims.get("tier") or self.cfg.default_tier),
            auth_method="jwt",
        )

    async def _jwt_key(self, token: str) -> Any:
        if self._jwks is not None:
            signing_key = await asyncio.to_thread(self._jwks.get_signing_key_from_jwt, token)
            return signing_key.key
        alg = jwt.get_unverified_header(token).get("alg", "")
        if alg.startswith("HS"):
            if self.cfg.jwt_secret is None:
                raise jwt.InvalidTokenError("HS* tokens are not accepted (gateway.jwt_secret is not set)")
            return self.cfg.jwt_secret.get_secret_value()
        if self.cfg.jwt_public_key is None:
            raise jwt.InvalidTokenError(f"{alg} tokens are not accepted (no public key / JWKS configured)")
        return self.cfg.jwt_public_key

    async def principal(self, request: HTTPConnection) -> Principal:
        """FastAPI dependency body: authenticate and count one request against rpm."""
        principal = await self.authenticate(request)
        tenant = request.headers.get("x-tenant-id")
        if tenant and principal.auth_method != "anonymous" and tenant != principal.tenant_id:
            raise HTTPException(403, "X-Tenant-ID does not match the credentials")
        if principal.auth_method != "anonymous":
            try:
                await self.limiter.hit(principal.tenant_id, self.limits(principal))
            except QuotaExceeded as exc:
                raise quota_http_error(exc) from exc
        request.state.principal = principal
        return principal


def quota_http_error(exc: QuotaExceeded) -> HTTPException:
    return HTTPException(429, str(exc), headers={"Retry-After": str(exc.retry_after)})


def require_role(principal: Principal, role: str) -> None:
    if not principal.has_role(role):
        raise HTTPException(403, f"role {role!r} required")


def issue_jwt(settings: Settings, principal: Principal, ttl_seconds: int = 3600) -> str:
    """HS256 token signed with gateway.jwt_secret (tests, service-to-service, the admin CLI)."""
    if settings.gateway.jwt_secret is None:
        raise ValueError("gateway.jwt_secret is not set")
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": principal.user_id,
        "tenant_id": principal.tenant_id,
        "roles": principal.roles,
        "tier": principal.tier,
        "vertical_access": principal.vertical_access,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    if settings.gateway.jwt_issuer:
        claims["iss"] = settings.gateway.jwt_issuer
    if settings.gateway.jwt_audience:
        claims["aud"] = settings.gateway.jwt_audience
    return jwt.encode(claims, settings.gateway.jwt_secret.get_secret_value(), algorithm="HS256")
