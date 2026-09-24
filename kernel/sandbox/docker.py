"""
``DockerSandbox`` - ``ISandbox`` backed by local Docker containers (docker CLI).

For self-hosted deployments without E2B. One long-running container per
sandbox (``sleep infinity``), commands via ``docker exec``. Uses the CLI rather
than docker-py to avoid another dependency; the command runner is injectable
for tests.

Security defaults: ``--cap-drop ALL`` + the few caps package managers need, ``no-new-privileges``, pids/cpu/memory
limits. Network is ``settings.sandbox.docker_network`` (package installs need
egress; use a filtered network in production).
"""

from __future__ import annotations

import asyncio
import shlex
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath

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

# runner(argv, stdin, timeout) -> (exit_code, stdout, stderr); exit 124 on timeout
Runner = Callable[[list[str], bytes | None, float | None], Awaitable[tuple[int, bytes, bytes]]]


async def subprocess_runner(argv: list[str], stdin: bytes | None, limit_sec: float | None) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=limit_sec)
    except TimeoutError:
        proc.kill()
        out, err = await proc.communicate()
        return TIMEOUT_EXIT_CODE, out, err + f"\n[timeout after {limit_sec}s]".encode()
    return (proc.returncode if proc.returncode is not None else -1), out, err


class DockerSandbox:
    def __init__(self, settings: Settings | None = None, *, runner: Runner | None = None, docker: str = "docker"):
        self.settings = settings or get_settings()
        self._run = runner or subprocess_runner
        self._docker = docker
        self._containers: dict[str, str] = {}
        self._specs: dict[str, SandboxSpec] = {}

    def _container(self, sandbox_id: str) -> str:
        try:
            return self._containers[sandbox_id]
        except KeyError:
            raise SandboxNotFoundError(sandbox_id) from None

    async def _docker_cmd(self, *args: str, stdin: bytes | None = None, limit_sec: float | None = 120) -> bytes:
        code, out, err = await self._run([self._docker, *args], stdin, limit_sec)
        if code != 0:
            raise RuntimeError(f"docker {args[0]} failed ({code}): {err.decode(errors='replace').strip()}")
        return out

    # -- ISandbox ------------------------------------------------------------------
    async def create(self, spec: SandboxSpec) -> str:
        s = self.settings.sandbox
        sandbox_id = f"docker-{uuid.uuid4().hex[:12]}"
        argv = [
            "run", "-d", "--name", sandbox_id,
            "--label", "agentic-platform=sandbox",
            "--cpus", str(spec.cpu),
            "--memory", f"{spec.memory_mb}m",
            "--pids-limit", "1024",
            "--cap-drop", "ALL",
            *[a for cap in ("CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID") for a in ("--cap-add", cap)],
            "--security-opt", "no-new-privileges",
            "--network", s.docker_network,
        ]  # fmt: skip
        for key, value in spec.env_vars.items():
            argv += ["-e", f"{key}={value}"]
        for port in spec.ports:
            argv += ["-p", f"127.0.0.1::{port}"]
        argv += [spec.image, "sleep", "infinity"]
        await self._docker_cmd(*argv)
        self._containers[sandbox_id] = sandbox_id
        self._specs[sandbox_id] = spec
        await self._docker_cmd("exec", sandbox_id, "mkdir", "-p", s.workspace_path)
        return sandbox_id

    async def close(self, sandbox_id: str) -> None:
        container = self._containers.pop(sandbox_id, None)
        self._specs.pop(sandbox_id, None)
        if container is not None:
            await self._run([self._docker, "rm", "-f", container], None, 60)

    async def exec(
        self,
        sandbox_id: str,
        command: str,
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
    ) -> CommandResult:
        container = self._container(sandbox_id)
        spec = self._specs.get(sandbox_id)
        timeout = timeout_sec or (spec.timeout_sec if spec else self.settings.sandbox.timeout_sec)
        argv = [self._docker, "exec", "-w", workdir]
        for key, value in (env or {}).items():
            argv += ["-e", f"{key}={value}"]
        # `timeout` inside the container kills the process tree (killing `docker exec` alone would not)
        argv += [container, "timeout", "-k", "5", str(timeout), "sh", "-c", command]
        start = time.perf_counter()
        code, out, err = await self._run(argv, None, timeout + 15)
        return CommandResult(
            exit_code=code,
            stdout=truncate(out.decode(errors="replace")),
            stderr=truncate(err.decode(errors="replace")),
            duration_ms=int((time.perf_counter() - start) * 1000),
        )

    async def write_file(self, sandbox_id: str, path: str, content: str) -> None:
        container = self._container(sandbox_id)
        parent = str(PurePosixPath(path).parent)
        script = f"mkdir -p {shlex.quote(parent)} && cat > {shlex.quote(path)}"
        await self._docker_cmd("exec", "-i", container, "sh", "-c", script, stdin=content.encode())

    async def write_files(self, sandbox_id: str, files: list[FileChange], workdir: str = "/workspace") -> None:
        deletes = [join_workdir(workdir, c) for c in files if c.action == "delete"]
        if deletes:
            container = self._container(sandbox_id)
            await self._docker_cmd("exec", container, "rm", "-rf", "--", *deletes)
        for change in files:
            if change.action != "delete":
                await self.write_file(sandbox_id, join_workdir(workdir, change), change.content)

    async def read_file(self, sandbox_id: str, path: str) -> str:
        container = self._container(sandbox_id)
        code, out, err = await self._run([self._docker, "exec", container, "cat", "--", path], None, 60)
        if code != 0:
            raise FileNotFoundError(f"{path}: {err.decode(errors='replace').strip()}")
        return out.decode("utf-8")

    async def list_files(self, sandbox_id: str, path: str = "/workspace") -> list[FileStat]:
        res = await self.exec(sandbox_id, find_command(path), workdir="/")
        if res.exit_code != 0:
            raise FileNotFoundError(f"{path}: {res.stderr.strip()}")
        return parse_find_output(res.stdout)

    async def download_dir(self, sandbox_id: str, src_path: str, dest_path: str) -> None:
        container = self._container(sandbox_id)
        data = await self._docker_cmd("exec", container, "tar", "-czf", "-", "-C", src_path, ".", limit_sec=600)
        await asyncio.to_thread(write_local, Path(dest_path), data)

    async def get_preview_url(self, sandbox_id: str, port: int) -> str:
        container = self._container(sandbox_id)
        out = (await self._docker_cmd("port", container, str(port))).decode().strip().splitlines()
        if not out:
            raise RuntimeError(f"Port {port} is not published for {sandbox_id}")
        return f"http://{out[0].strip()}"
