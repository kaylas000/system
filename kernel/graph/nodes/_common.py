"""Shared helpers for graph nodes."""

from __future__ import annotations

import base64
import binascii
import hashlib
import posixpath
import re
import shlex
from typing import Any

from ...protocols import LLMResponse
from ...state import FileChange, TokenUsage, utcnow


def log(node: str, message: str) -> str:
    return f"{utcnow().isoformat(timespec='seconds')} [{node}] {message}"


def usage_from_response(resp: LLMResponse) -> TokenUsage:
    usage = resp.usage or {}
    prompt = int(usage.get("prompt_tokens", usage.get("prompt", 0)))
    completion = int(usage.get("completion_tokens", usage.get("completion", 0)))
    total = int(usage.get("total_tokens", usage.get("total", prompt + completion)))
    return TokenUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cost_usd=resp.cost_usd,
        model_name=resp.model,
    )


def add_usage(current: TokenUsage | None, resp: LLMResponse) -> TokenUsage:
    return (current or TokenUsage()).add(usage_from_response(resp))


class UnsafePathError(ValueError):
    pass


def sanitize_changes(changes: list[FileChange]) -> list[FileChange]:
    """Reject absolute paths and path traversal in LLM-produced file changes."""
    safe: list[FileChange] = []
    for change in changes:
        raw = change.path.replace("\\", "/").strip()
        norm = posixpath.normpath(raw)
        if not raw or raw.startswith("/") or norm == "." or norm.startswith("../") or norm == "..":
            raise UnsafePathError(f"Unsafe file path from LLM: {change.path!r}")
        safe.append(change if norm == change.path else change.model_copy(update={"path": norm}))
    return safe


CONTEXT_UPLOAD_MAX_BYTES = 1_000_000  # per file
CONTEXT_UPLOAD_TOTAL_BYTES = 5_000_000
PLANNER_CONTEXT_CHARS = 6_000  # per file in the planner prompt


def decode_context_files(items: list[dict[str, str]]) -> tuple[list[FileChange], list[str]]:
    """``GenerateRequest.context_files`` (``{name, content_base64}``) -> text files + warnings.

    Files are written into the workspace under ``name`` (e.g. shared contracts ``contracts/openapi.yaml``);
    unsafe paths, binary / oversized files are skipped with a warning.
    """
    files: list[FileChange] = []
    warnings: list[str] = []
    total = 0
    for item in items:
        name = str(item.get("name") or "").strip()
        try:
            (change,) = sanitize_changes([FileChange(path=name or ".", content="", action="create")])
            data = base64.b64decode(str(item.get("content_base64") or ""), validate=True)
        except (UnsafePathError, binascii.Error, ValueError) as exc:
            warnings.append(f"context file {name!r} skipped: {exc}")
            continue
        if len(data) > CONTEXT_UPLOAD_MAX_BYTES or total + len(data) > CONTEXT_UPLOAD_TOTAL_BYTES:
            warnings.append(f"context file {name!r} skipped: too large ({len(data)} bytes)")
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            warnings.append(f"context file {name!r} skipped: not UTF-8 text")
            continue
        total += len(data)
        files.append(change.model_copy(update={"content": text}))
    return files, warnings


def merged_metadata(state: Any, **updates: Any) -> dict[str, Any]:
    meta = dict(state.get("metadata") or {})
    meta.update(updates)
    return meta


def content_hashes(changes: list[FileChange]) -> dict[str, str]:
    """Stable {path: sha256[:16]} index for ``AgentState.project_files``."""
    return {c.path: hashlib.sha256(c.content.encode("utf-8")).hexdigest()[:16] for c in changes if c.action != "delete"}


# -- workspace context for LLM nodes ------------------------------------------------
TREE_LIMIT = 400
CONTEXT_FILE_CHARS = 12_000
CONTEXT_TOTAL_CHARS = 80_000
_EXCLUDED_DIRS = ("node_modules", ".next", ".git", "dist", "build", "coverage", ".venv", "__pycache__", ".turbo")
_PATH_RE = re.compile(
    r"(?<![\w/.-])((?:\./)?(?:[\w@()\[\]-]+/)*[\w@()\[\]-]+"
    r"\.(?:tsx?|jsx?|mjs|cjs|prisma|json|css|md|ya?ml|py|toml))"
)


async def list_workspace_files(sandbox: Any, sandbox_id: str, workspace: str) -> list[str]:
    """Relative paths of project files (heavy/generated directories pruned). Empty on failure."""
    prune = " -o ".join(f"-name {shlex.quote(d)}" for d in _EXCLUDED_DIRS)
    cmd = f"find . \\( {prune} \\) -prune -o -type f -print | sed 's|^\\./||' | sort | head -n {TREE_LIMIT}"
    try:
        res = await sandbox.exec(sandbox_id, cmd, workdir=workspace, timeout_sec=30)
    except Exception:
        return []
    if res.exit_code != 0:
        return []
    return [line for line in res.stdout.splitlines() if line and not line.endswith((".lock", "lock.yaml"))]


def mentioned_paths(*texts: str) -> list[str]:
    """File paths mentioned in free text (task description / inputs)."""
    found: list[str] = []
    for text in texts:
        found += [m.group(1).removeprefix("./") for m in _PATH_RE.finditer(text or "")]
    return list(dict.fromkeys(found))


async def read_context_files(
    sandbox: Any, sandbox_id: str, workspace: str, paths: list[str], max_files: int = 12
) -> list[dict[str, str]]:
    """``[{path, content}]`` for existing files, truncated per file and in total."""
    out: list[dict[str, str]] = []
    total = 0
    for path in paths[:max_files]:
        full = f"{workspace.rstrip('/')}/{path}"
        try:
            content = str(await sandbox.read_file(sandbox_id, full))
        except Exception:
            continue
        if len(content) > CONTEXT_FILE_CHARS:
            content = content[:CONTEXT_FILE_CHARS] + "\n... (truncated)"
        if total + len(content) > CONTEXT_TOTAL_CHARS:
            break
        total += len(content)
        out.append({"path": path, "content": content})
    return out
