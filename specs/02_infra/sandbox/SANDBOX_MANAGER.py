# specs/02_infra/sandbox/SANDBOX_MANAGER.py
"""
Sandbox Pool Manager.
Handles: Creation, Reuse (Session affinity), Health Checks, Cleanup, Quotas.
"""

from __future__ import annotations
import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Optional, List, AsyncGenerator
from contextlib import asynccontextmanager

from kernel.protocols import ISandbox, SandboxSpec, CommandResult, FileStat, FileChange
from kernel.config import settings

# --- Provider Implementations (Abstracted) ---


class E2BSandbox(ISandbox):
    """Wrapper around e2b_code_interpreter.Sandbox"""

    def __init__(self, sandbox_id: str, sandbox_obj: Any):  # Any = e2b.Sandbox
        self.sandbox_id = sandbox_id
        self._sandbox = sandbox_obj
        self._created_at = datetime.utcnow()

    @classmethod
    async def create(cls, spec: SandboxSpec) -> "E2BSandbox":
        from e2b_code_interpreter import Sandbox as E2BSandboxClient

        # Map our spec to E2B params
        sbx = await asyncio.to_thread(
            E2BSandboxClient.create,
            template=spec.image,  # E2B uses template IDs
            timeout=spec.timeout_sec,
            env_vars=spec.env_vars,
        )
        return cls(sbx.sandbox_id, sbx)

    async def close(self) -> None:
        await asyncio.to_thread(self._sandbox.close)

    async def exec(
        self, command: str, workdir: str = "/workspace", env: Optional[Dict] = None, stream: bool = False
    ) -> CommandResult:
        # E2B supports streaming via .start() but we use blocking for simplicity here
        proc = await asyncio.to_thread(self._sandbox.commands.run, command, cwd=workdir, env=env)
        return CommandResult(
            exit_code=proc.exit_code, stdout=proc.stdout, stderr=proc.stderr, duration_ms=0
        )  # E2B doesn't give duration easily

    async def write_file(self, path: str, content: str) -> None:
        await asyncio.to_thread(self._sandbox.files.write, path, content)

    async def write_files(self, files: List[FileChange]) -> None:
        # Batch write via tar upload is faster
        import tarfile, io

        tar_bytes = io.BytesIO()
        with tarfile.open(fileobj=tar_bytes, mode="w") as tar:
            for f in files:
                data = f.content.encode()
                info = tarfile.TarInfo(name=f.path)
                info.size = len(data)
                info.mtime = int(datetime.utcnow().timestamp())
                tar.addfile(info, io.BytesIO(data))
        tar_bytes.seek(0)
        await asyncio.to_thread(self._sandbox.files.write_bytes, tar_bytes.read(), "/workspace")  # Extract at root

    async def read_file(self, path: str) -> str:
        return await asyncio.to_thread(self._sandbox.files.read, path)

    async def list_files(self, path: str = "/workspace") -> List[FileStat]:
        entries = await asyncio.to_thread(self._sandbox.files.list, path)
        return [FileStat(path=e.name, size=e.size, is_dir=e.is_dir, modified_at=e.mtime) for e in entries]

    async def download_dir(self, src_path: str, dest_path: str) -> None:
        # E2B: download as zip
        zip_bytes = await asyncio.to_thread(self._sandbox.download, src_path)
        with open(dest_path, "wb") as f:
            f.write(zip_bytes)

    async def get_preview_url(self, port: int) -> str:
        return self._sandbox.get_host(port)


# --- Pool Manager ---


@dataclass
class SandboxSlot:
    sandbox: ISandbox
    spec: SandboxSpec
    created_at: datetime
    last_used: datetime
    in_use: bool = False
    vertical_id: Optional[str] = None  # Affinity: reuse sandbox for same vertical (warm caches)


class SandboxManager:
    def __init__(self, max_pool_size: int = 20, ttl_minutes: int = 30):
        self._pool: Dict[str, SandboxSlot] = {}
        self._lock = asyncio.Lock()
        self._max_pool = max_pool_size
        self._ttl = timedelta(minutes=ttl_minutes)
        self._cleanup_task: Optional[asyncio.Task] = None

    async def start(self):
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self):
        if self._cleanup_task:
            self._cleanup_task.cancel()
        async with self._lock:
            for slot in self._pool.values():
                await slot.sandbox.close()
            self._pool.clear()

    @asynccontextmanager
    async def acquire(self, vertical_id: str, spec: SandboxSpec) -> AsyncGenerator[ISandbox, None]:
        """Get sandbox from pool or create new. Returns context manager for auto-release."""
        sandbox_id = None
        async with self._lock:
            # 1. Try find warm sandbox for this vertical
            for sid, slot in self._pool.items():
                if not slot.in_use and slot.vertical_id == vertical_id and slot.spec == spec:
                    slot.in_use = True
                    slot.last_used = datetime.utcnow()
                    sandbox_id = sid
                    break

            # 2. Create new if pool not full
            if not sandbox_id and len(self._pool) < self._max_pool:
                sbx = await E2BSandbox.create(spec)  # TODO: Factory pattern for providers
                slot = SandboxSlot(
                    sandbox=sbx,
                    spec=spec,
                    created_at=datetime.utcnow(),
                    last_used=datetime.utcnow(),
                    in_use=True,
                    vertical_id=vertical_id,
                )
                self._pool[sbx.sandbox_id] = slot
                sandbox_id = sbx.sandbox_id

            # 3. Wait for release (simplified: raise error if exhausted)
            if not sandbox_id:
                raise RuntimeError("Sandbox pool exhausted")

        try:
            yield self._pool[sandbox_id].sandbox
        finally:
            async with self._lock:
                if sandbox_id in self._pool:
                    self._pool[sandbox_id].in_use = False
                    self._pool[sandbox_id].last_used = datetime.utcnow()

    async def _cleanup_loop(self):
        while True:
            await asyncio.sleep(60)
            now = datetime.utcnow()
            async with self._lock:
                to_remove = [
                    sid for sid, slot in self._pool.items() if not slot.in_use and (now - slot.last_used) > self._ttl
                ]
                for sid in to_remove:
                    await self._pool[sid].sandbox.close()
                    del self._pool[sid]


# Global Singleton (initialized in Kernel main)
sandbox_manager: Optional[SandboxManager] = None
