"""Runtime dependencies injected into graph nodes via ``config["configurable"]``."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..config import Settings, get_settings
from ..protocols import ILLMClient, ISandbox, IVertical


@dataclass(frozen=True)
class KernelDeps:
    vertical: IVertical
    llm: ILLMClient
    sandbox: ISandbox
    settings: Settings
    tool_registry: Any | None = None  # kernel.tools.registry.ToolRegistry (optional)


class MissingDependencyError(RuntimeError):
    pass


def resolve_deps(config: Mapping[str, Any] | None, defaults: Mapping[str, Any]) -> KernelDeps:
    """Merge per-run ``configurable`` values over builder-level defaults."""
    configurable: Mapping[str, Any] = (config or {}).get("configurable", {}) or {}

    def pick(key: str, *aliases: str) -> Any:
        for k in (key, *aliases):
            if configurable.get(k) is not None:
                return configurable[k]
            if defaults.get(k) is not None:
                return defaults[k]
        return None

    vertical = pick("vertical")
    llm = pick("llm_client", "llm")
    sandbox = pick("sandbox")
    missing = [name for name, value in (("vertical", vertical), ("llm_client", llm), ("sandbox", sandbox)) if not value]
    if missing:
        raise MissingDependencyError(f"Missing kernel dependencies in config['configurable']: {missing}")
    settings = pick("settings") or get_settings()
    return KernelDeps(
        vertical=vertical,
        llm=llm,
        sandbox=sandbox,
        settings=settings,
        tool_registry=pick("tool_registry"),
    )
