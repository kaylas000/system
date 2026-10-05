"""Sandbox tools and the registry."""

from __future__ import annotations

from ..protocols import ISandbox
from ._paths import UnsafePathError
from .filesystem import FileSystemTool
from .git import GitTool
from .registry import ToolRegistry
from .shell import ShellPolicy, ShellTool


def default_tool_registry(
    sandbox: ISandbox,
    *,
    workspace: str = "/workspace",
    shell_policy: ShellPolicy | None = None,
    allow_git_push: bool = False,
) -> ToolRegistry:
    """Registry with filesystem / shell / git. LSP and RAG tools are added in later phases."""
    registry = ToolRegistry(sandbox)
    registry.register(FileSystemTool(sandbox, workspace))
    registry.register(ShellTool(sandbox, shell_policy, workspace))
    registry.register(GitTool(sandbox, allow_push=allow_git_push, default_workspace=workspace))
    return registry


__all__ = [
    "FileSystemTool",
    "GitTool",
    "ShellPolicy",
    "ShellTool",
    "ToolRegistry",
    "UnsafePathError",
    "default_tool_registry",
]
