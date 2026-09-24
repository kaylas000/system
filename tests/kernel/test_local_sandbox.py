from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from kernel.protocols import SandboxSpec
from kernel.sandbox import LocalSandbox, SandboxNotFoundError
from kernel.state import FileChange


async def test_lifecycle(tmp_path: Path) -> None:
    sb = LocalSandbox(tmp_path)
    sid = await sb.create(SandboxSpec(image="x", env_vars={"GREETING": "hi"}))
    await sb.write_files(sid, [FileChange(path="src/a.txt", content="alpha", action="create")], "/workspace")
    assert await sb.read_file(sid, "/workspace/src/a.txt") == "alpha"

    res = await sb.exec(sid, 'cat src/a.txt && echo " $GREETING" && echo err >&2', workdir="/workspace")
    assert res.exit_code == 0 and res.stdout.strip() == "alpha hi" and res.stderr.strip() == "err"
    assert (await sb.exec(sid, "exit 3")).exit_code == 3

    files = {f.path for f in await sb.list_files(sid)}
    assert {"/workspace/src", "/workspace/src/a.txt"} <= files

    await sb.write_files(sid, [FileChange(path="src/a.txt", action="delete")])
    assert not (sb.root(sid) / "workspace/src/a.txt").exists()

    await sb.write_file(sid, "/workspace/b.txt", "beta")
    dest = tmp_path / "out" / "a.tar.gz"
    await sb.download_dir(sid, "/workspace", str(dest))
    with tarfile.open(dest) as tar:
        assert "./b.txt" in tar.getnames()

    assert await sb.get_preview_url(sid, 3000) == "http://localhost:3000"
    await sb.close(sid)
    with pytest.raises(SandboxNotFoundError):
        await sb.read_file(sid, "/workspace/b.txt")


async def test_timeout(tmp_path: Path) -> None:
    sb = LocalSandbox(tmp_path)
    sid = await sb.create(SandboxSpec(image="x"))
    res = await sb.exec(sid, "sleep 5", timeout_sec=1)
    assert res.exit_code == 124 and "timeout" in res.stderr
    await sb.close(sid)


async def test_path_escape_blocked(tmp_path: Path) -> None:
    sb = LocalSandbox(tmp_path)
    sid = await sb.create(SandboxSpec(image="x"))
    with pytest.raises(PermissionError):
        await sb.write_file(sid, "/../../etc/evil", "x")
    with pytest.raises(PermissionError):  # symlink escape
        sb.root(sid).joinpath("link").symlink_to("/")
        await sb.read_file(sid, "/link/etc/hostname")
    await sb.close(sid)
