"""
Completion webhooks (``GenerateRequest.webhook_url``; written by the agent).

* SSRF guard: only http(s); every resolved address must be public unless ``gateway.allow_private_webhooks``
  (checked when the run is created and again before each delivery — DNS can change).
* Delivery is best effort in the background: 3 attempts with backoff; 2xx = delivered.
* Body: ``{"event": "done"|"error"|"interrupt"|"cancelled", "run_id", "vertical_id", "data", "ts"}``;
  with ``gateway.webhook_secret`` the header ``X-Autogen-Signature: sha256=<hex HMAC of the body>``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
from typing import Any
from urllib.parse import urlsplit

from kernel.config import Settings
from kernel.observability import metrics as m

logger = logging.getLogger(__name__)


class WebhookURLError(ValueError):
    pass


async def validate_webhook_url(url: str, allow_private: bool = False) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise WebhookURLError("webhook_url must be an absolute http(s) URL")
    if parts.username or parts.password:
        raise WebhookURLError("credentials in webhook_url are not allowed")
    if allow_private:
        return
    host = parts.hostname
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, parts.port or 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise WebhookURLError(f"webhook host {host!r} does not resolve") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise WebhookURLError(f"webhook host {host!r} resolves to a non-public address")


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class WebhookSender:
    def __init__(self, settings: Settings, attempts: int = 3, backoff: float = 1.0, timeout: float = 10.0) -> None:
        self.settings = settings
        self.attempts = attempts
        self.backoff = backoff
        self.timeout = timeout
        self._tasks: set[asyncio.Task[bool]] = set()

    def dispatch(self, url: str, payload: dict[str, Any]) -> asyncio.Task[bool]:
        task = asyncio.create_task(self.send(url, payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def send(self, url: str, payload: dict[str, Any]) -> bool:
        import httpx

        cfg = self.settings.gateway
        try:
            await validate_webhook_url(url, cfg.allow_private_webhooks)
        except WebhookURLError as exc:
            logger.warning("webhook skipped: %s", exc)
            m.WEBHOOKS.labels(status="rejected").inc()
            return False
        body = json.dumps(payload, default=str).encode()
        headers = {"Content-Type": "application/json", "User-Agent": "autogen-gateway"}
        if cfg.webhook_secret is not None:
            headers["X-Autogen-Signature"] = sign(cfg.webhook_secret.get_secret_value(), body)
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
            for attempt in range(self.attempts):
                try:
                    resp = await client.post(url, content=body, headers=headers)
                    if 200 <= resp.status_code < 300:
                        m.WEBHOOKS.labels(status="delivered").inc()
                        return True
                    logger.warning("webhook %s -> HTTP %s", url, resp.status_code)
                except httpx.HTTPError as exc:
                    logger.warning("webhook %s failed: %s", url, exc)
                if attempt + 1 < self.attempts:
                    await asyncio.sleep(self.backoff * 2**attempt)
        m.WEBHOOKS.labels(status="failed").inc()
        return False

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
