"""
``LocalSandbox`` - DEVELOPMENT/TEST ONLY implementation of ``ISandbox``.

Runs commands on the host inside a temp directory, NO isolation. Sandbox paths
are mapped into the temp root: ``/workspace/x`` -> ``<root>/workspace/x``.
Production must use E2B / Daytona / Docker (phase 2).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tarfile
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath

from ..protocols import CommandResult, FileStat, SandboxSpec
from ..state import FileChange

OUTPUT_LIMIT = 200_000


class SandboxNotFoundError(KeyError):
    pass


class LocalSandbox:
    def __init__(self, base_dir: str | Path | None = None) -> None:
        self._base_dir = Path(base_dir) if base_dir else None
        self._roots: dict[str, Path] = {}
        self._specs: dict[str, SandboxSpec] = {}

    # -- helpers -------------------------------------------------------------------
    def root(self, sandbox_id: str) -> Path:
        try:
            return self._roots[sandbox_id]
        except KeyError:
            raise SandboxNotFoundError(sandbox_id) from None

    def host_path(self, sandbox_id: str, path: str) -> Path:
        """Map an absolute sandbox path onto the host, refusing escapes."""
        root = self.root(sandbox_id).resolve()
        rel = PurePosixPath("/", path).relative_to("/")
        target = (root / rel).resolve()
        if target != root and root not in target.parents:
            raise PermissionError(f"Path escapes sandbox: {path}")
        return target

    # -- ISandbox ------------------------------------------------------------------
    async def create(self, spec: SandboxSpec) -> str:
        sandbox_id = f"local-{uuid.uuid4().hex[:12]}"
        if self._base_dir:
            self._base_dir.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix=f"{sandbox_id}-", dir=self._base_dir))
        (root / "workspace").mkdir()
        self._roots[sandbox_id] = root
        self._specs[sandbox_id] = spec
        return sandbox_id

    async def close(self, sandbox_id: str) -> None:
        root = self._roots.pop(sandbox_id, None)
        self._specs.pop(sandbox_id, None)
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)

    async def exec(
        self,
        sandbox_id: str,
        command: str,
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
    ) -> CommandResult:
        spec = self._specs[sandbox_id] if sandbox_id in self._specs else None
        cwd = self.host_path(sandbox_id, workdir)
        cwd.mkdir(parents=True, exist_ok=True)
        full_env = {**os.environ, **(spec.env_vars if spec else {}), **(env or {})}
        timeout = timeout_sec or (spec.timeout_sec if spec else 300)
        start = time.perf_counter()
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            env=full_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            code = proc.returncode if proc.returncode is not None else -1
        except TimeoutError:
            proc.kill()
            out, err = await proc.communicate()
            code = 124
            err += f"\n[timeout after {timeout}s]".encode()
        return CommandResult(
            exit_code=code,
            stdout=out.decode(errors="replace")[-OUTPUT_LIMIT:],
            stderr=err.decode(errors="replace")[-OUTPUT_LIMIT:],
            duration_ms=int((time.perf_counter() - start) * 1000),
        )

    async def write_file(self, sandbox_id: str, path: str, content: str) -> None:
        target = self.host_path(sandbox_id, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    async def write_files(self, sandbox_id: str, files: list[FileChange], workdir: str = "/workspace") -> None:
        for change in files:
            full = str(PurePosixPath(workdir) / change.path)
            if change.action == "delete":
                target = self.host_path(sandbox_id, full)
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink(missing_ok=True)
            else:
                await self.write_file(sandbox_id, full, change.content)

    async def read_file(self, sandbox_id: str, path: str) -> str:
        return self.host_path(sandbox_id, path).read_text(encoding="utf-8")

    async def list_files(self, sandbox_id: str, path: str = "/workspace") -> list[FileStat]:
        base = self.host_path(sandbox_id, path)
        root = self.root(sandbox_id).resolve()
        stats: list[FileStat] = []
        for p in sorted(base.rglob("*")):
            st = p.stat()
            stats.append(
                FileStat(
                    path="/" + p.relative_to(root).as_posix(),
                    size=st.st_size,
                    is_dir=p.is_dir(),
                    modified_at=st.st_mtime,
                )
            )
        return stats

    async def download_dir(self, sandbox_id: str, src_path: str, dest_path: str) -> None:
        src = self.host_path(sandbox_id, src_path)
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)

        def _pack() -> None:
            with tarfile.open(dest, "w:gz") as tar:
                tar.add(src, arcname=".")

        await asyncio.to_thread(_pack)

    async def get_preview_url(self, sandbox_id: str, port: int) -> str:
        self.root(sandbox_id)
        return f"http://localhost:{port}"
