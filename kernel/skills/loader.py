"""
Vertical loader (port of ``specs/03_skills/registry/VERTICAL_LOADER.py``).

Fixes vs. the spec (``specs/ISSUES.md`` SK-08): syntax error ``from typing: Dict``
(S-01); ``skills_dir``/``prompts_dir`` were overwritten with absolute host paths
inside the manifest (they end up in the checkpointed state); ``discover`` crashed
on one bad manifest; ``VerticalImpl`` got only the manifest. Now
``VerticalImpl(manifest, vertical_dir, tool_registry=...)`` - same signature as
``GenericVertical``, which custom verticals are expected to subclass.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..protocols import IVertical, VerticalManifest
from .generic_vertical import GenericVertical, VerticalLoadError, find_manifest, import_module_from, load_manifest

logger = logging.getLogger("autogen.vertical")
IMPL_NAMES = ("vertical_impl.py", "VERTICAL_IMPL.py")


class VerticalLoader:
    def __init__(self, verticals_root: Path | str, *, tool_registry: Any = None) -> None:
        self.verticals_root = Path(verticals_root)
        self.tool_registry = tool_registry
        self._verticals: dict[str, IVertical] = {}
        self.errors: dict[str, str] = {}

    def _dirs(self) -> dict[str, Path]:
        """vertical id -> directory (id from the manifest, not the directory name)."""
        found: dict[str, Path] = {}
        if not self.verticals_root.is_dir():
            return found
        for v_dir in sorted(p for p in self.verticals_root.iterdir() if p.is_dir()):
            if find_manifest(v_dir) is None:
                continue
            try:
                manifest = load_manifest(v_dir)
            except VerticalLoadError as exc:
                self.errors[v_dir.name] = str(exc)
                logger.error("Bad vertical %s: %s", v_dir.name, exc)
                continue
            if manifest.id in found:
                self.errors[v_dir.name] = f"duplicate vertical id {manifest.id!r}"
                continue
            found[manifest.id] = v_dir
        return found

    def discover(self) -> dict[str, VerticalManifest]:
        return {vid: load_manifest(d) for vid, d in self._dirs().items()}

    def load_vertical(self, vertical_id: str) -> IVertical:
        if vertical_id in self._verticals:
            return self._verticals[vertical_id]
        v_dir = self._dirs().get(vertical_id)
        if v_dir is None:
            raise VerticalLoadError(f"Vertical not found: {vertical_id!r} (in {self.verticals_root})")
        manifest = load_manifest(v_dir)
        impl_path = next((v_dir / n for n in IMPL_NAMES if (v_dir / n).is_file()), None)
        vertical: IVertical
        if impl_path is not None:
            module = import_module_from(impl_path, f"vertical_impl_{vertical_id}")
            impl_cls = getattr(module, "VerticalImpl", None)
            if impl_cls is None:
                raise VerticalLoadError(f"{impl_path} must define class VerticalImpl")
            vertical = impl_cls(manifest, v_dir, tool_registry=self.tool_registry)
        else:
            vertical = GenericVertical(manifest, v_dir, tool_registry=self.tool_registry)
        if not isinstance(vertical, IVertical):
            raise VerticalLoadError(f"{vertical_id}: implementation does not satisfy IVertical")
        self._verticals[vertical_id] = vertical
        return vertical
