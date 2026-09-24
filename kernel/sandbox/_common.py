"""Helpers shared by remote sandbox backends (E2B, Docker)."""

from __future__ import annotations

import shlex
from pathlib import Path, PurePosixPath

from ..protocols import FileStat
from ..state import FileChange

OUTPUT_LIMIT = 200_000
TIMEOUT_EXIT_CODE = 124

# `find` prints: type \t size \t mtime \t path   (GNU findutils and busybox >= 1.30 support -printf)
FIND_FORMAT = r"%y\t%s\t%T@\t%p\n"


class SandboxNotFoundError(KeyError):
    pass


class SandboxQuotaError(RuntimeError):
    pass


def find_command(path: str) -> str:
    return f"find {shlex.quote(path)} -mindepth 1 -printf {shlex.quote(FIND_FORMAT)}"


def parse_find_output(stdout: str) -> list[FileStat]:
    stats: list[FileStat] = []
    for line in stdout.splitlines():
        parts = line.split("\t", 3)
        if len(parts) != 4:
            continue
        kind, size, mtime, path = parts
        try:
            stats.append(FileStat(path=path, size=int(size), is_dir=kind == "d", modified_at=float(mtime)))
        except ValueError:
            continue
    return sorted(stats, key=lambda s: s.path)


def join_workdir(workdir: str, change: FileChange) -> str:
    """``FileChange.path`` is already sanitized (relative, no ``..``) by the kernel."""
    rel = PurePosixPath(change.path)
    if rel.is_absolute() or ".." in rel.parts:
        raise PermissionError(f"Unsafe path in FileChange: {change.path}")
    return str(PurePosixPath(workdir) / rel)


def truncate(text: str) -> str:
    return text[-OUTPUT_LIMIT:]


def write_local(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
