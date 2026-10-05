"""Path handling shared by sandbox tools: every LLM-supplied path stays inside the workspace."""

from __future__ import annotations

import posixpath
import re

from ..state import AgentState

DEFAULT_WORKSPACE = "/workspace"
IGNORED_DIRS = ("node_modules", ".git", ".next", "dist", "build", ".turbo", "coverage", "__pycache__", ".venv")


class UnsafePathError(ValueError):
    pass


def workspace_of(state: AgentState | dict[str, object] | None, default: str = DEFAULT_WORKSPACE) -> str:
    ws = (state or {}).get("workspace_path") or default
    return posixpath.normpath(str(ws))


def resolve(workspace: str, path: str | None) -> str:
    """Resolve ``path`` (relative to the workspace, or absolute inside it) to an absolute sandbox path."""
    raw = (path or ".").strip() or "."
    if "\x00" in raw:
        raise UnsafePathError("NUL byte in path")
    full = posixpath.normpath(raw if raw.startswith("/") else posixpath.join(workspace, raw))
    if full != workspace and not full.startswith(workspace.rstrip("/") + "/"):
        raise UnsafePathError(f"Path escapes workspace: {path}")
    return full


def relative(workspace: str, full: str) -> str:
    rel = posixpath.relpath(full, workspace)
    return "." if rel == "." else rel


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Glob with ``**`` support: ``**/`` = any number of dirs, ``*`` / ``?`` never cross ``/``.

    A pattern without ``/`` matches the basename anywhere (like ``find -name``).
    """
    if "/" not in pattern:
        pattern = "**/" + pattern
    i, out = 0, []
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "{" and "}" in pattern[i:]:
            end = pattern.index("}", i)
            out.append("(?:" + "|".join(re.escape(p) for p in pattern[i + 1 : end].split(",")) + ")")
            i = end + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")
