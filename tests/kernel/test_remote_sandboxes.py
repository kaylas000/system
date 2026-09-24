"""E2BSandbox / DockerSandbox against fakes, SandboxManager against LocalSandbox."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kernel.config import SandboxSection, Settings
from kernel.protocols import SandboxSpec
from kernel.sandbox import (
    DockerSandbox,
    E2BSandbox,
    LocalSandbox,
    SandboxManager,
    SandboxNotFoundError,
    SandboxQuotaError,
    create_sandbox,
)
from kernel.sandbox._common import parse_find_output
from kernel.state import FileChange

SPEC = SandboxSpec(image="img:1", env_vars={"A": "1"}, ports=[3000], timeout_sec=30)


# --------------------------------------------------------------------------- E2B
class CommandExitException(Exception):  # same name as the SDK's -> mapped by name
    def __init__(self, exit_code: int, stdout: str, stderr: str) -> None:
        super().__init__(stderr)
        self.exit_code, self.stdout, self.stderr = exit_code, stdout, stderr


class TimeoutException(Exception):
    pass


class FileNotFoundException(Exception):
    pass


class FakeBox:
    def __init__(self) -> None:
        self.sandbox_id = "sbx-1"
        self.runs: list[dict[str, Any]] = []
        self.files_store: dict[str, str | bytes] = {}
        self.killed = False
        self.commands = SimpleNamespace(run=self._run)
        self.files = SimpleNamespace(write=self._write, write_files=self._write_files, read=self._read)

    async def _run(self, cmd: str, **kw: Any) -> Any:
        self.runs.append({"cmd": cmd, **kw})
        if cmd.startswith("fail"):
            raise CommandExitException(2, "out", "err")
        if cmd.startswith("slow"):
            raise TimeoutException("deadline")
        if cmd.startswith("find"):
            return SimpleNamespace(
                exit_code=0, stdout="d\t4096\t1.5\t/workspace/src\nf\t3\t2.0\t/workspace/src/a\n", stderr=""
            )
        if cmd.startswith("tar"):
            self.files_store[cmd.split()[2]] = b"TGZ"
        return SimpleNamespace(exit_code=0, stdout="ok", stderr="")

    async def _write(self, path: str, data: str) -> None:
        self.files_store[path] = data

    async def _write_files(self, entries: list[dict[str, Any]]) -> None:
        for e in entries:
            self.files_store[e["path"]] = e["data"]

    async def _read(self, path: str, format: str = "text") -> Any:
        if path not in self.files_store:
            raise FileNotFoundException(path)
        return self.files_store[path]

    def get_host(self, port: int) -> str:
        return f"{port}-sbx-1.e2b.app"

    async def kill(self) -> None:
        self.killed = True


async def test_e2b_sandbox(tmp_path: Path) -> None:
    box = FakeBox()
    created: dict[str, Any] = {}

    async def create_fn(**kw: Any) -> FakeBox:
        created.update(kw)
        return box

    settings = Settings(sandbox=SandboxSection(provider="e2b", e2b_template_map={"img:1": "tpl-web"}, ttl_minutes=5))
    sb = E2BSandbox(settings, create_fn=create_fn)
    sid = await sb.create(SPEC)
    assert sid == "sbx-1"
    assert created["template"] == "tpl-web" and created["timeout"] == 300 and created["envs"] == {"A": "1"}
    assert box.runs[0]["user"] == "root" and "mkdir -p /workspace" in box.runs[0]["cmd"]

    ok = await sb.exec(sid, "echo", workdir="/workspace/app", env={"X": "y"})
    assert (ok.exit_code, ok.stdout) == (0, "ok")
    assert box.runs[-1]["cwd"] == "/workspace/app" and box.runs[-1]["envs"] == {"X": "y"}
    assert box.runs[-1]["timeout"] == 30  # spec.timeout_sec
    bad = await sb.exec(sid, "fail now")
    assert (bad.exit_code, bad.stdout, bad.stderr) == (2, "out", "err")
    slow = await sb.exec(sid, "slow", timeout_sec=1)
    assert slow.exit_code == 124

    await sb.write_files(
        sid,
        [FileChange(path="src/a.ts", content="x", action="create"), FileChange(path="old", action="delete")],
    )
    assert box.files_store["/workspace/src/a.ts"] == "x"
    assert any(r["cmd"] == "rm -rf -- /workspace/old" for r in box.runs)
    assert await sb.read_file(sid, "/workspace/src/a.ts") == "x"
    with pytest.raises(FileNotFoundError):
        await sb.read_file(sid, "/nope")

    stats = await sb.list_files(sid)
    assert [(s.path, s.is_dir, s.size) for s in stats] == [
        ("/workspace/src", True, 4096),
        ("/workspace/src/a", False, 3),
    ]

    dest = tmp_path / "out" / "a.tar.gz"
    await sb.download_dir(sid, "/workspace", str(dest))
    assert dest.read_bytes() == b"TGZ"
    assert await sb.get_preview_url(sid, 3000) == "https://3000-sbx-1.e2b.app"

    await sb.close(sid)
    assert box.killed
    with pytest.raises(SandboxNotFoundError):
        await sb.exec(sid, "echo")


def test_parse_find_output_skips_garbage() -> None:
    out = parse_find_output("f\t1\t1.0\t/w/b\nbroken line\nf\tx\t1\t/w/c\nd\t0\t1.0\t/w/a\n")
    assert [s.path for s in out] == ["/w/a", "/w/b"]


# ------------------------------------------------------------------------ Docker
class FakeDocker:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bytes | None]] = []

    async def __call__(self, argv: list[str], stdin: bytes | None, limit_sec: float | None) -> tuple[int, bytes, bytes]:
        self.calls.append((argv, stdin))
        sub = argv[1]
        if sub == "exec" and argv[-1] == "missing":
            return 1, b"", b"cat: missing: No such file"
        if sub == "exec" and "cat" in argv:
            return 0, b"content", b""
        if sub == "exec" and "tar" in argv:
            return 0, b"TGZ", b""
        if sub == "port":
            return 0, b"127.0.0.1:49153\n", b""
        return 0, b"", b""


async def test_docker_sandbox(tmp_path: Path) -> None:
    fake = FakeDocker()
    sb = DockerSandbox(Settings(), runner=fake)
    sid = await sb.create(SPEC)
    run_argv = fake.calls[0][0]
    assert run_argv[:3] == ["docker", "run", "-d"] and "img:1" in run_argv
    assert ["-e", "A=1"] == run_argv[run_argv.index("-e") : run_argv.index("-e") + 2]
    assert "127.0.0.1::3000" in run_argv and "--cap-drop" in run_argv
    assert fake.calls[1][0][-3:] == ["mkdir", "-p", "/workspace"]

    await sb.exec(sid, "npm test && echo 'x'", env={"CI": "1"})
    argv = fake.calls[-1][0]
    assert argv[:4] == ["docker", "exec", "-w", "/workspace"] and ["-e", "CI=1"] == argv[4:6]
    assert argv[-7:] == ["timeout", "-k", "5", "30", "sh", "-c", "npm test && echo 'x'"]

    await sb.write_files(sid, [FileChange(path="dir with space/a.txt", content="hé", action="create")])
    argv, stdin = fake.calls[-1]
    assert argv[-1] == "mkdir -p '/workspace/dir with space' && cat > '/workspace/dir with space/a.txt'"
    assert stdin == "hé".encode()
    with pytest.raises(PermissionError):
        await sb.write_files(sid, [FileChange(path="../etc/passwd", content="", action="create")])

    assert await sb.read_file(sid, "/workspace/a") == "content"
    with pytest.raises(FileNotFoundError):
        await sb.read_file(sid, "missing")
    dest = tmp_path / "x.tgz"
    await sb.download_dir(sid, "/workspace", str(dest))
    assert dest.read_bytes() == b"TGZ"
    assert await sb.get_preview_url(sid, 3000) == "http://127.0.0.1:49153"
    await sb.close(sid)
    assert fake.calls[-1][0] == ["docker", "rm", "-f", sid]


# ----------------------------------------------------------------------- Manager
async def test_manager_quota_ttl_and_stop(tmp_path: Path) -> None:
    mgr = SandboxManager(LocalSandbox(tmp_path), max_concurrent=2, ttl_minutes=1, acquire_timeout=0.05)
    a = await mgr.create(SPEC)
    b = await mgr.create(SPEC)
    with pytest.raises(SandboxQuotaError):
        await mgr.create(SPEC)

    await mgr.close(a)  # frees a slot
    c = await mgr.create(SPEC)
    res = await mgr.exec(c, "echo hi")
    assert res.stdout.strip() == "hi"

    expired = await mgr.cleanup_expired(now=mgr._last_used[b] + 61)
    assert set(expired) == {b, c}
    assert mgr.active == []
    with pytest.raises(SandboxNotFoundError):
        await mgr.exec(b, "echo")

    d = await mgr.create(SPEC)
    mgr.start(interval=0.01)
    await asyncio.sleep(0.02)
    await mgr.stop()
    assert mgr.active == [] and d not in mgr.backend._roots  # type: ignore[attr-defined]


async def test_manager_waits_for_free_slot(tmp_path: Path) -> None:
    mgr = SandboxManager(LocalSandbox(tmp_path), max_concurrent=1, acquire_timeout=2)
    a = await mgr.create(SPEC)
    waiter = asyncio.create_task(mgr.create(SPEC))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    await mgr.close(a)
    b = await asyncio.wait_for(waiter, 1)
    assert mgr.active == [b]
    await mgr.stop()


def test_create_sandbox_factory(tmp_path: Path) -> None:
    for provider, cls in (("local", LocalSandbox), ("docker", DockerSandbox), ("e2b", E2BSandbox)):
        mgr = create_sandbox(Settings(sandbox=SandboxSection(provider=provider, max_concurrent=3)), base_dir=tmp_path)
        assert isinstance(mgr.backend, cls) and mgr.max_concurrent == 3
    with pytest.raises(ValueError, match="daytona"):
        create_sandbox(Settings(sandbox=SandboxSection(provider="daytona")))
