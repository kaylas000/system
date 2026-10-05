"""
``SandboxManager`` - quota / TTL / shutdown wrapper around any ``ISandbox`` backend.

Port of ``specs/02_infra/sandbox/SANDBOX_MANAGER.py::SandboxManager`` with
changes (``specs/ISSUES.md`` I-08):

* It *is* an ``ISandbox`` (delegating by ``sandbox_id``), so the kernel does not
  change; the spec's ``acquire()`` context manager did not fit the graph, where
  a sandbox lives across many nodes and HITL pauses.
* No warm reuse of sandboxes between runs: a reused workspace leaks files and
  secrets from the previous run. Warm start belongs to the sandbox image.
* Pool exhaustion waits (up to ``acquire_timeout``) instead of failing at once.
* Idle sandboxes (no call for ``ttl_minutes``) are closed by ``cleanup_expired``
  / the background loop - covers runs abandoned at a HITL pause.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path

from ..config import Settings, get_settings
from ..protocols import CommandResult, FileStat, ISandbox, SandboxSpec
from ..state import FileChange
from ._common import SandboxNotFoundError, SandboxQuotaError


class SandboxManager:
    def __init__(
        self,
        backend: ISandbox,
        *,
        max_concurrent: int = 20,
        ttl_minutes: float = 60,
        acquire_timeout: float = 60,
    ) -> None:
        self.backend = backend
        self.max_concurrent = max_concurrent
        self.ttl_sec = ttl_minutes * 60
        self.acquire_timeout = acquire_timeout
        self._slots = asyncio.Semaphore(max_concurrent)
        self._last_used: dict[str, float] = {}
        self._cleanup_task: asyncio.Task[None] | None = None

    # -- bookkeeping ---------------------------------------------------------------
    @property
    def active(self) -> list[str]:
        return list(self._last_used)

    def _touch(self, sandbox_id: str) -> None:
        if sandbox_id not in self._last_used:
            raise SandboxNotFoundError(sandbox_id)
        self._last_used[sandbox_id] = time.monotonic()

    async def cleanup_expired(self, now: float | None = None) -> list[str]:
        now = time.monotonic() if now is None else now
        expired = [sid for sid, ts in self._last_used.items() if now - ts > self.ttl_sec]
        for sid in expired:
            with contextlib.suppress(Exception):
                await self.close(sid)
        return expired

    async def _cleanup_loop(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            await self.cleanup_expired()

    def start(self, interval: float = 60) -> None:
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop(interval))

    async def stop(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task
            self._cleanup_task = None
        for sid in list(self._last_used):
            with contextlib.suppress(Exception):
                await self.close(sid)

    # -- ISandbox ------------------------------------------------------------------
    async def create(self, spec: SandboxSpec) -> str:
        try:
            await asyncio.wait_for(self._slots.acquire(), timeout=self.acquire_timeout)
        except TimeoutError:
            raise SandboxQuotaError(f"Sandbox quota exhausted ({self.max_concurrent} active)") from None
        try:
            sandbox_id = await self.backend.create(spec)
        except BaseException:
            self._slots.release()
            raise
        self._last_used[sandbox_id] = time.monotonic()
        return sandbox_id

    async def close(self, sandbox_id: str) -> None:
        if self._last_used.pop(sandbox_id, None) is None:
            await self.backend.close(sandbox_id)  # unknown to us (e.g. created before restart): best effort
            return
        try:
            await self.backend.close(sandbox_id)
        finally:
            self._slots.release()

    async def exec(
        self,
        sandbox_id: str,
        command: str,
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
    ) -> CommandResult:
        self._touch(sandbox_id)
        try:
            return await self.backend.exec(sandbox_id, command, workdir, env, timeout_sec)
        finally:
            if sandbox_id in self._last_used:
                self._last_used[sandbox_id] = time.monotonic()

    async def write_file(self, sandbox_id: str, path: str, content: str) -> None:
        self._touch(sandbox_id)
        await self.backend.write_file(sandbox_id, path, content)

    async def write_files(self, sandbox_id: str, files: list[FileChange], workdir: str = "/workspace") -> None:
        self._touch(sandbox_id)
        await self.backend.write_files(sandbox_id, files, workdir)

    async def read_file(self, sandbox_id: str, path: str) -> str:
        self._touch(sandbox_id)
        return await self.backend.read_file(sandbox_id, path)

    async def list_files(self, sandbox_id: str, path: str = "/workspace") -> list[FileStat]:
        self._touch(sandbox_id)
        return await self.backend.list_files(sandbox_id, path)

    async def download_dir(self, sandbox_id: str, src_path: str, dest_path: str) -> None:
        self._touch(sandbox_id)
        await self.backend.download_dir(sandbox_id, src_path, dest_path)

    async def get_preview_url(self, sandbox_id: str, port: int) -> str:
        self._touch(sandbox_id)
        return await self.backend.get_preview_url(sandbox_id, port)


def create_sandbox(settings: Settings | None = None, *, base_dir: str | Path | None = None) -> SandboxManager:
    """Build the configured backend (``settings.sandbox.provider``) wrapped in a ``SandboxManager``."""
    settings = settings or get_settings()
    s = settings.sandbox
    backend: ISandbox
    if s.provider == "e2b":
        from .e2b import E2BSandbox

        backend = E2BSandbox(settings)
    elif s.provider == "docker":
        from .docker import DockerSandbox

        backend = DockerSandbox(settings)
    elif s.provider == "local":
        from .local import LocalSandbox

        backend = LocalSandbox(base_dir)
    else:
        raise ValueError(f"Unsupported sandbox provider: {s.provider!r} (supported: e2b, docker, local)")
    return SandboxManager(backend, max_concurrent=s.max_concurrent, ttl_minutes=s.ttl_minutes)
