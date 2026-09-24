"""Shared helpers for graph nodes."""

from __future__ import annotations

import hashlib
import posixpath
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


def merged_metadata(state: Any, **updates: Any) -> dict[str, Any]:
    meta = dict(state.get("metadata") or {})
    meta.update(updates)
    return meta


def content_hashes(changes: list[FileChange]) -> dict[str, str]:
    """Stable {path: sha256[:16]} index for ``AgentState.project_files``."""
    return {c.path: hashlib.sha256(c.content.encode("utf-8")).hexdigest()[:16] for c in changes if c.action != "delete"}
