# specs/07_gateway/auth/AUTH_MANAGER.py
"""
Authentication, Authorization & Quotas.
JWT Validation, API Key Management, Rate Limiting (Redis).
"""

from __future__ import annotations
import jwt
import time
import hashlib
from typing: Dict, Optional, List
from dataclasses import dataclass
from fastapi import HTTPException, Security, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials, APIKeyHeader
import redis.asyncio as redis
from kernel.config import settings

# --- Security Schemes ---
bearer_scheme = HTTPBearer(auto_error=False)
api_key_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)

@dataclass
class Principal:
    user_id: str
    tenant_id: str
    roles: List[str] # ["admin", "developer", "viewer"]
    vertical_access: List[str] # ["saas_web", "*"]
    rate_limit_tier: str # "free", "pro", "enterprise"

class AuthManager:
    def __init__(self):
        self.redis = redis.from_url(settings.redis.url, encoding="utf-8", decode_responses=True)
        self.jwt_secret = settings.auth.jwt_secret
        self.jwt_algo = "HS256"
        self.jwt_issuer = "autogen-platform"

    async def authenticate(self, 
                           token: HTTPAuthorizationCredentials = Security(bearer_scheme),
                           api_key: str = Security(api_key_scheme)) -> Principal:
        """Validate JWT or API Key."""
        if token:
            return await self._validate_jwt(token.credentials)
        if api_key:
            return await self._validate_api_key(api_key)
        raise HTTPException(401, "Missing authentication")

    async def _validate_jwt(self, token: str) -> Principal:
        try:
            payload = jwt.decode(token, self.jwt_secret, algorithms=[self.jwt_algo], issuer=self.jwt_issuer)
            return Principal(
                user_id=payload["sub"],
                tenant_id=payload.get("tenant_id", "default"),
                roles=payload.get("roles", ["developer"]),
                vertical_access=payload.get("vertical_access", ["*"]),
                rate_limit_tier=payload.get("tier", "pro")
            )
        except jwt.ExpiredSignatureError:
            raise HTTPException(401, "Token expired")
        except jwt.InvalidTokenError as e:
            raise HTTPException(401, f"Invalid token: {e}")

    async def _validate_api_key(self, key: str) -> Principal:
        # Format: agk_<tenant>_<random>
        # Store hash in Redis: `apikey:{hash}` -> JSON Principal
        key_hash = hashlib.sha256(key.encode()).hexdigest()[:32]
        data = await self.redis.get(f"apikey:{key_hash}")
        if not data:
            raise HTTPException(401, "Invalid API Key")
        import json
        return Principal(**json.loads(data))

    async def check_vertical_access(self, principal: Principal, vertical_id: str):
        if "*" not in principal.vertical_access and vertical_id not in principal.vertical_access:
            raise HTTPException(403, f"Access denied to vertical: {vertical_id}")

# --- Rate Limiter (Token Bucket per Tenant/User) ---
class RateLimiter:
    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client
        self.tiers = {
            "free": {"rpm": 10, "rpd": 100, "concurrent": 1},
            "pro": {"rpm": 60, "rpd": 1000, "concurrent": 5},
            "enterprise": {"rpm": 300, "rpd": 10000, "concurrent": 20},
        }

    async def check_limit(self, principal: Principal, cost: int = 1) -> bool:
        tier = self.tiers.get(principal.rate_limit_tier, self.tiers["free"])
        key_base = f"ratelimit:{principal.tenant_id}:{principal.user_id}"
        
        pipe = self.redis.pipeline()
        now = int(time.time())
        minute_key = f"{key_base}:min:{now // 60}"
        day_key = f"{key_base}:day:{now // 86400}"
        
        pipe.incrby(minute_key, cost)
        pipe.expire(minute_key, 120)
        pipe.incrby(day_key, cost)
        pipe.expire(day_key, 86400 * 2)
        results = await pipe.execute()
        
        if results[0] > tier["rpm"]:
            raise HTTPException(429, f"Rate limit exceeded: {tier['rpm']} req/min")
        if results[2] > tier["rpd"]:
            raise HTTPException(429, f"Daily quota exceeded: {tier['rpd']} req/day")
        return True

    async def check_concurrent(self, principal: Principal) -> bool:
        tier = self.tiers.get(principal.rate_limit_tier, self.tiers["free"])
        key = f"concurrent:{principal.tenant_id}"
        current = await self.redis.incr(key)
        if current == 1:
            await self.redis.expire(key, 3600)
        if current > tier["concurrent"]:
            await self.redis.decr(key)
            raise HTTPException(429, f"Concurrent run limit: {tier['concurrent']}")
        return True

    async def release_concurrent(self, principal: Principal):
        await self.redis.decr(f"concurrent:{principal.tenant_id}")

# --- FastAPI Dependencies ---
auth_manager = AuthManager()
rate_limiter = RateLimiter(redis.from_url(settings.redis.url))

async def get_principal(principal: Principal = Depends(auth_manager.authenticate)) -> Principal:
    return principal

async def check_rate_limit(principal: Principal = Depends(get_principal)):
    await rate_limiter.check_limit(principal)
    await rate_limiter.check_concurrent(principal)
    return principal

async def release_concurrent(principal: Principal = Depends(get_principal)):
    await rate_limiter.release_concurrent(principal)
