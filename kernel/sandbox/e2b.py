"""
``E2BSandbox`` - ``ISandbox`` backed by E2B cloud micro-VMs (e2b SDK v2, async API).

Port of ``specs/02_infra/sandbox/SANDBOX_MANAGER.py::E2BSandbox`` with fixes
(``specs/ISSUES.md`` I-07): the spec used ``e2b_code_interpreter`` sync calls
through ``to_thread``, the non-existent ``env_vars=``/``files.write_bytes``/
``download``/``close`` APIs, wrote a tar archive as a single file, and treated a
non-zero exit code as an exception. Here: native ``AsyncSandbox``, one instance
per sandbox id, ``CommandExitException`` mapped to ``CommandResult``.
"""

from __future__ import annotations

import asyncio
import shlex
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..config import Settings, get_settings
from ..protocols import CommandResult, FileStat, SandboxSpec
from ..state import FileChange
from ._common import (
    TIMEOUT_EXIT_CODE,
    SandboxNotFoundError,
    find_command,
    join_workdir,
    parse_find_output,
    truncate,
    write_local,
)

CreateFn = Callable[..., Awaitable[Any]]


class E2BSandbox:
    def __init__(self, settings: Settings | None = None, *, create_fn: CreateFn | None = None) -> None:
        self.settings = settings or get_settings()
        self._create_fn = create_fn
        self._boxes: dict[str, Any] = {}
        self._specs: dict[str, SandboxSpec] = {}

    # -- helpers -------------------------------------------------------------------
    def _sdk(self) -> Any:
        try:
            import e2b
        except ImportError as exc:  # pragma: no cover - optional extra
            raise RuntimeError("E2BSandbox requires `pip install .[e2b]`") from exc
        return e2b

    def _box(self, sandbox_id: str) -> Any:
        try:
            return self._boxes[sandbox_id]
        except KeyError:
            raise SandboxNotFoundError(sandbox_id) from None

    def template_for(self, image: str) -> str | None:
        s = self.settings.sandbox
        return s.e2b_template_map.get(image, s.e2b_default_template)

    # -- ISandbox ------------------------------------------------------------------
    async def create(self, spec: SandboxSpec) -> str:
        s = self.settings.sandbox
        kwargs: dict[str, Any] = {
            "template": self.template_for(spec.image),
            "timeout": s.ttl_minutes * 60,  # E2B `timeout` = sandbox lifetime, not per-command
            "envs": dict(spec.env_vars),
            "metadata": {"image": spec.image, "owner": "agentic-platform"},
        }
        if s.api_key is not None:
            kwargs["api_key"] = s.api_key.get_secret_value()
        create = self._create_fn or self._sdk().AsyncSandbox.create
        box = await create(**kwargs)
        sandbox_id = str(getattr(box, "sandbox_id", None) or f"e2b-{uuid.uuid4().hex[:12]}")
        self._boxes[sandbox_id] = box
        self._specs[sandbox_id] = spec
        ws = shlex.quote(s.workspace_path)
        await box.commands.run(f"mkdir -p {ws} && chmod 777 {ws}", user="root", timeout=60)
        return sandbox_id

    async def close(self, sandbox_id: str) -> None:
        box = self._boxes.pop(sandbox_id, None)
        self._specs.pop(sandbox_id, None)
        if box is not None:
            await box.kill()

    async def exec(
        self,
        sandbox_id: str,
        command: str,
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
    ) -> CommandResult:
        box = self._box(sandbox_id)
        spec = self._specs.get(sandbox_id)
        timeout = timeout_sec or (spec.timeout_sec if spec else self.settings.sandbox.timeout_sec)
        start = time.perf_counter()
        try:
            res = await box.commands.run(command, cwd=workdir, envs=env or None, timeout=timeout)
            code, out, err = int(res.exit_code), res.stdout, res.stderr
        except Exception as exc:  # e2b raises on non-zero exit and on timeout
            name = type(exc).__name__
            if name == "CommandExitException" or hasattr(exc, "exit_code"):
                code, out, err = int(exc.exit_code), exc.stdout or "", exc.stderr or ""  # type: ignore[attr-defined]
            elif name == "TimeoutException":
                code, out, err = TIMEOUT_EXIT_CODE, "", f"[timeout after {timeout}s] {exc}"
            else:
                raise
        return CommandResult(
            exit_code=code,
            stdout=truncate(out),
            stderr=truncate(err),
            duration_ms=int((time.perf_counter() - start) * 1000),
        )

    async def write_file(self, sandbox_id: str, path: str, content: str) -> None:
        await self._box(sandbox_id).files.write(path, content)  # creates parent dirs

    async def write_files(self, sandbox_id: str, files: list[FileChange], workdir: str = "/workspace") -> None:
        box = self._box(sandbox_id)
        deletes = [join_workdir(workdir, c) for c in files if c.action == "delete"]
        writes = [(join_workdir(workdir, c), c.content) for c in files if c.action != "delete"]
        if deletes:
            await self.exec(sandbox_id, "rm -rf -- " + " ".join(shlex.quote(p) for p in deletes), workdir="/")
        if writes:
            # e2b WriteEntry is a TypedDict {"path", "data"}
            await box.files.write_files([{"path": p, "data": d} for p, d in writes])

    async def read_file(self, sandbox_id: str, path: str) -> str:
        try:
            return str(await self._box(sandbox_id).files.read(path, format="text"))
        except Exception as exc:
            if type(exc).__name__ in ("FileNotFoundException", "NotFoundException"):
                raise FileNotFoundError(path) from exc
            raise

    async def list_files(self, sandbox_id: str, path: str = "/workspace") -> list[FileStat]:
        res = await self.exec(sandbox_id, find_command(path), workdir="/")
        if res.exit_code != 0:
            raise FileNotFoundError(f"{path}: {res.stderr.strip()}")
        return parse_find_output(res.stdout)

    async def download_dir(self, sandbox_id: str, src_path: str, dest_path: str) -> None:
        box = self._box(sandbox_id)
        remote = f"/tmp/artifact-{uuid.uuid4().hex[:8]}.tar.gz"
        res = await self.exec(sandbox_id, f"tar -czf {remote} -C {shlex.quote(src_path)} .", workdir="/")
        if res.exit_code != 0:
            raise RuntimeError(f"tar failed in sandbox: {res.stderr.strip()}")
        data = await box.files.read(remote, format="bytes")
        await asyncio.to_thread(write_local, Path(dest_path), bytes(data))
        await self.exec(sandbox_id, f"rm -f {remote}", workdir="/")

    async def get_preview_url(self, sandbox_id: str, port: int) -> str:
        host = str(self._box(sandbox_id).get_host(port))
        return host if host.startswith("http") else f"https://{host}"
